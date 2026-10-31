"""M5-07: three-level metering priority, non-blocking fallback, and zero body leakage."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from uuid import UUID

import pytest
from holm_domain import (
    ArtifactStorageType,
    ErrorCode,
    MeteringSource,
    MetricsCapability,
)
from holm_gateway.metering import MeteringService
from holm_gateway.openai_types import OpenAIRequestExtension
from holm_gateway.request_context import RequestRecordContext
from holm_gateway.tokenizer_bundle import (
    BundleTokenizer,
    TokenizerBundleReason,
    TokenizerBundleUnusable,
)
from holm_ledger import (
    AbortedEvent,
    CompletedEvent,
    EventType,
    FailedEvent,
    LedgerEvent,
)
from holm_protocols import (
    AdmissionController,
    AuthenticatedPrincipal,
    CanonicalMessage,
    CanonicalRequest,
    Completed,
    ContentDelta,
    InferenceCore,
    PlatformError,
    SamplingParameters,
    ServiceModelReference,
    ServiceRoute,
    StopReason,
    TextContent,
    UsageInfo,
    UsageReported,
)

REQUEST_ID: Final[UUID] = UUID("57000000-0000-4000-8000-000000000001")
USER_ID: Final[UUID] = UUID("57000000-0000-4000-8000-000000000002")
KEY_ID: Final[UUID] = UUID("57000000-0000-4000-8000-000000000003")
SERVICE_ID: Final[UUID] = UUID("57000000-0000-4000-8000-000000000004")
INSTANCE_ID: Final[UUID] = UUID("57000000-0000-4000-8000-000000000005")
NOW: Final[datetime] = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
BUNDLE_PATH: Final[str] = "/var/lib/holm/tokenizers/m5-07"
BUNDLE_HASH: Final[str] = "a" * 64

#: Sentinels that must reach the Runtime and must never reach storage or logs.
PROMPT_SENTINEL: Final[str] = "M5_07_PROMPT_SENTINEL"
OUTPUT_SENTINEL: Final[str] = "M5_07_OUTPUT_SENTINEL"

_STATUS: Final[dict[ErrorCode, int]] = {
    ErrorCode.BACKEND_UNAVAILABLE: 503,
    ErrorCode.INFERENCE_TIMEOUT: 504,
    ErrorCode.USAGE_STORAGE_UNAVAILABLE: 503,
}


class _Ledger:
    def __init__(self) -> None:
        self.events: list[LedgerEvent] = []

    async def submit(self, event: LedgerEvent) -> object:
        self.events.append(event)
        return None

    def terminal(self) -> LedgerEvent:
        return self.events[-1]


class _CountingTokenizer(BundleTokenizer):
    """Count one Token per word so expectations stay readable."""

    def __init__(self) -> None:  # noqa: D107 - deliberately bypasses the BPE tables
        super().__init__(
            vocab={},
            merge_ranks={},
            added_tokens=(),
            normalize_nfc=False,
            max_digit_run=1,
        )

    def count(self, text: str) -> int:
        return len(text.split())

    def count_all(self, texts: Iterable[str]) -> int:
        return sum(self.count(text) for text in texts)


class _Loader:
    """A Tokenizer loader whose latency and failure mode the test controls."""

    def __init__(
        self,
        *,
        delay_seconds: float = 0.0,
        reason: TokenizerBundleReason | None = None,
    ) -> None:
        self.delay_seconds = delay_seconds
        self.reason = reason
        self.calls: list[tuple[Path, str]] = []

    def __call__(self, path: Path, manifest_hash: str) -> BundleTokenizer:
        self.calls.append((path, manifest_hash))
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        if self.reason is not None:
            raise TokenizerBundleUnusable(self.reason)
        return _CountingTokenizer()


class _Runtime:
    """A Runtime whose reported Usage and pacing the test controls."""

    def __init__(
        self,
        *,
        report_usage: bool = True,
        hold_after_first: bool = False,
        deltas: int = 3,
    ) -> None:
        self.report_usage = report_usage
        self.hold_after_first = hold_after_first
        self.deltas = deltas
        self.cancelled = False

    async def stream(
        self,
        endpoint: str,
        request_id: UUID,
        canonical_request: CanonicalRequest,
    ) -> AsyncIterator[Any]:
        del endpoint, request_id, canonical_request
        try:
            for index in range(self.deltas):
                yield ContentDelta(f"{OUTPUT_SENTINEL}-{index}")
                if self.hold_after_first:
                    await asyncio.Event().wait()
            if self.report_usage:
                yield UsageReported(UsageInfo(input_tokens=11, output_tokens=7))
            yield Completed(StopReason.STOP)
        except GeneratorExit:
            self.cancelled = True
            raise


class _UnusedAuthenticator:
    async def authenticate(self, credential: str | None) -> Any:
        del credential
        raise AssertionError("infer_routed must not authenticate")


class _UnusedRoutes:
    async def find(self, public_model_name: str) -> None:
        del public_model_name
        raise AssertionError("infer_routed must not resolve")

    async def list_enabled(self) -> tuple[()]:
        return ()


def _route(
    *,
    bundle_path: str | None = BUNDLE_PATH,
    bundle_hash: str | None = BUNDLE_HASH,
    storage_type: str | None = ArtifactStorageType.MANAGED.value,
) -> ServiceRoute:
    return ServiceRoute(
        service_id=SERVICE_ID,
        instance_id=INSTANCE_ID,
        public_model_name="m5-07-model",
        enabled=True,
        ready=True,
        endpoint="runtime:8000",
        runtime_name="vllm",
        runtime_model_name="internal-model",
        tokenizer_bundle_path=bundle_path,
        tokenizer_bundle_hash=bundle_hash,
        artifact_storage_type=storage_type,
    )


def _request(*, stream: bool = False) -> CanonicalRequest:
    return CanonicalRequest(
        service_model=ServiceModelReference("m5-07-model"),
        stream=stream,
        sampling=SamplingParameters(maximum_output_tokens=8),
        messages=(CanonicalMessage("user", (TextContent(f"{PROMPT_SENTINEL} two three"),)),),
        tools=(),
        extension=OpenAIRequestExtension(False),
    )


def _service(
    loader: _Loader | None = None,
    *,
    timeout_seconds: float = 5.0,
    per_request_metrics_runtimes: tuple[str, ...] = (),
) -> MeteringService:
    return MeteringService(
        worker_threads=2,
        timeout_seconds=timeout_seconds,
        loader=loader or _Loader(),
        per_request_metrics_runtimes=per_request_metrics_runtimes,
    )


def _context(
    ledger: _Ledger,
    metering: MeteringService,
    *,
    route: ServiceRoute | None = None,
) -> RequestRecordContext:
    context = RequestRecordContext(
        REQUEST_ID,
        NOW,
        time.monotonic(),
        ledger=ledger,
        failure_status=lambda error: _STATUS[error.code],
        metering=metering,
    )
    context.set_stream(False)
    context.begin_received(256)
    context.set_authenticated(
        AuthenticatedPrincipal(USER_ID, KEY_ID, frozenset(), True, frozenset())
    )
    context.set_route(route or _route())
    return context


def _core(runtime: _Runtime) -> InferenceCore:
    return InferenceCore(
        authenticator=_UnusedAuthenticator(),
        routes=_UnusedRoutes(),
        runtime=runtime,
        admission=AdmissionController(4, 4),
        first_token_timeout_seconds=2,
        total_timeout_seconds=10,
    )


async def _drive(
    core: InferenceCore,
    context: RequestRecordContext,
    *,
    stream: bool = False,
) -> None:
    response = await core.infer_routed(
        route=_route(),
        request_id=REQUEST_ID,
        request=_request(stream=stream),
        lifecycle=context,
    )
    async for _event in response.events:
        pass


def test_M5_07_runtime_usage_is_the_first_priority() -> None:
    async def scenario() -> None:
        ledger, metering = _Ledger(), _service()
        context = _context(ledger, metering)
        await _drive(_core(_Runtime()), context)
        await metering.aclose()

        terminal = ledger.terminal()
        assert isinstance(terminal, CompletedEvent)
        assert (terminal.input_tokens, terminal.output_tokens) == (11, 7)
        assert terminal.metering_source is MeteringSource.RUNTIME_USAGE
        assert terminal.metrics_capability is MetricsCapability.FULL
        assert terminal.metering_source is MeteringSource.RUNTIME_USAGE
        assert (terminal.input_tokens, terminal.output_tokens) == (11, 7)

    asyncio.run(scenario())


def test_M5_07_missing_usage_falls_back_to_the_bundle_and_stays_completed() -> None:
    """Closes the registered defect: a normal completion is no longer rewritten to Failed."""

    async def scenario() -> None:
        ledger, metering = _Ledger(), _service()
        context = _context(ledger, metering)
        await _drive(_core(_Runtime(report_usage=False)), context)
        await metering.aclose()

        terminal = ledger.terminal()
        assert terminal.event_type is EventType.COMPLETED
        assert not any(event.event_type is EventType.FAILED for event in ledger.events)
        assert isinstance(terminal, CompletedEvent)
        # "M5_07_PROMPT_SENTINEL two three" plus the "user" role is four words.
        assert terminal.input_tokens == 4
        assert terminal.output_tokens == 3
        assert terminal.metering_source is MeteringSource.TOKENIZER_FALLBACK
        assert terminal.metrics_capability is MetricsCapability.PARTIAL
        assert ledger.terminal().event_type is EventType.COMPLETED
        assert terminal.metering_source is MeteringSource.TOKENIZER_FALLBACK

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("route_kwargs", "expected_calls"),
    [
        ({"bundle_path": None, "bundle_hash": None}, 0),
        ({"storage_type": ArtifactStorageType.EXTERNAL.value}, 0),
    ],
)
def test_M5_07_absent_or_external_bundle_is_unsupported_and_reads_nothing(
    route_kwargs: dict[str, Any], expected_calls: int
) -> None:
    async def scenario() -> None:
        loader = _Loader()
        ledger, metering = _Ledger(), _service(loader)
        route = _route(**route_kwargs)
        context = _context(ledger, metering, route=route)
        await _drive(_core(_Runtime(report_usage=False)), context)
        await metering.aclose()

        terminal = ledger.terminal()
        assert isinstance(terminal, CompletedEvent)
        assert (terminal.input_tokens, terminal.output_tokens) == (None, None)
        assert terminal.metering_source is MeteringSource.UNAVAILABLE
        assert terminal.metrics_capability is MetricsCapability.UNSUPPORTED
        # An External Artifact's source directory is never opened, statted, or
        # otherwise touched by the Gateway.
        assert len(loader.calls) == expected_calls
        assert terminal.metrics_capability is MetricsCapability.UNSUPPORTED

    asyncio.run(scenario())


def test_M5_07_unverifiable_bundle_is_unsupported_and_never_estimated() -> None:
    async def scenario() -> None:
        loader = _Loader(reason=TokenizerBundleReason.HASH_MISMATCH)
        ledger, metering = _Ledger(), _service(loader)
        context = _context(ledger, metering)
        await _drive(_core(_Runtime(report_usage=False)), context)
        await metering.aclose()

        terminal = ledger.terminal()
        assert isinstance(terminal, CompletedEvent)
        assert (terminal.input_tokens, terminal.output_tokens) == (None, None)
        assert terminal.metering_source is MeteringSource.UNAVAILABLE
        assert terminal.metrics_capability is MetricsCapability.UNSUPPORTED
        assert loader.calls

    asyncio.run(scenario())


def test_M5_07_cold_load_timeout_still_writes_the_terminal_event() -> None:
    async def scenario() -> None:
        ledger = _Ledger()
        metering = _service(_Loader(delay_seconds=1.0), timeout_seconds=0.05)
        context = _context(ledger, metering)
        await _drive(_core(_Runtime(report_usage=False)), context)
        await metering.aclose()

        terminal = ledger.terminal()
        assert isinstance(terminal, CompletedEvent)
        assert (terminal.input_tokens, terminal.output_tokens) == (None, None)
        assert terminal.metering_source is MeteringSource.UNAVAILABLE
        assert terminal.metrics_capability is MetricsCapability.PARTIAL

    asyncio.run(scenario())


def test_M5_07_slow_tokenizer_delays_neither_first_event_nor_response() -> None:
    """TDD 1206: a cold Bundle load may delay the Ledger, never the client."""

    async def scenario() -> None:
        ledger = _Ledger()
        metering = _service(_Loader(delay_seconds=1.5), timeout_seconds=5.0)

        stream_context = _context(ledger, metering)
        started = time.monotonic()
        response = await _core(_Runtime(report_usage=False)).infer_routed(
            route=_route(),
            request_id=REQUEST_ID,
            request=_request(stream=True),
            lifecycle=stream_context,
        )
        events = response.events
        await anext(events)
        first_event_seconds = time.monotonic() - started
        async for _event in events:
            pass
        stream_seconds = time.monotonic() - started

        non_stream_context = _context(ledger, metering)
        started = time.monotonic()
        await _drive(_core(_Runtime(report_usage=False)), non_stream_context)
        non_stream_seconds = time.monotonic() - started

        assert first_event_seconds < 0.5, first_event_seconds
        assert stream_seconds < 0.5, stream_seconds
        assert non_stream_seconds < 0.5, non_stream_seconds
        # The terminal event is still owed at this point, and arrives once the
        # cold load finishes.
        assert not any(event.event_type is EventType.COMPLETED for event in ledger.events)
        await metering.drain()
        completed = [event for event in ledger.events if event.event_type is EventType.COMPLETED]
        assert len(completed) == 2
        assert all(
            event.metering_source is MeteringSource.TOKENIZER_FALLBACK  # type: ignore[union-attr]
            for event in completed
        )
        await metering.aclose()

    asyncio.run(scenario())


def test_M5_07_client_abort_counts_observed_output_without_waiting_upstream() -> None:
    async def scenario() -> None:
        ledger, metering = _Ledger(), _service()
        context = _context(ledger, metering)
        runtime = _Runtime(report_usage=False, hold_after_first=True)
        response = await _core(runtime).infer_routed(
            route=_route(),
            request_id=REQUEST_ID,
            request=_request(stream=True),
            lifecycle=context,
        )
        events = response.events
        await anext(events)
        started = time.monotonic()
        await events.aclose()
        await metering.drain()
        settled_seconds = time.monotonic() - started

        terminal = ledger.terminal()
        assert isinstance(terminal, AbortedEvent)
        assert terminal.client_disconnected is True
        assert terminal.output_tokens == 1
        assert terminal.metering_source is MeteringSource.TOKENIZER_FALLBACK
        assert runtime.cancelled
        # Upstream is cancelled immediately; nothing waits for Tokens that were
        # never received.
        assert settled_seconds < 1.0, settled_seconds
        assert ledger.terminal().event_type is EventType.ABORTED
        await metering.aclose()

    asyncio.run(scenario())


def test_M5_07_failed_request_records_its_metering_provenance() -> None:
    async def scenario() -> None:
        ledger, metering = _Ledger(), _service()
        context = _context(ledger, metering)
        await context.mark_in_progress(None)
        context.mark_forwarded()
        context.begin_metering(_request())
        context.observe_event(ContentDelta(f"{OUTPUT_SENTINEL} second"))
        await context.mark_error(PlatformError(ErrorCode.INFERENCE_TIMEOUT))
        await metering.drain()

        terminal = ledger.terminal()
        assert isinstance(terminal, FailedEvent)
        assert terminal.output_tokens == 2
        assert terminal.metering_source is MeteringSource.TOKENIZER_FALLBACK
        await metering.aclose()

    asyncio.run(scenario())


def test_M5_07_runtime_metrics_apply_only_when_the_runtime_declares_them() -> None:
    async def scenario() -> None:
        ledger = _Ledger()
        undeclared = _service()
        context = _context(ledger, undeclared)
        await context.mark_in_progress(None)
        context.mark_forwarded()
        context.observe_runtime_metrics(5, 6)
        assert context.metrics_input_tokens is None
        await undeclared.aclose()

        declared = _service(per_request_metrics_runtimes=("vllm",))
        context = _context(_Ledger(), declared)
        await context.mark_in_progress(None)
        context.mark_forwarded()
        context.observe_runtime_metrics(5, 6)
        await context.mark_completed()
        assert context.metering_source is MeteringSource.RUNTIME_METRICS
        assert (context.input_tokens, context.output_tokens) == (5, 6)
        await declared.aclose()

    asyncio.run(scenario())


def test_M5_07_metering_text_never_reaches_storage_events_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        ledger, metering = _Ledger(), _service()
        context = _context(ledger, metering)
        with caplog.at_level(logging.DEBUG):
            await _drive(_core(_Runtime(report_usage=False)), context)
            await metering.aclose()

        # Positive control first: without it, "no sentinel found" would also be
        # true of a run that recorded nothing at all.
        assert [event.event_type for event in ledger.events] == [
            EventType.RECEIVED,
            EventType.IN_PROGRESS,
            EventType.COMPLETED,
        ]
        assert len([event for event in ledger.events if isinstance(event, CompletedEvent)]) == 1
        terminal = ledger.terminal()
        assert isinstance(terminal, CompletedEvent)
        assert terminal.input_tokens == 4

        written = "".join(
            [
                *(event.model_dump_json() for event in ledger.events),
                *(record.getMessage() + str(record.__dict__) for record in caplog.records),
            ]
        )
        assert PROMPT_SENTINEL not in written
        assert OUTPUT_SENTINEL not in written

    asyncio.run(scenario())


def test_M5_07_metering_text_is_released_once_the_request_is_metered() -> None:
    async def scenario() -> None:
        ledger, metering = _Ledger(), _service()
        context = _context(ledger, metering)
        await _drive(_core(_Runtime(report_usage=False)), context)
        await metering.aclose()

        request_metering = context.metering_request
        assert request_metering is not None
        assert request_metering._input_texts == ()
        assert request_metering._output_chunks == []

    asyncio.run(scenario())


def test_M5_07_bundle_cache_holds_the_tdd_ceiling_and_evicts_least_recent() -> None:
    class _SizedLoader(_Loader):
        def __init__(self, size_bytes: int) -> None:
            super().__init__()
            self.size_bytes = size_bytes

        def __call__(self, path: Path, manifest_hash: str) -> BundleTokenizer:
            tokenizer = super().__call__(path, manifest_hash)
            tokenizer._estimated_bytes = self.size_bytes  # noqa: SLF001 - test-only sizing
            return tokenizer

    async def scenario() -> None:
        assert MeteringService().resident_cache_bytes == 0
        loader = _SizedLoader(200)
        metering = MeteringService(worker_threads=1, loader=loader, cache_max_bytes=300)

        async def count(path: str) -> None:
            request = metering.begin(
                tokenizer_bundle_path=path,
                tokenizer_bundle_hash=BUNDLE_HASH,
                artifact_storage_type=ArtifactStorageType.MANAGED.value,
            )
            request.start_input(("one two",))
            outcome = await request.resolve(usage_input=None, usage_output=None)
            assert outcome.input_tokens == 2

        await count("/tokenizers/a")
        assert metering.resident_cache_bytes == 200
        await count("/tokenizers/b")
        # The ceiling holds: the least recently used Bundle left instead.
        assert metering.resident_cache_bytes == 200
        await count("/tokenizers/a")
        assert [path for path, _ in loader.calls] == [
            Path("/tokenizers/a"),
            Path("/tokenizers/b"),
            Path("/tokenizers/a"),
        ]
        await count("/tokenizers/a")
        assert len(loader.calls) == 3
        await metering.aclose()

    asyncio.run(scenario())


def test_M5_07_default_cache_ceiling_is_512_mib() -> None:
    from holm_gateway.configuration import GatewayConfiguration

    assert GatewayConfiguration().tokenizer_cache_max_bytes == 512 * 1024 * 1024
    assert GatewayConfiguration().tokenizer_fallback_timeout_seconds == 5.0

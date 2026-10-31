"""M5-03 Request Ledger lifecycle and forwarding-gate unit coverage."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from holm_domain import ErrorCode, RequestRecordStatus
from holm_gateway.openai_types import OpenAIRequestExtension
from holm_gateway.request_context import RequestRecordContext
from holm_ledger import (
    AbortedEvent,
    CompletedEvent,
    EventType,
    FailedEvent,
    InProgressEvent,
    LedgerEvent,
    ReceivedEvent,
    SequenceAction,
    SpoolWriterUnavailableError,
)
from holm_protocols import (
    AdmissionController,
    AuthenticatedPrincipal,
    CanonicalMessage,
    CanonicalRequest,
    Completed,
    ContentDelta,
    InferenceCore,
    InferenceFailure,
    PlatformError,
    SamplingParameters,
    ServiceModelReference,
    ServiceRoute,
    StopReason,
    TextContent,
    UsageInfo,
    UsageReported,
)
from pydantic import ValidationError

REQUEST_ID = UUID("53000000-0000-4000-8000-000000000001")
USER_ID = UUID("53000000-0000-4000-8000-000000000002")
KEY_ID = UUID("53000000-0000-4000-8000-000000000003")
SERVICE_ID = UUID("53000000-0000-4000-8000-000000000004")
INSTANCE_ID = UUID("53000000-0000-4000-8000-000000000005")
NOW = datetime(2026, 9, 17, 15, 0, tzinfo=UTC)

_STATUS = {
    ErrorCode.INVALID_API_KEY: 401,
    ErrorCode.GATEWAY_OVERLOADED: 503,
    ErrorCode.BACKEND_UNAVAILABLE: 503,
    ErrorCode.USAGE_STORAGE_UNAVAILABLE: 503,
}


class _Ledger:
    def __init__(self, *, fail_on: EventType | None = None) -> None:
        self.events: list[LedgerEvent] = []
        self.attempts: list[EventType] = []
        self.fail_on = fail_on
        self.unavailable = False

    async def submit(self, event: LedgerEvent) -> object:
        self.attempts.append(event.event_type)
        if self.unavailable or event.event_type is self.fail_on:
            self.unavailable = True
            raise SpoolWriterUnavailableError("injected without internal details")
        self.events.append(event)
        return SequenceAction.WRITE


class _Runtime:
    def __init__(self, *, block_after_first: bool = False) -> None:
        self.calls = 0
        self.block_after_first = block_after_first

    async def stream(
        self,
        endpoint: str,
        request_id: UUID,
        canonical_request: CanonicalRequest,
    ) -> AsyncIterator[Any]:
        del endpoint, request_id, canonical_request
        self.calls += 1
        yield ContentDelta("memory only")
        if self.block_after_first:
            await asyncio.Event().wait()
        yield UsageReported(UsageInfo(input_tokens=3, output_tokens=2))
        yield Completed(StopReason.STOP)


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


def _route() -> ServiceRoute:
    return ServiceRoute(
        service_id=SERVICE_ID,
        instance_id=INSTANCE_ID,
        public_model_name="m5-03-model",
        enabled=True,
        ready=True,
        endpoint="runtime:8000",
        runtime_name="vllm",
        runtime_model_name="internal-model",
    )


def _request(*, maximum_tokens: int | None = 8, stream: bool = False) -> CanonicalRequest:
    return CanonicalRequest(
        service_model=ServiceModelReference("m5-03-model"),
        stream=stream,
        sampling=SamplingParameters(maximum_output_tokens=maximum_tokens),
        messages=(CanonicalMessage("user", (TextContent("M5_03_BODY_SENTINEL"),)),),
        tools=(),
        extension=OpenAIRequestExtension(False),
    )


def _context(ledger: _Ledger | None = None) -> RequestRecordContext:
    context = RequestRecordContext(
        REQUEST_ID,
        NOW,
        time.monotonic(),
        ledger=ledger,
        failure_status=lambda error: _STATUS[error.code],
    )
    context.set_stream(False)
    context.begin_received(123)
    context.set_authenticated(
        AuthenticatedPrincipal(USER_ID, KEY_ID, frozenset(), True, frozenset())
    )
    context.set_route(_route())
    return context


def _core(runtime: _Runtime, admission: AdmissionController) -> InferenceCore:
    return InferenceCore(
        authenticator=_UnusedAuthenticator(),
        routes=_UnusedRoutes(),
        runtime=runtime,
        admission=admission,
        first_token_timeout_seconds=1,
        total_timeout_seconds=5,
    )


def test_M5_03_requested_max_tokens_accepts_omitted_and_zero_only() -> None:
    base = {
        "request_id": REQUEST_ID,
        "event_seq": 2,
        "created_at": NOW,
        "received_at": NOW,
        "user_id": USER_ID,
        "api_key_id": KEY_ID,
        "service_id": SERVICE_ID,
        "instance_id": INSTANCE_ID,
    }
    assert InProgressEvent(**base, requested_max_tokens=None).requested_max_tokens is None
    assert InProgressEvent(**base, requested_max_tokens=0).requested_max_tokens == 0
    with pytest.raises(ValidationError):
        InProgressEvent(**base, requested_max_tokens=-1)


def test_M5_03_normal_lifecycle_is_received_in_progress_completed() -> None:
    async def scenario() -> None:
        ledger = _Ledger()
        context = _context(ledger)
        await context.mark_in_progress(None)
        context.mark_forwarded()
        context.observe_event(UsageReported(UsageInfo(input_tokens=3, output_tokens=2)))
        context.observe_event(Completed(StopReason.STOP))
        await context.mark_completed()

        assert [event.event_type for event in ledger.events] == [
            EventType.RECEIVED,
            EventType.IN_PROGRESS,
            EventType.COMPLETED,
        ]
        assert [event.event_seq for event in ledger.events] == [1, 2, 3]
        assert isinstance(ledger.events[0], ReceivedEvent)
        assert ledger.events[0].input_bytes == 123
        assert isinstance(ledger.events[2], CompletedEvent)
        assert (ledger.events[2].input_tokens, ledger.events[2].output_tokens) == (3, 2)

    asyncio.run(scenario())


def test_M5_03_TC_LG_08_in_progress_failure_prevents_runtime_and_releases_permit() -> None:
    async def scenario() -> None:
        ledger = _Ledger(fail_on=EventType.IN_PROGRESS)
        context = _context(ledger)
        runtime = _Runtime()
        admission = AdmissionController(1, 1)
        with pytest.raises(InferenceFailure) as raised:
            await _core(runtime, admission).infer_routed(
                route=_route(),
                request_id=REQUEST_ID,
                request=_request(),
                lifecycle=context,
            )
        assert raised.value.error.code is ErrorCode.USAGE_STORAGE_UNAVAILABLE
        assert runtime.calls == 0
        assert await admission.active_counts() == (0, {})

        response = await _core(runtime, admission).infer_routed(
            route=_route(),
            request_id=UUID(int=REQUEST_ID.int + 1),
            request=_request(),
            lifecycle=_context(),
        )
        assert [event async for event in response.events]
        assert runtime.calls == 1

    asyncio.run(scenario())


def test_M5_03_received_failure_prevents_runtime() -> None:
    async def scenario() -> None:
        ledger = _Ledger(fail_on=EventType.RECEIVED)
        runtime = _Runtime()
        with pytest.raises(InferenceFailure) as raised:
            await _core(runtime, AdmissionController(1, 1)).infer_routed(
                route=_route(),
                request_id=REQUEST_ID,
                request=_request(),
                lifecycle=_context(ledger),
            )
        assert raised.value.error.code is ErrorCode.USAGE_STORAGE_UNAVAILABLE
        assert runtime.calls == 0

    asyncio.run(scenario())


def test_M5_03_writer_already_unavailable_fails_without_runtime() -> None:
    async def scenario() -> None:
        ledger = _Ledger()
        ledger.unavailable = True
        runtime = _Runtime()
        context = _context(ledger)
        with pytest.raises(InferenceFailure) as raised:
            await context.raise_if_received_unavailable()
        assert raised.value.error.code is ErrorCode.USAGE_STORAGE_UNAVAILABLE
        assert ledger.attempts == [EventType.RECEIVED]
        assert runtime.calls == 0

    asyncio.run(scenario())


def test_M5_03_overload_is_in_progress_failed_but_postgres_status_stays_rejected() -> None:
    async def scenario() -> None:
        ledger = _Ledger()
        admission = AdmissionController(1, 1)
        occupied = await admission.try_acquire(INSTANCE_ID)
        assert occupied is not None
        context = _context(ledger)
        try:
            with pytest.raises(InferenceFailure) as raised:
                await _core(_Runtime(), admission).infer_routed(
                    route=_route(),
                    request_id=REQUEST_ID,
                    request=_request(maximum_tokens=0),
                    lifecycle=context,
                )
            assert raised.value.error.code is ErrorCode.GATEWAY_OVERLOADED
            assert [event.event_type for event in ledger.events] == [
                EventType.RECEIVED,
                EventType.IN_PROGRESS,
                EventType.FAILED,
            ]
            failed = ledger.events[-1]
            assert isinstance(failed, FailedEvent)
            assert failed.error_code is ErrorCode.GATEWAY_OVERLOADED
            assert context.status is RequestRecordStatus.REJECTED
        finally:
            await occupied.release()

    asyncio.run(scenario())


def test_M5_03_stream_close_persists_aborted_before_returning() -> None:
    async def scenario() -> None:
        ledger = _Ledger()
        runtime = _Runtime(block_after_first=True)
        response = await _core(runtime, AdmissionController(1, 1)).infer_routed(
            route=_route(),
            request_id=REQUEST_ID,
            request=_request(stream=True),
            lifecycle=_context(ledger),
        )
        await anext(response.events)
        await response.events.aclose()
        assert isinstance(ledger.events[-1], AbortedEvent)
        assert ledger.events[-1].event_seq == 3

    asyncio.run(scenario())


def test_M5_03_terminal_write_failure_is_logged_and_next_request_is_rejected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        ledger = _Ledger(fail_on=EventType.COMPLETED)
        context = _context(ledger)
        await context.mark_in_progress(8)
        context.mark_forwarded()
        context.observe_event(UsageReported(UsageInfo(input_tokens=1, output_tokens=1)))
        await context.mark_completed()
        assert context.storage_failed
        assert any("terminal event" in record.message for record in caplog.records)

        next_context = _context(ledger)
        with pytest.raises(InferenceFailure) as raised:
            await next_context.mark_error(PlatformError(ErrorCode.INVALID_API_KEY))
        assert raised.value.error.code is ErrorCode.USAGE_STORAGE_UNAVAILABLE

    asyncio.run(scenario())

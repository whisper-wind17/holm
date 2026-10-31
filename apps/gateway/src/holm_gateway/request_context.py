"""Request-local metadata and zero-body Request Record delivery."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal, Never, Protocol
from uuid import UUID

from holm_common import new_uuid7, utc_now
from holm_domain import (
    ErrorCode,
    LedgerHealth,
    MeteringSource,
    MetricsCapability,
    RequestFinishReason,
    RequestRecordStatus,
)
from holm_ledger import (
    AbortedEvent,
    CompletedEvent,
    FailedEvent,
    InProgressEvent,
    LedgerEvent,
    ReceivedEvent,
    RejectedEvent,
    RequestOperation,
    RequestProtocol,
)
from holm_protocols import (
    AuthenticatedPrincipal,
    CanonicalEvent,
    CanonicalMessage,
    CanonicalRequest,
    Completed,
    ContentDelta,
    InferenceFailure,
    PlatformError,
    ServiceRoute,
    StopReason,
    TextContent,
    ThinkingDelta,
    UsageReported,
)
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from holm_gateway.metering import MeteringOutcome, MeteringService, RequestMetering
from holm_gateway.service_metrics import GatewayServiceMetrics
from holm_gateway.stage_timing import (
    GatewayStageTimings,
    StageTimingSnapshot,
    bind_stage_timings,
    reset_stage_timings,
)

_LOGGER: Final[logging.Logger] = logging.getLogger("holm.gateway")
_REQUEST_ID_KEY: Final[str] = "holm_gateway_request_id"
_REQUEST_RECORD_KEY: Final[str] = "holm_gateway_request_record"
_CHAT_PATH: Final[Literal["/v1/chat/completions"]] = "/v1/chat/completions"

type FailureRenderer = Callable[[PlatformError, UUID], Response]
type FailureStatus = Callable[[PlatformError], int]
type LedgerHealthProbe = Callable[[], LedgerHealth]
type TerminalWriteFailureSink = Callable[[str], None]
type StageTimingSink = Callable[[UUID, StageTimingSnapshot], None]


class LedgerEventSink(Protocol):
    def submit(self, event: LedgerEvent) -> Coroutine[Any, Any, object]: ...


def request_id(request: Request) -> UUID:
    value = request.scope.get(_REQUEST_ID_KEY)
    if not isinstance(value, UUID):
        raise RuntimeError("request context is absent")
    return value


def request_lifecycle(request: Request) -> RequestRecordContext:
    value = request.scope.get(_REQUEST_RECORD_KEY)
    if not isinstance(value, RequestRecordContext):
        raise RuntimeError("Request Record context is absent")
    return value


def _metering_texts(request: CanonicalRequest) -> tuple[str, ...]:
    """Collect the request text this Gateway may count, and nothing else.

    Only text the client actually sent is counted.  The Runtime's chat template
    and special Tokens are not observable here, which is exactly why a fallback
    measurement is reported as ``partial`` rather than ``full``.
    """

    texts: list[str] = []
    for message in request.messages:
        texts.extend(_message_texts(message))
    for tool in request.tools:
        texts.append(tool.name)
        if tool.description is not None:
            texts.append(tool.description)
    return tuple(texts)


def _message_texts(message: CanonicalMessage) -> tuple[str, ...]:
    texts = [message.role]
    texts.extend(block.text for block in message.content if isinstance(block, TextContent))
    for call in message.tool_calls:
        texts.append(call.name)
        texts.append(call.arguments)
    return tuple(texts)


def _finish_reason(reason: StopReason | None) -> RequestFinishReason | None:
    if reason is None:
        return None
    try:
        return RequestFinishReason(reason.value)
    except ValueError:
        return RequestFinishReason.OTHER


@dataclass(slots=True)
class RequestRecordContext:
    """Mutable request metadata whose vocabulary cannot contain request bodies."""

    request_id: UUID
    received_at: datetime
    started_at: float
    stage_timings: GatewayStageTimings = field(default_factory=GatewayStageTimings)
    ledger: LedgerEventSink | None = field(default=None, repr=False)
    ledger_health: LedgerHealthProbe | None = field(default=None, repr=False)
    terminal_write_failure: TerminalWriteFailureSink | None = field(default=None, repr=False)
    failure_status: FailureStatus | None = field(default=None, repr=False)
    metering: MeteringService | None = field(default=None, repr=False)
    user_id: UUID | None = None
    api_key_id: UUID | None = None
    service_id: UUID | None = None
    instance_id: UUID | None = None
    stream: bool = False
    protocol: RequestProtocol = RequestProtocol.OPENAI
    operation: RequestOperation = RequestOperation.CHAT_COMPLETIONS
    endpoint: Literal["/v1/chat/completions", "/v1/messages"] = _CHAT_PATH
    in_progress: bool = False
    forwarded: bool = False
    status: RequestRecordStatus | None = None
    error_code: ErrorCode | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    ttft_ms: int | None = None
    finish_reason: RequestFinishReason | None = None
    metering_source: MeteringSource | None = None
    metrics_capability: MetricsCapability | None = None
    event_seq: int = 0
    received_submission: asyncio.Task[object] | None = field(default=None, repr=False)
    terminal_recorded: bool = False
    storage_failed: bool = False
    runtime_input_tokens: int | None = None
    runtime_output_tokens: int | None = None
    metrics_input_tokens: int | None = None
    metrics_output_tokens: int | None = None
    metering_request: RequestMetering | None = field(default=None, repr=False)
    runtime_name: str = field(default="", repr=False)
    service_metrics: GatewayServiceMetrics | None = field(default=None, repr=False)
    first_token_seconds: float | None = field(default=None, repr=False)
    last_token_seconds: float | None = field(default=None, repr=False)

    def set_stream(self, stream: bool) -> None:
        self.stream = stream

    def raise_if_ledger_unavailable(self) -> None:
        """Refuse admission before Received when the Ledger is unavailable.

        This is the whole point of the periodic health monitor: the decision is
        one in-memory read, it happens before any Ledger write and long before
        routing, so an Unavailable Ledger means the Runtime is never contacted.
        ``Delayed`` deliberately does not reject - calls continue and only the
        administrator view changes.
        """

        probe = self.ledger_health
        if probe is None or self.ledger is None:
            return
        if probe() is not LedgerHealth.UNAVAILABLE:
            return
        # Nothing durable exists for this request, so no terminal event is owed;
        # marking storage failed keeps `mark_error` from inventing one.
        self.storage_failed = True
        self._raise_storage_unavailable()

    def begin_received(self, input_bytes: int) -> None:
        """Submit Received without waiting so auth and routing can overlap its commit."""

        if self.received_submission is not None or self.event_seq != 0:
            raise RuntimeError("Received was already submitted")
        if self.ledger is None:
            return
        self.event_seq = 1
        event = ReceivedEvent(
            request_id=self.request_id,
            event_seq=self.event_seq,
            created_at=self.received_at,
            received_at=self.received_at,
            protocol=self.protocol,
            operation=self.operation,
            endpoint=self.endpoint,
            stream=self.stream,
            input_bytes=input_bytes,
        )
        self.received_submission = asyncio.create_task(self.ledger.submit(event))

    async def raise_if_received_unavailable(self) -> None:
        """Yield once and surface an unavailable Writer without awaiting a commit."""

        submission = self.received_submission
        if submission is None:
            return
        await asyncio.sleep(0)
        if not submission.done():
            return
        try:
            submission.result()
        except Exception:
            self.storage_failed = True
            self._raise_storage_unavailable()

    def set_authenticated(self, principal: AuthenticatedPrincipal) -> None:
        self.user_id = principal.user_id
        self.api_key_id = principal.api_key_id

    def set_route(self, route: ServiceRoute) -> None:
        self.service_id = route.service_id
        self.instance_id = route.instance_id
        self.runtime_name = route.runtime_name
        if self.metering is not None:
            self.metering_request = self.metering.begin(
                tokenizer_bundle_path=route.tokenizer_bundle_path,
                tokenizer_bundle_hash=route.tokenizer_bundle_hash,
                artifact_storage_type=route.artifact_storage_type,
            )

    def begin_metering(self, request: CanonicalRequest) -> None:
        """Schedule fallback preparation after Dispatch; never await it here."""

        if self.metering_request is not None:
            self.metering_request.start_input(_metering_texts(request))

    async def mark_in_progress(self, requested_max_tokens: int | None) -> None:
        if self.in_progress:
            return
        if self.ledger is not None:
            if (
                self.user_id is None
                or self.api_key_id is None
                or self.service_id is None
                or self.instance_id is None
            ):
                raise RuntimeError("InProgress requires authenticated route metadata")
            started_at = self.stage_timings.now()
            try:
                self.event_seq += 1
                await self._submit_required(
                    InProgressEvent(
                        request_id=self.request_id,
                        event_seq=self.event_seq,
                        created_at=utc_now(),
                        received_at=self.received_at,
                        user_id=self.user_id,
                        api_key_id=self.api_key_id,
                        service_id=self.service_id,
                        instance_id=self.instance_id,
                        requested_max_tokens=requested_max_tokens,
                    )
                )
            finally:
                self.stage_timings.finish_ledger_commit(started_at)
        self.in_progress = True

    def mark_forwarded(self) -> None:
        if not self.in_progress:
            raise RuntimeError("Runtime forwarding requires durable InProgress")
        self.forwarded = True
        if self.service_id is not None and self.service_metrics is not None:
            self.service_metrics.forwarded(self.service_id)

    def observe_event(self, event: CanonicalEvent) -> None:
        if isinstance(event, (ContentDelta, ThinkingDelta)) and event.text:
            observed_seconds = max(0.0, time.monotonic() - self.started_at)
            if self.first_token_seconds is None:
                self.first_token_seconds = observed_seconds
            self.last_token_seconds = observed_seconds
        if self.ttft_ms is None:
            self.stage_timings.begin_downstream_flush()
            self.record_first_byte(time.monotonic())
        if isinstance(event, UsageReported):
            self.runtime_input_tokens = event.usage.input_tokens
            self.runtime_output_tokens = event.usage.output_tokens
        elif isinstance(event, Completed):
            self.finish_reason = _finish_reason(event.stop_reason)
        elif isinstance(event, (ContentDelta, ThinkingDelta)) and self.metering_request is not None:
            # Observed output is kept in memory only until this request's
            # metering is decided; it is never logged, persisted, or returned.
            self.metering_request.observe_output(event.text)

    def record_first_byte(self, observed_at: float) -> None:
        if self.ttft_ms is None:
            self.ttft_ms = max(0, round((observed_at - self.started_at) * 1000))

    def observe_runtime_metrics(self, input_tokens: int | None, output_tokens: int | None) -> None:
        """Accept level 2 metering counts only from a Runtime certified for them."""

        if self.metering is None or not self.metering.supports_per_request_metrics(
            self.runtime_name
        ):
            return
        self.metrics_input_tokens = input_tokens
        self.metrics_output_tokens = output_tokens

    def _runtime_usage_complete(self) -> bool:
        return self.runtime_input_tokens is not None and self.runtime_output_tokens is not None

    def _metering_is_immediate(self) -> bool:
        """Whether deciding the metering source can finish without any waiting."""

        if self._runtime_usage_complete():
            return True
        if self.metrics_input_tokens is not None and self.metrics_output_tokens is not None:
            return True
        metering = self.metering_request
        return metering is None or not metering.fallback_available

    async def _resolve_without_ledger(self) -> None:
        """Resolve immediate service metrics when no Ledger is configured.

        Without a Ledger there is no terminal event to defer, so a fallback that
        would have to wait is simply not used; metrics keep the Runtime's
        own counts or stay null rather than delaying the response.
        """

        if self._metering_is_immediate():
            await self._resolve_metering()
        elif self.metering_request is not None:
            self.metering_request.release()

    async def _resolve_metering(self) -> None:
        metering = self.metering_request
        if metering is None:
            self.input_tokens = self.runtime_input_tokens
            self.output_tokens = self.runtime_output_tokens
            if self._runtime_usage_complete():
                self.metering_source = MeteringSource.RUNTIME_USAGE
                self.metrics_capability = MetricsCapability.FULL
            return
        self._apply_metering(
            await metering.resolve(
                usage_input=self.runtime_input_tokens,
                usage_output=self.runtime_output_tokens,
                metrics_input=self.metrics_input_tokens,
                metrics_output=self.metrics_output_tokens,
            )
        )

    def _apply_metering(self, outcome: MeteringOutcome) -> None:
        self.input_tokens = outcome.input_tokens
        self.output_tokens = outcome.output_tokens
        self.metering_source = outcome.source
        self.metrics_capability = outcome.capability
        if outcome.reason is not None:
            _LOGGER.info(
                "request metering incomplete",
                extra={
                    "metering_reason": outcome.reason,
                    "metering_source": outcome.source.value,
                    "request_id": str(self.request_id),
                },
            )

    async def mark_error(self, error: PlatformError) -> None:
        self.error_code = error.code
        self.status = RequestRecordStatus.FAILED if self.forwarded else RequestRecordStatus.REJECTED
        if self.terminal_recorded or self.storage_failed:
            return
        if self.ledger is None or self.received_submission is None:
            if self.in_progress:
                await self._resolve_without_ledger()
            elif self.metering_request is not None:
                self.metering_request.release()
            return
        if self.failure_status is None:
            raise RuntimeError("Ledger lifecycle requires an error status mapper")
        http_status = self.failure_status(error)
        self.event_seq += 1
        self.terminal_recorded = True
        if not self.in_progress:
            if self.metering_request is not None:
                self.metering_request.release()
            await self._submit_required(
                RejectedEvent(
                    request_id=self.request_id,
                    event_seq=self.event_seq,
                    created_at=utc_now(),
                    received_at=self.received_at,
                    error_code=error.code,
                    http_status=http_status,
                    user_id=self.user_id,
                    api_key_id=self.api_key_id,
                    service_id=self.service_id,
                    instance_id=self.instance_id,
                )
            )
            return
        if self._defer_terminal(
            lambda seq, duration: self._finish_failed(seq, duration, error.code, http_status)
        ):
            return
        await self._resolve_metering()
        await self._submit_terminal(
            self._failed_event(self.event_seq, self._duration_ms(), error.code, http_status)
        )

    async def mark_aborted(self) -> None:
        # Closing an upstream iterator after an encoder failure is cleanup, not
        # a second terminal outcome. Preserve the failure for metrics as well.
        if self.terminal_recorded or self.storage_failed or self.error_code is not None:
            return
        self.status = RequestRecordStatus.ABORTED
        self.error_code = None
        if self.ledger is None or self.received_submission is None:
            await self._resolve_without_ledger()
            return
        self.event_seq += 1
        self.terminal_recorded = True
        if not self.in_progress:
            if self.metering_request is not None:
                self.metering_request.release()
            await self._submit_terminal(
                self._aborted_event(self.event_seq, self._duration_ms()),
                preserve_during_cancellation=True,
            )
            return
        if self._defer_terminal(self._finish_aborted):
            return
        await self._resolve_metering()
        await self._submit_terminal(
            self._aborted_event(self.event_seq, self._duration_ms()),
            preserve_during_cancellation=True,
        )

    async def mark_completed(self) -> None:
        if self.status is None:
            self.status = RequestRecordStatus.COMPLETED
        if self.terminal_recorded or self.storage_failed or not self.in_progress:
            return
        if self.ledger is None or self.received_submission is None:
            await self._resolve_without_ledger()
            return
        self.event_seq += 1
        self.terminal_recorded = True
        if self._defer_terminal(self._finish_completed):
            return
        await self._resolve_metering()
        await self._submit_terminal(self._completed_event(self.event_seq, self._duration_ms()))

    def _defer_terminal(
        self,
        finish: Callable[[int, int], Coroutine[Any, Any, None]],
    ) -> bool:
        """Hand the terminal write to a background task when metering must wait.

        The client's response and the streaming events are already complete or
        in flight at this point; making either wait for a cold Tokenizer load
        would violate the metering order.  A cold load may only delay the Ledger write.
        """

        metering = self.metering
        if self._metering_is_immediate() or metering is None:
            return False
        metering.track(finish(self.event_seq, self._duration_ms()))
        return True

    async def _finish_completed(self, event_seq: int, duration_ms: int) -> None:
        await self._resolve_metering()
        await self._submit_terminal(self._completed_event(event_seq, duration_ms))

    async def _finish_aborted(self, event_seq: int, duration_ms: int) -> None:
        await self._resolve_metering()
        await self._submit_terminal(self._aborted_event(event_seq, duration_ms))

    async def _finish_failed(
        self, event_seq: int, duration_ms: int, error_code: ErrorCode, http_status: int
    ) -> None:
        await self._resolve_metering()
        await self._submit_terminal(
            self._failed_event(event_seq, duration_ms, error_code, http_status)
        )

    def _completed_event(self, event_seq: int, duration_ms: int) -> CompletedEvent:
        return CompletedEvent(
            request_id=self.request_id,
            event_seq=event_seq,
            created_at=utc_now(),
            received_at=self.received_at,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            ttft_ms=self.ttft_ms,
            duration_ms=duration_ms,
            finish_reason=self.finish_reason,
            http_status=200,
            metering_source=self.metering_source,
            metrics_capability=self.metrics_capability,
        )

    def _aborted_event(self, event_seq: int, duration_ms: int) -> AbortedEvent:
        return AbortedEvent(
            request_id=self.request_id,
            event_seq=event_seq,
            created_at=utc_now(),
            received_at=self.received_at,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            duration_ms=duration_ms,
            client_disconnected=True,
            user_id=self.user_id,
            api_key_id=self.api_key_id,
            service_id=self.service_id,
            instance_id=self.instance_id,
            metering_source=self.metering_source,
            metrics_capability=self.metrics_capability,
        )

    def _failed_event(
        self, event_seq: int, duration_ms: int, error_code: ErrorCode, http_status: int
    ) -> FailedEvent:
        return FailedEvent(
            request_id=self.request_id,
            event_seq=event_seq,
            created_at=utc_now(),
            received_at=self.received_at,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            ttft_ms=self.ttft_ms,
            duration_ms=duration_ms,
            error_code=error_code,
            http_status=http_status,
            metering_source=self.metering_source,
            metrics_capability=self.metrics_capability,
        )

    def _duration_ms(self) -> int:
        return max(0, round((time.monotonic() - self.started_at) * 1000))

    async def _submit_required(self, event: LedgerEvent) -> None:
        if self.ledger is None or self.storage_failed:
            self._raise_storage_unavailable()
        received = self.received_submission
        if received is None:
            raise RuntimeError("Received must precede the Ledger transition")
        ledger = self.ledger
        if ledger is None:
            self._raise_storage_unavailable()
        transition = asyncio.create_task(ledger.submit(event))
        results = await asyncio.gather(received, transition, return_exceptions=True)
        if any(isinstance(result, BaseException) for result in results):
            self.storage_failed = True
            self._raise_storage_unavailable()

    async def _submit_terminal(
        self,
        event: LedgerEvent,
        *,
        preserve_during_cancellation: bool = False,
    ) -> None:
        if self.ledger is None:
            return
        submission = asyncio.create_task(self.ledger.submit(event))
        try:
            if preserve_during_cancellation:
                await asyncio.wait_for(asyncio.shield(submission), timeout=1.0)
            else:
                await submission
        except asyncio.CancelledError:
            if not preserve_during_cancellation:
                raise
            try:
                await asyncio.wait_for(asyncio.shield(submission), timeout=1.0)
            except Exception:
                self._terminal_write_failed(event)
        except Exception:
            self._terminal_write_failed(event)

    def _terminal_write_failed(self, event: LedgerEvent) -> None:
        self.storage_failed = True
        # Losing a terminal event is the worst Ledger outcome there
        # is, so it stops new admissions and is reported at the highest
        # severity. The record carries identifiers only, never a request body.
        _LOGGER.critical(
            "request Ledger terminal event could not be persisted",
            extra={
                "error_code": "ledger_terminal_write_failed",
                "event_type": event.event_type.value,
                "request_id": str(self.request_id),
            },
        )
        sink = self.terminal_write_failure
        if sink is not None:
            sink("ledger_terminal_write_failed")

    @staticmethod
    def _raise_storage_unavailable() -> Never:
        raise InferenceFailure(PlatformError(ErrorCode.USAGE_STORAGE_UNAVAILABLE)) from None


class RequestContextMiddleware:
    """Attach one Request ID and emit only allowlisted completion metadata."""

    __slots__ = (
        "_app",
        "_failure_renderer",
        "_anthropic_failure_renderer",
        "_failure_status",
        "_ledger",
        "_ledger_health",
        "_metering",
        "_terminal_write_failure",
        "_stage_timing_sink",
        "_service_metrics",
    )

    def __init__(
        self,
        app: ASGIApp,
        ledger: LedgerEventSink | None,
        failure_renderer: FailureRenderer,
        failure_status: FailureStatus,
        metering: MeteringService | None = None,
        ledger_health: LedgerHealthProbe | None = None,
        terminal_write_failure: TerminalWriteFailureSink | None = None,
        stage_timing_sink: StageTimingSink | None = None,
        service_metrics: GatewayServiceMetrics | None = None,
        anthropic_failure_renderer: FailureRenderer | None = None,
    ) -> None:
        self._app = app
        self._ledger = ledger
        self._failure_renderer = failure_renderer
        self._anthropic_failure_renderer = anthropic_failure_renderer
        self._failure_status = failure_status
        self._metering = metering
        self._ledger_health = ledger_health
        self._terminal_write_failure = terminal_write_failure
        self._stage_timing_sink = stage_timing_sink
        self._service_metrics = service_metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        path = scope.get("path", "")
        anthropic = (
            path == "/v1/messages"
            or path.startswith("/v1/messages/")
            or (
                path == "/v1/models"
                and any(
                    name.lower() == b"anthropic-version" for name, _ in scope.get("headers", ())
                )
            )
        )
        failure_renderer = (
            self._anthropic_failure_renderer if anthropic else None
        ) or self._failure_renderer
        current_request_id = new_uuid7()
        scope[_REQUEST_ID_KEY] = current_request_id
        started_at = time.monotonic()
        stage_timings = GatewayStageTimings()
        record_context = (
            RequestRecordContext(
                current_request_id,
                utc_now(),
                started_at,
                stage_timings=stage_timings,
                ledger=self._ledger,
                ledger_health=self._ledger_health,
                terminal_write_failure=self._terminal_write_failure,
                failure_status=self._failure_status,
                metering=self._metering,
                service_metrics=self._service_metrics,
            )
            if scope.get("method") == "POST" and path in {_CHAT_PATH, "/v1/messages"}
            else None
        )
        if record_context is not None:
            if anthropic:
                record_context.protocol = RequestProtocol.ANTHROPIC
                record_context.operation = RequestOperation.MESSAGES
                record_context.endpoint = "/v1/messages"
            scope[_REQUEST_RECORD_KEY] = record_context
        status_code = 499
        response_started = False
        timing_token = bind_stage_timings(stage_timings)

        async def send_with_request_id(message: Message) -> None:
            nonlocal response_started, status_code
            if message["type"] == "http.response.start":
                response_started = True
                status_code = int(message["status"])
                headers = list(message.get("headers", ()))
                if anthropic and not any(name.lower() == b"request-id" for name, _ in headers):
                    headers.append((b"request-id", str(current_request_id).encode("ascii")))
                    message["headers"] = headers
                if not any(name.lower() == b"x-request-id" for name, _ in headers):
                    headers.append((b"x-request-id", str(current_request_id).encode("ascii")))
                    message["headers"] = headers
            elif (
                message["type"] == "http.response.body"
                and message.get("body")
                and record_context is not None
            ):
                record_context.stage_timings.finish_downstream_flush()
                record_context.record_first_byte(time.monotonic())
            await send(message)

        try:
            await self._app(scope, receive, send_with_request_id)
        except asyncio.CancelledError:
            if record_context is not None:
                await record_context.mark_aborted()
            raise
        except Exception:
            error = PlatformError(ErrorCode.BACKEND_UNAVAILABLE)
            if record_context is not None:
                try:
                    await record_context.mark_error(error)
                except InferenceFailure as failure:
                    error = failure.error
            if not response_started:
                status_code = 503
                await failure_renderer(error, current_request_id)(
                    scope, receive, send_with_request_id
                )
        finally:
            reset_stage_timings(timing_token)
            duration_ms = max(0, round((time.monotonic() - started_at) * 1000))
            if record_context is not None and self._stage_timing_sink is not None:
                self._stage_timing_sink(current_request_id, stage_timings.snapshot())
            if (
                record_context is not None
                and record_context.service_id is not None
                and self._service_metrics is not None
            ):
                self._service_metrics.finished(
                    record_context.service_id,
                    forwarded=record_context.forwarded,
                    status=record_context.status,
                    http_status=status_code,
                    error_code=record_context.error_code,
                    duration_seconds=duration_ms / 1_000,
                    first_token_seconds=record_context.first_token_seconds,
                    last_token_seconds=record_context.last_token_seconds,
                    output_tokens=record_context.output_tokens,
                )
            _LOGGER.info(
                "gateway request completed",
                extra={
                    "duration_ms": duration_ms,
                    "request_id": str(current_request_id),
                    "service_id": (
                        str(record_context.service_id)
                        if record_context is not None and record_context.service_id is not None
                        else None
                    ),
                    "status_code": status_code,
                },
            )


__all__ = [
    "FailureRenderer",
    "FailureStatus",
    "LedgerEventSink",
    "LedgerHealthProbe",
    "RequestContextMiddleware",
    "RequestRecordContext",
    "TerminalWriteFailureSink",
    "StageTimingSink",
    "request_id",
    "request_lifecycle",
]

"""In-memory Stub Runtime for the FX-RT-01 through FX-RT-11 fixtures."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Final, Self
from uuid import uuid4

FIXTURE_REVIEW_STATUS: Final = "待真机复核"
FIRST_TOKEN_TIMEOUT_BOUNDARY_SECONDS: Final = 600.0
METRICS_COLLECTION_TIMEOUT_SECONDS: Final = 2.0
METRICS_RESPONSE_LIMIT_BYTES: Final = 2 * 1024 * 1024
REQUEST_ID_HEADER: Final = "x-request-id"
RUNTIME_START_TIME_METRIC: Final = "process_start_time_seconds"
_PROMETHEUS_CONTENT_TYPE: Final = "text/plain; version=0.0.4; charset=utf-8"
_OPENMETRICS_CONTENT_TYPE: Final = "application/openmetrics-text; version=1.0.0; charset=utf-8"
_MAX_REQUEST_BYTES: Final = 1024 * 1024
_MODEL_CREATED_AT: Final = 0
_TRANSPORT_TIMING_CAPACITY: Final = 2_048


class RuntimeShape(StrEnum):
    """Runtime-native Prometheus naming shape."""

    VLLM = "vllm"
    SGLANG = "sglang"


class RuntimeMode(StrEnum):
    """M0-06A and M0-06B deterministic Runtime behavior modes."""

    FX_RT_01 = "FX-RT-01"
    FX_RT_02 = "FX-RT-02"
    FX_RT_03 = "FX-RT-03"
    FX_RT_04 = "FX-RT-04"
    FX_RT_05 = "FX-RT-05"
    FX_RT_06 = "FX-RT-06"
    FX_RT_07 = "FX-RT-07"
    FX_RT_08 = "FX-RT-08"
    FX_RT_09 = "FX-RT-09"
    FX_RT_10 = "FX-RT-10"
    FX_RT_11 = "FX-RT-11"


class HTTPFailurePoint(StrEnum):
    """Distinct FX-RT-04 5xx injection points."""

    BEFORE_FIRST_EVENT = "before_first_event"
    AFTER_FIRST_EVENT = "after_first_event"


class DisconnectPoint(StrEnum):
    """Distinct FX-RT-05 connection interruption points."""

    ON_CONNECT = "on_connect"
    AFTER_FIRST_EVENT = "after_first_event"
    BEFORE_TERMINAL = "before_terminal"


class MetricDrift(StrEnum):
    """Independently selectable FX-RT-08 metric incompatibilities."""

    HISTOGRAM_TO_GAUGE = "histogram_to_gauge"
    SECONDS_TO_MILLISECONDS = "seconds_to_milliseconds"
    LABEL_SET_CHANGE = "label_set_change"


class MetricsFault(StrEnum):
    """Distinct FX-RT-10 abnormal scrape responses."""

    OVERSIZED = "oversized"
    DUPLICATE_FAMILY = "duplicate_family"
    INVALID_TEXT = "invalid_text"
    TIMEOUT = "timeout"


class UsageVariant(StrEnum):
    """Terminal Usage shapes required by FX-RT-01/03/07."""

    COMPLETE = "complete"
    MISSING = "missing"
    PROMPT_ONLY = "prompt_only"
    COMPLETION_ONLY = "completion_only"


@dataclass(frozen=True, slots=True)
class StubTransportTiming:
    """Request-correlated wire timestamps used only by performance diagnostics."""

    request_id: str
    headers_written_at_ns: int
    first_chunk_written_at_ns: int
    tcp_nodelay: int


@dataclass(frozen=True, slots=True)
class StubRuntimeConfig:
    """Immutable constructed behavior for one isolated Stub Runtime."""

    runtime_shape: RuntimeShape = RuntimeShape.VLLM
    mode: RuntimeMode = RuntimeMode.FX_RT_01
    usage_variant: UsageVariant = UsageVariant.COMPLETE
    model_id: str = "holm-stub-model"
    first_event_delay_seconds: float = 0.01
    event_interval_seconds: float = 0.005
    prompt_tokens: int = 4
    completion_tokens: int = 3
    http_failure_point: HTTPFailurePoint | None = None
    disconnect_point: DisconnectPoint | None = None
    metric_drifts: frozenset[MetricDrift] = frozenset()
    metrics_fault: MetricsFault | None = None
    metrics_delay_seconds: float = METRICS_COLLECTION_TIMEOUT_SECONDS + 0.1
    request_id_echo_supported: bool = True

    def __post_init__(self) -> None:
        if not self.model_id or "\n" in self.model_id or "\r" in self.model_id:
            raise ValueError("model_id must be non-empty and single-line")
        if self.first_event_delay_seconds < 0:
            raise ValueError("first_event_delay_seconds must be non-negative")
        if self.event_interval_seconds < 0:
            raise ValueError("event_interval_seconds must be non-negative")
        if self.prompt_tokens < 0:
            raise ValueError("prompt_tokens must be non-negative")
        if self.completion_tokens <= 0:
            raise ValueError("completion_tokens must be positive")
        if self.metrics_delay_seconds < 0:
            raise ValueError("metrics_delay_seconds must be non-negative")

        partial_variants = {
            UsageVariant.MISSING,
            UsageVariant.PROMPT_ONLY,
            UsageVariant.COMPLETION_ONLY,
        }
        if self.mode is RuntimeMode.FX_RT_07 and self.usage_variant not in partial_variants:
            raise ValueError("FX-RT-07 requires missing or partial Usage")
        if (
            self.mode is not RuntimeMode.FX_RT_07
            and self.usage_variant is not UsageVariant.COMPLETE
        ):
            raise ValueError("only FX-RT-07 permits missing or partial Usage")

        if (
            self.mode is RuntimeMode.FX_RT_02
            and self.first_event_delay_seconds <= FIRST_TOKEN_TIMEOUT_BOUNDARY_SECONDS
        ):
            raise ValueError("FX-RT-02 delay must exceed the 600 second boundary")
        if (self.mode is RuntimeMode.FX_RT_04) is (self.http_failure_point is None):
            raise ValueError("HTTP failure point must be set only for FX-RT-04")
        if (self.mode is RuntimeMode.FX_RT_05) is (self.disconnect_point is None):
            raise ValueError("disconnect point must be set only for FX-RT-05")
        if (self.mode is RuntimeMode.FX_RT_08) is (not self.metric_drifts):
            raise ValueError("metric drift must be set only for FX-RT-08")
        if (self.mode is RuntimeMode.FX_RT_10) is (self.metrics_fault is None):
            raise ValueError("metrics fault must be set only for FX-RT-10")
        if (
            self.mode is RuntimeMode.FX_RT_10
            and self.metrics_fault is MetricsFault.TIMEOUT
            and self.metrics_delay_seconds <= METRICS_COLLECTION_TIMEOUT_SECONDS
        ):
            raise ValueError("FX-RT-10 timeout delay must exceed 2 seconds")


@dataclass(frozen=True, slots=True)
class _MetricSource:
    canonical: str
    native_name: str
    metric_type: str
    selector: str | None = None


CANONICAL_METRIC_FAMILIES: Final[Mapping[RuntimeShape, tuple[_MetricSource, ...]]] = {
    RuntimeShape.VLLM: (
        _MetricSource("runtime_ttft_seconds", "vllm:time_to_first_token_seconds", "histogram"),
        _MetricSource(
            "runtime_tpot_seconds",
            "vllm:request_time_per_output_token_seconds",
            "histogram",
        ),
        _MetricSource(
            "runtime_e2e_latency_seconds", "vllm:e2e_request_latency_seconds", "histogram"
        ),
        _MetricSource("runtime_output_tokens_total", "vllm:generation_tokens_total", "counter"),
        _MetricSource(
            "runtime_requests_total",
            "vllm:request_success_total",
            "counter",
            'finished_reason="stop"',
        ),
        _MetricSource("runtime_requests_running", "vllm:num_requests_running", "gauge"),
        _MetricSource("runtime_requests_waiting", "vllm:num_requests_waiting", "gauge"),
        _MetricSource("runtime_queue_time_seconds", "vllm:request_queue_time_seconds", "histogram"),
        _MetricSource("runtime_kv_cache_usage_ratio", "vllm:kv_cache_usage_perc", "gauge"),
        _MetricSource(
            "runtime_errors_total",
            "vllm:request_success_total",
            "counter",
            'finished_reason="abort"',
        ),
    ),
    RuntimeShape.SGLANG: (
        _MetricSource("runtime_ttft_seconds", "sglang:time_to_first_token_seconds", "histogram"),
        _MetricSource("runtime_tpot_seconds", "sglang:inter_token_latency_seconds", "histogram"),
        _MetricSource(
            "runtime_e2e_latency_seconds", "sglang:e2e_request_latency_seconds", "histogram"
        ),
        _MetricSource("runtime_output_tokens_total", "sglang:generation_tokens_total", "counter"),
        _MetricSource("runtime_requests_total", "sglang:num_requests_total", "counter"),
        _MetricSource("runtime_requests_running", "sglang:num_running_reqs", "gauge"),
        _MetricSource("runtime_requests_waiting", "sglang:num_queue_reqs", "gauge"),
        _MetricSource("runtime_queue_time_seconds", "sglang:queue_time_seconds", "histogram"),
        _MetricSource("runtime_kv_cache_usage_ratio", "sglang:token_usage", "gauge"),
        _MetricSource("runtime_errors_total", "sglang:num_aborted_requests_total", "counter"),
    ),
}


class _StubHTTPServer(ThreadingHTTPServer):
    request_queue_size = 1024
    daemon_threads = True
    block_on_close = True

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler_class: type[BaseHTTPRequestHandler],
        *,
        reuse_port: bool = False,
    ) -> None:
        self._handler_condition = threading.Condition()
        self._active_handler_count = 0
        self._accepted_connection_count = 0
        self._handler_threads: set[threading.Thread] = set()
        self.allow_reuse_port = reuse_port
        super().__init__(server_address, request_handler_class)

    @property
    def active_handler_count(self) -> int:
        with self._handler_condition:
            return self._active_handler_count

    @property
    def accepted_connection_count(self) -> int:
        with self._handler_condition:
            return self._accepted_connection_count

    @property
    def live_handler_thread_count(self) -> int:
        with self._handler_condition:
            return sum(thread.is_alive() for thread in self._handler_threads)

    def wait_for_idle(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while True:
            with self._handler_condition:
                self._handler_threads = {
                    thread for thread in self._handler_threads if thread.is_alive()
                }
                handler_threads = tuple(self._handler_threads)
                if not handler_threads:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
            for handler_thread in handler_threads:
                handler_thread.join(max(0.0, deadline - time.monotonic()))

    def process_request(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: tuple[str, int],
    ) -> None:
        handler_thread = threading.Thread(
            target=self.process_request_thread,
            args=(request, client_address),
            name="holm-stub-runtime-handler",
            daemon=self.daemon_threads,
        )
        with self._handler_condition:
            self._accepted_connection_count += 1
            self._active_handler_count += 1
            self._handler_threads.add(handler_thread)
        try:
            handler_thread.start()
        except BaseException:
            with self._handler_condition:
                self._active_handler_count -= 1
                self._handler_threads.discard(handler_thread)
                self._handler_condition.notify_all()
            self.shutdown_request(request)
            raise

    def process_request_thread(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: tuple[str, int],
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._handler_condition:
                self._active_handler_count -= 1
                self._handler_condition.notify_all()


class StubRuntime:
    """A loopback-only HTTP server whose request and response bodies stay in memory."""

    def __init__(
        self,
        config: StubRuntimeConfig | None = None,
        *,
        port: int = 0,
        reuse_port: bool = False,
    ) -> None:
        if not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if reuse_port and not hasattr(socket, "SO_REUSEPORT"):
            raise RuntimeError("SO_REUSEPORT is unavailable")
        self.config = config or StubRuntimeConfig()
        self._shutdown_event = threading.Event()
        self._client_cancelled_event = threading.Event()
        self._event_count_lock = threading.Lock()
        self._generated_event_count = 0
        self._transport_timing_lock = threading.Lock()
        self._transport_timings: deque[StubTransportTiming] = deque(
            maxlen=_TRANSPORT_TIMING_CAPACITY
        )
        self._metrics_state_lock = threading.Lock()
        self._runtime_generation = 0
        self._runtime_start_time_seconds = datetime.now(UTC).timestamp()
        handler = _handler_for(self)
        self._server = _StubHTTPServer(
            ("127.0.0.1", port),
            handler,
            reuse_port=reuse_port,
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.01},
            name="holm-stub-runtime",
            daemon=True,
        )
        self._started = False
        self._closed = False

    @property
    def host(self) -> str:
        return str(self._server.server_address[0])

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    @property
    def active_request_count(self) -> int:
        return self._server.active_handler_count

    @property
    def accepted_connection_count(self) -> int:
        """Return the cumulative TCP accepts since this Stub started."""

        return self._server.accepted_connection_count

    @property
    def generated_event_count(self) -> int:
        with self._event_count_lock:
            return self._generated_event_count

    def drain_transport_timings(self) -> tuple[StubTransportTiming, ...]:
        """Remove and return bounded transport diagnostics accumulated so far."""

        with self._transport_timing_lock:
            timings = tuple(self._transport_timings)
            self._transport_timings.clear()
        return timings

    @property
    def live_handler_thread_count(self) -> int:
        return self._server.live_handler_thread_count

    def start(self) -> Self:
        if self._closed:
            raise RuntimeError("Stub Runtime is already closed")
        if not self._started:
            self._thread.start()
            self._started = True
        return self

    def close(self) -> None:
        if self._closed:
            return
        self._shutdown_event.set()
        if self._started:
            self._server.shutdown()
        self._server.server_close()
        if self._started:
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                raise RuntimeError("Stub Runtime did not stop within 2 seconds")
        if not self._server.wait_for_idle(2.0):
            raise RuntimeError("Stub Runtime request handlers did not stop within 2 seconds")
        self._closed = True

    def wait_for_idle(self, timeout_seconds: float = 2.0) -> bool:
        """Wait for all accepted request handlers to return."""

        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        return self._server.wait_for_idle(timeout_seconds)

    def wait_for_client_cancellation(self, timeout_seconds: float = 2.0) -> bool:
        """Return whether an FX-RT-06 downstream cancellation reached the Runtime."""

        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        return self._client_cancelled_event.wait(timeout_seconds)

    def restart_runtime(self) -> None:
        """Apply the counter reset and Start Time change required by FX-RT-09."""

        if self.config.mode is not RuntimeMode.FX_RT_09:
            raise RuntimeError("restart_runtime is available only in FX-RT-09")
        with self._metrics_state_lock:
            previous_start_time = self._runtime_start_time_seconds
            self._runtime_generation += 1
            self._runtime_start_time_seconds = max(
                datetime.now(UTC).timestamp(), previous_start_time + 0.000001
            )

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def models_payload(self) -> dict[str, object]:
        return {
            "object": "list",
            "data": [
                {
                    "id": self.config.model_id,
                    "object": "model",
                    "created": _MODEL_CREATED_AT,
                    "owned_by": "holm-stub-runtime",
                }
            ],
        }

    def completion_payload(self, request_id: str | None = None) -> dict[str, object]:
        response_id = f"chatcmpl-{uuid4().hex}"
        payload: dict[str, object] = {
            "id": response_id,
            "object": "chat.completion",
            "created": _utc_timestamp(),
            "model": self.config.model_id,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": _completion_text(self.config.completion_tokens),
                    },
                    "finish_reason": "stop",
                }
            ],
        }
        usage = _usage_payload(self.config)
        if usage is not None:
            payload["usage"] = usage
        if request_id is not None:
            payload["request_id"] = request_id
        return payload

    def stream_events(self, request_id: str | None = None) -> Iterator[bytes]:
        response_id = f"chatcmpl-{uuid4().hex}"
        created = _utc_timestamp()

        if self.config.mode is RuntimeMode.FX_RT_03:
            pass
        elif self._wait_for_shutdown(self.config.first_event_delay_seconds):
            return

        for token_index in range(self.config.completion_tokens):
            if token_index and self._wait_for_shutdown(self.config.event_interval_seconds):
                return
            delta: dict[str, str] = {"content": f"stub-{token_index + 1}"}
            if token_index == 0:
                delta["role"] = "assistant"
            token_event: dict[str, object] = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": self.config.model_id,
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }
            if request_id is not None:
                token_event["request_id"] = request_id
            yield _sse_json(token_event)

        if self._wait_for_shutdown(self.config.event_interval_seconds):
            return
        terminal: dict[str, object] = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": self.config.model_id,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        usage = _usage_payload(self.config)
        if usage is not None:
            terminal["usage"] = usage
        if request_id is not None:
            terminal["request_id"] = request_id
        yield _sse_json(terminal)
        yield b"data: [DONE]\n\n"

    def metrics_payload(self) -> bytes:
        if (
            self.config.mode is RuntimeMode.FX_RT_10
            and self.config.metrics_fault is MetricsFault.TIMEOUT
            and self._shutdown_event.wait(self.config.metrics_delay_seconds)
        ):
            return b""

        metrics = self._base_metrics_payload()
        if self.config.mode is RuntimeMode.FX_RT_08:
            metrics = _apply_metric_drifts(
                metrics,
                self.config.runtime_shape,
                self.config.model_id,
                self.config.metric_drifts,
            )
        elif self.config.mode is RuntimeMode.FX_RT_09:
            with self._metrics_state_lock:
                generation = self._runtime_generation
                start_time = self._runtime_start_time_seconds
            if generation:
                metrics = _reset_counter_samples(metrics, self.config.runtime_shape)
            metrics += _runtime_start_time_family(start_time)
        elif self.config.mode is RuntimeMode.FX_RT_10:
            if self.config.metrics_fault is MetricsFault.OVERSIZED:
                encoded = metrics.encode("utf-8")
                padding_length = METRICS_RESPONSE_LIMIT_BYTES + 1 - len(encoded)
                return encoded + b"# padding " + (b"x" * max(1, padding_length)) + b"\n"
            if self.config.metrics_fault is MetricsFault.DUPLICATE_FAMILY:
                metrics += _metric_family(metrics, _ttft_metric_name(self.config.runtime_shape))
            elif self.config.metrics_fault is MetricsFault.INVALID_TEXT:
                return b'# TYPE malformed counter\nmalformed{label="unterminated" 1\n'
        return metrics.encode("utf-8")

    def metrics_content_type(self) -> str:
        if (
            self.config.mode is RuntimeMode.FX_RT_08
            and MetricDrift.SECONDS_TO_MILLISECONDS in self.config.metric_drifts
        ):
            return _OPENMETRICS_CONTENT_TYPE
        return _PROMETHEUS_CONTENT_TYPE

    def _base_metrics_payload(self) -> str:
        if self.config.runtime_shape is RuntimeShape.VLLM:
            return _vllm_metrics(self.config.model_id)
        return _sglang_metrics(self.config.model_id)

    def _wait_for_shutdown(self, delay_seconds: float) -> bool:
        if delay_seconds <= 0:
            return self._shutdown_event.is_set()
        return self._shutdown_event.wait(delay_seconds)

    def _record_generated_event(self) -> None:
        with self._event_count_lock:
            self._generated_event_count += 1

    def _record_transport_timing(
        self,
        request_id: str,
        headers_written_at_ns: int,
        first_chunk_written_at_ns: int,
        tcp_nodelay: int,
    ) -> None:
        with self._transport_timing_lock:
            self._transport_timings.append(
                StubTransportTiming(
                    request_id=request_id,
                    headers_written_at_ns=headers_written_at_ns,
                    first_chunk_written_at_ns=first_chunk_written_at_ns,
                    tcp_nodelay=tcp_nodelay,
                )
            )

    def _mark_client_cancelled(self) -> None:
        if self.config.mode is RuntimeMode.FX_RT_06:
            self._client_cancelled_event.set()


def _handler_for(runtime: StubRuntime) -> type[BaseHTTPRequestHandler]:
    class StubRuntimeRequestHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            """Disable the standard request logger; fixture payloads must never be logged."""

        def do_GET(self) -> None:
            if self.path == "/health":
                self._send_json(HTTPStatus.OK, {"status": "ok"})
                return
            if self.path == "/v1/models":
                self._send_json(HTTPStatus.OK, runtime.models_payload())
                return
            if self.path == "/metrics":
                self._send_bytes(
                    HTTPStatus.OK,
                    runtime.metrics_payload(),
                    runtime.metrics_content_type(),
                )
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def do_POST(self) -> None:
            if self.path != "/v1/chat/completions":
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return

            if (
                runtime.config.mode is RuntimeMode.FX_RT_05
                and runtime.config.disconnect_point is DisconnectPoint.ON_CONNECT
            ):
                self._disconnect_connection()
                return

            payload = self._read_json_object()
            if payload is None:
                return
            stream = payload.get("stream", False)
            if not isinstance(stream, bool):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})
                return

            request_id = self._echoed_request_id()
            if (
                runtime.config.mode is RuntimeMode.FX_RT_04
                and runtime.config.http_failure_point is HTTPFailurePoint.BEFORE_FIRST_EVENT
            ):
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    _runtime_error_payload(),
                    request_id=request_id,
                )
                return
            if stream:
                self._send_stream(request_id)
                return
            self._send_json(
                HTTPStatus.OK,
                runtime.completion_payload(request_id),
                request_id=request_id,
            )

        def _read_json_object(self) -> dict[str, object] | None:
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                self._send_json(HTTPStatus.LENGTH_REQUIRED, {"error": "length_required"})
                return None
            try:
                content_length = int(raw_length)
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})
                return None
            if content_length < 0 or content_length > _MAX_REQUEST_BYTES:
                self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request_too_large"})
                return None

            request_body = self.rfile.read(content_length)
            try:
                parsed = json.loads(request_body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})
                return None
            if not isinstance(parsed, dict):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})
                return None
            return parsed

        def _send_stream(self, request_id: str | None) -> None:
            keep_alive = runtime.config.mode is RuntimeMode.FX_RT_03
            transport_request_id = self._request_id_header()
            if keep_alive:
                # PERF-02: FX-RT-03 writes headers and its immediate first SSE chunk
                # separately.  Disable Nagle so that fixture-only write shape cannot
                # combine with the peer's delayed ACK timer and manufacture ~40 ms TTFT.
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            if keep_alive:
                self.send_header("Transfer-Encoding", "chunked")
            else:
                self.send_header("Connection", "close")
            if request_id is not None:
                self.send_header(REQUEST_ID_HEADER, request_id)
            self.end_headers()
            headers_written_at_ns = time.monotonic_ns()
            try:
                for event_index, event in enumerate(runtime.stream_events(request_id)):
                    runtime._record_generated_event()
                    if (
                        runtime.config.mode is RuntimeMode.FX_RT_05
                        and runtime.config.disconnect_point is DisconnectPoint.BEFORE_TERMINAL
                        and event_index == runtime.config.completion_tokens
                    ):
                        self._disconnect_connection()
                        return
                    self._write_stream_bytes(event, chunked=keep_alive)
                    self.wfile.flush()
                    if event_index == 0 and transport_request_id is not None:
                        runtime._record_transport_timing(
                            transport_request_id,
                            headers_written_at_ns,
                            time.monotonic_ns(),
                            self.connection.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY),
                        )
                    if event_index != 0:
                        continue
                    if (
                        runtime.config.mode is RuntimeMode.FX_RT_04
                        and runtime.config.http_failure_point is HTTPFailurePoint.AFTER_FIRST_EVENT
                    ):
                        runtime._record_generated_event()
                        # HTTP cannot replace its status line after the stream starts. Certified
                        # Runtime adapters therefore consume a late 5xx as an in-stream error
                        # envelope; the static status_code preserves the injected classification.
                        self._write_stream_bytes(
                            _sse_json(_runtime_error_payload()),
                            chunked=keep_alive,
                        )
                        self.wfile.flush()
                        self.close_connection = True
                        return
                    if (
                        runtime.config.mode is RuntimeMode.FX_RT_05
                        and runtime.config.disconnect_point is DisconnectPoint.AFTER_FIRST_EVENT
                    ):
                        self._disconnect_connection()
                        return
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                runtime._mark_client_cancelled()
                return
            if keep_alive:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
                self.close_connection = False
            else:
                self.close_connection = True

        def _write_stream_bytes(self, payload: bytes, *, chunked: bool) -> None:
            if chunked:
                self.wfile.write(f"{len(payload):X}\r\n".encode("ascii"))
                self.wfile.write(payload)
                self.wfile.write(b"\r\n")
                return
            self.wfile.write(payload)

        def _send_json(
            self,
            status: HTTPStatus,
            payload: Mapping[str, object],
            *,
            request_id: str | None = None,
        ) -> None:
            body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            response_headers = None
            if request_id is not None:
                response_headers = {REQUEST_ID_HEADER: request_id}
            self._send_bytes(
                status,
                body,
                "application/json; charset=utf-8",
                response_headers=response_headers,
            )

        def _send_bytes(
            self,
            status: HTTPStatus,
            body: bytes,
            content_type: str,
            *,
            response_headers: Mapping[str, str] | None = None,
        ) -> None:
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                if response_headers is not None:
                    for name, value in response_headers.items():
                        self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                runtime._mark_client_cancelled()
            self.close_connection = True

        def _echoed_request_id(self) -> str | None:
            if (
                runtime.config.mode is not RuntimeMode.FX_RT_11
                or not runtime.config.request_id_echo_supported
            ):
                return None
            return self._request_id_header()

        def _request_id_header(self) -> str | None:
            request_id = self.headers.get(REQUEST_ID_HEADER)
            if (
                request_id is None
                or not request_id
                or len(request_id) > 128
                or "\r" in request_id
                or "\n" in request_id
            ):
                return None
            return request_id

        def _disconnect_connection(self) -> None:
            with suppress(OSError):
                self.connection.shutdown(socket.SHUT_RDWR)
            self.close_connection = True

    return StubRuntimeRequestHandler


def _utc_timestamp() -> int:
    return int(datetime.now(UTC).timestamp())


def _completion_text(completion_tokens: int) -> str:
    return " ".join(f"stub-{index + 1}" for index in range(completion_tokens))


def _usage_payload(config: StubRuntimeConfig) -> dict[str, int] | None:
    if config.usage_variant is UsageVariant.MISSING:
        return None
    if config.usage_variant is UsageVariant.PROMPT_ONLY:
        return {"prompt_tokens": config.prompt_tokens}
    if config.usage_variant is UsageVariant.COMPLETION_ONLY:
        return {"completion_tokens": config.completion_tokens}
    return {
        "prompt_tokens": config.prompt_tokens,
        "completion_tokens": config.completion_tokens,
        "total_tokens": config.prompt_tokens + config.completion_tokens,
    }


def _sse_json(payload: Mapping[str, object]) -> bytes:
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return b"data: " + encoded + b"\n\n"


def _runtime_error_payload() -> dict[str, object]:
    return {
        "error": {
            "type": "server_error",
            "code": "runtime_error",
            "message": "Injected server failure.",
            "status_code": HTTPStatus.INTERNAL_SERVER_ERROR,
        }
    }


def _prometheus_labels(model_id: str, extra: Mapping[str, str] | None = None) -> str:
    labels = {"model_name": model_id}
    if extra:
        labels.update(extra)
    rendered = ",".join(
        f'{key}="{_escape_prometheus_label(value)}"' for key, value in sorted(labels.items())
    )
    return f"{{{rendered}}}"


def _escape_prometheus_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _histogram_family(
    name: str,
    help_text: str,
    model_id: str,
    observation: float,
    buckets: tuple[float, ...],
    extra_labels: Mapping[str, str] | None = None,
) -> list[str]:
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} histogram"]
    for bucket in buckets:
        count = 1 if observation <= bucket else 0
        bucket_labels = dict(extra_labels or {})
        bucket_labels["le"] = str(bucket)
        labels = _prometheus_labels(model_id, bucket_labels)
        lines.append(f"{name}_bucket{labels} {count}")
    infinity_extra_labels = dict(extra_labels or {})
    infinity_extra_labels["le"] = "+Inf"
    infinity_labels = _prometheus_labels(model_id, infinity_extra_labels)
    model_labels = _prometheus_labels(model_id, extra_labels)
    lines.extend(
        (
            f"{name}_bucket{infinity_labels} 1",
            f"{name}_sum{model_labels} {observation}",
            f"{name}_count{model_labels} 1",
        )
    )
    return lines


def _ttft_metric_name(runtime_shape: RuntimeShape) -> str:
    if runtime_shape is RuntimeShape.VLLM:
        return "vllm:time_to_first_token_seconds"
    return "sglang:time_to_first_token_seconds"


def _metric_family(metrics: str, metric_name: str) -> str:
    lines = metrics.splitlines()
    start_marker = f"# HELP {metric_name} "
    start_index = next(
        (index for index, line in enumerate(lines) if line.startswith(start_marker)),
        None,
    )
    if start_index is None:
        raise RuntimeError(f"metric family is missing: {metric_name}")
    end_index = next(
        (
            index
            for index in range(start_index + 1, len(lines))
            if lines[index].startswith("# HELP ")
        ),
        len(lines),
    )
    return "\n".join(lines[start_index:end_index]) + "\n"


def _apply_metric_drifts(
    metrics: str,
    runtime_shape: RuntimeShape,
    model_id: str,
    drifts: frozenset[MetricDrift],
) -> str:
    metric_name = _ttft_metric_name(runtime_shape)
    original_family = _metric_family(metrics, metric_name)
    uses_milliseconds = MetricDrift.SECONDS_TO_MILLISECONDS in drifts
    observation = 50.0 if uses_milliseconds else 0.05
    unit = "milliseconds" if uses_milliseconds else "seconds"
    extra_labels: Mapping[str, str] = (
        {"replica": "drifted"} if MetricDrift.LABEL_SET_CHANGE in drifts else {}
    )
    if MetricDrift.HISTOGRAM_TO_GAUGE in drifts:
        replacement_lines = _simple_family(
            metric_name,
            "gauge",
            f"Time to first token in {unit}.",
            ((extra_labels, observation),),
            model_id,
        )
    else:
        buckets = (10.0, 100.0, 1000.0) if uses_milliseconds else (0.01, 0.1, 1.0)
        replacement_lines = _histogram_family(
            metric_name,
            f"Histogram of time to first token in {unit}.",
            model_id,
            observation,
            buckets,
            extra_labels,
        )
    if uses_milliseconds:
        replacement_lines.insert(2, f"# UNIT {metric_name} milliseconds")
    replacement_family = "\n".join(replacement_lines) + "\n"
    drifted_metrics = metrics.replace(original_family, replacement_family, 1)
    if uses_milliseconds:
        drifted_metrics += "# EOF\n"
    return drifted_metrics


def _reset_counter_samples(metrics: str, runtime_shape: RuntimeShape) -> str:
    counter_names = {
        source.native_name
        for source in CANONICAL_METRIC_FAMILIES[runtime_shape]
        if source.metric_type == "counter"
    }
    reset_lines: list[str] = []
    for line in metrics.splitlines():
        if any(
            line.startswith(f"{name}{{") or line.startswith(f"{name} ") for name in counter_names
        ):
            sample, _, _ = line.rpartition(" ")
            reset_lines.append(f"{sample} 0.0")
        else:
            reset_lines.append(line)
    return "\n".join(reset_lines) + "\n"


def _runtime_start_time_family(start_time_seconds: float) -> str:
    return "\n".join(
        (
            f"# HELP {RUNTIME_START_TIME_METRIC} Runtime process start time in seconds.",
            f"# TYPE {RUNTIME_START_TIME_METRIC} gauge",
            f"{RUNTIME_START_TIME_METRIC} {start_time_seconds}",
            "",
        )
    )


def _simple_family(
    name: str,
    metric_type: str,
    help_text: str,
    samples: tuple[tuple[Mapping[str, str], float], ...],
    model_id: str,
) -> list[str]:
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} {metric_type}"]
    for extra_labels, value in samples:
        lines.append(f"{name}{_prometheus_labels(model_id, extra_labels)} {value}")
    return lines


def _vllm_metrics(model_id: str) -> str:
    lines: list[str] = []
    lines.extend(
        _histogram_family(
            "vllm:time_to_first_token_seconds",
            "Histogram of time to first token in seconds.",
            model_id,
            0.05,
            (0.01, 0.1, 1.0),
        )
    )
    lines.extend(
        _histogram_family(
            "vllm:request_time_per_output_token_seconds",
            "Histogram of time per output token in seconds.",
            model_id,
            0.02,
            (0.01, 0.1, 1.0),
        )
    )
    lines.extend(
        _histogram_family(
            "vllm:e2e_request_latency_seconds",
            "Histogram of end-to-end request latency in seconds.",
            model_id,
            0.25,
            (0.1, 0.5, 1.0),
        )
    )
    lines.extend(
        _simple_family(
            "vllm:generation_tokens_total",
            "counter",
            "Number of generation tokens processed.",
            (({}, 8.0),),
            model_id,
        )
    )
    lines.extend(
        _simple_family(
            "vllm:request_success_total",
            "counter",
            "Number of finished requests by finish reason.",
            (({"finished_reason": "stop"}, 3.0), ({"finished_reason": "abort"}, 1.0)),
            model_id,
        )
    )
    lines.extend(
        _simple_family(
            "vllm:num_requests_running",
            "gauge",
            "Number of requests currently running.",
            (({}, 1.0),),
            model_id,
        )
    )
    lines.extend(
        _simple_family(
            "vllm:num_requests_waiting",
            "gauge",
            "Number of requests currently waiting.",
            (({}, 0.0),),
            model_id,
        )
    )
    lines.extend(
        _histogram_family(
            "vllm:request_queue_time_seconds",
            "Histogram of time spent waiting in seconds.",
            model_id,
            0.005,
            (0.001, 0.01, 0.1),
        )
    )
    lines.extend(
        _simple_family(
            "vllm:kv_cache_usage_perc",
            "gauge",
            "Fraction of used KV cache blocks.",
            (({}, 0.25),),
            model_id,
        )
    )
    return "\n".join(lines) + "\n"


def _sglang_metrics(model_id: str) -> str:
    lines: list[str] = []
    lines.extend(
        _histogram_family(
            "sglang:time_to_first_token_seconds",
            "Histogram of time to first token in seconds.",
            model_id,
            0.05,
            (0.01, 0.1, 1.0),
        )
    )
    lines.extend(
        _histogram_family(
            "sglang:inter_token_latency_seconds",
            "Histogram of inter-token latency in seconds.",
            model_id,
            0.02,
            (0.01, 0.1, 1.0),
        )
    )
    lines.extend(
        _histogram_family(
            "sglang:e2e_request_latency_seconds",
            "Histogram of end-to-end request latency in seconds.",
            model_id,
            0.25,
            (0.1, 0.5, 1.0),
        )
    )
    lines.extend(
        _simple_family(
            "sglang:generation_tokens_total",
            "counter",
            "Number of generation tokens processed.",
            (({"is_streaming": "true"}, 8.0),),
            model_id,
        )
    )
    lines.extend(
        _simple_family(
            "sglang:num_requests_total",
            "counter",
            "Number of requests processed.",
            (({"is_streaming": "true"}, 3.0),),
            model_id,
        )
    )
    lines.extend(
        _simple_family(
            "sglang:num_running_reqs",
            "gauge",
            "Number of running requests.",
            (({}, 1.0),),
            model_id,
        )
    )
    lines.extend(
        _simple_family(
            "sglang:num_queue_reqs",
            "gauge",
            "Number of requests in the waiting queue.",
            (({}, 0.0),),
            model_id,
        )
    )
    lines.extend(
        _histogram_family(
            "sglang:queue_time_seconds",
            "Histogram of queueing time in seconds.",
            model_id,
            0.005,
            (0.001, 0.01, 0.1),
        )
    )
    lines.extend(
        _simple_family(
            "sglang:token_usage",
            "gauge",
            "KV cache token usage ratio.",
            (({}, 0.25),),
            model_id,
        )
    )
    lines.extend(
        _simple_family(
            "sglang:num_aborted_requests_total",
            "counter",
            "Number of requests aborted.",
            (({}, 1.0),),
            model_id,
        )
    )
    return "\n".join(lines) + "\n"

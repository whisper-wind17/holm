"""Re-runnable TC-PF-01 benchmark and frozen-report loader."""

from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing
import os
import platform
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import CodeType
from typing import Any, Final, cast
from uuid import UUID

import httpx
import sqlalchemy as sa
import uvicorn
from fixtures.stub_runtime.server import (
    RuntimeMode,
    StubRuntime,
    StubRuntimeConfig,
    StubTransportTiming,
)
from holm_gateway.application import create_app
from holm_gateway.authentication import ApiKeyAuthenticator
from holm_gateway.configuration import GatewayConfiguration
from holm_gateway.route_snapshot import SnapshotApiKeyValidationLookup
from holm_gateway.stage_timing import StageTimingSnapshot
from holm_ledger import SpoolWriter
from holm_runtimes import RuntimeRequestCapabilities
from holm_security import ApiKeyPepperKeyring
from performance.judgement import (
    PLATFORM_EXTRA_TTFT_TARGET_MS,
    REQUIRED_PERCENTILES,
    REQUIRED_STAGES,
    assert_baseline_judgement,
)
from starlette.types import ASGIApp

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
BASELINE_ROOT: Final[Path] = Path(__file__).with_name("baseline")
PROFILE_PATH: Final[Path] = BASELINE_ROOT / "TC_PF_01_profile.json"
REPORT_PATH: Final[Path] = BASELINE_ROOT / "TC_PF_01_gateway_stage_baseline.json"
_INTEGRATION_ROOT: Final[Path] = REPOSITORY_ROOT / "tests" / "integration"
_REPORT_CASE: Final[str] = "TC-PF-01/TC-GW-29"
_REQUEST_PATH: Final[str] = "/v1/chat/completions"
_MODEL_NAME: Final[str] = "m4-04-model"
_COLLECTOR_CAPACITY: Final[int] = 2_048
_PROCESS_START_TIMEOUT_SECONDS: Final[float] = 20.0
_PROCESS_STOP_TIMEOUT_SECONDS: Final[float] = 10.0
_STUB_RUNTIME_WORKERS: Final[int] = 8
_LOAD_CLIENT_PROCESSES: Final[int] = 8
_LOAD_START_DELAY_SECONDS: Final[float] = 0.1


class BaselineReportError(ValueError):
    """Raised when a baseline cannot carry the frozen TC-PF-01 judgement."""


def _load_mapping(path: Path, label: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BaselineReportError(f"{label} 无法读取") from error
    if not isinstance(payload, dict):
        raise BaselineReportError(f"{label} 必须是 JSON object")
    return cast(Mapping[str, Any], payload)


def load_frozen_profile() -> Mapping[str, Any]:
    return _load_mapping(PROFILE_PATH, "冻结 Profile")


def load_baseline_report() -> Mapping[str, Any]:
    report = _load_mapping(REPORT_PATH, "冻结基线报告")
    validate_report(report)
    return report


def validate_report(payload: Mapping[str, Any]) -> None:
    """Fail loud for every malformed or non-comparable report."""

    try:
        if payload.get("case") != _REPORT_CASE:
            raise AssertionError("报告 case 与 TC-PF-01/TC-GW-29 不匹配")
        assert_baseline_judgement(payload)
        if dict(cast(Mapping[str, Any], payload["profile"])) != dict(load_frozen_profile()):
            raise AssertionError("报告 Profile 与仓库冻结 Profile 不一致")
        totals = cast(Mapping[str, Any], payload["totals"])
        if totals.get("requests_failed") != 0:
            raise AssertionError("零错误率报告的 requests_failed 必须为 0")
        environment = payload.get("environment")
        if not isinstance(environment, Mapping):
            raise AssertionError("报告缺少 environment")
        for field in ("host", "cpu", "memory_mib", "python"):
            if not environment.get(field):
                raise AssertionError(f"报告 environment 缺少 {field}")
    except (AssertionError, KeyError, TypeError, ValueError) as error:
        raise BaselineReportError(str(error)) from error


@dataclass(slots=True)
class _ProcessTimingSink:
    """Fixed-capacity, non-blocking bridge from the Gateway process."""

    timing_queue: Any
    overflowed: Any
    transport_timings: dict[UUID, _GatewayTransportTiming]

    def __call__(self, request_id: UUID, snapshot: StageTimingSnapshot) -> None:
        try:
            self.timing_queue.put_nowait(
                (request_id, snapshot, self.transport_timings.pop(request_id, None))
            )
        except queue.Full:
            self.overflowed.set()


class _TimingCollector:
    """Bounded parent-side sink; values are removed as clients finish."""

    def __init__(self, timing_queue: Any, overflowed: Any) -> None:
        self._timing_queue = timing_queue
        self._overflowed = overflowed
        self._samples: dict[UUID, tuple[StageTimingSnapshot, _GatewayTransportTiming | None]] = {}

    def take(self, request_id: UUID) -> tuple[StageTimingSnapshot, _GatewayTransportTiming | None]:
        deadline = time.monotonic() + 5.0
        while True:
            if self._overflowed.is_set():
                raise BaselineReportError("阶段计时 sink 达到固定容量")
            with suppress(queue.Empty):
                queued_request_id, snapshot, transport_timing = self._timing_queue.get_nowait()
                self._samples[queued_request_id] = (snapshot, transport_timing)
            sample = self._samples.pop(request_id, None)
            if sample is not None:
                return sample
            if time.monotonic() >= deadline:
                raise BaselineReportError("请求完成后未收到阶段计时")
            time.sleep(0.0005)


@dataclass(frozen=True, slots=True)
class _GatewaySample:
    ttft_ms: float
    request_id: UUID
    stages: StageTimingSnapshot
    transport_timing: _GatewayTransportTiming | None


@dataclass(frozen=True, slots=True)
class _GatewayObservation:
    ttft_ms: float
    request_id: UUID


@dataclass(frozen=True, slots=True)
class _DirectSample:
    ttft_ms: float


@dataclass(slots=True)
class _GatewayTransportTiming:
    headers_received_at_ns: int
    first_chunk_received_at_ns: int | None
    tcp_nodelay: int


@dataclass(frozen=True, slots=True)
class CpuMonitoringFunction:
    """One sanitized PEP 669 event-interval row from the Gateway loop thread."""

    filename: str
    line_number: int
    function_name: str
    entry_events: int
    self_seconds: float


@dataclass(frozen=True, slots=True)
class CpuMonitoringWindow:
    """CPU observations for one explicitly enabled PEP 669 window."""

    process_cpu_seconds: float
    attributed_cpu_seconds: float
    monitoring_events: int
    stack_mismatches: int
    functions: tuple[CpuMonitoringFunction, ...]


class _TimestampingStream(httpx.AsyncByteStream):
    """Record when httpcore hands the first response-body bytes to HTTPX."""

    def __init__(
        self,
        inner: httpx.AsyncByteStream,
        timing: _GatewayTransportTiming,
    ) -> None:
        self._inner = inner
        self._timing = timing

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            if self._timing.first_chunk_received_at_ns is None:
                self._timing.first_chunk_received_at_ns = time.monotonic_ns()
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


@dataclass(frozen=True, slots=True)
class _RuntimeProcessRig:
    base_url: str
    connections: tuple[Any, ...]
    processes: tuple[Any, ...]

    def accepted_connection_count(self) -> int:
        for connection in self.connections:
            connection.send(("accepted_connection_count",))
        total = 0
        for index, (connection, process) in enumerate(
            zip(self.connections, self.processes, strict=True)
        ):
            message = _receive_child_message(
                connection,
                process,
                f"Stub Runtime worker {index + 1}",
            )
            if len(message) != 2 or message[0] != "accepted_connection_count":
                raise BaselineReportError("Stub Runtime accept 计数消息无效")
            total += int(message[1])
        return total

    def drain_transport_timings(self) -> tuple[StubTransportTiming, ...]:
        for connection in self.connections:
            connection.send(("drain_transport_timings",))
        timings: list[StubTransportTiming] = []
        for index, (connection, process) in enumerate(
            zip(self.connections, self.processes, strict=True)
        ):
            message = _receive_child_message(
                connection,
                process,
                f"Stub Runtime worker {index + 1}",
            )
            if len(message) != 2 or message[0] != "transport_timings":
                raise BaselineReportError("Stub Runtime 传输计时消息无效")
            timings.extend(message[1])
        return tuple(timings)


@contextmanager
def _ephemeral_postgres() -> Iterator[tuple[Any, Any]]:
    """Reuse the Integration fixture and M4-04 route-snapshot bootstrap verbatim."""

    integration_path = str(_INTEGRATION_ROOT)
    sys.path.insert(0, integration_path)
    try:
        import conftest as integration_conftest
        import test_M4_04_snapshot_integration as snapshot_fixture

        fixture_factory = getattr(integration_conftest.ephemeral_postgres, "__wrapped__", None)
        if fixture_factory is None:
            raise BaselineReportError("无法复用 Integration PostgreSQL fixture")
        fixture_iterator = fixture_factory()
        fixture = next(fixture_iterator)
        try:
            yield fixture, snapshot_fixture
        finally:
            try:
                next(fixture_iterator)
            except StopIteration:
                pass
            else:
                raise BaselineReportError("Integration PostgreSQL fixture 未正常收敛")
    finally:
        if sys.path and sys.path[0] == integration_path:
            sys.path.pop(0)


def _bound_socket() -> socket.socket:
    bound = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    bound.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    bound.bind(("127.0.0.1", 0))
    bound.listen(2_048)
    bound.setblocking(False)
    return bound


@asynccontextmanager
async def _serve(app: ASGIApp) -> AsyncIterator[str]:
    bound = _bound_socket()
    port = int(bound.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            access_log=False,
            log_level="warning",
            lifespan="on",
            proxy_headers=False,
            server_header=False,
        )
    )
    task = asyncio.create_task(server.serve(sockets=[bound]))
    try:
        deadline = time.monotonic() + 10.0
        while not server.started:
            if task.done():
                await task
                raise BaselineReportError("基准 HTTP 服务未启动")
            if time.monotonic() >= deadline:
                raise BaselineReportError("基准 HTTP 服务启动超时")
            await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=10.0)
        finally:
            bound.close()


def _send_child_message(sender: Any, message: tuple[Any, ...]) -> None:
    with suppress(BrokenPipeError, EOFError, OSError):
        sender.send(message)


def _runtime_process_worker(connection: Any, stop_event: Any, port: int) -> None:
    try:
        with StubRuntime(
            StubRuntimeConfig(
                mode=RuntimeMode.FX_RT_03,
                completion_tokens=1,
                event_interval_seconds=0,
            ),
            port=port,
            reuse_port=True,
        ) as runtime:
            _send_child_message(connection, ("ready", runtime.base_url))
            while not stop_event.is_set():
                if not connection.poll(0.05):
                    continue
                command = connection.recv()
                if command == ("accepted_connection_count",):
                    _send_child_message(
                        connection,
                        ("accepted_connection_count", runtime.accepted_connection_count),
                    )
                elif command == ("drain_transport_timings",):
                    _send_child_message(
                        connection,
                        ("transport_timings", runtime.drain_transport_timings()),
                    )
                else:
                    raise BaselineReportError("Stub Runtime worker 收到未知命令")
    except BaseException as error:
        _send_child_message(connection, ("error", type(error).__name__))
        raise
    finally:
        connection.close()


class _MonitoringProfiler:
    """Attribute process CPU intervals between PEP 669 lifecycle events.

    The callbacks intentionally observe only the Gateway event-loop thread.  This
    prevents the process-wide clock from being counted once per Python thread;
    CPU consumed by helper threads remains visible as unattributed residual.
    """

    def __init__(self) -> None:
        monitoring = cast(Any, sys.monitoring)
        self._monitoring = monitoring
        self._tool_id = int(monitoring.PROFILER_ID)
        self._thread_id = threading.get_ident()
        self._stack: list[CodeType] = []
        self._nanoseconds_by_code: dict[CodeType, int] = {}
        self._entries_by_code: dict[CodeType, int] = {}
        self._last_cpu_ns: int | None = None
        self._started_cpu_ns: int | None = None
        self._event_count = 0
        self._stack_mismatches = 0
        monitoring.use_tool_id(self._tool_id, "holm-perf-03")
        for event, callback in (
            (monitoring.events.PY_START, self._enter),
            (monitoring.events.PY_RESUME, self._enter),
            (monitoring.events.PY_RETURN, self._exit),
            (monitoring.events.PY_YIELD, self._exit),
            (monitoring.events.PY_UNWIND, self._exit),
        ):
            monitoring.register_callback(self._tool_id, event, callback)
        self._events = (
            monitoring.events.PY_START
            | monitoring.events.PY_RESUME
            | monitoring.events.PY_RETURN
            | monitoring.events.PY_YIELD
            | monitoring.events.PY_UNWIND
        )

    def _attribute_until_now(self) -> None:
        now = time.process_time_ns()
        if self._last_cpu_ns is not None and self._stack:
            code = self._stack[-1]
            self._nanoseconds_by_code[code] = self._nanoseconds_by_code.get(code, 0) + (
                now - self._last_cpu_ns
            )
        self._last_cpu_ns = now

    def _enter(self, code: CodeType, _instruction_offset: int) -> None:
        if threading.get_ident() != self._thread_id:
            return
        self._attribute_until_now()
        self._stack.append(code)
        self._entries_by_code[code] = self._entries_by_code.get(code, 0) + 1
        self._event_count += 1

    def _exit(self, code: CodeType, _instruction_offset: int, _value: Any) -> None:
        if threading.get_ident() != self._thread_id:
            return
        self._attribute_until_now()
        if self._stack and self._stack[-1] is code:
            self._stack.pop()
        else:
            self._stack_mismatches += 1
            for index in range(len(self._stack) - 1, -1, -1):
                if self._stack[index] is code:
                    del self._stack[index]
                    break
        self._event_count += 1

    def start(self) -> None:
        if self._started_cpu_ns is not None:
            raise BaselineReportError("Gateway sys.monitoring 窗口状态冲突")
        self._stack.clear()
        self._nanoseconds_by_code.clear()
        self._entries_by_code.clear()
        self._event_count = 0
        self._stack_mismatches = 0
        self._started_cpu_ns = time.process_time_ns()
        self._last_cpu_ns = self._started_cpu_ns
        self._monitoring.set_events(self._tool_id, self._events)

    def stop(self) -> tuple[float, float, int, int, tuple[tuple[Any, ...], ...]]:
        if self._started_cpu_ns is None:
            raise BaselineReportError("Gateway sys.monitoring 窗口尚未开始")
        self._attribute_until_now()
        stopped_cpu_ns = time.process_time_ns()
        self._monitoring.set_events(self._tool_id, 0)
        rows = tuple(
            (
                code.co_filename,
                code.co_firstlineno,
                getattr(code, "co_qualname", code.co_name),
                self._entries_by_code.get(code, 0),
                attributed_ns / 1_000_000_000,
            )
            for code, attributed_ns in self._nanoseconds_by_code.items()
        )
        process_cpu_seconds = (stopped_cpu_ns - self._started_cpu_ns) / 1_000_000_000
        attributed_cpu_seconds = sum(self._nanoseconds_by_code.values()) / 1_000_000_000
        result = (
            process_cpu_seconds,
            attributed_cpu_seconds,
            self._event_count,
            self._stack_mismatches,
            rows,
        )
        self._started_cpu_ns = None
        self._last_cpu_ns = None
        self._stack.clear()
        return result

    def close(self) -> None:
        self._monitoring.set_events(self._tool_id, 0)
        self._monitoring.free_tool_id(self._tool_id)


async def _serve_gateway_profile_control(profile_control: Any) -> None:
    """Run control and PEP 669 setup only when the test-only switch is enabled."""

    profiler = _MonitoringProfiler()
    process_cpu_started_at: int | None = None
    try:
        while True:
            try:
                command = await asyncio.to_thread(profile_control.recv)
            except EOFError:
                return
            if command == ("cpu_window_start",):
                if process_cpu_started_at is not None:
                    raise BaselineReportError("Gateway CPU 对照窗口状态冲突")
                process_cpu_started_at = time.process_time_ns()
                _send_child_message(profile_control, ("cpu_window_started",))
            elif command == ("cpu_window_stop",):
                if process_cpu_started_at is None:
                    raise BaselineReportError("Gateway CPU 对照窗口尚未开始")
                elapsed = (time.process_time_ns() - process_cpu_started_at) / 1_000_000_000
                process_cpu_started_at = None
                _send_child_message(profile_control, ("cpu_window", elapsed))
            elif command == ("monitoring_start",):
                if process_cpu_started_at is not None:
                    raise BaselineReportError("Gateway sys.monitoring 窗口状态冲突")
                profiler.start()
                _send_child_message(profile_control, ("monitoring_started",))
            elif command == ("monitoring_stop",):
                result = profiler.stop()
                _send_child_message(profile_control, ("monitoring", *result))
            else:
                raise BaselineReportError("Gateway 剖析控制收到未知命令")
    finally:
        profiler.close()


async def _gateway_process_worker_async(
    fixture: Any,
    max_inflight: int,
    timing_queue: Any,
    overflowed: Any,
    sender: Any,
    stop_event: Any,
    profile_control: Any | None,
) -> None:
    import test_M4_04_snapshot_integration as snapshot_fixture

    async with snapshot_fixture._environment(fixture, listen=False) as environment:
        keyring = ApiKeyPepperKeyring(current_key_id="m4-04", keys={"m4-04": b"K" * 32})
        with tempfile.TemporaryDirectory(prefix="holm-m4-14-") as temporary:
            ledger = SpoolWriter(Path(temporary) / "request-ledger.sqlite3")
            transport_timings: dict[UUID, _GatewayTransportTiming] = {}

            async def record_response_headers(response: httpx.Response) -> None:
                raw_request_id = response.request.headers.get("x-request-id")
                network_stream = response.extensions.get("network_stream")
                if raw_request_id is None or network_stream is None:
                    raise BaselineReportError("Gateway 上游响应缺少传输诊断上下文")
                upstream_socket = network_stream.get_extra_info("socket")
                if upstream_socket is None:
                    raise BaselineReportError("Gateway 上游连接无法读取 socket 选项")
                timing = _GatewayTransportTiming(
                    headers_received_at_ns=time.monotonic_ns(),
                    first_chunk_received_at_ns=None,
                    tcp_nodelay=int(
                        upstream_socket.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
                    ),
                )
                transport_timings[UUID(raw_request_id)] = timing
                response.stream = _TimestampingStream(response.stream, timing)

            configuration = GatewayConfiguration(
                max_inflight_requests=max_inflight,
                max_inflight_per_instance=max_inflight,
            )
            upstream_client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    None,
                    connect=configuration.runtime_connect_timeout_seconds,
                    pool=configuration.runtime_pool_timeout_ms / 1000,
                ),
                limits=httpx.Limits(
                    max_connections=max_inflight,
                    max_keepalive_connections=min(32, max_inflight),
                    keepalive_expiry=None,
                ),
                follow_redirects=False,
                trust_env=False,
                event_hooks={"response": [record_response_headers]},
            )
            try:
                gateway = create_app(
                    authenticator=ApiKeyAuthenticator(
                        lookup=SnapshotApiKeyValidationLookup(environment.manager),
                        keyring=keyring,
                        availability_check=environment.manager.ensure_available,
                    ),
                    routes=environment.manager,
                    runtime_capabilities={
                        "vllm": RuntimeRequestCapabilities(
                            openai_chat_completions=frozenset(
                                {"model", "messages", "stream", "max_tokens"}
                            )
                        )
                    },
                    configuration=configuration,
                    upstream_client=upstream_client,
                    request_ledger=ledger,
                    stage_timing_sink=_ProcessTimingSink(
                        timing_queue,
                        overflowed,
                        transport_timings,
                    ),
                )
                async with _serve(gateway) as gateway_url:
                    _send_child_message(
                        sender,
                        ("ready", gateway_url, environment.plaintext_key),
                    )
                    profile_task = (
                        asyncio.create_task(_serve_gateway_profile_control(profile_control))
                        if profile_control is not None
                        else None
                    )
                    try:
                        while not stop_event.is_set():
                            if profile_task is not None and profile_task.done():
                                await profile_task
                            await asyncio.sleep(0.01)
                    finally:
                        if profile_task is not None:
                            profile_task.cancel()
                            with suppress(asyncio.CancelledError):
                                await profile_task
            finally:
                await upstream_client.aclose()


def _gateway_process_worker(
    fixture: Any,
    max_inflight: int,
    timing_queue: Any,
    overflowed: Any,
    sender: Any,
    stop_event: Any,
    profile_control: Any | None,
) -> None:
    try:
        asyncio.run(
            _gateway_process_worker_async(
                fixture,
                max_inflight,
                timing_queue,
                overflowed,
                sender,
                stop_event,
                profile_control,
            )
        )
    except BaseException as error:
        _send_child_message(sender, ("error", type(error).__name__))
        raise
    finally:
        sender.close()
        if profile_control is not None:
            profile_control.close()


def _wait_for_child(receiver: Any, process: Any, label: str) -> tuple[Any, ...]:
    message = _receive_child_message(receiver, process, label)
    if message and message[0] == "ready":
        return message
    error_type = message[1] if len(message) > 1 else "unknown"
    raise BaselineReportError(f"{label} 子进程启动失败：{error_type}")


def _receive_child_message(receiver: Any, process: Any, label: str) -> tuple[Any, ...]:
    deadline = time.monotonic() + _PROCESS_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if receiver.poll(0.05):
            return cast(tuple[Any, ...], receiver.recv())
        if not process.is_alive():
            raise BaselineReportError(f"{label} 子进程在响应前退出")
    raise BaselineReportError(f"{label} 子进程响应超时")


def _stop_child(process: Any, stop_event: Any) -> None:
    stop_event.set()
    process.join(_PROCESS_STOP_TIMEOUT_SECONDS)
    if process.is_alive():
        process.terminate()
        process.join(_PROCESS_STOP_TIMEOUT_SECONDS)


@dataclass(frozen=True, slots=True)
class _GatewayProcessRig:
    gateway_url: str
    key: str
    timing_queue: Any
    overflowed: Any
    pid: int
    process: Any
    profile_control: Any | None

    def __iter__(self) -> Iterator[Any]:
        """Preserve the five-value M4-14 harness interface."""

        yield self.gateway_url
        yield self.key
        yield self.timing_queue
        yield self.overflowed
        yield self.pid

    def _profile_command(self, command: str, expected: str) -> tuple[Any, ...]:
        if self.profile_control is None:
            raise BaselineReportError("Gateway CPU 剖析开关未启用")
        self.profile_control.send((command,))
        message = _receive_child_message(
            self.profile_control,
            self.process,
            "Gateway CPU 剖析控制",
        )
        if not message or message[0] != expected:
            raise BaselineReportError("Gateway CPU 剖析控制响应无效")
        return message

    def start_cpu_window(self) -> None:
        self._profile_command("cpu_window_start", "cpu_window_started")

    def stop_cpu_window(self) -> float:
        return float(self._profile_command("cpu_window_stop", "cpu_window")[1])

    def start_cpu_monitoring(self) -> None:
        self._profile_command("monitoring_start", "monitoring_started")

    def stop_cpu_monitoring(self) -> CpuMonitoringWindow:
        message = self._profile_command("monitoring_stop", "monitoring")
        rows = tuple(
            CpuMonitoringFunction(
                filename=str(row[0]),
                line_number=int(row[1]),
                function_name=str(row[2]),
                entry_events=int(row[3]),
                self_seconds=float(row[4]),
            )
            for row in message[5]
        )
        return CpuMonitoringWindow(
            process_cpu_seconds=float(message[1]),
            attributed_cpu_seconds=float(message[2]),
            monitoring_events=int(message[3]),
            stack_mismatches=int(message[4]),
            functions=rows,
        )


@contextmanager
def _runtime_process() -> Iterator[_RuntimeProcessRig]:
    context = multiprocessing.get_context("fork")
    stop_event = context.Event()
    processes: list[Any] = []
    connections: list[Any] = []
    runtime_url: str | None = None
    try:
        for worker_index in range(_STUB_RUNTIME_WORKERS):
            parent, child = context.Pipe(duplex=True)
            port = 0 if runtime_url is None else int(httpx.URL(runtime_url).port or 0)
            process = context.Process(
                target=_runtime_process_worker,
                args=(child, stop_event, port),
                name=f"m4-14-stub-runtime-{worker_index + 1}",
            )
            process.start()
            processes.append(process)
            child.close()
            connections.append(parent)
            message = _wait_for_child(
                parent,
                process,
                f"Stub Runtime worker {worker_index + 1}",
            )
            worker_url = str(message[1])
            if runtime_url is None:
                runtime_url = worker_url
            elif worker_url != runtime_url:
                raise BaselineReportError("Stub Runtime workers 未共用同一个 Endpoint")
        if runtime_url is None:
            raise BaselineReportError("Stub Runtime 没有启动 worker")
        yield _RuntimeProcessRig(
            base_url=runtime_url,
            connections=tuple(connections),
            processes=tuple(processes),
        )
    finally:
        stop_event.set()
        for process in processes:
            process.join(_PROCESS_STOP_TIMEOUT_SECONDS)
        for process in processes:
            if process.is_alive():
                process.terminate()
        for process in processes:
            if process.is_alive():
                process.join(_PROCESS_STOP_TIMEOUT_SECONDS)
        for connection in connections:
            connection.close()


@contextmanager
def _gateway_process(
    fixture: Any,
    max_inflight: int,
    *,
    enable_cpu_monitoring: bool = False,
) -> Iterator[_GatewayProcessRig]:
    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    profile_parent: Any | None = None
    profile_child: Any | None = None
    if enable_cpu_monitoring:
        profile_parent, profile_child = context.Pipe(duplex=True)
    stop_event = context.Event()
    timing_queue = context.Queue(maxsize=_COLLECTOR_CAPACITY)
    overflowed = context.Event()
    process = context.Process(
        target=_gateway_process_worker,
        args=(
            fixture,
            max_inflight,
            timing_queue,
            overflowed,
            sender,
            stop_event,
            profile_child,
        ),
        name="m4-14-gateway",
    )
    process.start()
    sender.close()
    if profile_child is not None:
        profile_child.close()
    try:
        message = _wait_for_child(receiver, process, "Gateway")
        yield _GatewayProcessRig(
            gateway_url=str(message[1]),
            key=str(message[2]),
            timing_queue=timing_queue,
            overflowed=overflowed,
            pid=int(cast(int, process.pid)),
            process=process,
            profile_control=profile_parent,
        )
    finally:
        receiver.close()
        if profile_parent is not None:
            profile_parent.close()
        _stop_child(process, stop_event)
        timing_queue.close()
        timing_queue.join_thread()


def _percentiles(values: Sequence[float]) -> dict[str, float]:
    if not values:
        raise BaselineReportError("分位统计没有样本")
    ordered = sorted(values)

    def percentile(fraction: float) -> float:
        position = (len(ordered) - 1) * fraction
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        weight = position - lower
        return round(ordered[lower] * (1.0 - weight) + ordered[upper] * weight, 3)

    return {"p50": percentile(0.50), "p95": percentile(0.95), "p99": percentile(0.99)}


def _stage_value(sample: StageTimingSnapshot, stage: str) -> float:
    value = getattr(sample, f"{stage}_ms")
    if value is None:
        raise BaselineReportError(f"成功请求缺少阶段计时：{stage}")
    return float(value)


async def _one_gateway_request(
    client: httpx.AsyncClient,
    gateway_url: str,
    key: str,
) -> _GatewayObservation:
    started_at = time.monotonic()
    async with client.stream(
        "POST",
        f"{gateway_url}{_REQUEST_PATH}",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": _MODEL_NAME, "messages": [], "stream": True, "max_tokens": 1},
    ) as response:
        if response.status_code != 200:
            error_code = "unknown"
            try:
                envelope = await response.aread()
                decoded = json.loads(envelope)
                if isinstance(decoded, dict) and isinstance(decoded.get("error"), dict):
                    raw_code = decoded["error"].get("code")
                    if isinstance(raw_code, str):
                        error_code = raw_code
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            raise BaselineReportError(f"基准请求返回 HTTP {response.status_code}/{error_code}")
        chunks = response.aiter_raw()
        try:
            await anext(chunks)
        except StopAsyncIteration:
            raise BaselineReportError("基准请求未收到首事件") from None
        observed_ttft_ms = (time.monotonic() - started_at) * 1000.0
        async for _chunk in chunks:
            pass
        raw_request_id = response.headers.get("x-request-id")
        if raw_request_id is None:
            raise BaselineReportError("基准响应缺少 x-request-id")
    return _GatewayObservation(
        ttft_ms=observed_ttft_ms,
        request_id=UUID(raw_request_id),
    )


async def _one_direct_request(
    client: httpx.AsyncClient,
    runtime_url: str,
) -> _DirectSample:
    started_at = time.monotonic()
    async with client.stream(
        "POST",
        f"{runtime_url}{_REQUEST_PATH}",
        json={"model": _MODEL_NAME, "messages": [], "stream": True, "max_tokens": 1},
    ) as response:
        if response.status_code != 200:
            raise BaselineReportError(f"直连 Stub 返回 HTTP {response.status_code}")
        chunks = response.aiter_raw()
        try:
            await anext(chunks)
        except StopAsyncIteration:
            raise BaselineReportError("直连 Stub 未收到首事件") from None
        observed_ttft_ms = (time.monotonic() - started_at) * 1000.0
        async for _chunk in chunks:
            pass
    return _DirectSample(ttft_ms=observed_ttft_ms)


async def _run_level[Sample](
    clients: Sequence[httpx.AsyncClient],
    concurrency: int,
    request: Callable[[httpx.AsyncClient], Awaitable[Sample]],
    *,
    start_at: float | None = None,
) -> list[Sample]:
    if concurrency > len(clients):
        raise BaselineReportError("负载并发超过预热客户端数量")
    ready = asyncio.Event()

    async def participant(client: httpx.AsyncClient) -> Sample:
        await ready.wait()
        if start_at is not None:
            await asyncio.sleep(max(0.0, start_at - time.monotonic()))
        return await request(client)

    tasks = [asyncio.create_task(participant(client)) for client in clients[:concurrency]]
    ready.set()
    return list(await asyncio.gather(*tasks))


def _measurement_block(
    samples: Sequence[_GatewaySample | _GatewayObservation | _DirectSample], elapsed: float
) -> dict[str, Any]:
    return {
        "ttft_ms": _percentiles([sample.ttft_ms for sample in samples]),
        "throughput_rps": round(len(samples) / elapsed, 3),
    }


def _subtract_percentiles(
    gateway: Mapping[str, Any], direct: Mapping[str, Any]
) -> dict[str, float]:
    return {
        percentile: round(float(gateway[percentile]) - float(direct[percentile]), 3)
        for percentile in REQUIRED_PERCENTILES
    }


def _target_status(platform_extra_ttft_ms: Mapping[str, Any]) -> dict[str, float | str]:
    p95 = float(platform_extra_ttft_ms["p95"])
    return {
        "platform_extra_ttft_target_ms": PLATFORM_EXTRA_TTFT_TARGET_MS,
        "status": "MET" if p95 <= PLATFORM_EXTRA_TTFT_TARGET_MS else "NOT_MET",
    }


def _new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        limits=httpx.Limits(
            max_connections=1,
            max_keepalive_connections=1,
            keepalive_expiry=None,
        ),
        timeout=httpx.Timeout(30.0),
        trust_env=False,
    )


async def _close_clients(clients: Sequence[httpx.AsyncClient]) -> None:
    for client in clients:
        await client.aclose()


def _split_concurrency(concurrency: int, process_count: int) -> list[int]:
    quotient, remainder = divmod(concurrency, process_count)
    return [quotient + (index < remainder) for index in range(process_count)]


async def _load_client_worker_async(
    connection: Any,
    slot_count: int,
    runtime_url: str,
    gateway_url: str | None,
    key: str | None,
) -> None:
    direct_clients = [_new_client() for _ in range(slot_count)]
    gateway_clients = [_new_client() for _ in range(slot_count)] if gateway_url is not None else []

    async def direct_request(client: httpx.AsyncClient) -> _DirectSample:
        return await _one_direct_request(client, runtime_url)

    async def gateway_request(client: httpx.AsyncClient) -> _GatewayObservation:
        if gateway_url is None or key is None:
            raise BaselineReportError("负载客户端没有 Gateway 配置")
        return await _one_gateway_request(client, gateway_url, key)

    try:
        _send_child_message(connection, ("ready",))
        while True:
            command = connection.recv()
            action = command[0]
            if action == "stop":
                _send_child_message(connection, ("stopped",))
                return
            if action != "run":
                raise BaselineReportError("负载客户端收到未知命令")
            path, concurrency, start_at, repetitions = (
                str(command[1]),
                int(command[2]),
                float(command[3]),
                int(command[4]),
            )
            if repetitions <= 0:
                raise BaselineReportError("负载客户端重复次数必须为正")
            if path == "direct":
                direct_samples: list[_DirectSample] = []
                for repetition in range(repetitions):
                    direct_samples.extend(
                        await _run_level(
                            direct_clients,
                            concurrency,
                            direct_request,
                            start_at=start_at if repetition == 0 else None,
                        )
                    )
                _send_child_message(connection, ("result", direct_samples))
            elif path == "gateway":
                gateway_samples: list[_GatewayObservation] = []
                for repetition in range(repetitions):
                    gateway_samples.extend(
                        await _run_level(
                            gateway_clients,
                            concurrency,
                            gateway_request,
                            start_at=start_at if repetition == 0 else None,
                        )
                    )
                _send_child_message(connection, ("result", gateway_samples))
            else:
                raise BaselineReportError("负载客户端收到未知路径")
    finally:
        await _close_clients(direct_clients)
        await _close_clients(gateway_clients)


def _load_client_worker(
    connection: Any,
    slot_count: int,
    runtime_url: str,
    gateway_url: str | None,
    key: str | None,
) -> None:
    try:
        asyncio.run(
            _load_client_worker_async(
                connection,
                slot_count,
                runtime_url,
                gateway_url,
                key,
            )
        )
    except BaseException as error:
        _send_child_message(connection, ("error", type(error).__name__))
        raise
    finally:
        connection.close()


class _LoadClientRig:
    """K persistent event loops, with one keep-alive client per concurrency slot."""

    def __init__(
        self,
        concurrency: int,
        runtime_url: str,
        gateway_url: str | None = None,
        key: str | None = None,
    ) -> None:
        context = multiprocessing.get_context("fork")
        self._slot_counts = _split_concurrency(concurrency, _LOAD_CLIENT_PROCESSES)
        self._connections: list[Any] = []
        self._processes: list[Any] = []
        for index, slot_count in enumerate(self._slot_counts):
            parent, child = context.Pipe(duplex=True)
            process = context.Process(
                target=_load_client_worker,
                args=(child, slot_count, runtime_url, gateway_url, key),
                name=f"m4-14-load-client-{index + 1}",
            )
            process.start()
            child.close()
            self._connections.append(parent)
            self._processes.append(process)
        for index, (connection, process) in enumerate(
            zip(self._connections, self._processes, strict=True)
        ):
            message = _receive_child_message(connection, process, f"负载客户端 {index + 1}")
            if message != ("ready",):
                raise BaselineReportError("负载客户端就绪消息无效")

    def run(
        self,
        path: str,
        concurrency: int,
        *,
        repetitions: int = 1,
    ) -> tuple[list[_DirectSample] | list[_GatewayObservation], float]:
        counts = _split_concurrency(concurrency, len(self._connections))
        if any(count > slots for count, slots in zip(counts, self._slot_counts, strict=True)):
            raise BaselineReportError("负载并发超过预热客户端数量")
        start_at = time.monotonic() + _LOAD_START_DELAY_SECONDS
        for connection, count in zip(self._connections, counts, strict=True):
            connection.send(("run", path, count, start_at, repetitions))
        samples: list[_DirectSample] | list[_GatewayObservation] = []
        for index, (connection, process) in enumerate(
            zip(self._connections, self._processes, strict=True)
        ):
            message = _receive_child_message(connection, process, f"负载客户端 {index + 1}")
            if message[0] != "result":
                raise BaselineReportError("负载客户端结果消息无效")
            samples.extend(message[1])
        return samples, max(time.monotonic() - start_at, 1e-9)

    def close(self) -> None:
        for connection, process in zip(self._connections, self._processes, strict=True):
            if process.is_alive():
                with suppress(BrokenPipeError, EOFError, OSError):
                    connection.send(("stop",))
        for connection, process in zip(self._connections, self._processes, strict=True):
            if process.is_alive():
                with suppress(BrokenPipeError, EOFError, OSError, BaselineReportError):
                    _receive_child_message(connection, process, "负载客户端")
            connection.close()
            process.join(_PROCESS_STOP_TIMEOUT_SECONDS)
            if process.is_alive():
                process.terminate()
                process.join(_PROCESS_STOP_TIMEOUT_SECONDS)


@contextmanager
def _load_client_rig(
    concurrency: int,
    runtime_url: str,
    gateway_url: str | None = None,
    key: str | None = None,
) -> Iterator[_LoadClientRig]:
    rig = _LoadClientRig(concurrency, runtime_url, gateway_url, key)
    try:
        yield rig
    finally:
        rig.close()


def _gateway_samples(
    observations: Sequence[_GatewayObservation],
    collector: _TimingCollector,
) -> list[_GatewaySample]:
    samples: list[_GatewaySample] = []
    for observation in observations:
        stages, transport_timing = collector.take(observation.request_id)
        samples.append(
            _GatewaySample(
                ttft_ms=observation.ttft_ms,
                request_id=observation.request_id,
                stages=stages,
                transport_timing=transport_timing,
            )
        )
    return samples


def _transport_diagnostics(
    gateway_samples: Sequence[_GatewaySample],
    stub_timings: Sequence[StubTransportTiming],
) -> dict[str, Any]:
    stub_by_request_id = {UUID(timing.request_id): timing for timing in stub_timings}
    aligned: list[dict[str, int | float]] = []
    missing_stub = 0
    missing_gateway = 0
    missing_gateway_first_chunk = 0
    for sample in gateway_samples:
        stub = stub_by_request_id.get(sample.request_id)
        gateway = sample.transport_timing
        if stub is None:
            missing_stub += 1
            continue
        if gateway is None:
            missing_gateway += 1
            continue
        if gateway.first_chunk_received_at_ns is None:
            missing_gateway_first_chunk += 1
            continue
        aligned.append(
            {
                "stub_headers_written_at_ns": stub.headers_written_at_ns,
                "stub_first_chunk_written_at_ns": stub.first_chunk_written_at_ns,
                "gateway_headers_received_at_ns": gateway.headers_received_at_ns,
                "gateway_first_chunk_received_at_ns": gateway.first_chunk_received_at_ns,
                "stub_tcp_nodelay": stub.tcp_nodelay,
                "gateway_tcp_nodelay": gateway.tcp_nodelay,
                "stub_write_gap_ms": (stub.first_chunk_written_at_ns - stub.headers_written_at_ns)
                / 1_000_000,
                "gateway_receive_gap_ms": (
                    gateway.first_chunk_received_at_ns - gateway.headers_received_at_ns
                )
                / 1_000_000,
                "first_chunk_transit_ms": (
                    gateway.first_chunk_received_at_ns - stub.first_chunk_written_at_ns
                )
                / 1_000_000,
            }
        )
    if len(aligned) != len(gateway_samples):
        raise BaselineReportError(
            "四点传输计时无法按 Request ID 对齐："
            f"samples={len(gateway_samples)}, stub={len(stub_timings)}, "
            f"missing_stub={missing_stub}, missing_gateway={missing_gateway}, "
            f"missing_gateway_first_chunk={missing_gateway_first_chunk}"
        )
    ordered = sorted(aligned, key=lambda item: float(item["gateway_receive_gap_ms"]))
    representative = ordered[round((len(ordered) - 1) * 0.95)]
    return {
        "sample_count": len(aligned),
        "stub_tcp_nodelay": sorted({int(item["stub_tcp_nodelay"]) for item in aligned}),
        "gateway_tcp_nodelay": sorted({int(item["gateway_tcp_nodelay"]) for item in aligned}),
        "stub_write_gap_ms": _percentiles([float(item["stub_write_gap_ms"]) for item in aligned]),
        "gateway_receive_gap_ms": _percentiles(
            [float(item["gateway_receive_gap_ms"]) for item in aligned]
        ),
        "first_chunk_transit_ms": _percentiles(
            [float(item["first_chunk_transit_ms"]) for item in aligned]
        ),
        "p95_rank_four_timestamps_ns": {
            key: int(representative[key])
            for key in (
                "stub_headers_written_at_ns",
                "stub_first_chunk_written_at_ns",
                "gateway_headers_received_at_ns",
                "gateway_first_chunk_received_at_ns",
            )
        },
    }


def _warmup_rig(
    rig: _LoadClientRig,
    path: str,
    warmup_requests: int,
    concurrency: int,
    collector: _TimingCollector | None = None,
) -> None:
    if warmup_requests < concurrency:
        raise BaselineReportError("预热请求数不足以覆盖每个并发槽位")
    remaining = warmup_requests
    while remaining:
        batch_size = min(remaining, concurrency)
        samples, _ = rig.run(path, batch_size)
        if collector is not None:
            _gateway_samples(cast(Sequence[_GatewayObservation], samples), collector)
        remaining -= batch_size


def _measure_direct_levels(
    profile: Mapping[str, Any], runtime: _RuntimeProcessRig
) -> list[dict[str, Any]]:
    concurrency = int(profile["concurrency"])
    configured_levels = cast(list[Mapping[str, Any]], profile["load_levels"])
    with _load_client_rig(concurrency, runtime.base_url) as rig:
        _warmup_rig(rig, "direct", int(profile["warmup_requests"]), concurrency)
        levels: list[dict[str, Any]] = []
        for configured in configured_levels:
            level_concurrency = int(configured["concurrency"])
            samples, elapsed = rig.run("direct", level_concurrency)
            levels.append(
                {
                    "label": str(configured["label"]),
                    "concurrency": level_concurrency,
                    **_measurement_block(samples, elapsed),
                }
            )
        return levels


def _measure_paths(
    profile: Mapping[str, Any],
    runtime: _RuntimeProcessRig,
    gateway_url: str,
    key: str,
    timing_queue: Any,
    overflowed: Any,
    gateway_pid: int,
) -> tuple[list[dict[str, Any]], list[_GatewaySample]]:
    concurrency = int(profile["concurrency"])
    warmup_requests = int(profile["warmup_requests"])
    configured_levels = cast(list[Mapping[str, Any]], profile["load_levels"])
    collector = _TimingCollector(timing_queue, overflowed)
    with _load_client_rig(concurrency, runtime.base_url, gateway_url, key) as rig:
        _warmup_rig(rig, "direct", warmup_requests, concurrency)
        _warmup_rig(rig, "gateway", warmup_requests, concurrency, collector)
        levels: list[dict[str, Any]] = []
        baseline_samples: list[_GatewaySample] = []
        for configured in configured_levels:
            level_concurrency = int(configured["concurrency"])
            direct_accepts_before = runtime.accepted_connection_count()
            direct_samples, direct_elapsed = rig.run("direct", level_concurrency)
            direct_accepts = runtime.accepted_connection_count() - direct_accepts_before
            gateway_accepts_before = runtime.accepted_connection_count()
            runtime.drain_transport_timings()
            gateway_cpu_before = _process_cpu_seconds(gateway_pid)
            observations, gateway_elapsed = rig.run("gateway", level_concurrency)
            gateway_cpu_seconds = _process_cpu_seconds(gateway_pid) - gateway_cpu_before
            gateway_accepts = runtime.accepted_connection_count() - gateway_accepts_before
            gateway_samples = _gateway_samples(
                cast(Sequence[_GatewayObservation], observations),
                collector,
            )
            stub_transport_timings = runtime.drain_transport_timings()
            direct = _measurement_block(direct_samples, direct_elapsed)
            gateway = _measurement_block(gateway_samples, gateway_elapsed)
            stage_buckets = {
                stage: _percentiles(
                    [_stage_value(sample.stages, stage) for sample in gateway_samples]
                )
                for stage in REQUIRED_STAGES
            }
            platform_extra_ttft_ms = _subtract_percentiles(
                cast(Mapping[str, Any], gateway["ttft_ms"]),
                cast(Mapping[str, Any], direct["ttft_ms"]),
            )
            levels.append(
                {
                    "label": str(configured["label"]),
                    "concurrency": level_concurrency,
                    "gateway": gateway,
                    "direct": direct,
                    "platform_extra_ttft_ms": platform_extra_ttft_ms,
                    **_target_status(platform_extra_ttft_ms),
                    "stages": stage_buckets,
                    "diagnostics": {
                        "gateway_cpu_seconds": round(gateway_cpu_seconds, 6),
                        "gateway_cpu_seconds_per_request": round(
                            gateway_cpu_seconds / len(gateway_samples), 9
                        ),
                        "gateway_cpu_one_core_utilization_percent": round(
                            gateway_cpu_seconds / gateway_elapsed * 100.0,
                            3,
                        ),
                        "stub_direct_tcp_accepts": direct_accepts,
                        "stub_direct_accepts_per_request": round(
                            direct_accepts / len(direct_samples), 6
                        ),
                        "stub_gateway_tcp_accepts": gateway_accepts,
                        "stub_gateway_accepts_per_request": round(
                            gateway_accepts / len(gateway_samples), 6
                        ),
                        "transport_timing": _transport_diagnostics(
                            gateway_samples,
                            stub_transport_timings,
                        ),
                    },
                }
            )
            if level_concurrency == concurrency:
                baseline_samples = gateway_samples
        return levels, baseline_samples


def _seed_runtime_endpoint(fixture: Any, snapshot_fixture: Any, runtime_url: str) -> None:
    snapshot_fixture._seed(fixture)
    parsed = httpx.URL(runtime_url)
    if parsed.host is None or parsed.port is None:
        raise BaselineReportError("Stub Runtime 地址无效")
    with fixture.engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE core.model_instances SET internal_endpoint = :endpoint "
                "WHERE id = :instance_id"
            ),
            {
                "endpoint": f"{parsed.host}:{parsed.port}",
                "instance_id": snapshot_fixture.INSTANCE_A,
            },
        )


def _commit() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout.strip()


def _memory_mib() -> int:
    pages = os.sysconf("SC_PHYS_PAGES")
    page_size = os.sysconf("SC_PAGE_SIZE")
    return int(pages * page_size / (1024 * 1024))


def _process_cpu_seconds(pid: int) -> float:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields = stat.rsplit(")", 1)[1].split()
        clock_ticks = int(os.sysconf("SC_CLK_TCK"))
        return (int(fields[11]) + int(fields[12])) / clock_ticks
    except (IndexError, OSError, UnicodeError, ValueError) as error:
        raise BaselineReportError("无法采样 Gateway 进程 CPU") from error


def _environment() -> dict[str, Any]:
    return {
        "host": platform.node(),
        "cpu": platform.processor() or platform.machine(),
        "memory_mib": _memory_mib(),
        "python": platform.python_version(),
    }


def _assert_frozen_profile(profile: Mapping[str, Any]) -> None:
    if dict(profile) != dict(load_frozen_profile()):
        raise BaselineReportError("实测只能使用仓库冻结 Profile")


def measure_direct(profile: Mapping[str, Any]) -> Mapping[str, Any]:
    """Measure the control path against FX-RT-03 without traversing Gateway."""

    _assert_frozen_profile(profile)
    with _runtime_process() as runtime:
        levels = _measure_direct_levels(profile, runtime)
    return {"profile": dict(profile), "levels": levels}


def measure(profile: Mapping[str, Any]) -> Mapping[str, Any]:
    _assert_frozen_profile(profile)
    max_inflight = int(profile["gateway_max_inflight"])
    with (
        _ephemeral_postgres() as (fixture, snapshot_fixture),
        _runtime_process() as runtime,
    ):
        _seed_runtime_endpoint(fixture, snapshot_fixture, runtime.base_url)
        with _gateway_process(fixture, max_inflight) as gateway_process:
            gateway_url, key, timing_queue, overflowed, gateway_pid = gateway_process
            levels, baseline_samples = _measure_paths(
                profile,
                runtime,
                gateway_url,
                key,
                timing_queue,
                overflowed,
                gateway_pid,
            )
    concurrency = int(profile["concurrency"])
    if len(baseline_samples) != concurrency:
        raise BaselineReportError("负载阶梯没有产生 200 并发基准样本")
    baseline_level = next(level for level in levels if int(level["concurrency"]) == concurrency)
    stage_buckets = {
        stage: _percentiles([_stage_value(sample.stages, stage) for sample in baseline_samples])
        for stage in REQUIRED_STAGES
    }
    return {
        "case": _REPORT_CASE,
        "commit": _commit(),
        "measured_at": datetime.now(UTC).isoformat(),
        "environment": _environment(),
        "profile": dict(profile),
        "platform_extra_ttft_target_ms": PLATFORM_EXTRA_TTFT_TARGET_MS,
        "status": baseline_level["status"],
        "totals": {
            "requests_completed": len(baseline_samples),
            "requests_failed": 0,
            "error_rate": 0.0,
        },
        "client_observed": {
            "ttft_ms": baseline_level["gateway"]["ttft_ms"],
            "platform_extra_ttft_ms": baseline_level["platform_extra_ttft_ms"],
        },
        "stages": stage_buckets,
        "levels": levels,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=PROFILE_PATH)
    parser.add_argument("--write", type=Path)
    return parser


def main() -> None:
    args = _parser().parse_args()
    profile = _load_mapping(args.profile, "负载 Profile")
    if dict(profile) != dict(load_frozen_profile()):
        raise BaselineReportError("指定 Profile 与仓库冻结 Profile 不一致")
    report = measure(profile)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.write is not None:
        args.write.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    validate_report(report)


if __name__ == "__main__":
    main()

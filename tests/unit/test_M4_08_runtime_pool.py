"""M4-08 persistent Runtime pools, connect timeout, and streaming behavior."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import UUID

import httpx
import pytest
from holm_domain import ErrorCode, ModelInstanceStatus
from holm_gateway.__main__ import _parse_args
from holm_gateway.configuration import GatewayConfiguration, GatewayConfigurationError
from holm_gateway.openai_adapter import OpenAIProtocolAdapter
from holm_gateway.openai_types import OpenAIRequestExtension
from holm_gateway.request_context import RequestRecordContext
from holm_gateway.route_snapshot import RouteSnapshot, RouteSnapshotManager
from holm_gateway.routing import GatewayRoute
from holm_gateway.vllm_adapter import VllmRuntimeAdapter
from holm_protocols import (
    AdmissionController,
    CanonicalMessage,
    CanonicalRequest,
    InferenceCore,
    InferenceFailure,
    SamplingParameters,
    ServiceModelReference,
    ServiceRoute,
    TextContent,
)
from holm_runtimes import load_runtime_compatibility
from starlette.responses import StreamingResponse
from starlette.types import Message, Scope

SERVICE_ID = UUID("78000000-0000-4000-8000-000000000001")
INSTANCE_ID = UUID("78000000-0000-4000-8000-000000000002")
REQUEST_ID = UUID("78000000-0000-4000-8000-000000000003")
NOW = datetime(2026, 9, 17, tzinfo=UTC)
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
VLLM_RUNTIME = load_runtime_compatibility(REPOSITORY_ROOT / "runtime-compat.yaml").runtime("vllm")
assert VLLM_RUNTIME is not None


def _protocol() -> OpenAIProtocolAdapter:
    return OpenAIProtocolAdapter({"vllm": VLLM_RUNTIME.request_capabilities}, maximum_json_depth=32)


def _gateway_route(
    endpoint: str,
    *,
    enabled: bool = True,
    status: str = ModelInstanceStatus.RUNNING.value,
    public_model_name: str = "m4-08-model",
) -> GatewayRoute:
    return GatewayRoute(
        service_id=SERVICE_ID,
        instance_id=INSTANCE_ID,
        public_model_name=public_model_name,
        enabled=enabled,
        instance_status=status,
        runtime_name="vllm",
        internal_endpoint=endpoint,
        internal_model_name="internal-model",
    )


def _service_route(endpoint: str) -> ServiceRoute:
    return ServiceRoute(
        service_id=SERVICE_ID,
        instance_id=INSTANCE_ID,
        public_model_name="m4-08-model",
        enabled=True,
        ready=True,
        endpoint=endpoint,
        runtime_name="vllm",
        runtime_model_name="internal-model",
    )


def _request(*, stream: bool, runtime_model: bool = False) -> CanonicalRequest:
    return CanonicalRequest(
        service_model=ServiceModelReference(
            "m4-08-model",
            runtime_model_name="internal-model" if runtime_model else None,
        ),
        stream=stream,
        sampling=SamplingParameters(maximum_output_tokens=8),
        messages=(CanonicalMessage("user", (TextContent("memory-only"),)),),
        tools=(),
        extension=OpenAIRequestExtension(False),
    )


def _snapshot(version: int, routes: tuple[GatewayRoute, ...]) -> RouteSnapshot:
    return RouteSnapshot(
        version=version,
        loaded_at=float(version),
        api_keys_by_lookup=MappingProxyType({}),
        users_by_id=MappingProxyType({}),
        key_scopes_by_key_id=MappingProxyType({}),
        routes_by_model=MappingProxyType({route.public_model_name: route for route in routes}),
    )


class _SnapshotSource:
    def __init__(self, snapshot: RouteSnapshot) -> None:
        self.snapshot = snapshot

    async def load(self, *, loaded_at: float) -> RouteSnapshot:
        del loaded_at
        return self.snapshot

    async def current_event_sequence(self) -> int:
        return self.snapshot.version


class _TrackingClient:
    created: list[_TrackingClient] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.closed = False
        self.__class__.created.append(self)

    def build_request(self, method: str, url: str, **kwargs: object) -> httpx.Request:
        return httpx.Request(method, url, **kwargs)

    async def send(self, request: httpx.Request, *, stream: bool) -> httpx.Response:
        del stream
        return httpx.Response(
            200,
            request=request,
            content=b"data: "
            + json.dumps(
                {"model": "internal-model", "choices": [{"finish_reason": "stop"}]}
            ).encode()
            + b"\n\ndata: [DONE]\n\n",
        )

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize("value", (0.0, 0.09, 60.01, True))
def test_M4_08_connect_timeout_configuration_rejects_out_of_bounds(value: object) -> None:
    with pytest.raises(GatewayConfigurationError) as raised:
        GatewayConfiguration(runtime_connect_timeout_seconds=value)  # type: ignore[arg-type]
    assert raised.value.code == "runtime_connect_timeout_invalid"


def test_M4_08_connect_timeout_defaults_to_three_seconds_in_config_and_cli() -> None:
    args = _parse_args(
        [
            "--database-url-file",
            "/tmp/database-url",
            "--api-key-pepper-keyring-file",
            "/tmp/keyring",
            "--runtime-compatibility",
            "/tmp/runtime-compat.yaml",
        ]
    )

    assert GatewayConfiguration().runtime_connect_timeout_seconds == 3.0
    assert args.runtime_connect_timeout_seconds == 3.0


def test_M4_08_snapshot_prewarms_only_running_enabled_endpoint_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        _TrackingClient.created = []
        monkeypatch.setattr(httpx, "AsyncClient", _TrackingClient)
        runtime = VllmRuntimeAdapter(
            max_response_bytes=4096,
            max_sse_event_bytes=4096,
            connect_timeout_seconds=3.0,
        )
        source = _SnapshotSource(
            _snapshot(
                1,
                (
                    _gateway_route("runtime-ready:8000"),
                    _gateway_route(
                        "runtime-starting:8000",
                        status=ModelInstanceStatus.STARTING.value,
                        public_model_name="m4-08-starting",
                    ),
                    _gateway_route(
                        "runtime-disabled:8000",
                        enabled=False,
                        public_model_name="m4-08-disabled",
                    ),
                ),
            )
        )
        manager = RouteSnapshotManager(
            source,
            listener=None,
            snapshot_observer=lambda snapshot: runtime.sync_routes(
                snapshot.routes_by_model.values()
            ),
        )

        await manager.refresh()
        await manager.wait_for_snapshot_observers()

        assert len(_TrackingClient.created) == 1
        timeout = _TrackingClient.created[0].kwargs["timeout"]
        assert isinstance(timeout, httpx.Timeout)
        assert timeout.connect == pytest.approx(3.0)
        limits = _TrackingClient.created[0].kwargs["limits"]
        assert isinstance(limits, httpx.Limits)
        assert limits.keepalive_expiry is None
        assert set(runtime._clients) == {"runtime-ready:8000"}
        await runtime.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("resolved_pool_limit", (16, 64, 1_000_000))
def test_PERF_01_keepalive_is_capped_by_the_current_finite_limit(
    monkeypatch: pytest.MonkeyPatch,
    resolved_pool_limit: int,
) -> None:
    async def scenario() -> None:
        _TrackingClient.created = []
        monkeypatch.setattr(httpx, "AsyncClient", _TrackingClient)
        runtime = VllmRuntimeAdapter(
            max_response_bytes=4096,
            max_sse_event_bytes=4096,
            pool_max_connections=resolved_pool_limit,
        )

        await runtime.sync_routes((_gateway_route("runtime-ready:8000"),))

        assert len(_TrackingClient.created) == 1
        limits = _TrackingClient.created[0].kwargs["limits"]
        assert isinstance(limits, httpx.Limits)
        assert limits.max_connections == resolved_pool_limit
        # PERF-01 measured worse latency after retaining every pooled connection;
        # the likely cause is saturation of the single Gateway event loop.  Keep
        # the evidence-backed current cap pinned until a different design is proven.
        assert limits.max_keepalive_connections == min(32, resolved_pool_limit)
        assert limits.max_keepalive_connections is not None
        await runtime.aclose()

    asyncio.run(scenario())


def test_M4_08_route_replacement_closes_old_idle_client_and_bounds_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        _TrackingClient.created = []
        monkeypatch.setattr(httpx, "AsyncClient", _TrackingClient)
        runtime = VllmRuntimeAdapter(max_response_bytes=4096, max_sse_event_bytes=4096)

        await runtime.sync_routes((_gateway_route("runtime-a:8000"),))
        first = _TrackingClient.created[0]
        await runtime.sync_routes((_gateway_route("runtime-b:8000"),))

        assert first.closed is True
        assert len(_TrackingClient.created) == 2
        assert set(runtime._clients) == {"runtime-b:8000"}
        await runtime.sync_routes(())
        assert _TrackingClient.created[1].closed is True
        assert runtime._clients == {}

    asyncio.run(scenario())


def test_M4_08_retired_endpoint_waits_for_active_request_before_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        _TrackingClient.created = []
        monkeypatch.setattr(httpx, "AsyncClient", _TrackingClient)
        runtime = VllmRuntimeAdapter(max_response_bytes=4096, max_sse_event_bytes=4096)
        await runtime.sync_routes((_gateway_route("runtime-a:8000"),))

        client = await runtime._acquire_client("runtime-a:8000")
        await runtime.sync_routes(())
        assert client is _TrackingClient.created[0]
        assert _TrackingClient.created[0].closed is False
        assert set(runtime._clients) == {"runtime-a:8000"}

        await runtime._release_client("runtime-a:8000")
        assert _TrackingClient.created[0].closed is True
        assert runtime._clients == {}

    asyncio.run(scenario())


async def _read_http_request(reader: asyncio.StreamReader) -> bool:
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, ConnectionResetError):
        return False
    content_length = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            content_length = int(line.split(b":", 1)[1].strip())
            break
    if content_length:
        await reader.readexactly(content_length)
    return True


def test_M4_08_idle_connection_is_observably_reused_after_one_second() -> None:
    async def scenario() -> None:
        accepted_connections = 0

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            nonlocal accepted_connections
            accepted_connections += 1
            try:
                while await _read_http_request(reader):
                    body = (
                        b"data: "
                        + json.dumps(
                            {
                                "model": "internal-model",
                                "choices": [{"finish_reason": "stop"}],
                            }
                        ).encode()
                        + b"\n\ndata: [DONE]\n\n"
                    )
                    writer.write(
                        b"HTTP/1.1 200 OK\r\n"
                        + f"Content-Length: {len(body)}\r\n".encode()
                        + b"Content-Type: text/event-stream\r\nConnection: keep-alive\r\n\r\n"
                        + body
                    )
                    await writer.drain()
            finally:
                writer.close()
                with suppress(Exception):
                    await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        endpoint = f"127.0.0.1:{port}"
        runtime = VllmRuntimeAdapter(
            max_response_bytes=4096,
            max_sse_event_bytes=4096,
            connect_timeout_seconds=3.0,
        )
        try:
            await runtime.sync_routes((_gateway_route(endpoint),))
            first = tuple(
                [
                    event
                    async for event in runtime.stream(
                        endpoint, REQUEST_ID, _request(stream=False, runtime_model=True)
                    )
                ]
            )
            await asyncio.sleep(1.05)
            second = tuple(
                [
                    event
                    async for event in runtime.stream(
                        endpoint, REQUEST_ID, _request(stream=False, runtime_model=True)
                    )
                ]
            )
            assert first and second
            assert accepted_connections == 1
        finally:
            await runtime.aclose()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


class _UnusedAuthenticator:
    async def authenticate(self, credential: str | None) -> Any:
        del credential
        raise AssertionError("authentication is not used by infer_routed")


class _UnusedRoutes:
    async def find(self, public_model_name: str) -> None:
        del public_model_name
        raise AssertionError("routing is not used by infer_routed")

    async def list_enabled(self) -> tuple[()]:
        return ()


def test_M4_08_connect_timeout_maps_backend_unavailable_and_releases_permit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class ConnectTimeoutClient(_TrackingClient):
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)
            super().__init__(**kwargs)

        async def send(self, request: httpx.Request, *, stream: bool) -> httpx.Response:
            del stream
            raise httpx.ConnectTimeout("private endpoint", request=request)

    async def scenario() -> None:
        monkeypatch.setattr(httpx, "AsyncClient", ConnectTimeoutClient)
        admission = AdmissionController(1, 1)
        runtime = VllmRuntimeAdapter(
            max_response_bytes=4096,
            max_sse_event_bytes=4096,
            connect_timeout_seconds=3.0,
        )
        core = InferenceCore(
            authenticator=_UnusedAuthenticator(),
            routes=_UnusedRoutes(),
            runtime=runtime,
            admission=admission,
            first_token_timeout_seconds=30.0,
            total_timeout_seconds=300.0,
        )
        record = RequestRecordContext(REQUEST_ID, NOW, time.monotonic())

        with pytest.raises(InferenceFailure) as raised:
            await core.infer_routed(
                route=_service_route("runtime-timeout:8000"),
                request_id=REQUEST_ID,
                request=_request(stream=False),
                lifecycle=record,
            )

        assert raised.value.error.code is ErrorCode.BACKEND_UNAVAILABLE
        assert record.error_code is ErrorCode.BACKEND_UNAVAILABLE
        assert await admission.active_counts() == (0, {})
        timeout = captured["timeout"]
        assert isinstance(timeout, httpx.Timeout)
        assert timeout.connect == pytest.approx(3.0)
        assert timeout.read is None and timeout.write is None
        await runtime.aclose()

    asyncio.run(scenario())


class _DelayedSseStream(httpx.AsyncByteStream):
    def __init__(self, *, delay_seconds: float, block_after_first: bool = False) -> None:
        self.delay_seconds = delay_seconds
        self.block_after_first = block_after_first
        self.closed = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            yield b'data: {"model":"internal-model","choices":[{"delta":{"content":"a"}}]}\n\n'
            if self.block_after_first:
                await asyncio.Event().wait()
            else:
                await asyncio.sleep(self.delay_seconds)
                yield b'data: {"model":"internal-model","choices":[{"finish_reason":"stop"}]}\n\n'
                yield b"data: [DONE]\n\n"
        finally:
            self.closed.set()

    async def aclose(self) -> None:
        self.closed.set()


def _stream_scope() -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/stream",
        "raw_path": b"/stream",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("gateway", 80),
    }


async def _never_disconnect() -> Message:
    await asyncio.Event().wait()
    return {"type": "http.disconnect"}


def _streaming_response(
    runtime: VllmRuntimeAdapter,
    protocol: OpenAIProtocolAdapter,
    request_id: UUID,
) -> StreamingResponse:
    return StreamingResponse(
        protocol._stream_body(
            runtime.stream("runtime:8000", request_id, _request(stream=True, runtime_model=True))
        ),
        media_type="text/event-stream",
    )


def test_M4_08_first_sse_event_reaches_client_before_delayed_remainder() -> None:
    async def scenario() -> None:
        stream = _DelayedSseStream(delay_seconds=2.0)

        async def upstream(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, request=request, stream=stream)

        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream), timeout=None)
        runtime = VllmRuntimeAdapter(client, max_response_bytes=4096, max_sse_event_bytes=4096)
        protocol = _protocol()
        first_body = asyncio.Event()
        chunks: list[bytes] = []

        async def send(message: Message) -> None:
            if message["type"] == "http.response.body" and message.get("body"):
                chunks.append(message["body"])
                first_body.set()

        response_task = asyncio.create_task(
            _streaming_response(runtime, protocol, REQUEST_ID)(
                _stream_scope(), _never_disconnect, send
            )
        )
        started = time.monotonic()
        try:
            await asyncio.wait_for(first_body.wait(), timeout=0.5)
            assert time.monotonic() - started < 0.5
            assert b'"content":"a"' in b"".join(chunks)
        finally:
            response_task.cancel()
            with suppress(asyncio.CancelledError):
                await response_task
            await client.aclose()

    asyncio.run(scenario())


def test_M4_08_slow_client_does_not_delay_peer_stream() -> None:
    async def scenario() -> None:
        slow_stream = _DelayedSseStream(delay_seconds=0, block_after_first=True)

        async def upstream(request: httpx.Request) -> httpx.Response:
            request_id = request.headers["x-request-id"]
            if request_id == str(REQUEST_ID):
                return httpx.Response(200, request=request, stream=slow_stream)
            return httpx.Response(
                200,
                request=request,
                content=(
                    b'data: {"model":"internal-model","choices":[{"delta":{"content":"b"}}]}\n\n'
                    b'data: {"model":"internal-model","choices":[{"finish_reason":"stop"}]}\n\n'
                    b"data: [DONE]\n\n"
                ),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream), timeout=None)
        runtime = VllmRuntimeAdapter(client, max_response_bytes=4096, max_sse_event_bytes=4096)
        protocol = _protocol()
        slow_send_started = asyncio.Event()
        release_slow_send = asyncio.Event()

        async def slow_send(message: Message) -> None:
            if message["type"] == "http.response.body" and message.get("body"):
                slow_send_started.set()
                await release_slow_send.wait()

        slow_task = asyncio.create_task(
            _streaming_response(runtime, protocol, REQUEST_ID)(
                _stream_scope(), _never_disconnect, slow_send
            )
        )
        await asyncio.wait_for(slow_send_started.wait(), timeout=0.5)

        peer_chunks: list[bytes] = []

        async def peer_send(message: Message) -> None:
            if message["type"] == "http.response.body" and message.get("body"):
                peer_chunks.append(message["body"])

        peer_id = UUID("78000000-0000-4000-8000-000000000004")
        started = time.monotonic()
        try:
            await asyncio.wait_for(
                _streaming_response(runtime, protocol, peer_id)(
                    _stream_scope(), _never_disconnect, peer_send
                ),
                timeout=0.5,
            )
            assert time.monotonic() - started < 0.5
            assert b'"content":"b"' in b"".join(peer_chunks)
            assert slow_task.done() is False
        finally:
            release_slow_send.set()
            slow_task.cancel()
            with suppress(asyncio.CancelledError):
                await slow_task
            await client.aclose()

    asyncio.run(scenario())


def test_M4_08_oversized_nonstream_response_keeps_stable_sanitized_error() -> None:
    secret_body = "BODY_SENTINEL"

    async def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, content=secret_body * 100)

    async def scenario() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream), timeout=None)
        runtime = VllmRuntimeAdapter(client, max_response_bytes=32, max_sse_event_bytes=4096)
        protocol = _protocol()
        try:
            with pytest.raises(InferenceFailure) as raised:
                tuple(
                    [
                        event
                        async for event in runtime.stream(
                            "private-runtime:8000",
                            REQUEST_ID,
                            _request(stream=False, runtime_model=True),
                        )
                    ]
                )
            mapped = protocol.map_error(raised.value.error, REQUEST_ID)
            payload = json.loads(mapped.body)
            assert mapped.status_code == 503
            assert payload["error"]["code"] == "backend_unavailable"
            assert secret_body.encode() not in mapped.body
            assert b"private-runtime" not in mapped.body
        finally:
            await client.aclose()

    asyncio.run(scenario())

"""PROTO-06 Gateway main path and critical failure-path tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any, NoReturn, cast
from uuid import UUID

import httpx
import pytest
from holm_domain import ModelInstanceStatus, RequestRecordStatus
from holm_gateway import application as gateway_application
from holm_gateway.application import AdmissionController, create_app
from holm_gateway.authentication import ApiKeyAuthenticator
from holm_gateway.configuration import GatewayConfiguration
from holm_gateway.request_context import LedgerEventSink
from holm_gateway.routing import CachedRouteDirectory, GatewayRoute
from holm_gateway.service_metrics import GatewayServiceMetrics, ServiceMetricWriter
from holm_ledger import AbortedEvent, CompletedEvent, EventType, LedgerEvent
from holm_persistence import AsyncSessionFactory
from holm_runtimes import load_runtime_compatibility
from holm_security import (
    ApiKeyPepperKeyring,
    ApiKeyValidationSnapshot,
    issue_api_key,
)
from starlette.types import ASGIApp, Message, Scope

SERVICE_A = UUID("00000000-0000-7000-8000-000000000601")
SERVICE_B = UUID("00000000-0000-7000-8000-000000000602")
USER_ID = UUID("00000000-0000-7000-8000-000000000603")
API_KEY_ID = UUID("00000000-0000-7000-8000-000000000604")
NOW = datetime(2026, 9, 15, tzinfo=UTC)
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
VLLM_RUNTIME = load_runtime_compatibility(REPOSITORY_ROOT / "runtime-compat.yaml").runtime("vllm")
assert VLLM_RUNTIME is not None


class RecordingSink:
    def __init__(self) -> None:
        self.records: list[CompletedEvent | AbortedEvent] = []

    async def submit(self, event: LedgerEvent) -> None:
        if isinstance(event, (CompletedEvent, AbortedEvent)):
            self.records.append(event)


class FakeRouteSource:
    def __init__(self, routes: tuple[GatewayRoute, ...]) -> None:
        self.routes = routes
        self.calls = 0

    async def list_routes(self) -> tuple[GatewayRoute, ...]:
        self.calls += 1
        return self.routes


def _route(
    *,
    service_id: UUID = SERVICE_A,
    public_name: str = "qwen-mini",
    enabled: bool = True,
    status: str = ModelInstanceStatus.RUNNING.value,
    endpoint: str | None = "fake-upstream:8000",
) -> GatewayRoute:
    return GatewayRoute(
        service_id=service_id,
        instance_id=UUID("00000000-0000-7000-8000-000000000605"),
        public_model_name=public_name,
        enabled=enabled,
        instance_status=status,
        runtime_name="vllm",
        internal_endpoint=endpoint,
        internal_model_name="mi_0123456789ab",
    )


def _authenticator(
    *,
    scopes: frozenset[UUID] = frozenset(),
    expired: bool = False,
) -> tuple[ApiKeyAuthenticator, str]:
    keyring = ApiKeyPepperKeyring(current_key_id="test", keys={"test": b"p" * 32})
    issued = issue_api_key(keyring)

    async def lookup(lookup_id: str) -> ApiKeyValidationSnapshot | None:
        if lookup_id != issued.lookup_id:
            return None
        return ApiKeyValidationSnapshot(
            user_id=USER_ID,
            api_key_id=API_KEY_ID,
            pepper_key_id=issued.pepper_key_id,
            secret_hmac=issued.secret_hmac,
            revoked=False,
            expired_status=expired,
            expires_at=NOW - timedelta(seconds=1) if expired else None,
            user_active=True,
            scoped_service_ids=scopes,
            database_now=NOW,
            user_is_admin=True,
            authorized_service_ids=frozenset({SERVICE_A, SERVICE_B}),
        )

    return ApiKeyAuthenticator(lookup=lookup, keyring=keyring), issued.plaintext


def _application(
    routes: tuple[GatewayRoute, ...],
    handler: Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]],
    *,
    scopes: frozenset[UUID] = frozenset(),
    expired: bool = False,
    configuration: GatewayConfiguration | None = None,
    admission: AdmissionController | None = None,
    request_ledger: LedgerEventSink | None = None,
    service_metrics: GatewayServiceMetrics | None = None,
) -> tuple[ASGIApp, str, httpx.AsyncClient]:
    if configuration is None:
        configuration = GatewayConfiguration()
    authenticator, plaintext = _authenticator(scopes=scopes, expired=expired)
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None)
    app = create_app(
        authenticator=authenticator,
        routes=CachedRouteDirectory(FakeRouteSource(routes), ttl_seconds=2.0),
        runtime_capabilities={"vllm": VLLM_RUNTIME.request_capabilities},
        configuration=configuration,
        upstream_client=upstream,
        admission=admission,
        request_ledger=request_ledger,
        service_metrics=service_metrics,
    )
    return app, plaintext, upstream


async def _unused_upstream(request: httpx.Request) -> httpx.Response:
    raise AssertionError("request unexpectedly reached upstream")


async def _request(
    app: ASGIApp,
    method: str,
    path: str,
    *,
    key: str | None,
    json_body: dict[str, object] | None = None,
) -> httpx.Response:
    headers = {} if key is None else {"Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        return await client.request(method, path, headers=headers, json=json_body)


def _error_code(response: httpx.Response) -> str:
    body = response.json()
    assert response.headers["x-request-id"] == body["error"]["request_id"]
    assert body["error"]["details"] == {}
    return str(body["error"]["code"])


def test_M6_06_service_sample_database_failure_does_not_change_inference_result() -> None:
    metrics = GatewayServiceMetrics()
    metrics.finished(
        SERVICE_A,
        forwarded=False,
        status=RequestRecordStatus.COMPLETED,
        http_status=200,
        error_code=None,
        duration_seconds=0.1,
        first_token_seconds=0.05,
        output_tokens=1,
    )

    def unavailable_factory() -> NoReturn:
        raise RuntimeError("database unavailable")

    writer = ServiceMetricWriter(metrics, cast(AsyncSessionFactory, unavailable_factory))

    async def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"data: "
            + json.dumps(
                {
                    "id": "chatcmpl-m606",
                    "object": "chat.completion.chunk",
                    "model": "mi_0123456789ab",
                    "choices": [{"delta": {"role": "assistant", "content": "ok"}}],
                }
            ).encode()
            + b"\n\ndata: [DONE]\n\n",
        )

    async def scenario() -> None:
        app, key, client = _application((_route(),), upstream, service_metrics=metrics)
        await writer.start()
        try:
            await asyncio.sleep(0.01)
            assert writer.write_failures_total >= 1
            response = await _request(
                app,
                "POST",
                "/v1/chat/completions",
                key=key,
                json_body={"model": "qwen-mini", "messages": []},
            )
            assert response.status_code == 200
            assert metrics.snapshot(sampled_at=NOW)[0].accepted_requests == 2
        finally:
            await writer.stop()
            await client.aclose()

    asyncio.run(scenario())


def test_PROTO_06_non_stream_forwards_internal_and_returns_public_model_without_body_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    captured: dict[str, object] = {}

    async def upstream(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            content=b"data: "
            + json.dumps(
                {
                    "id": "chatcmpl-1",
                    "object": "chat.completion.chunk",
                    "model": "mi_0123456789ab",
                    "choices": [{"delta": {"role": "assistant", "content": "SECRET_OUTPUT"}}],
                }
            ).encode()
            + b"\n\ndata: [DONE]\n\n",
        )

    async def scenario() -> None:
        sink = RecordingSink()
        app, key, client = _application((_route(),), upstream, request_ledger=sink)
        try:
            with caplog.at_level("INFO", logger="holm.gateway"):
                response = await _request(
                    app,
                    "POST",
                    "/v1/chat/completions",
                    key=key,
                    json_body={
                        "model": "qwen-mini",
                        "messages": [{"role": "user", "content": "SECRET_PROMPT"}],
                    },
                )
            assert response.status_code == 200
            assert response.json()["model"] == "qwen-mini"
            assert response.json()["choices"][0]["message"]["content"] == "SECRET_OUTPUT"
            assert captured["model"] == "mi_0123456789ab"
            rendered_logs = " ".join(
                f"{record.getMessage()} {record.__dict__}" for record in caplog.records
            )
            assert "SECRET_PROMPT" not in rendered_logs
            assert "SECRET_OUTPUT" not in rendered_logs
            assert key not in rendered_logs
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_PROTO_06_stream_rewrites_each_event_and_forwards_done() -> None:
    async def upstream(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["model"] == "mi_0123456789ab"
        body = (
            b'data: {"id":"1","model":"mi_0123456789ab","choices":[{"delta":{"content":"a"}}]}\n\n'
            b'data: {"id":"1","model":"mi_0123456789ab","choices":[{"delta":{"content":"b"}}]}\n\n'
            b"data: [DONE]\n\n"
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    async def scenario() -> None:
        sink = RecordingSink()
        app, key, client = _application((_route(),), upstream, request_ledger=sink)
        try:
            response = await _request(
                app,
                "POST",
                "/v1/chat/completions",
                key=key,
                json_body={"model": "qwen-mini", "messages": [], "stream": True},
            )
            assert response.status_code == 200
            assert response.text.count('"model":"qwen-mini"') == 2
            assert "mi_0123456789ab" not in response.text
            assert response.text.endswith("data: [DONE]\n\n")
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("client_requests_usage", (False, True))
def test_PROTO_12_PROTO_18_stream_usage_is_captured_without_changing_client_contract(
    client_requests_usage: bool,
) -> None:
    captured: dict[str, object] = {}

    async def upstream(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        body = (
            b'data: {"id":"1","model":"mi_0123456789ab","choices":'
            b'[{"delta":{"content":"a"},"finish_reason":"stop"}]}\n\n'
            b'data: {"id":"1","model":"mi_0123456789ab","choices":[],"usage":'
            b'{"prompt_tokens":7,"completion_tokens":5,"total_tokens":12}}\n\n'
            b"data: [DONE]\n\n"
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    async def scenario() -> None:
        sink = RecordingSink()
        app, key, client = _application((_route(),), upstream, request_ledger=sink)
        body: dict[str, object] = {
            "model": "qwen-mini",
            "messages": [],
            "stream": True,
        }
        if client_requests_usage:
            body["stream_options"] = {"include_usage": True}
        try:
            response = await _request(
                app,
                "POST",
                "/v1/chat/completions",
                key=key,
                json_body=body,
            )
            assert response.status_code == 200
            assert captured["stream_options"] == {"include_usage": True}
            events = [
                json.loads(line.removeprefix("data: "))
                for line in response.text.splitlines()
                if line.startswith("data: {")
            ]
            if client_requests_usage:
                assert any("usage" in event for event in events)
            else:
                assert all("usage" not in event for event in events)
            assert len(sink.records) == 1
            record = sink.records[0]
            assert record.event_type is EventType.COMPLETED
            assert record.input_tokens == 7
            assert record.output_tokens == 5
            assert record.finish_reason is not None
            assert record.finish_reason.value == "stop"
            assert record.ttft_ms is not None
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_CLEAN_02_PROTO_12_requests_retain_both_ledger_records_without_direct_queue() -> None:
    async def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"data: "
            + json.dumps(
                {
                    "model": "mi_0123456789ab",
                    "choices": [{"finish_reason": "future_reason"}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 3},
                }
            ).encode()
            + b"\n\ndata: [DONE]\n\n",
        )

    async def scenario() -> None:
        writer = RecordingSink()
        app, key, client = _application((_route(),), upstream, request_ledger=writer)
        try:
            first = await _request(
                app,
                "POST",
                "/v1/chat/completions",
                key=key,
                json_body={"model": "qwen-mini", "messages": []},
            )
            second = await _request(
                app,
                "POST",
                "/v1/chat/completions",
                key=key,
                json_body={"model": "qwen-mini", "messages": []},
            )
            assert first.status_code == second.status_code == 200
            assert len(writer.records) == 2
            assert all(record.event_type is EventType.COMPLETED for record in writer.records)
            assert all(record.input_tokens == 2 for record in writer.records)
            assert all(record.output_tokens == 3 for record in writer.records)
            assert not hasattr(app, "_request_records")
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("key_mode", "expected_code"),
    (("missing", "invalid_api_key"), ("wrong", "invalid_api_key"), ("expired", "api_key_expired")),
)
def test_PROTO_06_authentication_failures_are_stable(key_mode: str, expected_code: str) -> None:
    async def scenario() -> None:
        app, key, client = _application((), _unused_upstream, expired=key_mode == "expired")
        try:
            presented = (
                None
                if key_mode == "missing"
                else ("holm_sk_invalid" if key_mode == "wrong" else key)
            )
            response = await _request(app, "GET", "/v1/models", key=presented)
            assert response.status_code == 401
            assert _error_code(response) == expected_code
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_BRAND_03_new_prefix_calls_and_old_prefix_is_invalid() -> None:
    # FX-BRAND-03-KEY: the existing issuance fixture, with only its prefix replaced.
    async def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"data: "
            + json.dumps(
                {
                    "id": "chatcmpl-brand03",
                    "object": "chat.completion.chunk",
                    "model": "mi_0123456789ab",
                    "choices": [{"delta": {"role": "assistant", "content": "ok"}}],
                }
            ).encode()
            + b"\n\ndata: [DONE]\n\n",
        )

    async def scenario() -> None:
        app, key, client = _application((_route(),), upstream)
        try:
            assert key.startswith("holm_sk_")
            for presented, expected in ((key, 200), ("pwsk_" + key.removeprefix("holm_sk_"), 401)):
                response = await _request(
                    app,
                    "POST",
                    "/v1/chat/completions",
                    key=presented,
                    json_body={"model": "qwen-mini", "messages": []},
                )
                assert response.status_code == expected
                if expected == 401:
                    assert _error_code(response) == "invalid_api_key"
            models = await _request(app, "GET", "/v1/models", key=key)
            assert models.status_code == 200
            assert models.json()["data"][0]["holm_status"] == "available"
            assert "private" + "ws_status" not in models.json()["data"][0]
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_PROTO_06_models_list_respects_scope_and_hides_disabled_services() -> None:
    async def scenario() -> None:
        routes = (
            _route(),
            _route(service_id=SERVICE_B, public_name="other", enabled=False),
        )
        app, key, client = _application(routes, _unused_upstream, scopes=frozenset({SERVICE_A}))
        try:
            response = await _request(app, "GET", "/v1/models", key=key)
            assert response.status_code == 200
            assert [item["id"] for item in response.json()["data"]] == ["qwen-mini"]
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("body", (b"not-json", b'{"model":"qwen-mini"}'))
def test_PROTO_06_invalid_or_oversized_request_body_returns_400(body: bytes) -> None:
    async def scenario() -> None:
        configuration = GatewayConfiguration(max_request_body_bytes=8)
        app, key, client = _application((_route(),), _unused_upstream, configuration=configuration)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as gateway:
                response = await gateway.post(
                    "/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    content=body,
                )
            assert response.status_code == 400
            assert _error_code(response) == "invalid_request"
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("routes", "scopes", "model", "status", "code"),
    (
        ((_route(),), frozenset({SERVICE_B}), "qwen-mini", 403, "service_not_authorized"),
        ((_route(),), frozenset(), "unknown", 404, "model_not_found"),
        ((_route(enabled=False),), frozenset(), "qwen-mini", 503, "service_disabled"),
        (
            (_route(status=ModelInstanceStatus.STARTING.value),),
            frozenset(),
            "qwen-mini",
            503,
            "backend_unavailable",
        ),
    ),
)
def test_PROTO_06_route_failures_are_stable(
    routes: tuple[GatewayRoute, ...],
    scopes: frozenset[UUID],
    model: str,
    status: int,
    code: str,
) -> None:
    async def scenario() -> None:
        app, key, client = _application(routes, _unused_upstream, scopes=scopes)
        try:
            response = await _request(
                app,
                "POST",
                "/v1/chat/completions",
                key=key,
                json_body={"model": model, "messages": []},
            )
            assert response.status_code == status
            assert _error_code(response) == code
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_PROTO_06_unreachable_upstream_is_sanitized_backend_unavailable() -> None:
    async def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("SECRET_INTERNAL_ADDRESS", request=request)

    async def scenario() -> None:
        app, key, client = _application((_route(),), unreachable)
        try:
            response = await _request(
                app,
                "POST",
                "/v1/chat/completions",
                key=key,
                json_body={"model": "qwen-mini", "messages": []},
            )
            assert response.status_code == 503
            assert _error_code(response) == "backend_unavailable"
            assert "SECRET_INTERNAL_ADDRESS" not in response.text
            assert "fake-upstream" not in response.text
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_PROTO_06_gateway_overloaded_rejects_without_reaching_upstream() -> None:
    async def scenario() -> None:
        admission = AdmissionController(1)
        occupied = await admission.try_acquire()
        assert occupied is not None
        app, key, client = _application((_route(),), _unused_upstream, admission=admission)
        try:
            response = await _request(
                app,
                "POST",
                "/v1/chat/completions",
                key=key,
                json_body={"model": "qwen-mini", "messages": []},
            )
            assert response.status_code == 503
            assert _error_code(response) == "gateway_overloaded"
        finally:
            await occupied.release()
            await client.aclose()

    asyncio.run(scenario())


class BlockingStream(httpx.AsyncByteStream):
    def __init__(self, first: bytes | None = None) -> None:
        self.first = first
        self.closed = asyncio.Event()
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            if self.first is not None:
                yield self.first
            self.waiting.set()
            await self.release.wait()
        finally:
            self.closed.set()

    async def aclose(self) -> None:
        self.release.set()
        self.closed.set()


class _FIX29Clock:
    """FX-FIX-29: advance real asyncio deadlines only at the body-read barrier."""

    def __init__(self) -> None:
        self.now = asyncio.get_running_loop().time()

    def __call__(self) -> float:
        return self.now


class _FIX29TimeoutStream(BlockingStream):
    def __init__(self, clock: _FIX29Clock, *, streaming: bool) -> None:
        super().__init__(
            None if streaming else b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
        )
        self.clock = clock

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self.first is not None:
            yield self.first
        self.waiting.set()
        # No deadline can expire during acquisition/opening. For non-streaming,
        # the initial SSE event has passed the first-event wait at this point.
        self.clock.now += 2
        await self.release.wait()

    async def aclose(self) -> None:
        # Force cleanup to suspend: closed must be true before the 504 returns,
        # not merely after an iterator's cancellation/finalization.
        await asyncio.sleep(0)
        await super().aclose()


async def _FIX29_timeout_scenario(*, streaming: bool) -> None:
    clock = _FIX29Clock()
    stream = _FIX29TimeoutStream(clock, streaming=streaming)
    opened = asyncio.Event()

    async def upstream(request: httpx.Request) -> httpx.Response:
        opened.set()
        return httpx.Response(200, stream=stream)

    configuration = GatewayConfiguration(
        first_token_timeout_seconds=0.01,
        total_timeout_seconds=1 if streaming else 0.02,
    )
    # Keep production wait_for/timeout and cancellation intact; only time is
    # controlled. The core's elapsed-time clock must match asyncio's deadlines.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(asyncio.get_running_loop(), "time", clock)
        patch.setattr(
            gateway_application,
            "InferenceCore",
            partial(gateway_application.InferenceCore, clock=clock),
        )
        app, key, client = _application((_route(),), upstream, configuration=configuration)
        try:
            response = await _request(
                app,
                "POST",
                "/v1/chat/completions",
                key=key,
                json_body={"model": "qwen-mini", "messages": [], "stream": streaming},
            )
            assert response.status_code == 504
            assert _error_code(response) == "inference_timeout"
            assert opened.is_set()
            assert stream.waiting.is_set()
            assert stream.closed.is_set(), "FIX-29: upstream not closed before 504"
        finally:
            await client.aclose()


def test_PROTO_06_first_token_timeout_returns_504_and_closes_upstream() -> None:
    asyncio.run(_FIX29_timeout_scenario(streaming=True))


def test_PROTO_06_non_stream_total_timeout_returns_504_and_closes_upstream() -> None:
    asyncio.run(_FIX29_timeout_scenario(streaming=False))


@pytest.mark.parametrize("streaming", (False, True))
def test_FIX_29_timeout_assertion_rejects_unclosed_upstream(
    monkeypatch: pytest.MonkeyPatch, streaming: bool
) -> None:
    async def no_close(self: _FIX29TimeoutStream) -> None:
        pass

    monkeypatch.setattr(_FIX29TimeoutStream, "aclose", no_close)
    with pytest.raises(AssertionError, match="FIX-29: upstream not closed before 504"):
        asyncio.run(_FIX29_timeout_scenario(streaming=streaming))


def _scope(body: bytes, key: str) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"authorization", f"Bearer {key}".encode("ascii")),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("gateway", 80),
    }


def test_PROTO_06_client_disconnect_cancels_and_closes_upstream() -> None:
    async def scenario() -> None:
        stream = BlockingStream(
            b'data: {"model":"mi_0123456789ab","choices":[{"delta":{"content":"a"}}]}\n\n'
        )

        async def upstream(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=stream)

        sink = RecordingSink()
        app, key, client = _application((_route(),), upstream, request_ledger=sink)
        body = json.dumps({"model": "qwen-mini", "messages": [], "stream": True}).encode()
        disconnect = asyncio.Event()
        received_request = False
        response_chunks: list[bytes] = []

        async def receive() -> Message:
            nonlocal received_request
            if not received_request:
                received_request = True
                return {"type": "http.request", "body": body, "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            if message["type"] == "http.response.body" and message.get("body"):
                response_chunks.append(message["body"])
                disconnect.set()

        try:
            await asyncio.wait_for(app(_scope(body, key), receive, send), timeout=1)
            await asyncio.wait_for(stream.closed.wait(), timeout=1)
            assert b'"model":"qwen-mini"' in b"".join(response_chunks)
            assert len(sink.records) == 1
            assert sink.records[0].event_type is EventType.ABORTED
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_PROTO_06_route_directory_uses_one_ttl_snapshot() -> None:
    async def scenario() -> None:
        source = FakeRouteSource((_route(),))
        directory = CachedRouteDirectory(source, ttl_seconds=2)
        assert await directory.find("qwen-mini") is not None
        assert await directory.list_enabled()
        assert await directory.find("qwen-mini") is not None
        assert source.calls == 1

    asyncio.run(scenario())


def test_M4_05_invalid_key_still_precedes_invalid_openai_body() -> None:
    """The structural split must retain PROTO-06 authentication ordering."""

    async def scenario() -> None:
        app, _, client = _application((_route(),), _unused_upstream)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as gateway:
                response = await gateway.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer holm_sk_invalid"},
                    content=b"not-json",
                )
            assert response.status_code == 401
            assert _error_code(response) == "invalid_api_key"
        finally:
            await client.aclose()

    asyncio.run(scenario())

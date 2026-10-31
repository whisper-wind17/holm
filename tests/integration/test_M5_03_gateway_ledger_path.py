"""M5-03 Gateway-to-real-SQLite Request Ledger integration coverage."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from holm_domain import ErrorCode, ModelInstanceStatus
from holm_gateway.application import create_app  # type: ignore[import-untyped]
from holm_gateway.authentication import (  # type: ignore[import-untyped]
    ApiKeyAuthenticator,
)
from holm_gateway.configuration import (  # type: ignore[import-untyped]
    GatewayConfiguration,
)
from holm_gateway.routing import (  # type: ignore[import-untyped]
    CachedRouteDirectory,
    GatewayRoute,
)
from holm_ledger import (
    EventType,
    FailedEvent,
    InProgressEvent,
    LedgerEvent,
    RejectedEvent,
    SpoolWriter,
    open_ledger,
)
from holm_runtimes import load_runtime_compatibility
from holm_security import (
    ApiKeyPepperKeyring,
    ApiKeyValidationSnapshot,
    issue_api_key,
)
from starlette.types import ASGIApp

SERVICE_ID = UUID("53100000-0000-4000-8000-000000000001")
OTHER_SERVICE_ID = UUID("53100000-0000-4000-8000-000000000002")
INSTANCE_ID = UUID("53100000-0000-4000-8000-000000000003")
USER_ID = UUID("53100000-0000-4000-8000-000000000004")
KEY_ID = UUID("53100000-0000-4000-8000-000000000005")
NOW = datetime(2026, 9, 17, 15, 30, tzinfo=UTC)
SENTINEL = "M5_03_GATEWAY_BODY_SENTINEL_5b28c0"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
VLLM_RUNTIME = load_runtime_compatibility(REPOSITORY_ROOT / "runtime-compat.yaml").runtime("vllm")
assert VLLM_RUNTIME is not None


class _RouteSource:
    def __init__(self, routes: tuple[GatewayRoute, ...]) -> None:
        self.routes = routes

    async def list_routes(self) -> tuple[GatewayRoute, ...]:
        return self.routes


class _BlockingLedger:
    def __init__(self) -> None:
        self.events: list[LedgerEvent] = []
        self.rejected_submitted = asyncio.Event()
        self.release = asyncio.Event()

    async def submit(self, event: LedgerEvent) -> object:
        if event.event_type is EventType.REJECTED:
            self.rejected_submitted.set()
            await self.release.wait()
        self.events.append(event)
        return object()


def _route(
    *,
    enabled: bool = True,
    status: ModelInstanceStatus = ModelInstanceStatus.RUNNING,
) -> GatewayRoute:
    ready = status is ModelInstanceStatus.RUNNING
    return GatewayRoute(
        service_id=SERVICE_ID,
        instance_id=INSTANCE_ID,
        public_model_name="m5-03-model",
        enabled=enabled,
        instance_status=status.value,
        runtime_name="vllm",
        internal_endpoint="runtime:8000" if ready else None,
        internal_model_name="internal-model",
    )


def _authenticator(
    *, scoped_service_ids: frozenset[UUID] = frozenset()
) -> tuple[ApiKeyAuthenticator, str]:
    keyring = ApiKeyPepperKeyring(current_key_id="m5-03", keys={"m5-03": b"p" * 32})
    issued = issue_api_key(keyring)

    async def lookup(lookup_id: str) -> ApiKeyValidationSnapshot | None:
        if lookup_id != issued.lookup_id:
            return None
        return ApiKeyValidationSnapshot(
            user_id=USER_ID,
            api_key_id=KEY_ID,
            pepper_key_id=issued.pepper_key_id,
            secret_hmac=issued.secret_hmac,
            revoked=False,
            expired_status=False,
            expires_at=None,
            user_active=True,
            scoped_service_ids=scoped_service_ids,
            database_now=NOW,
            user_is_admin=True,
            authorized_service_ids=frozenset({SERVICE_ID}),
        )

    return ApiKeyAuthenticator(lookup=lookup, keyring=keyring), issued.plaintext


def _app(
    handler: Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]],
    *,
    ledger: Any,
    routes: tuple[GatewayRoute, ...] = (_route(),),
    scoped_service_ids: frozenset[UUID] = frozenset(),
    configuration: GatewayConfiguration | None = None,
) -> tuple[ASGIApp, str, httpx.AsyncClient]:
    assert VLLM_RUNTIME is not None
    authenticator, key = _authenticator(scoped_service_ids=scoped_service_ids)
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None)
    app = create_app(
        authenticator=authenticator,
        routes=CachedRouteDirectory(_RouteSource(routes)),
        runtime_capabilities={"vllm": VLLM_RUNTIME.request_capabilities},
        configuration=configuration,
        upstream_client=upstream,
        request_ledger=ledger,
    )
    return app, key, upstream


async def _post(
    app: ASGIApp,
    key: str,
    *,
    model: str = "m5-03-model",
    stream: bool = False,
) -> httpx.Response:
    payload: dict[str, object] = {
        "model": model,
        "messages": [{"role": "user", "content": SENTINEL}],
        "stream": stream,
    }
    if not stream:
        payload["max_tokens"] = 0
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        return await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json=payload,
        )


def _ledger_path(tmp_path: Path) -> Path:
    directory = tmp_path / "spool"
    directory.mkdir(mode=0o700)
    return directory / "request-ledger.sqlite3"


def _events(path: Path) -> tuple[LedgerEvent, ...]:
    with open_ledger(path) as ledger:
        return tuple(item.event for item in ledger.list_unacked())


@pytest.mark.parametrize("stream", [False, True])
def test_M5_03_normal_gateway_calls_persist_complete_real_sequences(
    tmp_path: Path, stream: bool
) -> None:
    async def upstream(request: httpx.Request) -> httpx.Response:
        if stream:
            return httpx.Response(
                200,
                content=(
                    b'data: {"model":"internal-model","choices":[{"delta":{"content":"ok"}}]}\n\n'
                    b'data: {"model":"internal-model","choices":'
                    b'[{"delta":{},"finish_reason":"stop"}]}\n\n'
                    b'data: {"model":"internal-model","choices":[],"usage":'
                    b'{"prompt_tokens":3,"completion_tokens":2}}\n\n'
                    b"data: [DONE]\n\n"
                ),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(
            200,
            content=b"data: "
            + json.dumps(
                {
                    "id": "chatcmpl-m503",
                    "object": "chat.completion.chunk",
                    "model": "internal-model",
                    "choices": [
                        {
                            "delta": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                }
            ).encode()
            + b"\n\ndata: [DONE]\n\n",
        )

    async def scenario() -> None:
        path = _ledger_path(tmp_path)
        writer = SpoolWriter(path)
        writer.start()
        app, key, client = _app(upstream, ledger=writer)
        try:
            response = await _post(app, key, stream=stream)
            assert response.status_code == 200
            events = _events(path)
            assert [event.event_type for event in events] == [
                EventType.RECEIVED,
                EventType.IN_PROGRESS,
                EventType.COMPLETED,
            ]
            assert [event.event_seq for event in events] == [1, 2, 3]
            in_progress = events[1]
            assert isinstance(in_progress, InProgressEvent)
            assert (
                in_progress.user_id,
                in_progress.api_key_id,
                in_progress.service_id,
                in_progress.instance_id,
                in_progress.requested_max_tokens,
            ) == (
                USER_ID,
                KEY_ID,
                SERVICE_ID,
                INSTANCE_ID,
                None if stream else 0,
            )
            for target in (path, Path(f"{path}-wal")):
                if target.exists():
                    assert SENTINEL.encode() not in target.read_bytes()
        finally:
            await client.aclose()
            await writer.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("case", "expected_code", "expected_status"),
    [
        ("invalid_key", ErrorCode.INVALID_API_KEY, 401),
        ("unauthorized", ErrorCode.SERVICE_NOT_AUTHORIZED, 403),
        ("missing_model", ErrorCode.MODEL_NOT_FOUND, 404),
        ("disabled", ErrorCode.SERVICE_DISABLED, 503),
        ("not_ready", ErrorCode.BACKEND_UNAVAILABLE, 503),
    ],
)
def test_M5_03_common_rejections_are_durable_before_response(
    tmp_path: Path,
    case: str,
    expected_code: ErrorCode,
    expected_status: int,
) -> None:
    async def unused(request: httpx.Request) -> httpx.Response:
        del request
        raise AssertionError("rejected request reached Runtime")

    async def scenario() -> None:
        path = _ledger_path(tmp_path)
        writer = SpoolWriter(path)
        writer.start()
        routes = (_route(),)
        scopes: frozenset[UUID] = frozenset()
        model = "m5-03-model"
        if case == "unauthorized":
            scopes = frozenset({OTHER_SERVICE_ID})
        elif case == "missing_model":
            model = "absent-model"
        elif case == "disabled":
            routes = (_route(enabled=False),)
        elif case == "not_ready":
            routes = (_route(status=ModelInstanceStatus.STARTING),)
        app, key, client = _app(
            unused,
            ledger=writer,
            routes=routes,
            scoped_service_ids=scopes,
        )
        if case == "invalid_key":
            key = "pw_invalid"
        try:
            response = await _post(app, key, model=model)
            assert response.status_code == expected_status
            assert response.json()["error"]["code"] == expected_code.value
            events = _events(path)
            assert [event.event_type for event in events] == [
                EventType.RECEIVED,
                EventType.REJECTED,
            ]
            rejected = events[-1]
            assert isinstance(rejected, RejectedEvent)
            assert (rejected.error_code, rejected.http_status) == (
                expected_code,
                expected_status,
            )
            if case == "unauthorized":
                assert (rejected.user_id, rejected.api_key_id) == (USER_ID, KEY_ID)
        finally:
            await client.aclose()
            await writer.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["connection", "first_token_timeout"])
def test_M5_03_runtime_failures_persist_failed(tmp_path: Path, failure: str) -> None:
    class NeverResponds(httpx.AsyncByteStream):
        async def __aiter__(self) -> Any:
            await asyncio.Event().wait()
            yield b"unreachable"

    async def upstream(request: httpx.Request) -> httpx.Response:
        if failure == "connection":
            raise httpx.ConnectError("internal endpoint omitted", request=request)
        return httpx.Response(200, stream=NeverResponds())

    async def scenario() -> None:
        path = _ledger_path(tmp_path)
        writer = SpoolWriter(path)
        writer.start()
        app, key, client = _app(
            upstream,
            ledger=writer,
            configuration=GatewayConfiguration(
                first_token_timeout_seconds=0.01,
                total_timeout_seconds=1,
            ),
        )
        try:
            response = await _post(app, key)
            expected = (
                ErrorCode.BACKEND_UNAVAILABLE
                if failure == "connection"
                else ErrorCode.INFERENCE_TIMEOUT
            )
            assert response.json()["error"]["code"] == expected.value
            events = _events(path)
            assert [event.event_type for event in events] == [
                EventType.RECEIVED,
                EventType.IN_PROGRESS,
                EventType.FAILED,
            ]
            failed = events[-1]
            assert isinstance(failed, FailedEvent)
            assert failed.error_code is expected
        finally:
            await client.aclose()
            await writer.close()

    asyncio.run(scenario())


def test_M5_03_invalid_json_stays_400_without_ledger_events(tmp_path: Path) -> None:
    async def unused(request: httpx.Request) -> httpx.Response:
        del request
        raise AssertionError("invalid JSON reached Runtime")

    async def scenario() -> None:
        path = _ledger_path(tmp_path)
        writer = SpoolWriter(path)
        writer.start()
        app, key, client = _app(unused, ledger=writer)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as caller:
                response = await caller.post(
                    "/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                    content=b'{"model":',
                )
            assert response.status_code == 400
            assert _events(path) == ()
        finally:
            await client.aclose()
            await writer.close()

    asyncio.run(scenario())


def test_M5_03_rejected_response_waits_for_durable_ack() -> None:
    async def unused(request: httpx.Request) -> httpx.Response:
        del request
        raise AssertionError("invalid key reached Runtime")

    async def scenario() -> None:
        ledger = _BlockingLedger()
        app, _, client = _app(unused, ledger=ledger)
        try:
            response_task = asyncio.create_task(_post(app, "pw_invalid"))
            await asyncio.wait_for(ledger.rejected_submitted.wait(), timeout=1)
            assert not response_task.done()
            ledger.release.set()
            response = await response_task
            assert response.status_code == 401
            assert [event.event_type for event in ledger.events] == [
                EventType.RECEIVED,
                EventType.REJECTED,
            ]
        finally:
            await client.aclose()

    asyncio.run(scenario())

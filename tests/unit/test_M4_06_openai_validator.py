"""M4-06 Runtime-capability validation and OpenAI boundary tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import UUID

import httpx
import pytest
from holm_domain import ModelInstanceStatus
from holm_gateway.application import create_app
from holm_gateway.authentication import ApiKeyAuthenticator
from holm_gateway.configuration import GatewayConfiguration
from holm_gateway.routing import CachedRouteDirectory, GatewayRoute
from holm_runtimes import (
    RuntimeCompatibilityError,
    RuntimeRequestCapabilities,
    load_runtime_compatibility,
)
from holm_security import ApiKeyPepperKeyring, ApiKeyValidationSnapshot, issue_api_key
from starlette.types import ASGIApp

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
RUNTIME_COMPATIBILITY: Final[Path] = REPOSITORY_ROOT / "runtime-compat.yaml"
SERVICE_ID: Final[UUID] = UUID("00000000-0000-7000-8000-000000004601")
INSTANCE_ID: Final[UUID] = UUID("00000000-0000-7000-8000-000000004602")
USER_ID: Final[UUID] = UUID("00000000-0000-7000-8000-000000004603")
KEY_ID: Final[UUID] = UUID("00000000-0000-7000-8000-000000004604")

_VLLM = load_runtime_compatibility(RUNTIME_COMPATIBILITY).runtime("vllm")
assert _VLLM is not None
VLLM_CAPABILITIES: Final = _VLLM.request_capabilities


class _RouteSource:
    def __init__(self, route: GatewayRoute) -> None:
        self._route = route

    async def list_routes(self) -> tuple[GatewayRoute, ...]:
        return (self._route,)


def _route(*, status: ModelInstanceStatus = ModelInstanceStatus.RUNNING) -> GatewayRoute:
    return GatewayRoute(
        service_id=SERVICE_ID,
        instance_id=INSTANCE_ID,
        public_model_name="qwen-mini",
        enabled=True,
        instance_status=status.value,
        runtime_name="vllm",
        internal_endpoint="private-runtime:8000",
        internal_model_name="mi_000000004606",
    )


def _authenticator() -> tuple[ApiKeyAuthenticator, str]:
    keyring = ApiKeyPepperKeyring(current_key_id="m4-06", keys={"m4-06": b"K" * 32})
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
            scoped_service_ids=frozenset(),
            database_now=datetime(2026, 9, 16, tzinfo=UTC),
            user_is_admin=True,
            authorized_service_ids=frozenset({SERVICE_ID}),
        )

    return ApiKeyAuthenticator(lookup=lookup, keyring=keyring), issued.plaintext


def _application(
    handler: Callable[[httpx.Request], Coroutine[object, object, httpx.Response]],
    *,
    route: GatewayRoute | None = None,
    capabilities: RuntimeRequestCapabilities = VLLM_CAPABILITIES,
    configuration: GatewayConfiguration | None = None,
) -> tuple[ASGIApp, str, httpx.AsyncClient]:
    authenticator, key = _authenticator()
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None)
    selected_route = route or _route()
    return (
        create_app(
            authenticator=authenticator,
            routes=CachedRouteDirectory(_RouteSource(selected_route), ttl_seconds=2),
            runtime_capabilities={selected_route.runtime_name: capabilities},
            configuration=configuration,
            upstream_client=upstream,
        ),
        key,
        upstream,
    )


async def _success(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        content=b"data: "
        + json.dumps(
            {
                "model": "mi_000000004606",
                "choices": [{"finish_reason": "stop", "delta": {"content": "ok"}}],
            }
        ).encode()
        + b"\n\ndata: [DONE]\n\n",
    )


@pytest.mark.parametrize("runtime_name", ("vllm", "sglang"))
@pytest.mark.parametrize("stream", (False, True))
@pytest.mark.parametrize(
    "fields",
    (
        {},
        {"reasoning_effort": None},
        *(
            {"reasoning_effort": value}
            for value in ("none", "minimal", "low", "medium", "high", "xhigh", "max")
        ),
    ),
)
def test_FIX_20_TC_RT_02_reasoning_effort_reaches_each_runtime(
    runtime_name: str, stream: bool, fields: dict[str, object]
) -> None:
    """FX-FIX-20: M4-06 HTTP fixture with the FIX-20 request-control matrix."""
    captured: list[dict[str, object]] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        if stream:
            return httpx.Response(
                200,
                content=(
                    b'data: {"choices":[{"delta":{"content":"ok"},'
                    b'"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
                ),
                headers={"content-type": "text/event-stream"},
            )
        return await _success(request)

    async def scenario() -> None:
        runtime = load_runtime_compatibility(RUNTIME_COMPATIBILITY).runtime(runtime_name)
        assert runtime is not None
        app, key, client = _application(
            upstream,
            route=replace(_route(), runtime_name=runtime_name),
            capabilities=runtime.request_capabilities,
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as gateway:
                response = await gateway.post(
                    "/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": "qwen-mini",
                        "messages": [{"role": "user", "content": "hello"}],
                        "stream": stream,
                        **fields,
                    },
                )
            assert response.status_code == 200
            assert len(captured) == 1
            if fields.get("reasoning_effort") is None:
                assert "reasoning_effort" not in captured[0]
            else:
                assert captured[0]["reasoning_effort"] == fields["reasoning_effort"]
            assert "chat_template_kwargs" not in captured[0]
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("runtime_name", ("vllm", "sglang"))
@pytest.mark.parametrize(
    "fields",
    (
        {"reasoning_effort": "extreme"},
        {"reasoning_effort": 1},
        {"reasoning_effort": True},
        {"reasoning_effort": []},
        {"reasoning_effort": {}},
        {"reasoning_effort": "SENTINEL_BODY"},
        {"reasoning_effort": "none", "chat_template_kwargs": {"enable_thinking": False}},
    ),
)
def test_FIX_20_TC_RT_02_invalid_reasoning_and_template_override_do_not_reach_runtime(
    runtime_name: str, fields: dict[str, object]
) -> None:
    async def unreachable(request: httpx.Request) -> httpx.Response:
        raise AssertionError("rejected request reached Runtime")

    async def scenario() -> None:
        runtime = load_runtime_compatibility(RUNTIME_COMPATIBILITY).runtime(runtime_name)
        assert runtime is not None
        app, key, client = _application(
            unreachable,
            route=replace(_route(), runtime_name=runtime_name),
            capabilities=runtime.request_capabilities,
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as gateway:
                response = await gateway.post(
                    "/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": "qwen-mini",
                        "messages": [{"role": "user", "content": "SENTINEL_BODY"}],
                        **fields,
                    },
                )
            _assert_invalid(response)
        finally:
            await client.aclose()

    asyncio.run(scenario())


def _assert_invalid(response: httpx.Response) -> None:
    assert response.status_code == 400
    payload = response.json()
    assert payload["error"]["code"] == "invalid_request"
    assert response.headers["x-request-id"] == payload["error"]["request_id"]
    assert all(
        forbidden not in response.text
        for forbidden in (
            "private-runtime",
            "mi_000000004606",
            "GPU",
            "container",
            "SENTINEL_BODY",
        )
    )


@pytest.mark.parametrize(
    ("field", "value", "forwarded"),
    (
        ("temperature", 0.25, 0.25),
        ("top_p", 0.75, 0.75),
        ("stop", "END", ["END"]),
        ("seed", 42, 42),
        ("max_tokens", 9, 9),
        (
            "tools",
            [
                {
                    "type": "function",
                    "function": {
                        "name": "weather",
                        "description": "look up weather",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"],
                        },
                    },
                }
            ],
            [
                {
                    "type": "function",
                    "function": {
                        "name": "weather",
                        "description": "look up weather",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"],
                        },
                    },
                }
            ],
        ),
    ),
)
def test_M4_06_TC_RT_02_declared_supported_field_reaches_runtime(
    field: str, value: object, forwarded: object
) -> None:
    captured: dict[str, object] = {}

    async def upstream(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return await _success(request)

    async def scenario() -> None:
        app, key, client = _application(upstream)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as gateway:
                response = await gateway.post(
                    "/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": "qwen-mini",
                        "messages": [{"role": "user", "content": "hello"}],
                        field: value,
                    },
                )
            assert response.status_code == 200
            assert captured[field] == forwarded
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(("field", "value"), (("n", 5), ("not_a_field", 1)))
def test_M4_06_TC_RT_02_unsupported_or_unknown_field_is_rejected(field: str, value: object) -> None:
    async def unreachable(request: httpx.Request) -> httpx.Response:
        raise AssertionError("rejected request reached Runtime")

    async def scenario() -> None:
        app, key, client = _application(unreachable)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as gateway:
                response = await gateway.post(
                    "/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={"model": "qwen-mini", "messages": [], field: value},
                )
            _assert_invalid(response)
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_M4_06_TC_RT_02_removing_declared_field_immediately_rejects_it() -> None:
    reduced = RuntimeRequestCapabilities(
        openai_chat_completions=(VLLM_CAPABILITIES.openai_chat_completions - {"temperature"})
    )
    reached = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal reached
        reached += 1
        return await _success(request)

    async def invoke(capabilities: RuntimeRequestCapabilities) -> httpx.Response:
        app, key, client = _application(upstream, capabilities=capabilities)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as gateway:
                return await gateway.post(
                    "/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={"model": "qwen-mini", "messages": [], "temperature": 0.4},
                )
        finally:
            await client.aclose()

    async def scenario() -> None:
        assert (await invoke(VLLM_CAPABILITIES)).status_code == 200
        rejected = await invoke(reduced)
        _assert_invalid(rejected)
        assert reached == 1

    asyncio.run(scenario())


def test_M4_06_runtime_capability_declaration_is_required(tmp_path: Path) -> None:
    payload = json.loads(RUNTIME_COMPATIBILITY.read_text(encoding="utf-8"))
    del payload["runtimes"][0]["request_capabilities"]
    missing = tmp_path / "runtime-compat-missing-capabilities.yaml"
    missing.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeCompatibilityError, match="request_capabilities"):
        load_runtime_compatibility(missing)


@pytest.mark.parametrize("method", ("GET", "PUT"))
def test_M4_06_invalid_method_returns_standard_400(method: str) -> None:
    async def scenario() -> None:
        app, key, client = _application(_success)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as gateway:
                response = await gateway.request(
                    method,
                    "/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={"model": "qwen-mini", "messages": []},
                )
            _assert_invalid(response)
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_M4_06_wrong_content_type_returns_standard_400() -> None:
    async def scenario() -> None:
        app, key, client = _application(_success)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as gateway:
                response = await gateway.post(
                    "/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "text/plain",
                    },
                    content=b'{"model":"qwen-mini","messages":[]}',
                )
            _assert_invalid(response)
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_M4_06_oversized_content_length_returns_standard_400() -> None:
    async def scenario() -> None:
        configuration = GatewayConfiguration(max_request_body_bytes=64)
        app, key, client = _application(_success, configuration=configuration)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as gateway:
                response = await gateway.post(
                    "/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                        "Content-Length": "65",
                    },
                    content=b'{"model":"qwen-mini","messages":[]}',
                )
            _assert_invalid(response)
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_M4_06_json_depth_limit_rejects_an_otherwise_valid_body() -> None:
    async def scenario() -> None:
        configuration = GatewayConfiguration(max_json_depth=3)
        app, key, client = _application(_success, configuration=configuration)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as gateway:
                response = await gateway.post(
                    "/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": "qwen-mini",
                        "messages": [{"role": "user", "content": "SENTINEL_BODY"}],
                    },
                )
            _assert_invalid(response)
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_M4_06_TC_GW_32_unhealthy_model_remains_visible_without_instance_details() -> None:
    async def scenario() -> None:
        app, key, client = _application(
            _success,
            route=_route(status=ModelInstanceStatus.UNHEALTHY),
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as gateway:
                response = await gateway.get(
                    "/v1/models", headers={"Authorization": f"Bearer {key}"}
                )
            assert response.status_code == 200
            assert response.json()["data"] == [
                {
                    "id": "qwen-mini",
                    "object": "model",
                    "created": 0,
                    "owned_by": "holm",
                    "holm_status": "unavailable",
                }
            ]
            assert "private-runtime" not in response.text
            assert "mi_000000004606" not in response.text
            assert "GPU" not in response.text
        finally:
            await client.aclose()

    asyncio.run(scenario())

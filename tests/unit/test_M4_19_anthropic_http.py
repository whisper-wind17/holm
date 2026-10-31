"""FX-M4-19 HTTP: authenticated synthetic Runtime, both protocol routes on one app."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from holm_domain import ModelInstanceStatus
from holm_gateway.application import create_app
from holm_gateway.routing import CachedRouteDirectory
from test_FIX_22_model_reasoning_efforts import POLICIES
from test_M4_06_openai_validator import (
    VLLM_CAPABILITIES,
    _application,
    _authenticator,
    _route,
    _RouteSource,
    _success,
)

BASE = {"model": "qwen-mini", "max_tokens": 32, "messages": [{"role": "user", "content": "hello"}]}


@pytest.mark.parametrize(
    "credential", ["x-api-key", "authorization", "both", "conflict", "missing"]
)
def test_M4_19_TC_GW_44_credentials_and_model_shapes(credential: str) -> None:
    async def scenario() -> None:
        app, key, upstream = _application(_success)
        headers = {}
        if credential in {"x-api-key", "both", "conflict"}:
            headers["x-api-key"] = key
        if credential in {"authorization", "both", "conflict"}:
            headers["authorization"] = "Bearer " + (
                "different" if credential == "conflict" else key
            )
        async with (
            upstream,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as client,
        ):
            response = await client.post("/v1/messages?beta=true", headers=headers, json=BASE)
            expected = 401 if credential in {"conflict", "missing"} else 200
            assert response.status_code == expected
            assert response.headers["request-id"] == response.headers["x-request-id"]
            if expected == 401:
                assert response.json()["error"]["type"] == "authentication_error"
                return
            assert response.json()["content"] == [{"type": "text", "text": "ok"}]
            headers["anthropic-version"] = "arbitrary"
            models = await client.get("/v1/models", headers=headers)
            assert models.status_code == 200 and models.json()["has_more"] is False
            assert models.json()["data"] == [
                {
                    "type": "model",
                    "id": "qwen-mini",
                    "display_name": "qwen-mini",
                    "created_at": "1970-01-01T00:00:00Z",
                }
            ]
            openai = await client.get("/v1/models", headers={"authorization": "Bearer " + key})
            assert openai.json()["object"] == "list"
            assert [m["id"] for m in openai.json()["data"]] == [
                m["id"] for m in models.json()["data"]
            ]
            for path in ("/v1/messages/count_tokens", "/v1/messages/unknown"):
                unsupported = await client.post(path, headers=headers, json=BASE)
                assert (
                    unsupported.status_code == 404
                    and unsupported.json()["error"]["type"] == "not_found_error"
                )
            invalid = await client.post("/v1/messages?other=true", headers=headers, json=BASE)
            assert invalid.status_code == 400 and "other" in invalid.json()["error"]["message"]

    asyncio.run(scenario())


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "architecture", [None, "Qwen3_5ForConditionalGeneration", "GptOssForCausalLM"]
)
def test_M4_19_TC_GW_41_42_43_runtime_reasoning_and_tools(
    stream: bool, architecture: str | None
) -> None:
    async def scenario() -> None:
        captured = []

        async def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            payload = {
                "model": "private",
                "choices": [
                    {
                        "delta": {
                            "reasoning_content": "thought",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "read", "arguments": '{"path":"a"}'},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 5},
            }
            return httpx.Response(
                200,
                content=b"data: " + json.dumps(payload).encode() + b"\n\ndata: [DONE]\n\n",
            )

        authenticator, key = _authenticator()
        upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app = create_app(
            authenticator=authenticator,
            routes=CachedRouteDirectory(
                _RouteSource(replace(_route(), model_architecture=architecture))
            ),
            runtime_capabilities={"vllm": VLLM_CAPABILITIES},
            reasoning_models=POLICIES,
            upstream_client=upstream,
        )
        async with (
            upstream,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as client,
        ):
            response = await client.post(
                "/v1/messages",
                headers={"x-api-key": key},
                json={
                    **BASE,
                    "stream": stream,
                    "thinking": {"type": "adaptive"},
                    "output_config": {"effort": "max"},
                },
            )
            assert response.status_code == 200, response.text
            if architecture is None:
                assert "reasoning_effort" not in captured[0]
            else:
                assert (
                    captured[0]["reasoning_effort"]
                    == {"Qwen3_5ForConditionalGeneration": "xhigh", "GptOssForCausalLM": "high"}[
                        architecture
                    ]
                )
            if stream:
                assert captured[0]["stream_options"] == {"include_usage": True}
                assert (
                    b"event: message_stop" in response.content
                    and b"signature_delta" in response.content
                )
            else:
                assert response.json()["content"] == [
                    {"type": "thinking", "thinking": "thought", "signature": ""},
                    {"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "a"}},
                ]
                assert response.json()["usage"] == {"input_tokens": 3, "output_tokens": 5}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "status,expected", [(ModelInstanceStatus.STOPPED, 503), (ModelInstanceStatus.RUNNING, 400)]
)
def test_M4_19_TC_GW_44_backend_and_context_errors(
    status: ModelInstanceStatus, expected: int
) -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "maximum context length is 8192 tokens private-runtime secret",
                        "param": "max_tokens",
                    }
                },
            )

        app, key, upstream = _application(handler, route=_route(status=status))
        async with (
            upstream,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as client,
        ):
            response = await client.post("/v1/messages", headers={"x-api-key": key}, json=BASE)
            assert response.status_code == expected
            assert "private-runtime" not in response.text and "secret" not in response.text

    asyncio.run(scenario())


def test_M4_19_TC_GW_42_core_timeout_remains_active_across_sse_events() -> None:
    from holm_gateway.configuration import GatewayConfiguration

    async def scenario() -> None:
        class RuntimeStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices":[{"delta":{"reasoning_content":"private-thought"}}]}\n\n'
                await asyncio.sleep(0)
                yield b'data: {"choices":[{"delta":{"content":"visible"}}]}\n\n'
                await asyncio.Event().wait()

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=RuntimeStream())

        config = GatewayConfiguration(first_token_timeout_seconds=1, total_timeout_seconds=1)
        app, key, upstream = _application(handler, configuration=config)
        async with (
            upstream,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as client,
        ):
            async with asyncio.timeout(5):
                response = await client.post(
                    "/v1/messages",
                    headers={"x-api-key": key},
                    json={
                        **BASE,
                        "stream": True,
                        "thinking": {"type": "adaptive", "display": "omitted"},
                    },
                )
            assert response.status_code == 200
            assert (
                b"event: error" in response.content
                and b"event: message_stop" not in response.content
            )
            assert "响应超时" in response.text and "private-thought" not in response.text

    asyncio.run(scenario())

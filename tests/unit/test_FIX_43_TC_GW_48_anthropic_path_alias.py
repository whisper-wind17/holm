"""FX-FIX-43: M4-06 HTTP fixtures and M4-19 request body, reused read-only."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from holm_gateway.path_alias import AnthropicPathAliasMiddleware
from starlette.types import Message, Receive, Scope, Send
from test_M4_06_openai_validator import _application, _success
from test_M4_19_anthropic_http import BASE


@pytest.mark.parametrize("stream", [False, True])
def test_FIX_43_TC_GW_48_messages_match_canonical(stream: bool) -> None:
    async def scenario() -> None:
        app, key, upstream = _application(_success)
        async with (
            upstream,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as client,
        ):
            results = []
            for path in ("/v1/messages", "/v1/v1/messages"):
                response = await client.post(
                    path + "?beta=true",
                    headers={"x-api-key": key},
                    json={**BASE, "stream": stream},
                )
                assert response.status_code == 200
                assert response.headers["request-id"] == response.headers["x-request-id"]
                if stream:
                    assert response.headers["content-type"].startswith("text/event-stream")
                    assert b"event: message_stop" in response.content
                    payloads = [
                        json.loads(line[6:])
                        for line in response.text.splitlines()
                        if line.startswith("data: ")
                    ]
                    for payload in payloads:
                        if payload["type"] == "message_start":
                            payload["message"].pop("id")
                    results.append(payloads)
                else:
                    payload = response.json()
                    payload.pop("id")
                    assert payload["content"] == [{"type": "text", "text": "ok"}]
                    results.append(payload)
            assert results[0] == results[1]

    asyncio.run(scenario())


def test_FIX_43_TC_GW_48_errors_models_and_openai_path() -> None:
    async def scenario() -> None:
        app, key, upstream = _application(_success)
        async with (
            upstream,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as client,
        ):
            for suffix, credential, status, error_type in (
                ("", "invalid", 401, "authentication_error"),
                ("/count_tokens", key, 404, "not_found_error"),
                ("/unknown", key, 404, "not_found_error"),
            ):
                errors = []
                for prefix in ("/v1", "/v1/v1"):
                    response = await client.post(
                        prefix + "/messages" + suffix,
                        headers={"x-api-key": credential},
                        json=BASE,
                    )
                    assert response.status_code == status
                    payload = response.json()
                    assert payload["type"] == "error"
                    assert payload["error"]["type"] == error_type
                    assert payload.pop("request_id") == response.headers["request-id"]
                    errors.append(payload)
                assert errors[0] == errors[1]
            for anthropic in (False, True):
                headers = {"authorization": "Bearer " + key}
                if anthropic:
                    headers["anthropic-version"] = "2023-06-01"
                canonical = await client.get("/v1/models", headers=headers)
                alias = await client.get("/v1/v1/models", headers=headers)
                assert canonical.status_code == alias.status_code == 200
                assert canonical.json() == alias.json()
                if anthropic:
                    assert alias.json()["has_more"] is False
                    assert alias.json()["data"][0]["type"] == "model"
                else:
                    assert alias.json()["object"] == "list"
            response = await client.post(
                "/v1/v1/chat/completions",
                headers={"authorization": "Bearer " + key},
                json=BASE,
            )
            # The existing OpenAI unknown-route handler maps to invalid_request.
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "invalid_request"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/v1/v1/messages", "/v1/messages"),
        ("/v1/v1/messages/count_tokens", "/v1/messages/count_tokens"),
        ("/v1/v1/messages/unknown", "/v1/messages/unknown"),
        ("/v1/v1/models", "/v1/models"),
        ("/v1/v1/chat/completions", "/v1/v1/chat/completions"),
        ("/v1/v1/messages-other", "/v1/v1/messages-other"),
        ("/v1/v1/models/unknown", "/v1/v1/models/unknown"),
        ("/v1/messages", "/v1/messages"),
    ],
)
def test_FIX_43_TC_GW_48_scope_before_request_context(path: str, expected: str) -> None:
    async def scenario() -> None:
        app, _, upstream = _application(_success)
        assert isinstance(app, AnthropicPathAliasMiddleware)
        seen: list[Scope] = []

        async def probe(scope: Scope, receive: Receive, send: Send) -> None:
            seen.append(scope)

        async def receive() -> Message:
            raise AssertionError("alias layer must not consume the body")

        async def send(message: Message) -> None:
            raise AssertionError("alias layer must not emit a response")

        scope: Scope = {
            "type": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"beta=a%2Fb",
            "headers": [(b"x-api-key", b"synthetic")],
        }
        async with upstream:
            await AnthropicPathAliasMiddleware(probe)(scope, receive, send)
        assert len(seen) == 1
        assert seen[0] == {**scope, "path": expected, "raw_path": expected.encode()}
        assert scope["path"] == path

    asyncio.run(scenario())

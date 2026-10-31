"""FX-FIX-25: synthetic client payloads using the authenticated M4-06 HTTP fixture."""

from __future__ import annotations

import asyncio
import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from holm_gateway.openai_adapter import OpenAIProtocolAdapter
from holm_gateway.vllm_adapter import _request_payload
from holm_host_agent.runtime_process import build_runtime_command
from holm_protocols import InferenceFailure, thaw_json
from holm_runtimes import RuntimeCompatibilityError, load_runtime_compatibility
from test_FIX_19_reasoning_parser import _spec
from test_M4_06_openai_validator import RUNTIME_COMPATIBILITY, _application, _route, _success

SCHEMA = {
    "type": "object",
    "properties": {
        "nested": {
            "type": "object",
            "properties": {"labels": {"type": "array", "items": {"enum": ["a", "b"]}}},
            "additionalProperties": False,
        },
        "union": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    },
    "required": ["nested"],
    "$defs": {"ref": {"type": "string"}},
}
BASE = {"model": "qwen-mini", "messages": [{"role": "user", "content": "hello"}]}


def _adapter() -> OpenAIProtocolAdapter:
    catalog = load_runtime_compatibility(RUNTIME_COMPATIBILITY)
    return OpenAIProtocolAdapter(
        {name: runtime.request_capabilities for name, runtime in catalog.runtimes.items()},
        maximum_json_depth=32,
    )


@pytest.mark.parametrize("runtime_name", ["vllm", "sglang"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "fields,expected",
    [({"tool_choice": mode}, {"tool_choice": mode}) for mode in ("none", "auto", "required")]
    + [
        (
            {"tool_choice": {"type": "function", "function": {"name": "lookup"}}},
            {"tool_choice": {"type": "function", "function": {"name": "lookup"}}},
        ),
        ({"parallel_tool_calls": True}, {"parallel_tool_calls": True}),
        ({"parallel_tool_calls": False}, {"parallel_tool_calls": False}),
        ({"max_completion_tokens": 16}, {"max_tokens": 16}),
        ({"max_tokens": 16, "max_completion_tokens": 16}, {"max_tokens": 16}),
        ({"max_completion_tokens": 0}, {"max_tokens": 0}),
        ({"presence_penalty": -2}, {"presence_penalty": -2.0}),
        ({"frequency_penalty": 2}, {"frequency_penalty": 2.0}),
        ({"user": "FX25_DISCARDED_USER"}, {}),
        *[
            ({"response_format": value}, {"response_format": value})
            for value in (
                {"type": "text"},
                {"type": "json_object"},
                {"type": "json_schema", "json_schema": {"name": "result", "schema": SCHEMA}},
                {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "result",
                        "schema": SCHEMA,
                        "strict": False,
                        "description": "result schema",
                    },
                },
            )
        ],
    ],
)
def test_FIX_25_TC_GW_33_fields_reach_each_runtime(
    runtime_name: str, stream: bool, fields: dict[str, object], expected: dict[str, object]
) -> None:
    captured: list[dict[str, object]] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        if stream:
            return httpx.Response(
                200,
                content=(
                    b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
                    b"data: [DONE]\n\n"
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
                    json={**BASE, "stream": stream, **fields},
                )
            assert response.status_code == 200
            wire = {**BASE, "model": "mi_000000004606", "stream": True, **expected}
            wire["stream_options"] = {"include_usage": True}
            assert captured == [wire]
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fields",
    [
        {"tool_choice": value}
        for value in (
            None,
            True,
            "function",
            {},
            {"type": "function", "function": {"name": 2}},
            {"type": "function", "function": {"name": "lookup"}, "extra": 1},
        )
    ]
    + [{"parallel_tool_calls": value} for value in (None, 1, "true")]
    + [
        {name: value}
        for name in ("presence_penalty", "frequency_penalty")
        for value in (None, True, "0", -2.1, 2.1, 10**400, float("inf"), float("nan"))
    ]
    + [{"max_completion_tokens": value} for value in (None, True, -1, 1.5, "2")]
    + [{"user": value} for value in (None, 5, {})]
    + [
        {"response_format": value}
        for value in (
            None,
            "text",
            {},
            {"type": "xml"},
            {"type": "text", "extra": 1},
            {"type": "json_schema"},
            {"type": "json_schema", "json_schema": {"name": 1, "schema": {}}},
            {"type": "json_schema", "json_schema": {"name": "x", "schema": []}},
            {"type": "json_schema", "json_schema": {"name": "x", "schema": {}, "strict": 1}},
            {"type": "json_schema", "json_schema": {"name": "x", "schema": {}, "description": 2}},
        )
    ]
    + [
        {"max_tokens": 1, "max_completion_tokens": 2},
        {"messages": [{"role": "tool", "content": "ok", "tool_call_id": 1}]},
        {"tools": [{"type": "function", "function": {"name": "x", "parameters": []}}]},
        {"tools": [{"type": "function", "function": {"name": "x", "parameters": None}}]},
        {"tools": [{"type": "function", "function": {"name": "x", "strict": 1}}]},
    ],
)
def test_FIX_25_TC_GW_33_invalid_fields_return_400_without_upstream(
    fields: dict[str, object],
) -> None:
    async def upstream(request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid request reached runtime")

    async def scenario() -> None:
        app, key, client = _application(upstream)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as gateway:
                response = await gateway.post(
                    "/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    content=json.dumps({**BASE, **fields}).encode(),
                )
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "invalid_request"
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_FIX_25_TC_GW_33_schema_is_frozen_complete_and_tool_result_correlated() -> None:
    payload = {
        **BASE,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "parameters": SCHEMA,
                    "strict": True,
                },
            }
        ],
        "messages": [{"role": "tool", "content": "result", "tool_call_id": "call_25"}],
    }
    request = _adapter().parse_request(json.dumps(payload).encode(), runtime_name="vllm")
    assert thaw_json(request.tools[0].input_schema) == SCHEMA
    with pytest.raises(FrozenInstanceError):
        request.tools[0].input_schema.members = ()
    wire = _request_payload(request, "qwen-mini")
    assert wire == {**payload, "stream": True}
    missing_id = _adapter().parse_request(
        json.dumps({**BASE, "messages": [{"role": "tool", "content": "result"}]}).encode(),
        runtime_name="vllm",
    )
    assert "tool_call_id" not in _request_payload(missing_id, "qwen-mini")["messages"][0]


@pytest.mark.parametrize(
    "fields",
    [
        {"logprobs": "PRIVATE_VALUE"},
        {("FX25_UNKNOWN_" + str(i) + "x" * 80): "PRIVATE_VALUE" for i in reversed(range(12))},
    ],
)
def test_FIX_25_TC_GW_33_unsupported_names_are_bounded_response_only(
    fields: dict[str, object],
) -> None:
    adapter = _adapter()
    with pytest.raises(InferenceFailure) as failure:
        adapter.parse_request(json.dumps({**BASE, **fields}).encode(), runtime_name="vllm")
    from uuid import UUID

    mapped = adapter.map_error(failure.value.error, UUID(int=25))
    assert mapped.status_code == 400
    assert json.loads(mapped.body)["error"]["details"] == {
        "unsupported_fields": [name[:64] for name in sorted(fields)[:8]]
    }
    assert b"PRIVATE_VALUE" not in mapped.body
    assert "FX25_UNKNOWN" not in repr(failure.value.error)


@pytest.mark.parametrize("runtime_name", ["vllm", "sglang"])
@pytest.mark.parametrize(
    "architecture", ["Qwen3_5ForConditionalGeneration", "GptOssForCausalLM", "Qwen2ForCausalLM"]
)
def test_FIX_25_launch_uses_one_architecture_read_and_independent_parsers(
    tmp_path: Path, runtime_name: str, architecture: str
) -> None:
    runtime = load_runtime_compatibility(RUNTIME_COMPATIBILITY).runtime(runtime_name)
    assert runtime is not None
    with patch(
        "holm_host_agent.runtime_process._model_architectures", return_value=(architecture,)
    ) as read:
        command = build_runtime_command(runtime, _spec(runtime_name, tmp_path), 38123)
    read.assert_called_once_with(str(tmp_path))
    if architecture == "Qwen2ForCausalLM":
        assert "--tool-call-parser" not in command
        assert "--enable-auto-tool-choice" not in command
        assert "--reasoning-parser" not in command
    else:
        assert command[command.index("--tool-call-parser") + 1] == (
            "qwen3_coder"
            if architecture.startswith("Qwen")
            else "openai"
            if runtime_name == "vllm"
            else "gpt-oss"
        )
        assert ("--enable-auto-tool-choice" in command) == (runtime_name == "vllm")
        assert (
            command[command.index("--reasoning-parser") + 1]
            == runtime.reasoning_parsers[architecture]
        )


@pytest.mark.parametrize("runtime_index", [0, 1])
@pytest.mark.parametrize("parser", ["unregistered", "--trust-remote-code", 42])
def test_FIX_25_catalog_rejects_unregistered_tool_parser(
    tmp_path: Path, runtime_index: int, parser: object
) -> None:
    payload = json.loads(RUNTIME_COMPATIBILITY.read_text())
    payload["runtimes"][runtime_index]["tool_call_parsers"] = {
        "Qwen3_5ForConditionalGeneration": parser
    }
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(RuntimeCompatibilityError, match="tool_call_parsers"):
        load_runtime_compatibility(path)


@pytest.mark.parametrize("runtime_name", ["vllm", "sglang"])
@pytest.mark.parametrize("stream", [False, True])
def test_FIX_25_TC_GW_33_legacy_request_wire_bytes_unchanged(
    runtime_name: str, stream: bool
) -> None:
    """FX-FIX-25 golden bytes, with FIX-38 forcing upstream streaming and usage."""
    payload = {
        **BASE,
        "stream": stream,
        "temperature": 0.5,
        "top_p": 0.8,
        "stop": ["end"],
        "seed": 5,
        "max_tokens": 8,
        "reasoning_effort": "none",
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "parameters": {
                        "type": "object",
                        "properties": {"q": {"type": "string", "description": "query"}},
                        "required": ["q"],
                    },
                },
            }
        ],
    }
    expected = (
        b'{"model":"mi_000000004606","messages":[{"role":"user","content":"hello"}],"stream":true'
        b',"temperature":0.5,"top_p":0.8,"stop":["end"],"seed":5,"max_tokens":8,'
        b'"reasoning_effort":"none","tools":[{"type":"function","function":{"name":"lookup",'
        b'"parameters":{"type":"object","properties":{"q":{"type":"string","description":"query"}},'
        b'"required":["q"]}}}],"stream_options":{"include_usage":true}}'
    )
    captured: list[bytes] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        captured.append(request.content)
        if stream:
            return httpx.Response(
                200,
                content=b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n',
                headers={"content-type": "text/event-stream"},
            )
        return await _success(request)

    async def scenario() -> None:
        app, key, client = _application(
            upstream, route=replace(_route(), runtime_name=runtime_name)
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as gateway:
                response = await gateway.post(
                    "/v1/chat/completions", json=payload, headers={"Authorization": f"Bearer {key}"}
                )
            assert response.status_code == 200
            assert captured == [expected]
        finally:
            await client.aclose()

    asyncio.run(scenario())

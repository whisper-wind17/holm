"""FX-M4-19-Claude-Code: synthetic content, captured 2.1.303 two-round field shapes.

Source: dispatch capture 2026-10-19 (first -p request and tool-result round).
No original prompts, credentials, tool arguments or client metadata are copied.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from uuid import UUID

import pytest
from holm_domain import ErrorCode
from holm_gateway.anthropic_adapter import AnthropicProtocolAdapter
from holm_gateway.request_context import RequestRecordContext
from holm_gateway.vllm_adapter import _request_payload
from holm_protocols import (
    Completed,
    ContentDelta,
    InferenceFailure,
    ModelReasoningPolicy,
    PlatformError,
    ReasoningEffort,
    StopReason,
    ThinkingDelta,
    ToolArgumentsDelta,
    ToolCallStarted,
    UsageInfo,
    UsageReported,
)

RID = UUID("41900000-0000-4000-8000-000000000041")
BASE = {"model": "public", "max_tokens": 32000, "messages": [{"role": "user", "content": "hello"}]}


def adapter() -> AnthropicProtocolAdapter:
    return AnthropicProtocolAdapter(maximum_json_depth=32, maximum_response_bytes=1024 * 1024)


def parse(fields: dict[str, object]):
    return adapter().parse_request(json.dumps({**BASE, **fields}).encode(), runtime_name="vllm")


def frames(data: bytes) -> list[dict]:
    return [json.loads(frame.split(b"\ndata: ", 1)[1]) for frame in data.split(b"\n\n") if frame]


def test_M4_19_TC_GW_41_captured_two_round_shapes() -> None:
    fields = {
        "stream": True,
        "system": [
            {"type": "text", "text": f"system-{i}", "cache_control": {"type": "ephemeral"}}
            for i in range(3)
        ],
        "thinking": {"type": "adaptive", "display": "omitted", "future": True},
        "output_config": {"effort": "high", "future": True},
        "context_management": {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]},
        "metadata": {"user_id": "FX-M4-19-discarded"},
        "service_tier": "auto",
        "tools": [
            {
                "name": f"tool_{i}",
                "description": "synthetic",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
                "cache_control": {"type": "ephemeral"},
            }
            for i in range(20)
        ],
        "messages": [
            {"role": "system", "content": "leading"},
            {"role": "user", "content": "first"},
        ],
    }
    first = parse(fields)
    assert first.include_stream_usage and first.reasoning is not None
    assert first.reasoning.effort is ReasoningEffort.HIGH and first.reasoning.nearest_supported
    assert len(first.tools) == 20
    assert first.messages[0].content[0].text == "system-0\nsystem-1\nsystem-2\nleading"
    second = parse(
        {
            **fields,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}],
                },
                {"role": "system", "content": "middle"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "DROP", "signature": ""},
                        {"type": "redacted_thinking", "data": "DROP"},
                        {
                            "type": "tool_use",
                            "id": "call_1",
                            "name": "tool_0",
                            "input": {"path": "a"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_1",
                            "is_error": False,
                            "content": "result",
                        }
                    ],
                },
                {
                    "role": "system",
                    "content": [
                        {"type": "text", "text": "tail", "cache_control": {"type": "ephemeral"}}
                    ],
                },
            ],
        }
    )
    wire = _request_payload(second, "internal")
    assert [m["role"] for m in wire["messages"]] == ["system", "user", "assistant", "tool", "user"]
    assert wire["messages"][-1]["content"] == [
        {"type": "text", "text": "middle"},
        {"type": "text", "text": "tail"},
    ]
    assert wire["messages"][-2] == {"role": "tool", "content": "result", "tool_call_id": "call_1"}
    assert json.loads(wire["messages"][2]["tool_calls"][0]["function"]["arguments"]) == {
        "path": "a"
    }
    assert "DROP" not in json.dumps(wire) and "FX-M4-19-discarded" not in json.dumps(wire)


def test_M4_19_TC_GW_41_images_tool_results_and_sampling() -> None:
    request = parse(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "after"},
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_1",
                            "content": [
                                {"type": "text", "text": "a"},
                                {"type": "text", "text": "b"},
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/png",
                                        "data": "YWJj",
                                    },
                                },
                            ],
                        },
                    ],
                }
            ],
            "top_k": 12,
            "temperature": 0.5,
            "top_p": 0.9,
            "stop_sequences": ["END"],
            "tool_choice": {"type": "any", "disable_parallel_tool_use": True},
        }
    )
    wire = _request_payload(request, "internal")
    assert [m["role"] for m in wire["messages"]] == ["tool", "user", "user"]
    assert wire["messages"][0]["content"] == "a\nb"
    assert wire["messages"][1]["content"][0]["image_url"]["url"] == "data:image/png;base64,YWJj"
    assert wire["messages"][2]["content"] == "after"
    assert wire["top_k"] == 12 and wire["stop"] == ["END"]
    assert wire["tool_choice"] == "required" and wire["parallel_tool_calls"] is False


@pytest.mark.parametrize(
    "thinking,effort",
    [
        ({"type": "enabled", "budget_tokens": 1024}, "low"),
        ({"type": "enabled", "budget_tokens": 8192}, "medium"),
        ({"type": "enabled", "budget_tokens": 32768}, "high"),
        ({"type": "adaptive"}, "medium"),
        ({"type": "disabled"}, "none"),
    ],
)
def test_M4_19_TC_GW_41_reasoning(thinking: dict, effort: str) -> None:
    request = parse({"thinking": thinking})
    assert request.reasoning is not None and request.reasoning.effort.value == effort
    policy = ModelReasoningPolicy((ReasoningEffort.LOW, ReasoningEffort.HIGH), {}, False)
    assert policy.resolve_nearest(ReasoningEffort.MEDIUM) is ReasoningEffort.LOW
    assert policy.resolve_nearest(ReasoningEffort.NONE) is ReasoningEffort.LOW
    with pytest.raises(InferenceFailure):
        policy.resolve(ReasoningEffort.MEDIUM)


@pytest.mark.parametrize(
    "fields,name",
    [
        ({"unknown_field": 1}, "unknown_field"),
        ({"tools": [{"type": "web_search_20250305"}]}, "web_search_20250305"),
        ({"messages": [{"role": "user", "content": [{"type": "document"}]}]}, "document"),
        ({"max_tokens": 0}, ""),
        ({"top_p": 2}, ""),
        ({"thinking": {"type": "enabled"}}, ""),
    ],
)
def test_M4_19_TC_GW_41_invalid(fields: dict, name: str) -> None:
    with pytest.raises(InferenceFailure) as failure:
        parse(fields)
    mapped = adapter().map_error(failure.value.error, RID)
    assert mapped.status_code == 400
    assert name in json.loads(mapped.body)["error"]["message"]


@pytest.mark.parametrize("omit", [False, True])
@pytest.mark.parametrize(
    "reason,expected,sequence",
    [
        (StopReason.STOP, "end_turn", None),
        (StopReason.STOP, "stop_sequence", "END"),
        (StopReason.LENGTH, "max_tokens", None),
        (StopReason.TOOL_CALLS, "tool_use", None),
        (StopReason.CONTENT_FILTER, "refusal", None),
        (StopReason.OTHER, "end_turn", None),
    ],
)
def test_M4_19_TC_GW_42_43_blocks_usage_and_stop(
    omit: bool, reason: StopReason, expected: str, sequence: str | None
) -> None:
    request = parse(
        {
            "thinking": {"type": "adaptive", **({"display": "omitted"} if omit else {})},
            "stop_sequences": ["END"],
        }
    )
    events = (
        ThinkingDelta("think"),
        ContentDelta("answer"),
        ToolCallStarted(0, "call_a", "read"),
        ToolArgumentsDelta(0, '{"path":'),
        ToolArgumentsDelta(0, '"a"}'),
        ToolCallStarted(1, None, "run"),
        ToolArgumentsDelta(1, "{}"),
        Completed(reason, stop_sequence=sequence),
        UsageReported(UsageInfo(20, 8)),
    )
    codec = adapter().bind(RID, request)
    wire = b"".join(codec.encode_stream_event(event) or b"" for event in events)
    assert codec.encoder is not None
    data = frames(wire + codec.encoder.finish())
    assert data[0]["type"] == "message_start" and data[0]["message"]["usage"]["input_tokens"] == 0
    assert data[-1] == {"type": "message_stop"}
    assert data[-2]["delta"] == {"stop_reason": expected, "stop_sequence": sequence}
    assert data[-2]["usage"] == {"input_tokens": 20, "output_tokens": 8}
    starts = [value["content_block"] for value in data if value["type"] == "content_block_start"]
    assert [block["type"] for block in starts] == ([] if omit else ["thinking"]) + [
        "text",
        "tool_use",
        "tool_use",
    ]
    assert starts[-1]["id"].startswith("toolu_")
    signatures = [
        value["delta"]
        for value in data
        if value["type"] == "content_block_delta" and value["delta"]["type"] == "signature_delta"
    ]
    assert signatures == ([] if omit else [{"type": "signature_delta", "signature": ""}])
    nonstream = json.loads(adapter().bind(RID, request).encode_non_stream(events))
    assert nonstream["id"] == "msg_" + str(RID)
    assert nonstream["content"][-2]["input"] == {"path": "a"}
    assert nonstream["stop_reason"] == expected and nonstream["usage"] == data[-2]["usage"]


def test_M4_19_TC_GW_42_interleaving_fails_and_ping_survives_silence() -> None:
    async def scenario() -> None:
        request = parse({"thinking": {"type": "adaptive", "display": "omitted"}})
        codec = adapter().bind(RID, request)
        context = RequestRecordContext(RID, datetime.now(UTC), time.monotonic())

        async def events():
            yield ThinkingDelta("secret")
            for _ in range(6):
                await asyncio.sleep(0.005)
                yield ThinkingDelta("secret")
            yield ToolCallStarted(0, "a", "read")
            yield ToolArgumentsDelta(0, "{}")
            yield ToolCallStarted(1, "b", "run")
            yield ToolArgumentsDelta(0, "interleaved")

        data = frames(
            b"".join(
                [part async for part in codec.stream_body(events(), context, ping_seconds=0.01)]
            )
        )
        assert any(value["type"] == "ping" for value in data)
        assert data[-1]["type"] == "error" and data[-1]["error"]["type"] == "api_error"
        assert context.error_code is ErrorCode.BACKEND_UNAVAILABLE
        assert b"secret" not in json.dumps(data).encode()
        assert all(value["type"] != "message_stop" for value in data)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "code,status,kind",
    [
        (ErrorCode.INVALID_REQUEST, 400, "invalid_request_error"),
        (ErrorCode.INVALID_API_KEY, 401, "authentication_error"),
        (ErrorCode.API_KEY_EXPIRED, 401, "authentication_error"),
        (ErrorCode.SERVICE_NOT_AUTHORIZED, 403, "permission_error"),
        (ErrorCode.MODEL_NOT_FOUND, 404, "not_found_error"),
        (ErrorCode.RATE_LIMIT_EXCEEDED, 429, "rate_limit_error"),
        (ErrorCode.GATEWAY_OVERLOADED, 503, "overloaded_error"),
        *[
            (code, 503, "api_error")
            for code in (
                ErrorCode.SERVICE_DISABLED,
                ErrorCode.BACKEND_UNAVAILABLE,
                ErrorCode.USAGE_STORAGE_UNAVAILABLE,
                ErrorCode.CONTROL_PLANE_UNAVAILABLE,
            )
        ],
        (ErrorCode.INFERENCE_TIMEOUT, 504, "api_error"),
    ],
)
def test_M4_19_TC_GW_44_error_mapping(code: ErrorCode, status: int, kind: str) -> None:
    error = PlatformError(code, limit_type="per_minute", limit=3, retry_after=17)
    mapped = adapter().map_error(error, RID)
    assert mapped.status_code == status and json.loads(mapped.body)["error"]["type"] == kind
    assert dict(mapped.headers)["request-id"] == str(RID) == dict(mapped.headers)["x-request-id"]
    if status == 429:
        assert dict(mapped.headers)["Retry-After"] == "17"
        assert "3 次" in json.loads(mapped.body)["error"]["message"]


def test_M4_19_TC_GW_42_encoder_failure_survives_upstream_close() -> None:
    from holm_domain import RequestRecordStatus

    async def scenario() -> None:
        context = RequestRecordContext(RID, datetime.now(UTC), time.monotonic(), forwarded=True)
        await context.mark_error(PlatformError(ErrorCode.BACKEND_UNAVAILABLE))
        await context.mark_aborted()
        assert context.error_code is ErrorCode.BACKEND_UNAVAILABLE
        assert context.status is RequestRecordStatus.FAILED

    asyncio.run(scenario())


def test_FIX_28_TC_GW_41_safeguards_discarded() -> None:
    # FX-FIX-28: synthetic values using the Claude Code field shapes from TC-GW-41.
    fields = {
        "stream": True,
        "system": [{"type": "text", "text": "synthetic", "cache_control": {"type": "ephemeral"}}],
        "thinking": {"type": "adaptive", "display": "omitted"},
        "output_config": {"effort": "high"},
        "context_management": {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]},
        "metadata": {"user_id": "FX-FIX-28-client"},
        "tools": [{"name": "read", "input_schema": {"type": "object"}}],
    }
    baseline = parse(fields)
    request = parse({**fields, "safeguards": {"synthetic": "FX-FIX-28-discarded"}})
    assert request == baseline
    wire = _request_payload(request, "internal")
    assert wire == _request_payload(baseline, "internal")
    assert "safeguards" not in wire
    assert "FX-FIX-28-discarded" not in json.dumps(wire)


def test_FIX_28_TC_GW_41_safeguards_does_not_allow_unknown_fields() -> None:
    with pytest.raises(InferenceFailure) as failure:
        parse(
            {
                "safeguards": {"synthetic": "FX-FIX-28-discarded"},
                "unknown_field": "FX-FIX-28-unknown-value",
            }
        )
    assert failure.value.error.code is ErrorCode.INVALID_REQUEST
    assert failure.value.error.unsupported_fields == ("unknown_field",)
    mapped = adapter().map_error(failure.value.error, RID)
    assert mapped.status_code == 400
    assert "unknown_field" in json.loads(mapped.body)["error"]["message"]
    assert b"FX-FIX-28" not in mapped.body

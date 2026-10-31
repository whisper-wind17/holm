"""FX-M4-19-runtime: synthetic reasoning/tool response, no model or GPU required."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID

import httpx
import pytest
from holm_gateway.metering import RequestMetering
from holm_gateway.openai_adapter import OpenAIProtocolAdapter
from holm_gateway.request_context import RequestRecordContext
from holm_gateway.sglang_adapter import SglangRuntimeAdapter
from holm_gateway.vllm_adapter import VllmRuntimeAdapter
from holm_protocols import ContentDelta, ThinkingDelta, ToolArgumentsDelta, ToolCallStarted
from holm_runtimes import load_runtime_compatibility

REQUEST_ID = UUID("41900000-0000-4000-8000-000000000001")


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("runtime_type", [VllmRuntimeAdapter, SglangRuntimeAdapter])
def test_M4_19_TC_GW_45_typed_output_once_and_no_request_extension(
    stream: bool, runtime_type: type[VllmRuntimeAdapter]
) -> None:
    async def scenario() -> None:
        catalog = load_runtime_compatibility(Path("runtime-compat.yaml"))
        protocol = OpenAIProtocolAdapter(
            {name: runtime.request_capabilities for name, runtime in catalog.runtimes.items()},
            maximum_json_depth=32,
        )
        canonical = protocol.parse_request(
            json.dumps(
                {
                    "model": "public",
                    "messages": [{"role": "user", "content": "input"}],
                    "stream": stream,
                }
            ).encode(),
            runtime_name="vllm",
        )
        canonical = replace(
            canonical,
            extension=None,
            service_model=replace(canonical.service_model, runtime_model_name="internal"),
        )
        output = {
            "reasoning_content": "thought",
            "content": "answer",
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_1",
                    "function": {"name": "read", "arguments": '{"path":"a"}'},
                }
            ],
        }
        payload = {
            "model": "internal",
            "choices": [{"delta" if stream else "message": output, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 7},
        }
        wire = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()

        async def upstream(request: httpx.Request) -> httpx.Response:
            sent = json.loads(request.content)
            assert sent["model"] == "internal"
            assert sent["stream"] is True
            assert sent["stream_options"] == {"include_usage": True}
            upstream_wire = json.dumps(
                {
                    **payload,
                    "choices": [{"delta": output, "finish_reason": "tool_calls"}],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
            return httpx.Response(200, content=b"data: " + upstream_wire + b"\n\ndata: [DONE]\n\n")

        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            runtime = runtime_type(client, max_response_bytes=4096, max_sse_event_bytes=4096)
            events = tuple(
                [event async for event in runtime.stream("runtime:8000", REQUEST_ID, canonical)]
            )
        assert [e.text for e in events if isinstance(e, ThinkingDelta)] == ["thought"]
        assert [e.text for e in events if isinstance(e, ContentDelta)] == ["answer"]
        assert [e.name for e in events if isinstance(e, ToolCallStarted)] == ["read"]
        assert [e.text for e in events if isinstance(e, ToolArgumentsDelta)] == ['{"path":"a"}']
        meter = Mock(spec=RequestMetering)
        context = RequestRecordContext(
            REQUEST_ID, datetime.now(UTC), time.monotonic(), metering_request=meter
        )
        for event in events:
            context.observe_event(event)
        assert [call.args[0] for call in meter.observe_output.call_args_list] == [
            "thought",
            "answer",
        ]
        assert (context.runtime_input_tokens, context.runtime_output_tokens) == (4, 7)
        expected = wire.replace(b'"internal"', b'"public"')
        if not stream:
            # FIX-38: index belongs to SSE fragments, not final tool calls.
            expected = expected.replace(b'"index":0,', b"")
        if stream:
            assert (
                b"".join(protocol.encode_stream_event(e) or b"" for e in events)
                == b"data: " + expected + b"\n\ndata: [DONE]\n\n"
            )
        else:
            assert protocol.encode_non_stream(events) == expected

    asyncio.run(scenario())

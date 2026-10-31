"""FX-FIX-38: synthetic Runtime SSE; no platform or GPU is required."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from holm_domain import ErrorCode
from holm_gateway.anthropic_adapter import AnthropicProtocolAdapter
from holm_gateway.openai_adapter import OpenAIProtocolAdapter
from holm_gateway.request_context import RequestRecordContext
from holm_gateway.service_metrics import GatewayServiceMetrics
from holm_gateway.sglang_adapter import SglangRuntimeAdapter
from holm_gateway.vllm_adapter import VllmRuntimeAdapter
from holm_protocols import CanonicalEvent, ContentDelta, InferenceFailure, ThinkingDelta
from holm_runtimes import load_runtime_compatibility

REQUEST_ID = UUID("38000000-0000-4000-8000-000000000001")


def _protocol() -> OpenAIProtocolAdapter:
    catalog = load_runtime_compatibility(Path("runtime-compat.yaml"))
    return OpenAIProtocolAdapter(
        {name: runtime.request_capabilities for name, runtime in catalog.runtimes.items()},
        maximum_json_depth=32,
    )


def _frame(delta: dict[str, object], **fields: object) -> bytes:
    return (
        b"data: "
        + json.dumps(
            {
                "id": "chatcmpl-fx38",
                "object": "chat.completion.chunk",
                "created": 123,
                "model": "private",
                "choices": [{"index": 0, "delta": delta, **fields}],
            }
        ).encode()
        + b"\n\n"
    )


@pytest.mark.parametrize("runtime_type", [VllmRuntimeAdapter, SglangRuntimeAdapter])
@pytest.mark.parametrize("protocol_name", ["openai", "anthropic"])
def test_FIX_38_TC_GW_43_45_nonstream_assembles_and_observes_tokens_early(
    runtime_type: type[VllmRuntimeAdapter], protocol_name: str
) -> None:
    async def scenario() -> None:
        protocol = _protocol()
        anthropic = AnthropicProtocolAdapter(maximum_json_depth=32, maximum_response_bytes=16384)
        request = (protocol if protocol_name == "openai" else anthropic).parse_request(
            json.dumps(
                {
                    "model": "public",
                    "messages": [{"role": "user", "content": "FX38_INPUT"}],
                    "max_tokens": 32,
                    "stream": False,
                }
            ).encode(),
            runtime_name="vllm",
        )
        request = replace(
            request, service_model=replace(request.service_model, runtime_model_name="private")
        )
        context = RequestRecordContext(REQUEST_ID, datetime.now(UTC), time.monotonic())
        first_seen = asyncio.Event()
        release_tail = asyncio.Event()

        class Source(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self) -> AsyncIterator[bytes]:
                yield _frame({"role": "assistant", "content": None})
                yield _frame({"reasoning": "think "})
                await release_tail.wait()
                yield _frame({"reasoning": "twice"})
                yield _frame({"content": "答"})
                yield _frame({"content": "案"})
                yield _frame(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_38",
                                "type": "function",
                                "function": {"name": "read", "arguments": '{"path":'},
                            }
                        ]
                    }
                )
                yield _frame(
                    {"tool_calls": [{"index": 0, "function": {"arguments": '"a"}'}}]},
                    finish_reason="tool_calls",
                )
                yield (
                    b'data: {"choices":[],"usage":{"prompt_tokens":3,'
                    b'"completion_tokens":7,"total_tokens":10}}\n\ndata: [DONE]\n\n'
                )

            async def aclose(self) -> None:
                self.closed = True

        source = Source()

        async def upstream(sent: httpx.Request) -> httpx.Response:
            payload = json.loads(sent.content)
            assert payload["stream"] is True
            assert payload["stream_options"] == {"include_usage": True}
            assert payload["model"] == "private"
            return httpx.Response(200, stream=source)

        events: list[CanonicalEvent] = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            runtime = runtime_type(client, max_response_bytes=16384, max_sse_event_bytes=4096)

            async def consume() -> None:
                async for event in runtime.stream("runtime:8000", REQUEST_ID, request):
                    context.observe_event(event)
                    events.append(event)
                    if isinstance(event, ThinkingDelta):
                        first_seen.set()

            async with asyncio.timeout(3):
                task = asyncio.create_task(consume())
                await first_seen.wait()
                assert not task.done()
                first = context.first_token_seconds
                assert first is not None
                assert context.last_token_seconds == first
                await asyncio.sleep(0.06)
                release_tail.set()
                await task
        assert source.closed
        assert context.first_token_seconds == first
        assert context.last_token_seconds is not None
        assert context.last_token_seconds - first >= 0.02
        assert (context.runtime_input_tokens, context.runtime_output_tokens) == (3, 7)
        metrics = GatewayServiceMetrics()
        metrics.finished(
            REQUEST_ID,
            forwarded=False,
            status=None,
            http_status=200,
            error_code=None,
            duration_seconds=time.monotonic() - context.started_at,
            first_token_seconds=first,
            last_token_seconds=context.last_token_seconds,
            output_tokens=context.runtime_output_tokens,
        )
        sample = metrics.snapshot(sampled_at=datetime.now(UTC))[0]
        # A response buffered before observation would put TPOT in the first bucket.
        assert sample.tpot_buckets[0] == 0
        assert sample.tpot_buckets[-1] == 1
        assert [event.text for event in events if isinstance(event, ContentDelta)] == ["答", "案"]
        if protocol_name == "openai":
            assert json.loads(protocol.encode_non_stream(tuple(events))) == {
                "id": "chatcmpl-fx38",
                "object": "chat.completion",
                "created": 123,
                "model": "public",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "答案",
                            "reasoning_content": "think twice",
                            "tool_calls": [
                                {
                                    "id": "call_38",
                                    "type": "function",
                                    "function": {"name": "read", "arguments": '{"path":"a"}'},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 7, "total_tokens": 10},
            }
        else:
            result = json.loads(
                anthropic.bind(REQUEST_ID, request).encode_non_stream(tuple(events))
            )
            assert result["content"] == [
                {"type": "thinking", "thinking": "think twice", "signature": ""},
                {"type": "text", "text": "答案"},
                {"type": "tool_use", "id": "call_38", "name": "read", "input": {"path": "a"}},
            ]
            assert result["stop_reason"] == "tool_use"
            assert result["usage"] == {"input_tokens": 3, "output_tokens": 7}

    asyncio.run(scenario())


@pytest.mark.parametrize("runtime_type", [VllmRuntimeAdapter, SglangRuntimeAdapter])
def test_FIX_38_TC_GW_20_nonstream_total_limit_closes_upstream(
    runtime_type: type[VllmRuntimeAdapter], caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        request = _protocol().parse_request(
            b'{"model":"public","messages":[]}', runtime_name="vllm"
        )
        request = replace(
            request, service_model=replace(request.service_model, runtime_model_name="private")
        )

        class Source(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self) -> AsyncIterator[bytes]:
                for _ in range(20):
                    yield _frame({"content": "FX38_PRIVATE_OUTPUT"})

            async def aclose(self) -> None:
                self.closed = True

        source = Source()

        async def upstream(sent: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=source)

        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            runtime = runtime_type(client, max_response_bytes=500, max_sse_event_bytes=4096)
            with pytest.raises(InferenceFailure) as failure:
                _ = [event async for event in runtime.stream("runtime:8000", REQUEST_ID, request)]
            assert failure.value.error.code is ErrorCode.BACKEND_UNAVAILABLE
            assert "FX38_PRIVATE_OUTPUT" not in str(failure.value)
        assert source.closed

    asyncio.run(scenario())
    assert "FX38_PRIVATE_OUTPUT" not in caplog.text

"""M3-06 SGLang Adapter contract against the unchanged FX-RT Stub Runtime."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from fixtures.stub_runtime import RuntimeMode, RuntimeShape, StubRuntime, StubRuntimeConfig
from holm_domain import ErrorCode
from holm_gateway.openai_adapter import OpenAIProtocolAdapter
from holm_gateway.sglang_adapter import SglangRuntimeAdapter
from holm_gateway.vllm_adapter import VllmRuntimeAdapter
from holm_protocols import (
    CanonicalEvent,
    CanonicalRequest,
    Completed,
    ContentDelta,
    InferenceFailure,
    UsageReported,
)
from holm_runtimes import load_runtime_compatibility

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CATALOG = load_runtime_compatibility(REPOSITORY_ROOT / "runtime-compat.yaml")
VLLM = CATALOG.runtime("vllm")
SGLANG = CATALOG.runtime("sglang")
assert VLLM is not None
assert SGLANG is not None


def _protocol() -> OpenAIProtocolAdapter:
    return OpenAIProtocolAdapter(
        {
            "vllm": VLLM.request_capabilities,
            "sglang": SGLANG.request_capabilities,
        },
        maximum_json_depth=32,
    )


def _canonical(*, stream: bool) -> CanonicalRequest:
    request = _protocol().parse_request(
        json.dumps(
            {
                "model": "public-m3-06-model",
                "messages": [{"role": "user", "content": "M306_BODY_SENTINEL"}],
                "max_tokens": 3,
                "stream": stream,
            }
        ).encode(),
        runtime_name="sglang",
    )
    return replace(
        request,
        service_model=replace(
            request.service_model,
            runtime_model_name="holm-stub-model",
        ),
    )


async def _events(
    adapter: VllmRuntimeAdapter,
    endpoint: str,
    request_id: UUID,
    request: CanonicalRequest,
) -> tuple[CanonicalEvent, ...]:
    return tuple([event async for event in adapter.stream(endpoint, request_id, request)])


def _canonical_projection(events: tuple[CanonicalEvent, ...]) -> tuple[object, ...]:
    projection: list[object] = []
    for event in events:
        if isinstance(event, ContentDelta):
            projection.append((type(event).__name__, event.text))
        elif isinstance(event, UsageReported):
            projection.append(
                (
                    type(event).__name__,
                    event.usage.input_tokens,
                    event.usage.output_tokens,
                )
            )
        elif isinstance(event, Completed):
            projection.append((type(event).__name__, event.stop_reason))
        else:
            projection.append(type(event).__name__)
    return tuple(projection)


def test_M3_06_TC_RT_01_02_03_sglang_health_calls_usage_stop_and_shared_contract(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        config = StubRuntimeConfig(
            first_event_delay_seconds=0,
            event_interval_seconds=0,
            prompt_tokens=7,
            completion_tokens=3,
        )
        vllm_stub = StubRuntime(replace(config, runtime_shape=RuntimeShape.VLLM))
        sglang_stub = StubRuntime(replace(config, runtime_shape=RuntimeShape.SGLANG))
        vllm = VllmRuntimeAdapter(max_response_bytes=4096, max_sse_event_bytes=4096)
        sglang = SglangRuntimeAdapter(max_response_bytes=4096, max_sse_event_bytes=4096)
        protocol = _protocol()
        try:
            with vllm_stub, sglang_stub:
                assert await vllm.health(f"{vllm_stub.host}:{vllm_stub.port}")
                assert await sglang.health(f"{sglang_stub.host}:{sglang_stub.port}")
                for stream in (False, True):
                    request = _canonical(stream=stream)
                    vllm_events = await _events(
                        vllm,
                        f"{vllm_stub.host}:{vllm_stub.port}",
                        UUID("00000000-0000-7000-8000-000000000361"),
                        request,
                    )
                    sglang_events = await _events(
                        sglang,
                        f"{sglang_stub.host}:{sglang_stub.port}",
                        UUID("00000000-0000-7000-8000-000000000362"),
                        request,
                    )
                    assert _canonical_projection(sglang_events) == _canonical_projection(
                        vllm_events
                    )
                    usage = [
                        event.usage for event in sglang_events if isinstance(event, UsageReported)
                    ]
                    assert usage
                    assert usage[-1].input_tokens == 7
                    assert usage[-1].output_tokens == 3
                    if stream:
                        rendered = b"".join(
                            encoded
                            for event in sglang_events
                            if (encoded := protocol.encode_stream_event(event)) is not None
                        )
                    else:
                        rendered = protocol.encode_non_stream(sglang_events)
                    assert b"M306_BODY_SENTINEL" not in rendered
        finally:
            await vllm.aclose()
            await sglang.aclose()
        assert not vllm_stub.is_alive
        assert not sglang_stub.is_alive

    with caplog.at_level("INFO"):
        asyncio.run(scenario())
    assert all("M306_BODY_SENTINEL" not in record.getMessage() for record in caplog.records)


def test_M3_06_TC_RT_12_concurrent_sglang_request_ids_do_not_cross() -> None:
    async def scenario() -> None:
        config = StubRuntimeConfig(
            runtime_shape=RuntimeShape.SGLANG,
            mode=RuntimeMode.FX_RT_11,
            first_event_delay_seconds=0,
            event_interval_seconds=0.001,
            completion_tokens=3,
        )
        adapter = SglangRuntimeAdapter(max_response_bytes=4096, max_sse_event_bytes=4096)
        protocol = _protocol()
        request = _canonical(stream=True)
        request_ids = tuple(UUID(f"00000000-0000-7000-8000-{index:012d}") for index in range(1, 9))
        try:
            with StubRuntime(config) as runtime:
                endpoint = f"{runtime.host}:{runtime.port}"

                async def invoke(request_id: UUID) -> bytes:
                    events = await _events(adapter, endpoint, request_id, request)
                    return b"".join(
                        encoded
                        for event in events
                        if (encoded := protocol.encode_stream_event(event)) is not None
                    )

                rendered = await asyncio.gather(*(invoke(request_id) for request_id in request_ids))
        finally:
            await adapter.aclose()

        for request_id, body in zip(request_ids, rendered, strict=True):
            assert str(request_id).encode() in body
            assert all(
                str(other).encode() not in body for other in request_ids if other != request_id
            )

    asyncio.run(scenario())


def test_M3_06_TC_RT_10_sglang_error_is_frozen_and_sanitized(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "M306_RUNTIME_SECRET http://internal-runtime:30000 GPU-3"

    async def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request, text=sentinel)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            adapter = SglangRuntimeAdapter(
                client,
                max_response_bytes=4096,
                max_sse_event_bytes=4096,
            )
            with pytest.raises(InferenceFailure) as captured:
                await _events(
                    adapter,
                    "internal-runtime:30000",
                    UUID("00000000-0000-7000-8000-000000000363"),
                    _canonical(stream=False),
                )
            assert captured.value.error.code is ErrorCode.BACKEND_UNAVAILABLE
            assert sentinel not in str(captured.value)
            assert sentinel not in repr(captured.value)

    with caplog.at_level("INFO"):
        asyncio.run(scenario())
    assert all(sentinel not in record.getMessage() for record in caplog.records)

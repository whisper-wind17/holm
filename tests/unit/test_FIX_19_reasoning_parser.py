"""FIX-19: FX-FIX-19 uses certified catalogs and synthetic CPU-only model metadata."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID

import httpx
import pytest
from holm_gateway.metering import RequestMetering
from holm_gateway.openai_adapter import OpenAIProtocolAdapter
from holm_gateway.openai_types import (
    DataSseLine,
    JsonObject,
    OpenAIResponseExtension,
    OpenAISseExtension,
    freeze_json,
    thaw_json,
)
from holm_gateway.request_context import RequestRecordContext
from holm_gateway.vllm_adapter import VllmRuntimeAdapter
from holm_host_agent.runtime_process import RuntimeLaunchSpec, build_runtime_command
from holm_protocols import (
    CanonicalRequest,
    ResponseCompleted,
    SamplingParameters,
    ServiceModelReference,
    StreamStarted,
)
from holm_runtimes import RuntimeCompatibilityError, load_runtime_compatibility

ROOT = Path(__file__).resolve().parents[2]
QWEN = "Qwen3_5ForConditionalGeneration"


def _spec(runtime: str, model_path: Path) -> RuntimeLaunchSpec:
    return RuntimeLaunchSpec(
        instance_id="0199f7d8-22a0-7000-8000-000000000019",
        runtime_name=runtime,
        model_path=str(model_path),
        gpu_uuids=("GPU-FX-FIX-19",),
        tensor_parallel_size=1,
        context_length=4096,
        gpu_memory_utilization=0.8,
        served_model_name="mi_fix19",
        config_version="instance-1",
    )


@pytest.mark.parametrize("runtime_name", ["vllm", "sglang"])
@pytest.mark.parametrize(
    "architecture",
    [
        QWEN,
        "Qwen2ForCausalLM",
        "Qwen3ForCausalLM",
        "Qwen3MoeForCausalLM",
        "DeepseekV3ForCausalLM",
        "--trust-remote-code",
    ],
)
def test_FIX_19_launch_selects_only_catalog_parser(
    tmp_path: Path, runtime_name: str, architecture: str
) -> None:
    runtime = load_runtime_compatibility(ROOT / "runtime-compat.yaml").runtime(runtime_name)
    assert runtime is not None
    config = tmp_path / "config.json"
    config.write_text('{"architectures": []}')
    base = build_runtime_command(runtime, _spec(runtime_name, tmp_path), 38123)
    original = json.dumps({"architectures": [architecture]}).encode()
    config.write_bytes(original)
    command = build_runtime_command(runtime, _spec(runtime_name, tmp_path), 38123)
    assert config.read_bytes() == original
    if architecture == QWEN:
        tool_flags = (
            ("--enable-auto-tool-choice", "--tool-call-parser", "qwen3_coder")
            if runtime_name == "vllm"
            else ("--tool-call-parser", "qwen3_coder")
        )
        assert command == (*base, "--reasoning-parser", "qwen3", *tool_flags)
    else:
        assert command == base
        assert "--reasoning-parser" not in command
        assert "--trust-remote-code" not in command


@pytest.mark.parametrize("runtime_name,gptoss", [("vllm", "openai_gptoss"), ("sglang", "gpt-oss")])
def test_FIX_19_catalog_contains_all_certified_architectures(
    tmp_path: Path, runtime_name: str, gptoss: str
) -> None:
    runtime = load_runtime_compatibility(ROOT / "runtime-compat.yaml").runtime(runtime_name)
    assert runtime is not None
    assert dict(runtime.reasoning_parsers) == {
        "Qwen3_5ForCausalLM": "qwen3",
        "Qwen3_5MoeForCausalLM": "qwen3",
        "Qwen3_5ForConditionalGeneration": "qwen3",
        "Qwen3_5MoeForConditionalGeneration": "qwen3",
        "GptOssForCausalLM": gptoss,
    }
    for architecture, parser in runtime.reasoning_parsers.items():
        (tmp_path / "config.json").write_text(json.dumps({"architectures": [architecture]}))
        tool_flags = (
            (
                "--enable-auto-tool-choice",
                "--tool-call-parser",
                runtime.tool_call_parsers[architecture],
            )
            if runtime_name == "vllm"
            else ("--tool-call-parser", runtime.tool_call_parsers[architecture])
        )
        expected = ("--reasoning-parser", parser, *tool_flags)
        assert (
            build_runtime_command(runtime, _spec(runtime_name, tmp_path), 38123)[-len(expected) :]
            == expected
        )


@pytest.mark.parametrize("runtime_name", ["vllm", "sglang"])
@pytest.mark.parametrize(
    "payload",
    [
        None,
        b" " * (1024 * 1024 + 1),
        b"invalid FX-FIX-19-private",
        b"\xff",
        b"[]",
        b"{}",
        b'{"architectures":"Qwen3_5ForConditionalGeneration"}',
        b'{"architectures":["Qwen3_5ForConditionalGeneration", 1]}',
    ],
    ids=[
        "missing",
        "oversized",
        "invalid-json",
        "invalid-encoding",
        "non-object",
        "missing-field",
        "non-list",
        "non-string-member",
    ],
)
def test_FIX_19_invalid_config_warns_without_blocking_launch(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, runtime_name: str, payload: bytes | None
) -> None:
    runtime = load_runtime_compatibility(ROOT / "runtime-compat.yaml").runtime(runtime_name)
    assert runtime is not None
    if payload is not None:
        (tmp_path / "config.json").write_bytes(payload)
    command = build_runtime_command(runtime, _spec(runtime_name, tmp_path), 38123)
    assert "--reasoning-parser" not in command
    assert [record.getMessage() for record in caplog.records] == [
        "runtime_model_config_unavailable"
    ]
    assert str(tmp_path) not in caplog.text
    assert "FX-FIX-19-private" not in caplog.text


@pytest.mark.parametrize("runtime_index", [0, 1])
@pytest.mark.parametrize("parser", ["unregistered", "--trust-remote-code", 3, None, {}])
def test_FIX_19_catalog_rejects_unregistered_or_non_string_parser(
    tmp_path: Path, runtime_index: int, parser: object
) -> None:
    payload = json.loads((ROOT / "runtime-compat.yaml").read_text())
    payload["runtimes"][runtime_index]["reasoning_parsers"] = {QWEN: parser}
    path = tmp_path / "runtime-compat.yaml"
    path.write_text(json.dumps(payload))
    with pytest.raises(RuntimeCompatibilityError, match="reasoning_parsers"):
        load_runtime_compatibility(path)


def test_FIX_19_empty_parser_mapping_is_valid(tmp_path: Path) -> None:
    payload = json.loads((ROOT / "runtime-compat.yaml").read_text())
    for item in payload["runtimes"]:
        item["reasoning_parsers"] = {}
    path = tmp_path / "runtime-compat.yaml"
    path.write_text(json.dumps(payload))
    (tmp_path / "config.json").write_text(json.dumps({"architectures": [QWEN]}))
    for runtime in load_runtime_compatibility(path).runtimes.values():
        assert "--reasoning-parser" not in build_runtime_command(
            runtime, _spec(runtime.name, tmp_path), 1
        )


def _encode(payload: dict[str, object], stream: bool) -> bytes:
    frozen = freeze_json(payload)
    assert isinstance(frozen, JsonObject)
    catalog = load_runtime_compatibility(ROOT / "runtime-compat.yaml")
    adapter = OpenAIProtocolAdapter(
        {name: runtime.request_capabilities for name, runtime in catalog.runtimes.items()},
        maximum_json_depth=64,
    )
    if stream:
        result = adapter.encode_stream_event(
            StreamStarted(OpenAISseExtension((DataSseLine(frozen),), "public-model"))
        )
        assert result is not None
    else:
        result = adapter.encode_non_stream(
            (ResponseCompleted(OpenAIResponseExtension(frozen, "public-model")),)
        )
    assert thaw_json(frozen) == payload
    return result


@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize("already_present", [True, False])
def test_FIX_19_gateway_normalizes_reasoning_only(
    stream: bool, already_present: bool, caplog: pytest.LogCaptureFixture
) -> None:
    message: dict[str, object] = {"reasoning": "FX-FIX-19-private-thought", "content": "回答"}
    if already_present:
        message["reasoning_content"] = "existing thought"
    field = "delta" if stream else "message"
    payload: dict[str, object] = {
        "model": "mi_internal",
        "choices": [{"index": 0, field: message}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 9, "total_tokens": 14},
    }
    output = _encode(payload, stream)
    result = json.loads(output.removeprefix(b"data: ").strip())
    assert result == {
        "model": "public-model",
        "choices": [
            {
                "index": 0,
                field: {
                    "content": "回答",
                    "reasoning_content": "existing thought"
                    if already_present
                    else "FX-FIX-19-private-thought",
                },
            }
        ],
        "usage": payload["usage"],
    }
    assert not caplog.records


@pytest.mark.parametrize("stream", [True, False])
def test_FIX_19_no_reasoning_preserves_wire_bytes(stream: bool) -> None:
    field = "delta" if stream else "message"
    payload: dict[str, object] = {
        "model": "mi_internal",
        "choices": [{"index": 0, field: {"content": "回答"}}],
    }
    expected = json.dumps(
        {**payload, "model": "public-model"}, ensure_ascii=False, separators=(",", ":")
    ).encode()
    if stream:
        expected = b"data: " + expected + b"\n\n"
    assert _encode(payload, stream) == expected


@pytest.mark.parametrize("field", ["delta", "message"])
@pytest.mark.parametrize(
    "message,expected",
    [
        ({"reasoning": "思考", "content": "回答"}, "思考回答"),
        ({"reasoning_content": "思考", "content": None}, "思考"),
        ({"reasoning": "思考", "content": None}, "思考"),
        ({"reasoning": "duplicate", "reasoning_content": "思考", "content": "回答"}, "思考回答"),
        ({"content": "回答"}, "回答"),
        ({"content": None}, None),
    ],
)
def test_FIX_19_fallback_metering_includes_observed_reasoning(
    field: str, message: dict[str, object], expected: str | None
) -> None:
    payload: dict[str, object] = {"choices": [{"delta": message}]}

    async def scenario() -> None:
        # CLEAN-05: keep FX-FIX-19 inputs, observing the production metering boundary.
        stream = field == "delta"
        wire = json.dumps(payload, ensure_ascii=False).encode()
        request_id = UUID("19000000-0000-4000-8000-000000000005")
        request = CanonicalRequest(
            service_model=ServiceModelReference("public-model", "mi_internal"),
            stream=stream,
            sampling=SamplingParameters(),
            messages=(),
            tools=(),
        )

        def upstream(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"data: " + wire + b"\n\ndata: [DONE]\n\n")

        meter = Mock(spec=RequestMetering)
        context = RequestRecordContext(
            request_id, datetime.now(UTC), time.monotonic(), metering_request=meter
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            runtime = VllmRuntimeAdapter(client, max_response_bytes=4096, max_sse_event_bytes=4096)
            async for event in runtime.stream("runtime:8000", request_id, request):
                context.observe_event(event)
        chunks = [call.args[0] for call in meter.observe_output.call_args_list]
        assert ("".join(chunks) if chunks else None) == expected

    asyncio.run(scenario())

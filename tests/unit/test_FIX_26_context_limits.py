"""FX-FIX-26: vLLM observed errors and SGLang 0.5.13 source-derived errors."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from holm_control_api.deployment_options import derive_model_deployment_options
from holm_control_api.hugging_face_resolution import _context_length as hf_context
from holm_control_api.modelscope_resolution import _context_length as ms_context
from holm_domain import Identified, Unrecognized
from holm_host_agent.runtime_process import _detect_out_of_memory
from holm_runtimes import load_runtime_compatibility
from test_M3_10_deployment_options import _model
from test_M4_06_openai_validator import _application, _route

BASE = {"model": "qwen-mini", "messages": [{"role": "user", "content": "hello"}]}


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "runtime,param,message,limit",
    [
        (
            "vllm",
            "input_tokens",
            "This model's maximum context length is 8192 tokens. However PRIVATE",
            8192,
        ),
        (
            "vllm",
            "max_tokens",
            "max_tokens=32000 cannot be greater than max_model_len=max_total_tokens=8192. PRIVATE",
            8192,
        ),
        ("vllm", "max_completion_tokens", "max_model_len=8192 PRIVATE", 8192),
        # WorkBuddy test replay: vLLM's character pre-check rejects before tokenizing.
        (
            "vllm",
            "input_text",
            "This model's maximum context length is 32768 tokens. However, you requested "
            "32768 output tokens and your prompt contains 157579 characters PRIVATE",
            32768,
        ),
        ("vllm", "input_tokens", "PRIVATE", None),
        (
            "sglang",
            None,
            "Requested token count exceeds the model's maximum context length "
            "of 8192 tokens. PRIVATE",
            8192,
        ),
        (
            "sglang",
            None,
            "The input (9000 tokens) is longer than the model's context length (8192 tokens).",
            8192,
        ),
    ],
)
def test_FIX_26_TC_GW_34_context_response(stream, runtime, param, message, limit):
    error = {"message": message, "param": param, "type": "BadRequestError", "code": 400}
    body = {"error": error} if runtime == "vllm" else {"object": "error", **error}
    response = _call(body, stream=stream, runtime=runtime)
    assert response.status_code == 400
    result = response.json()["error"]
    assert result["code"] == "invalid_request"
    assert result["details"] == {
        "reason": "context_length_exceeded",
        **({"max_context_tokens": limit} if limit else {}),
    }
    suffix = f"（上限 {limit} tokens）" if limit else ""
    assert (
        result["message"]
        == f"请求超出模型上下文长度{suffix}，本次调用未执行，请缩短输入或减小 max_tokens 后重试"
    )
    for secret in ("PRIVATE", "However", "BadRequestError"):
        assert secret not in response.text


class Body(httpx.AsyncByteStream):
    def __init__(self, content):
        self.content = content
        self.closed = False
        self.reads = 0

    async def __aiter__(self):
        self.reads += 1
        if isinstance(self.content, Exception):
            raise self.content
        yield self.content

    async def aclose(self):
        self.closed = True


def _call(body, *, stream=False, runtime="vllm", status=400, fields=None):
    content = body if isinstance(body, (bytes, Exception)) else json.dumps(body).encode()
    source = Body(content)

    async def upstream(request):
        return httpx.Response(status, stream=source)

    async def scenario():
        catalog = load_runtime_compatibility(Path("runtime-compat.yaml"))
        app, key, client = _application(
            upstream,
            route=replace(_route(), runtime_name=runtime),
            capabilities=catalog.runtime(runtime).request_capabilities,
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as gateway:
                response = await gateway.post(
                    "/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={**BASE, "stream": stream, **(fields or {})},
                )
            if not fields:
                assert source.closed
                assert source.reads == (1 if status == 400 else 0)
            return response
        finally:
            await client.aclose()

    return asyncio.run(scenario())


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"x" * 16385,
        {"error": {"param": "other"}},
        {"error": {"param": []}},
        httpx.ReadError("PRIVATE"),
        b'{"error":{"param":"input_tokens"},"pad":"' + b"x" * 16384 + b'"}',
    ],
)
def test_FIX_26_TC_GW_34_generic_fallback(body):
    response = _call(body)
    assert response.status_code == 400
    assert response.json()["error"]["details"] == {}
    assert response.json()["error"]["message"].startswith("请求格式无效")


@pytest.mark.parametrize("status", [422, 500])
def test_FIX_26_TC_GW_34_other_status_never_reads_body(status):
    response = _call(b"PRIVATE", status=status)
    assert response.status_code == (400 if status == 422 else 503)


def test_FIX_26_TC_GW_34_unsupported_names_share_bounded_source():
    names = [f"z{i}" + "x" * 70 for i in range(10)]
    response = _call({}, fields=dict.fromkeys(names, "PRIVATE"))
    result = response.json()["error"]
    expected = [name[:64] for name in sorted(names)[:8]]
    assert result["details"]["unsupported_fields"] == expected
    assert (
        result["message"]
        == f"请求包含不支持的字段：{', '.join(expected)}，本次调用未执行，请去掉这些字段后重试"
    )
    assert "PRIVATE" not in response.text


@pytest.mark.parametrize("resolver", [hf_context, ms_context])
@pytest.mark.parametrize(
    "config,expected",
    [
        ({"text_config": {"max_position_embeddings": 262144}}, Identified(262144)),
        (
            {"max_position_embeddings": 32768, "text_config": {"max_position_embeddings": 262144}},
            Identified(32768),
        ),
        ({}, Unrecognized()),
        ({"text_config": {"max_position_embeddings": True}}, Unrecognized()),
        ({"text_config": {"max_position_embeddings": -1}}, Unrecognized()),
        (
            {"max_position_embeddings": None, "text_config": {"max_position_embeddings": 262144}},
            Unrecognized(),
        ),
    ],
)
def test_FIX_26_nested_context(resolver, config, expected):
    assert resolver(config) == expected


@pytest.mark.parametrize("runtime", ["vllm", "sglang"])
@pytest.mark.parametrize("limit", [32768, 262144])
def test_FIX_26_context_steps(runtime, limit):
    model = replace(_model(limit), supported_runtimes=(runtime,))
    options = derive_model_deployment_options(
        model, load_runtime_compatibility(Path("runtime-compat.yaml")), discovered_gpu_count=1
    )
    assert options.runtimes[0].context_lengths == tuple(
        n for n in (2048, 4096, 8192, 16384, 32768, 65536, 131072, 262144) if n <= limit
    )
    assert options.runtimes[0].default_context_length == 4096


@pytest.mark.parametrize("runtime", ["vllm", "sglang"])
def test_FIX_26_kv_oom_signature(tmp_path, runtime):
    entry = load_runtime_compatibility(Path("runtime-compat.yaml")).runtime(runtime)
    log = tmp_path / "runtime.log"
    log.write_text(
        "To serve at least one request, 40 GiB KV cache is needed, "
        "which is larger than the available KV cache memory"
    )
    assert _detect_out_of_memory(
        log, exit_code=1, signatures=entry.failure_signatures.out_of_memory
    )


def test_FIX_26_TC_GW_34_read_stops_at_body_bound_and_accepts_exact_limit():
    from holm_gateway.vllm_adapter import _read_error_body

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * 16384
            yield b"x"
            pytest.fail("read continued after error body limit")

    async def scenario():
        response = httpx.Response(400, stream=Chunks())
        assert await _read_error_body(response) is None
        await response.aclose()
        body = b'{"error":{"param":"input_tokens"}}'
        response = httpx.Response(400, stream=Body(body + b" " * (16384 - len(body))))
        assert await _read_error_body(response) == {"error": {"param": "input_tokens"}}
        await response.aclose()

    asyncio.run(scenario())

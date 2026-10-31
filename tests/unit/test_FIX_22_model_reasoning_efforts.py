"""FX-FIX-22: M4-06 authenticated HTTP fixture with CPU-only reasoning policies."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import httpx
import pytest
from holm_gateway.__main__ import _reasoning_models
from holm_gateway.application import create_app
from holm_gateway.routing import CachedRouteDirectory
from holm_protocols import ModelReasoningPolicy, ReasoningEffort
from holm_runtimes import RuntimeCompatibilityError, load_runtime_compatibility
from holm_runtimes.compatibility import REASONING_EFFORTS
from test_M4_06_openai_validator import (
    VLLM_CAPABILITIES,
    _authenticator,
    _route,
    _RouteSource,
)

CATALOG_PATH = Path(__file__).resolve().parents[2] / "runtime-compat.yaml"
QWEN_ARCHITECTURES = (
    "Qwen3_5ForCausalLM",
    "Qwen3_5MoeForCausalLM",
    "Qwen3_5ForConditionalGeneration",
    "Qwen3_5MoeForConditionalGeneration",
)


POLICIES = {
    **_reasoning_models(load_runtime_compatibility(CATALOG_PATH)),
    # Sparse policy exercises a valid enum that neither efforts nor aliases accepts.
    "FXRestricted": ModelReasoningPolicy((ReasoningEffort.LOW,), MappingProxyType({}), True),
}


def test_FIX_22_catalog_efforts_match_protocol_enum() -> None:
    assert {effort.value for effort in ReasoningEffort} == REASONING_EFFORTS


def test_FIX_22_catalog_declares_exact_initial_model_policies() -> None:
    catalog = load_runtime_compatibility(CATALOG_PATH)
    assert set(catalog.reasoning_models) == {*QWEN_ARCHITECTURES, "GptOssForCausalLM"}
    for architecture in QWEN_ARCHITECTURES:
        policy = catalog.reasoning_models[architecture]
        assert policy.efforts == ("xhigh", "medium", "low")
        assert dict(policy.aliases) == {"high": "xhigh", "max": "xhigh", "minimal": "low"}
        assert policy.can_disable is True
    policy = catalog.reasoning_models["GptOssForCausalLM"]
    assert policy.efforts == ("low", "medium", "high")
    assert dict(policy.aliases) == {"minimal": "low", "xhigh": "high", "max": "high"}
    assert policy.can_disable is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("efforts", ["low", "unknown"]),
        ("efforts", ["none"]),
        ("efforts", ["low", "low"]),
        ("efforts", []),
        ("efforts", "low"),
        ("efforts", [1]),
        ("aliases", {"high": "medium"}),
        ("aliases", {"high": "unknown"}),
        ("aliases", {"unknown": "low"}),
        ("aliases", {"high": "none"}),
        ("aliases", []),
        ("can_disable", "false"),
        ("can_disable", 1),
    ],
)
def test_FIX_22_invalid_reasoning_declaration_fails_loading(
    tmp_path: Path, field: str, value: object
) -> None:
    document = json.loads(CATALOG_PATH.read_text())
    policy = {"efforts": ["low"], "aliases": {}, "can_disable": True}
    policy[field] = value
    document["reasoning_models"] = {"FXRestricted": policy}
    path = tmp_path / "FX-FIX-22-invalid.json"
    path.write_text(json.dumps(document))
    with pytest.raises(RuntimeCompatibilityError, match="reasoning_models"):
        load_runtime_compatibility(path)


@pytest.mark.parametrize("value", [None, [], {"FXRestricted": {}}, {"FXRestricted": None}])
def test_FIX_22_malformed_reasoning_models_fail_loading(tmp_path: Path, value: object) -> None:
    document = json.loads(CATALOG_PATH.read_text())
    document["reasoning_models"] = value
    path = tmp_path / "FX-FIX-22-malformed.json"
    path.write_text(json.dumps(document))
    with pytest.raises(RuntimeCompatibilityError, match="reasoning_models"):
        load_runtime_compatibility(path)


def test_FIX_22_legacy_catalog_has_no_model_overrides(tmp_path: Path) -> None:
    document = json.loads(CATALOG_PATH.read_text())
    del document["reasoning_models"]
    path = tmp_path / "FX-FIX-22-legacy.json"
    path.write_text(json.dumps(document))
    assert _reasoning_models(load_runtime_compatibility(path)) == {}


@pytest.mark.parametrize("runtime_name", ("vllm", "sglang"))
@pytest.mark.parametrize("stream", (False, True))
@pytest.mark.parametrize(
    "architecture,effort,expected",
    [
        *(
            (architecture, effort, expected)
            for architecture in QWEN_ARCHITECTURES
            for effort, expected in (
                ("high", "xhigh"),
                ("max", "xhigh"),
                ("minimal", "low"),
                ("low", "low"),
                ("medium", "medium"),
                ("xhigh", "xhigh"),
                ("none", "none"),
                (None, None),
            )
        ),
        *(
            ("GptOssForCausalLM", effort, expected)
            for effort, expected in (
                ("minimal", "low"),
                ("xhigh", "high"),
                ("max", "high"),
                ("low", "low"),
                ("medium", "medium"),
                ("high", "high"),
                ("none", 400),
                (None, None),
            )
        ),
        *(
            (architecture, effort.value, effort.value)
            for architecture in ("Qwen2ForCausalLM", None)
            for effort in ReasoningEffort
        ),
        ("FXRestricted", "high", 400),
    ],
)
def test_FIX_22_reasoning_policy_before_runtime(
    runtime_name: str,
    stream: bool,
    architecture: str | None,
    effort: str | None,
    expected: str | int | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
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
        return httpx.Response(
            200,
            content=b"data: "
            + json.dumps({"choices": [{"delta": {"content": "ok"}}]}).encode()
            + b"\n\ndata: [DONE]\n\n",
        )

    async def scenario() -> None:
        authenticator, key = _authenticator()
        route = replace(_route(), runtime_name=runtime_name, model_architecture=architecture)
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as runtime:
            app = create_app(
                authenticator=authenticator,
                routes=CachedRouteDirectory(_RouteSource(route), ttl_seconds=2),
                runtime_capabilities={runtime_name: VLLM_CAPABILITIES},
                reasoning_models=POLICIES,
                upstream_client=runtime,
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as gateway:
                response = await gateway.post(
                    "/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": "qwen-mini",
                        "messages": [{"role": "user", "content": "FX22_BODY_SENTINEL"}],
                        "stream": stream,
                        "reasoning_effort": effort,
                    },
                )
        if expected == 400:
            assert response.status_code == 400
            error = response.json()["error"]
            assert error["code"] == "invalid_request"
            assert error["request_id"]
            assert error["details"] == {
                "supported_reasoning_efforts": (
                    ["low", "none"] if architecture == "FXRestricted" else ["low", "medium", "high"]
                )
            }
            assert captured == []
            for forbidden in (
                "FX22_BODY_SENTINEL",
                "private-runtime",
                "mi_000000004606",
                architecture,
            ):
                assert forbidden not in response.text
        else:
            assert response.status_code == 200
            assert len(captured) == 1
            if expected is None:
                assert "reasoning_effort" not in captured[0]
            else:
                assert captured[0]["reasoning_effort"] == expected
            assert "chat_template_kwargs" not in captured[0]
        assert "FX22_BODY_SENTINEL" not in caplog.text
        assert key not in caplog.text

    asyncio.run(scenario())

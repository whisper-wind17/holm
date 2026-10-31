"""M3-06 manifest-driven SGLang argv and fail-loud Adapter selection."""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
from collections.abc import AsyncGenerator, Iterable
from pathlib import Path
from uuid import UUID

import pytest
from holm_domain import ErrorCode, ModelInstanceStatus, RuntimeType
from holm_gateway.routing import GatewayRoute, InferenceRouteDirectory
from holm_gateway.runtime_adapter_registry import RuntimeAdapterRegistry
from holm_host_agent.runtime_process import RuntimeLaunchSpec, build_runtime_command
from holm_protocols import (
    CanonicalEvent,
    CanonicalRequest,
    InferenceFailure,
    ResponseStarted,
    SamplingParameters,
    ServiceModelReference,
)
from holm_runtimes import (
    RuntimeCompatibilityError,
    load_runtime_compatibility,
    validate_runtime_launch_policy,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
REQUEST_ID = UUID("00000000-0000-7000-8000-000000000306")


def _spec(runtime_name: str) -> RuntimeLaunchSpec:
    return RuntimeLaunchSpec(
        instance_id="00000000-0000-7000-8000-000000000306",
        runtime_name=runtime_name,
        model_path="/models/managed/qwen",
        gpu_uuids=("GPU-00000000-0000-0000-0000-000000000306",),
        tensor_parallel_size=1,
        context_length=4096,
        gpu_memory_utilization=0.9,
        served_model_name="mi_m306sglang",
        config_version="m3-06-v1",
    )


def _route(runtime_name: str, endpoint: str) -> GatewayRoute:
    return GatewayRoute(
        service_id=UUID("00000000-0000-7000-8000-000000000316"),
        instance_id=UUID("00000000-0000-7000-8000-000000000326"),
        public_model_name="m3-06-model",
        enabled=True,
        instance_status=ModelInstanceStatus.RUNNING.value,
        runtime_name=runtime_name,
        internal_endpoint=endpoint,
        internal_model_name="mi_m306sglang",
    )


def _canonical_request() -> CanonicalRequest:
    return CanonicalRequest(
        service_model=ServiceModelReference("m3-06-model", "mi_m306sglang"),
        stream=False,
        sampling=SamplingParameters(),
        messages=(),
        tools=(),
    )


class _RecordingAdapter:
    def __init__(self) -> None:
        self.stream_endpoints: list[str] = []
        self.synced_routes: tuple[GatewayRoute, ...] = ()
        self.closed = False

    async def health(self, endpoint: str) -> bool:
        self.stream_endpoints.append(f"health:{endpoint}")
        return True

    async def sync_routes(self, routes: Iterable[GatewayRoute]) -> None:
        self.synced_routes = tuple(routes)

    async def aclose(self) -> None:
        self.closed = True

    async def stream(
        self,
        endpoint: str,
        request_id: UUID,
        canonical_request: CanonicalRequest,
    ) -> AsyncGenerator[CanonicalEvent, None]:
        del request_id, canonical_request
        self.stream_endpoints.append(endpoint)
        yield ResponseStarted()


class _Routes:
    def __init__(self, routes: tuple[GatewayRoute, ...]) -> None:
        self._routes = routes

    async def find(self, public_model_name: str) -> GatewayRoute | None:
        return next(
            (route for route in self._routes if route.public_model_name == public_model_name),
            None,
        )

    async def list_enabled(self) -> tuple[GatewayRoute, ...]:
        return tuple(route for route in self._routes if route.enabled)


def _has_runtime_name_control_flow(source: str) -> bool:
    tree = ast.parse(source)
    runtime_names = {"vllm", "sglang"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.IfExp, ast.While)):
            constants = {
                value.value
                for value in ast.walk(node.test)
                if isinstance(value, ast.Constant) and isinstance(value.value, str)
            }
            if constants & runtime_names:
                return True
        if isinstance(node, ast.Match):
            constants = {
                value.value
                for case in node.cases
                for value in ast.walk(case.pattern)
                if isinstance(value, ast.Constant) and isinstance(value.value, str)
            }
            if constants & runtime_names:
                return True
    return False


def test_M3_06_TC_RT_01_manifest_drives_distinct_argv_without_changing_vllm() -> None:
    catalog = load_runtime_compatibility(REPOSITORY_ROOT / "runtime-compat.yaml")
    vllm = catalog.runtime(RuntimeType.VLLM.value)
    sglang = catalog.runtime(RuntimeType.SGLANG.value)
    assert vllm is not None
    assert sglang is not None
    assert sglang.gpu_policy.certified_gpu_counts == frozenset({1, 2, 4})
    assert sglang.gpu_policy.max_gpus_per_instance == "auto"
    assert (
        validate_runtime_launch_policy(
            sglang,
            selected_gpu_count=3,
            context_length=4096,
            tensor_parallel_size=3,
        )
        is None
    )
    assert sglang.tp_policy.size_source == "selected_gpu_count"
    assert sglang.tp_policy.model_partition_check == "runtime_adapter"
    # FIX-26 expands the approved catalog; retain exact-value coverage.
    assert sglang.context_policy.allowed_steps == (
        2048,
        4096,
        8192,
        16384,
        32768,
        65536,
        131072,
        262144,
    )
    assert sglang.context_policy.default == 4096

    assert build_runtime_command(vllm, _spec("vllm"), 38_123) == (
        "/opt/holm-runtimes/vllm/bin/vllm",
        "serve",
        "/models/managed/qwen",
        "--host",
        "127.0.0.1",
        "--port",
        "38123",
        "--tensor-parallel-size",
        "1",
        "--max-model-len",
        "4096",
        "--gpu-memory-utilization",
        "0.9",
        "--served-model-name",
        "mi_m306sglang",
        "--no-enable-log-requests",
    )
    sglang_command = build_runtime_command(sglang, _spec("sglang"), 38_124)
    assert sglang_command == (
        "/opt/holm-runtimes/sglang/bin/sglang",
        "serve",
        "--model-path",
        "/models/managed/qwen",
        "--host",
        "127.0.0.1",
        "--port",
        "38124",
        "--tp-size",
        "1",
        "--context-length",
        "4096",
        "--mem-fraction-static",
        "0.9",
        "--served-model-name",
        "mi_m306sglang",
        "--enable-metrics",
    )
    assert "--log-requests" not in sglang_command
    assert "--trust-remote-code" not in sglang_command


@pytest.mark.parametrize(
    ("enable_metrics", "expected"),
    (
        ("--ENABLE-metrics", "arguments.enable_metrics is invalid"),
        ("enable-metrics", "arguments.enable_metrics is invalid"),
    ),
)
def test_M6_04A_metrics_flag_uses_existing_safe_flag_guard(
    tmp_path: Path,
    enable_metrics: str,
    expected: str,
) -> None:
    payload = json.loads((REPOSITORY_ROOT / "runtime-compat.yaml").read_text(encoding="utf-8"))
    payload["runtimes"][1]["arguments"]["enable_metrics"] = enable_metrics
    path = tmp_path / "runtime-compat.yaml"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeCompatibilityError, match=expected):
        load_runtime_compatibility(path)


def test_M6_04A_metrics_flag_is_required_even_when_runtime_uses_null(tmp_path: Path) -> None:
    payload = json.loads((REPOSITORY_ROOT / "runtime-compat.yaml").read_text(encoding="utf-8"))
    del payload["runtimes"][0]["arguments"]["enable_metrics"]
    path = tmp_path / "runtime-compat.yaml"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeCompatibilityError, match="arguments keys must contain"):
        load_runtime_compatibility(path)


def test_M3_06_TC_RT_01_runtime_name_branch_guard_has_positive_control() -> None:
    from holm_host_agent import runtime_process

    assert _has_runtime_name_control_flow("if runtime.name == 'sglang':\n    pass\n")
    source = inspect.getsource(runtime_process.build_runtime_command)
    assert not _has_runtime_name_control_flow(source)


def test_M3_06_TC_RT_01_changing_manifest_flag_changes_argv_without_code_change(
    tmp_path: Path,
) -> None:
    payload = json.loads((REPOSITORY_ROOT / "runtime-compat.yaml").read_text(encoding="utf-8"))
    payload["runtimes"][1]["arguments"]["tensor_parallel_size"] = "--tp"
    path = tmp_path / "runtime-compat.yaml"
    path.write_text(json.dumps(payload), encoding="utf-8")
    runtime = load_runtime_compatibility(path).runtime("sglang")
    assert runtime is not None

    command = build_runtime_command(runtime, _spec("sglang"), 38_124)
    assert "--tp" in command
    assert "--tp-size" not in command


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ({"model_path": "model path"}, "arguments.model_path is invalid"),
        ({"raw_arguments": "--arbitrary value"}, "keys must contain"),
    ),
)
def test_M3_06_TC_RT_10_argument_schema_rejects_unsafe_or_unknown_fields(
    tmp_path: Path,
    mutation: dict[str, object],
    expected: str,
) -> None:
    payload = json.loads((REPOSITORY_ROOT / "runtime-compat.yaml").read_text(encoding="utf-8"))
    arguments = payload["runtimes"][1]["arguments"]
    arguments.update(mutation)
    path = tmp_path / "runtime-compat.yaml"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeCompatibilityError, match=expected):
        load_runtime_compatibility(path)


def test_M3_06_registry_selects_by_route_data_and_syncs_each_runtime_only() -> None:
    async def scenario() -> None:
        vllm = _RecordingAdapter()
        sglang = _RecordingAdapter()
        registry = RuntimeAdapterRegistry({"vllm": vllm, "sglang": sglang})
        routes = (_route("vllm", "vllm:8000"), _route("sglang", "sglang:30000"))

        await registry.sync_routes(routes)
        for route in routes:
            directory = InferenceRouteDirectory(_Routes((route,)), registry.bind_route)
            selected = await directory.find(route.public_model_name)
            assert selected is not None
            tuple(
                [
                    event
                    async for event in registry.stream(
                        selected.endpoint or "",
                        REQUEST_ID,
                        _canonical_request(),
                    )
                ]
            )

        assert vllm.stream_endpoints == ["vllm:8000"]
        assert sglang.stream_endpoints == ["sglang:30000"]
        assert tuple(route.runtime_name for route in vllm.synced_routes) == ("vllm",)
        assert tuple(route.runtime_name for route in sglang.synced_routes) == ("sglang",)
        await registry.aclose()
        assert vllm.closed and sglang.closed

    asyncio.run(scenario())


def test_M3_06_unknown_runtime_fails_loud_without_vllm_fallback() -> None:
    async def scenario() -> None:
        adapter = _RecordingAdapter()
        registry = RuntimeAdapterRegistry({"vllm": adapter})
        directory = InferenceRouteDirectory(
            _Routes((_route("unknown-runtime", "unknown:9000"),)),
            registry.bind_route,
        )

        with pytest.raises(InferenceFailure) as captured:
            await directory.find("m3-06-model")
        assert captured.value.error.code is ErrorCode.BACKEND_UNAVAILABLE
        assert adapter.stream_endpoints == []

    asyncio.run(scenario())

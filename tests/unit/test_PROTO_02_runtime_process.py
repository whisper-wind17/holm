"""PROTO-02 command composition and stable Runtime request refusals."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest
from fixtures.fake_nvml import FixtureId, scenario_for
from holm_contracts.external_paths import ExternalPathPolicy
from holm_host_agent.path_validation import RuntimeIdentity
from holm_host_agent.runtime_process import (
    RuntimeLaunchSpec,
    RuntimeManager,
    RuntimeRequestError,
    build_runtime_command,
    build_runtime_environment,
)
from holm_runtimes import RuntimeCompatibilityError, load_runtime_compatibility

GPU_UUID: Final[str] = "GPU-00000000-0000-0002-0000-000000000001"
INSTANCE_ID: Final[str] = "0199f7d8-22a0-7000-8000-000000000002"
RUNTIME_IDENTITY: Final = RuntimeIdentity(uid=65_534, gid=65_534)


def _catalog(executable: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "runtimes": [
            {
                "name": "vllm",
                "version": "0.29.0",
                "launcher": "process",
                "executable": str(executable),
                "supported_architectures": ["Qwen2ForCausalLM"],
                "reasoning_parsers": {},
                "tool_call_parsers": {},
                "request_capabilities": {"openai_chat_completions": ["messages", "model"]},
                "gpu_memory_utilization": {"minimum": 0.1, "maximum": 0.95},
                "gpu_policy": {
                    "certified_gpu_counts": [1, 2, 4],
                    "max_gpus_per_instance": 4,
                    "require_homogeneous_devices": True,
                    "p2p_topology": "preferred",
                },
                "tp_policy": {
                    "size_source": "selected_gpu_count",
                    "model_partition_check": "runtime_adapter",
                },
                "context_policy": {"allowed_steps": [2048, 4096], "default": 2048},
                "failure_signatures": {
                    "out_of_memory": ["CUDA out of memory", "torch.OutOfMemoryError"]
                },
                "arguments": {
                    "reasoning_parser": "--reasoning-parser",
                    "tool_call_parser": "--tool-call-parser",
                    "enable_auto_tool_choice": None,
                    "serve": "serve",
                    "host": "--host",
                    "port": "--port",
                    "tensor_parallel_size": "--tensor-parallel-size",
                    "max_model_len": "--max-model-len",
                    "gpu_memory_utilization": "--gpu-memory-utilization",
                    "served_model_name": "--served-model-name",
                    "disable_request_logging": "--disable-log-requests",
                    "enable_metrics": None,
                },
            }
        ],
    }


def _write_catalog(path: Path, executable: Path) -> Path:
    path.write_text(json.dumps(_catalog(executable)), encoding="utf-8")
    return path


def _tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    os.chmod(tmp_path, 0o755)
    executable = tmp_path / "vllm"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    model_root = tmp_path / "models"
    model = model_root / "model"
    model.mkdir(parents=True)
    model_root.chmod(0o755)
    model.chmod(0o755)
    config = _write_catalog(tmp_path / "runtime-compat.yaml", executable)
    return config, model_root, model


def _spec(model: Path) -> RuntimeLaunchSpec:
    return RuntimeLaunchSpec(
        instance_id=INSTANCE_ID,
        runtime_name="vllm",
        model_path=str(model),
        gpu_uuids=(GPU_UUID,),
        tensor_parallel_size=1,
        context_length=4096,
        gpu_memory_utilization=0.9,
        served_model_name="mi_proto02",
        config_version="proto-02-v1",
    )


def _manager(
    tmp_path: Path,
    config: Path,
    model_root: Path,
    *,
    identity: RuntimeIdentity | None = RUNTIME_IDENTITY,
    fixture_id: FixtureId = FixtureId.FX_NV_02,
) -> RuntimeManager:
    return RuntimeManager(
        compatibility_path=config,
        state_root=tmp_path / "state",
        model_path_policy=ExternalPathPolicy.for_configured_roots((model_root,)),
        runtime_identity=identity,
        nvml_factory=lambda: scenario_for(fixture_id).open(),
    )


def test_PROTO_02_catalog_and_command_are_closed_and_never_enable_remote_code(
    tmp_path: Path,
) -> None:
    config, _model_root, model = _tree(tmp_path)
    runtime = load_runtime_compatibility(config).runtime("vllm")
    assert runtime is not None
    command = build_runtime_command(runtime, _spec(model), 38_123)

    assert command == (
        str(runtime.executable),
        "serve",
        str(model),
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
        "mi_proto02",
        "--disable-log-requests",
    )
    assert "--trust-remote-code" not in command
    assert build_runtime_environment(runtime, _spec(model), tmp_path / "home") == {
        "CUDA_VISIBLE_DEVICES": GPU_UUID,
        "HF_HUB_OFFLINE": "1",
        "HOME": str(tmp_path / "home"),
        "PATH": (
            f"{runtime.executable.parent}:"
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        ),
    }

    unsafe = _catalog(runtime.executable)
    runtime_entry = unsafe["runtimes"][0]  # type: ignore[index]
    runtime_entry["arguments"]["disable_request_logging"] = "--trust-remote-code"  # type: ignore[index]
    config.write_text(json.dumps(unsafe), encoding="utf-8")
    with pytest.raises(RuntimeCompatibilityError):
        load_runtime_compatibility(config)


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"runtime_name": "shell"}, "runtime_not_allowed"),
        ({"tensor_parallel_size": 2}, "gpu_count_mismatch"),
        ({"gpu_uuids": (GPU_UUID, GPU_UUID), "tensor_parallel_size": 2}, "gpu_set_invalid"),
        (
            {
                "gpu_uuids": ("GPU-one", "GPU-two", "GPU-three"),
                "tensor_parallel_size": 3,
            },
            "gpu_not_found",
        ),
        (
            {
                "gpu_uuids": ("GPU-one", "GPU-two", "GPU-three", "GPU-four", "GPU-five"),
                "tensor_parallel_size": 5,
            },
            "gpu_count_exceeds_maximum",
        ),
        ({"gpu_uuids": ("GPU-not-present",)}, "gpu_not_found"),
        ({"served_model_name": "../../unsafe"}, "served_model_name_invalid"),
        ({"context_length": 3000}, "context_length_not_allowed"),
        ({"gpu_memory_utilization": 0.99}, "gpu_memory_utilization_out_of_range"),
        ({"config_version": "bad version"}, "config_version_invalid"),
    ],
)
def test_PROTO_02_typed_launch_rejects_each_invalid_value_with_stable_reason(
    tmp_path: Path,
    change: dict[str, object],
    expected: str,
) -> None:
    config, model_root, model = _tree(tmp_path)
    manager = _manager(tmp_path, config, model_root)

    with pytest.raises(RuntimeRequestError) as captured:
        asyncio.run(manager.ensure(replace(_spec(model), **change)))
    assert captured.value.code == expected


def test_M3_03_TC_GPU_03_host_agent_rejects_heterogeneous_selected_devices(
    tmp_path: Path,
) -> None:
    config, model_root, model = _tree(tmp_path)
    payload = _catalog(config.parent / "vllm")
    runtime_entry = payload["runtimes"][0]  # type: ignore[index]
    runtime_entry["gpu_policy"] = {  # type: ignore[index]
        "certified_gpu_counts": [2],
        "max_gpus_per_instance": 2,
        "require_homogeneous_devices": True,
        "p2p_topology": "preferred",
    }
    config.write_text(json.dumps(payload), encoding="utf-8")
    scenario = scenario_for(FixtureId.FX_NV_05)
    expectation = next(
        item for item in scenario.deployment_expectations if item.case_id == "heterogeneous-model"
    )
    manager = _manager(
        tmp_path,
        config,
        model_root,
        fixture_id=FixtureId.FX_NV_05,
    )

    with pytest.raises(RuntimeRequestError) as captured:
        asyncio.run(
            manager.ensure(
                replace(
                    _spec(model),
                    gpu_uuids=expectation.gpu_uuids,
                    tensor_parallel_size=len(expectation.gpu_uuids),
                )
            )
        )
    assert captured.value.code == "unsupported_gpu_topology"


def test_PROTO_02_model_root_and_runtime_identity_fail_closed(tmp_path: Path) -> None:
    config, model_root, model = _tree(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o755)

    with pytest.raises(RuntimeRequestError) as outside_error:
        asyncio.run(_manager(tmp_path, config, model_root).ensure(_spec(outside)))
    assert outside_error.value.code == "model_path_not_allowed"

    with pytest.raises(RuntimeRequestError) as identity_error:
        asyncio.run(_manager(tmp_path, config, model_root, identity=None).ensure(_spec(model)))
    assert identity_error.value.code == "runtime_identity_unavailable"

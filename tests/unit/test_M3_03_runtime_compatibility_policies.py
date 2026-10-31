"""M3-03 closed Runtime policy schema and manifest-driven launch decisions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from holm_runtimes import (
    RuntimeCompatibility,
    RuntimeCompatibilityError,
    load_runtime_compatibility,
    validate_runtime_launch_policy,
)


def _runtime_entry(executable: Path) -> dict[str, object]:
    return {
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
        "context_policy": {"allowed_steps": [2048, 4096, 8192], "default": 4096},
        "failure_signatures": {"out_of_memory": ["CUDA out of memory", "torch.OutOfMemoryError"]},
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


def _payload(executable: Path) -> dict[str, object]:
    return {"schema_version": 1, "runtimes": [_runtime_entry(executable)]}


def _write(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _entry(payload: dict[str, object]) -> dict[str, object]:
    runtimes = cast(list[object], payload["runtimes"])
    return cast(dict[str, object], runtimes[0])


def _load(path: Path, payload: dict[str, object]) -> RuntimeCompatibility:
    runtime = load_runtime_compatibility(_write(path, payload)).runtime("vllm")
    assert runtime is not None
    return runtime


def test_M3_03_manifest_exposes_certified_gpu_tp_and_context_policies(tmp_path: Path) -> None:
    runtime = _load(tmp_path / "runtime-compat.yaml", _payload(tmp_path / "vllm"))

    assert runtime.image_digest is None
    assert runtime.gpu_policy.certified_gpu_counts == frozenset({1, 2, 4})
    assert runtime.gpu_policy.max_gpus_per_instance == 4
    assert runtime.gpu_policy.require_homogeneous_devices
    assert runtime.gpu_policy.p2p_topology == "preferred"
    assert runtime.tp_policy.size_source == "selected_gpu_count"
    assert runtime.tp_policy.model_partition_check == "runtime_adapter"
    assert runtime.context_policy.allowed_steps == (2048, 4096, 8192)
    assert runtime.context_policy.default == 4096
    assert runtime.context_policy.default in runtime.context_policy.allowed_steps
    assert runtime.failure_signatures.out_of_memory == (
        "CUDA out of memory",
        "torch.OutOfMemoryError",
    )


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        (None, "keys must contain"),
        ({"out_of_memory": []}, "out_of_memory must be non-empty"),
        ({"out_of_memory": "CUDA out of memory"}, "out_of_memory must be non-empty"),
        ({"out_of_memory": [""]}, "contains an invalid signature"),
    ),
    ids=("missing", "empty", "not-array", "empty-signature"),
)
def test_M3_13A_failure_signature_guard_fails_loud(
    tmp_path: Path,
    mutation: object,
    expected: str,
) -> None:
    payload = _payload(tmp_path / "vllm")
    entry = _entry(payload)
    if mutation is None:
        del entry["failure_signatures"]
    else:
        entry["failure_signatures"] = mutation

    with pytest.raises(RuntimeCompatibilityError, match=expected):
        load_runtime_compatibility(_write(tmp_path / "runtime-compat.yaml", payload))


def test_FIX_08_TC_GPU_03_policy_does_not_reject_uncertified_gpu_count(
    tmp_path: Path,
) -> None:
    config = tmp_path / "runtime-compat.yaml"
    payload = _payload(tmp_path / "vllm")
    runtime = _load(config, payload)

    assert (
        validate_runtime_launch_policy(
            runtime,
            selected_gpu_count=3,
            context_length=4096,
            tensor_parallel_size=3,
        )
        is None
    )

    gpu_policy = cast(dict[str, object], _entry(payload)["gpu_policy"])
    gpu_policy["certified_gpu_counts"] = [1, 2, 3, 4]
    recertified_runtime = _load(config, payload)
    assert (
        validate_runtime_launch_policy(
            recertified_runtime,
            selected_gpu_count=3,
            context_length=4096,
            tensor_parallel_size=3,
        )
        is None
    )

    gpu_policy["certified_gpu_counts"] = [1, 2, 4]
    gpu_policy["max_gpus_per_instance"] = "auto"
    automatic_runtime = _load(config, payload)
    assert automatic_runtime.gpu_policy.max_gpus_per_instance == "auto"
    assert (
        validate_runtime_launch_policy(
            automatic_runtime,
            selected_gpu_count=8,
            context_length=4096,
            tensor_parallel_size=8,
        )
        is None
    )


@pytest.mark.parametrize(
    ("selected_gpu_count", "context_length", "tensor_parallel_size", "expected"),
    (
        (5, 4096, 5, "gpu_count_exceeds_maximum"),
        (2, 3000, 2, "context_length_not_allowed"),
        (2, 4096, 1, "gpu_count_mismatch"),
    ),
)
def test_M3_03_policy_rejections_use_stable_short_codes(
    tmp_path: Path,
    selected_gpu_count: int,
    context_length: int,
    tensor_parallel_size: int,
    expected: str,
) -> None:
    runtime = _load(tmp_path / "runtime-compat.yaml", _payload(tmp_path / "vllm"))

    assert (
        validate_runtime_launch_policy(
            runtime,
            selected_gpu_count=selected_gpu_count,
            context_length=context_length,
            tensor_parallel_size=tensor_parallel_size,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("case", "expected"),
    (
        ("missing-policy", "gpu_policy"),
        ("wrong-type", "context_policy.default"),
        ("unknown-runtime-key", "keys must contain"),
        ("legacy-context-key", "keys must contain"),
    ),
)
def test_M3_03_missing_wrong_type_unknown_and_legacy_fields_fail_loud(
    tmp_path: Path,
    case: str,
    expected: str,
) -> None:
    payload = _payload(tmp_path / "vllm")
    entry = _entry(payload)
    if case == "missing-policy":
        del entry["gpu_policy"]
    elif case == "wrong-type":
        context_policy = cast(dict[str, object], entry["context_policy"])
        context_policy["default"] = False
    elif case == "unknown-runtime-key":
        entry["unknown"] = "ignored-by-unsafe-parser"
    else:
        entry["context_lengths"] = [2048, 4096]

    with pytest.raises(RuntimeCompatibilityError, match=expected):
        load_runtime_compatibility(_write(tmp_path / "runtime-compat.yaml", payload))


def test_M3_03_docker_requires_sha256_digest_while_process_remains_valid(tmp_path: Path) -> None:
    config = tmp_path / "runtime-compat.yaml"
    process_payload = _payload(tmp_path / "vllm")
    assert _load(config, process_payload).launcher == "process"

    docker_payload = _payload(tmp_path / "vllm")
    docker_entry = _entry(docker_payload)
    docker_entry["launcher"] = "docker"
    with pytest.raises(RuntimeCompatibilityError, match="image_digest is required for docker"):
        load_runtime_compatibility(_write(config, docker_payload))

    docker_entry["image_digest"] = "latest"
    with pytest.raises(RuntimeCompatibilityError, match="image_digest is invalid"):
        load_runtime_compatibility(_write(config, docker_payload))

    docker_entry["image_digest"] = f"sha256:{'a' * 64}"
    docker_runtime = _load(config, docker_payload)
    assert docker_runtime.launcher == "docker"
    assert docker_runtime.image_digest == f"sha256:{'a' * 64}"

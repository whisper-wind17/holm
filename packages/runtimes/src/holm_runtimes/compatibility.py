"""Load the closed, repository-controlled Runtime compatibility catalog."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal, cast

_REQUIRED_RUNTIME_KEYS: Final = frozenset(
    {
        "name",
        "version",
        "launcher",
        "executable",
        "supported_architectures",
        "reasoning_parsers",
        "tool_call_parsers",
        "request_capabilities",
        "gpu_memory_utilization",
        "gpu_policy",
        "tp_policy",
        "context_policy",
        "failure_signatures",
        "arguments",
    }
)
_OPTIONAL_RUNTIME_KEYS: Final = frozenset(
    {"image_digest", "metrics", "container_resources", "minimum_driver_version"}
)
_CANONICAL_RUNTIME_METRIC_TYPES: Final = MappingProxyType(
    {
        "runtime_ttft_seconds": "histogram",
        "runtime_tpot_seconds": "histogram",
        "runtime_e2e_latency_seconds": "histogram",
        "runtime_output_tokens_total": "counter",
        "runtime_requests_total": "counter",
        "runtime_requests_running": "gauge",
        "runtime_requests_waiting": "gauge",
        "runtime_queue_time_seconds": "histogram",
        "runtime_kv_cache_usage_ratio": "gauge",
        "runtime_errors_total": "counter",
    }
)
_METRIC_SOURCE_KEYS: Final = frozenset({"source", "type"})
_OPTIONAL_METRIC_SOURCE_KEYS: Final = frozenset({"labels"})
_REQUEST_CAPABILITY_KEYS: Final = frozenset({"openai_chat_completions"})
_GPU_POLICY_KEYS: Final = frozenset(
    {
        "certified_gpu_counts",
        "max_gpus_per_instance",
        "require_homogeneous_devices",
        "p2p_topology",
    }
)
_TP_POLICY_KEYS: Final = frozenset({"size_source", "model_partition_check"})
_CONTEXT_POLICY_KEYS: Final = frozenset({"allowed_steps", "default"})
_FAILURE_SIGNATURE_KEYS: Final = frozenset({"out_of_memory"})
_ARGUMENT_KEYS: Final = frozenset(
    {
        "reasoning_parser",
        "tool_call_parser",
        "enable_auto_tool_choice",
        "serve",
        "host",
        "port",
        "tensor_parallel_size",
        "max_model_len",
        "gpu_memory_utilization",
        "served_model_name",
        "disable_request_logging",
        "enable_metrics",
    }
)
_OPTIONAL_ARGUMENT_KEYS: Final = frozenset({"model_path"})
# Certified parser registrations in vLLM 0.29.0 and SGLang 0.5.13.
# Keep runtime imports out of the Host Agent; adding names requires certification.
_REGISTERED_REASONING_PARSERS: Final = MappingProxyType(
    {
        ("vllm", "0.29.0"): frozenset({"qwen3", "openai_gptoss"}),
        ("sglang", "0.5.13"): frozenset({"qwen3", "gpt-oss"}),
    }
)
_REGISTERED_TOOL_CALL_PARSERS: Final = MappingProxyType(
    {
        ("vllm", "0.29.0"): frozenset({"qwen3_coder", "openai"}),
        ("sglang", "0.5.13"): frozenset({"qwen3_coder", "gpt-oss"}),
    }
)
_SAFE_TOKEN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SAFE_FLAG: Final = re.compile(r"^--[a-z][a-z0-9-]{1,63}$")
_PROMETHEUS_NAME: Final = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_PROMETHEUS_LABEL: Final = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_IMAGE_DIGEST: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
_LAUNCHERS: Final = frozenset({"process", "container", "docker"})
_P2P_TOPOLOGIES: Final = frozenset({"not_required", "preferred", "required"})
# Runtimes stays dependency-free; a contract test guards protocol enum drift.
REASONING_EFFORTS: Final = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})


class RuntimeCompatibilityError(ValueError):
    """The compatibility file is absent, malformed, or unsafe."""


@dataclass(frozen=True, slots=True)
class RuntimeArguments:
    """Trusted CLI tokens for one Runtime; no request can replace these."""

    reasoning_parser: str
    tool_call_parser: str
    enable_auto_tool_choice: str | None
    serve: str
    model_path: str | None
    host: str
    port: str
    tensor_parallel_size: str
    max_model_len: str
    gpu_memory_utilization: str
    served_model_name: str
    disable_request_logging: str | None
    enable_metrics: str | None


@dataclass(frozen=True, slots=True)
class RuntimeRequestCapabilities:
    """Validated external request fields certified for one Runtime."""

    openai_chat_completions: frozenset[str]


@dataclass(frozen=True, slots=True)
class RuntimeGpuPolicy:
    """Certification coverage and topology policy for one Runtime version."""

    certified_gpu_counts: frozenset[int]
    max_gpus_per_instance: Literal["auto"] | int
    require_homogeneous_devices: bool
    p2p_topology: str


@dataclass(frozen=True, slots=True)
class RuntimeTpPolicy:
    """How tensor-parallel size and model partitioning are validated."""

    size_source: str
    model_partition_check: str


@dataclass(frozen=True, slots=True)
class RuntimeContextPolicy:
    """Closed Context Length steps and the certified safe default."""

    allowed_steps: tuple[int, ...]
    default: int


@dataclass(frozen=True, slots=True)
class RuntimeMetricCompatibility:
    """One manifest-certified native source for a canonical Runtime metric."""

    source: str
    metric_type: str
    labels: Mapping[str, frozenset[str]]


@dataclass(frozen=True, slots=True)
class RuntimeFailureSignatures:
    """Bounded log signatures used only for sanitized failure classification."""

    out_of_memory: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuntimeContainerResources:
    port: int
    shm_bytes_per_gpu: int
    tmpfs_bytes: int
    memory_bytes_per_gpu: int
    nano_cpus_per_gpu: int
    pids_limit: int


@dataclass(frozen=True, slots=True)
class RuntimeCompatibility:
    """One validated Runtime version and its accepted typed-value bounds."""

    name: str
    version: str
    launcher: str
    image_digest: str | None
    container_resources: RuntimeContainerResources | None
    executable: Path
    supported_architectures: tuple[str, ...]
    reasoning_parsers: Mapping[str, str]
    tool_call_parsers: Mapping[str, str]
    request_capabilities: RuntimeRequestCapabilities
    gpu_memory_utilization_minimum: float
    gpu_memory_utilization_maximum: float
    gpu_policy: RuntimeGpuPolicy
    tp_policy: RuntimeTpPolicy
    context_policy: RuntimeContextPolicy
    failure_signatures: RuntimeFailureSignatures
    arguments: RuntimeArguments
    metrics: Mapping[str, RuntimeMetricCompatibility | None] | None
    minimum_driver_version: str | None = None


@dataclass(frozen=True, slots=True)
class ModelReasoningCompatibility:
    """Template capabilities, kept as strings at the dependency-free catalog boundary."""

    efforts: tuple[str, ...]
    aliases: Mapping[str, str]
    can_disable: bool


@dataclass(frozen=True, slots=True)
class RuntimeCompatibilityCatalog:
    """Immutable name lookup for the exact catalog revision loaded by the Agent."""

    schema_version: int
    runtimes: Mapping[str, RuntimeCompatibility]
    reasoning_models: Mapping[str, ModelReasoningCompatibility] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def runtime(self, name: str) -> RuntimeCompatibility | None:
        return self.runtimes.get(name)


def _mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise RuntimeCompatibilityError(f"{field} must be an object")
    return cast(dict[str, object], value)


def _closed(mapping: Mapping[str, object], expected: frozenset[str], field: str) -> None:
    actual = frozenset(mapping)
    if actual != expected:
        raise RuntimeCompatibilityError(
            f"{field} keys must be exactly {sorted(expected)}; found {sorted(actual)}"
        )


def _closed_with_optional(
    mapping: Mapping[str, object],
    required: frozenset[str],
    optional: frozenset[str],
    field: str,
) -> None:
    actual = frozenset(mapping)
    missing = required - actual
    unknown = actual - required - optional
    if missing or unknown:
        raise RuntimeCompatibilityError(
            f"{field} keys must contain {sorted(required)} and only optional "
            f"{sorted(optional)}; found {sorted(actual)}"
        )


def _string(value: object, field: str, *, pattern: re.Pattern[str] = _SAFE_TOKEN) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise RuntimeCompatibilityError(f"{field} is invalid")
    return value


def _optional_string(
    value: object,
    field: str,
    *,
    pattern: re.Pattern[str] = _SAFE_TOKEN,
) -> str | None:
    if value is None:
        return None
    return _string(value, field, pattern=pattern)


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeCompatibilityError(f"{field} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise RuntimeCompatibilityError(f"{field} must be finite")
    return result


def _positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RuntimeCompatibilityError(f"{field} must be a positive integer")
    return value


def _positive_integer_steps(value: object, field: str) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise RuntimeCompatibilityError(f"{field} must be non-empty")
    values = tuple(_positive_integer(item, field) for item in value)
    if tuple(sorted(set(values))) != values:
        raise RuntimeCompatibilityError(f"{field} must be sorted and unique")
    return values


def _request_capabilities(value: object, field: str) -> RuntimeRequestCapabilities:
    data = _mapping(value, field)
    _closed(data, _REQUEST_CAPABILITY_KEYS, field)
    values = data["openai_chat_completions"]
    if not isinstance(values, list) or not values:
        raise RuntimeCompatibilityError(f"{field}.openai_chat_completions must be non-empty")
    fields = tuple(_string(item, f"{field}.openai_chat_completions") for item in values)
    if len(set(fields)) != len(fields):
        raise RuntimeCompatibilityError(f"{field}.openai_chat_completions must be unique")
    return RuntimeRequestCapabilities(openai_chat_completions=frozenset(fields))


def _gpu_policy(value: object, field: str) -> RuntimeGpuPolicy:
    data = _mapping(value, field)
    _closed(data, _GPU_POLICY_KEYS, field)
    counts = _positive_integer_steps(data["certified_gpu_counts"], f"{field}.certified_gpu_counts")
    raw_maximum = data["max_gpus_per_instance"]
    maximum: Literal["auto"] | int = (
        "auto"
        if raw_maximum == "auto"
        else _positive_integer(raw_maximum, f"{field}.max_gpus_per_instance")
    )
    homogeneous = data["require_homogeneous_devices"]
    if not isinstance(homogeneous, bool):
        raise RuntimeCompatibilityError(f"{field}.require_homogeneous_devices must be a boolean")
    p2p_topology = _string(data["p2p_topology"], f"{field}.p2p_topology")
    if p2p_topology not in _P2P_TOPOLOGIES:
        raise RuntimeCompatibilityError(f"{field}.p2p_topology is invalid")
    return RuntimeGpuPolicy(frozenset(counts), maximum, homogeneous, p2p_topology)


def _tp_policy(value: object, field: str) -> RuntimeTpPolicy:
    data = _mapping(value, field)
    _closed(data, _TP_POLICY_KEYS, field)
    size_source = _string(data["size_source"], f"{field}.size_source")
    if size_source != "selected_gpu_count":
        raise RuntimeCompatibilityError(f"{field}.size_source must be selected_gpu_count")
    partition_check = _string(data["model_partition_check"], f"{field}.model_partition_check")
    if partition_check != "runtime_adapter":
        raise RuntimeCompatibilityError(f"{field}.model_partition_check must be runtime_adapter")
    return RuntimeTpPolicy(size_source, partition_check)


def _context_policy(value: object, field: str) -> RuntimeContextPolicy:
    data = _mapping(value, field)
    _closed(data, _CONTEXT_POLICY_KEYS, field)
    allowed_steps = _positive_integer_steps(data["allowed_steps"], f"{field}.allowed_steps")
    default = _positive_integer(data["default"], f"{field}.default")
    if default not in allowed_steps:
        raise RuntimeCompatibilityError(f"{field}.default must be an allowed step")
    return RuntimeContextPolicy(allowed_steps, default)


def _failure_signatures(value: object, field: str) -> RuntimeFailureSignatures:
    data = _mapping(value, field)
    _closed(data, _FAILURE_SIGNATURE_KEYS, field)
    raw_signatures = data["out_of_memory"]
    if not isinstance(raw_signatures, list) or not raw_signatures:
        raise RuntimeCompatibilityError(f"{field}.out_of_memory must be non-empty")
    signatures: list[str] = []
    for raw_signature in raw_signatures:
        signature_field = f"{field}.out_of_memory"
        if (
            not isinstance(raw_signature, str)
            or not 1 <= len(raw_signature) <= 256
            or raw_signature != raw_signature.strip()
            or any(character in raw_signature for character in "\x00\r\n")
        ):
            raise RuntimeCompatibilityError(f"{signature_field} contains an invalid signature")
        signatures.append(raw_signature)
    if len(set(signatures)) != len(signatures):
        raise RuntimeCompatibilityError(f"{field}.out_of_memory must be unique")
    return RuntimeFailureSignatures(tuple(signatures))


def _metric_labels(value: object, field: str) -> Mapping[str, frozenset[str]]:
    data = _mapping(value, field)
    result: dict[str, frozenset[str]] = {}
    for name, raw_values in data.items():
        if _PROMETHEUS_LABEL.fullmatch(name) is None:
            raise RuntimeCompatibilityError(f"{field} contains an invalid label name")
        if not isinstance(raw_values, list) or not raw_values:
            raise RuntimeCompatibilityError(f"{field}.{name} must be non-empty")
        values = tuple(_string(item, f"{field}.{name}") for item in raw_values)
        if len(set(values)) != len(values):
            raise RuntimeCompatibilityError(f"{field}.{name} must be unique")
        result[name] = frozenset(values)
    return MappingProxyType(result)


def _metrics(
    value: object,
    field: str,
) -> Mapping[str, RuntimeMetricCompatibility | None]:
    data = _mapping(value, field)
    _closed(data, frozenset(_CANONICAL_RUNTIME_METRIC_TYPES), field)
    result: dict[str, RuntimeMetricCompatibility | None] = {}
    for canonical, expected_type in _CANONICAL_RUNTIME_METRIC_TYPES.items():
        raw_source = data[canonical]
        metric_field = f"{field}.{canonical}"
        if raw_source is None:
            result[canonical] = None
            continue
        source_data = _mapping(raw_source, metric_field)
        _closed_with_optional(
            source_data,
            _METRIC_SOURCE_KEYS,
            _OPTIONAL_METRIC_SOURCE_KEYS,
            metric_field,
        )
        source = _string(source_data["source"], f"{metric_field}.source", pattern=_PROMETHEUS_NAME)
        metric_type = _string(source_data["type"], f"{metric_field}.type")
        if metric_type != expected_type:
            raise RuntimeCompatibilityError(f"{metric_field}.type must be {expected_type}")
        if canonical == "runtime_output_tokens_total" and source == "sglang:gen_throughput":
            raise RuntimeCompatibilityError(
                f"{metric_field}.source must be a Generation Token Counter"
            )
        labels = (
            _metric_labels(source_data["labels"], f"{metric_field}.labels")
            if "labels" in source_data
            else MappingProxyType({})
        )
        result[canonical] = RuntimeMetricCompatibility(source, metric_type, labels)
    return MappingProxyType(result)


def validate_runtime_launch_policy(
    runtime: RuntimeCompatibility,
    *,
    selected_gpu_count: int,
    context_length: int,
    tensor_parallel_size: int,
) -> str | None:
    """Return a stable rejection code, or ``None`` for a catalog-approved request."""

    if tensor_parallel_size < 1 or tensor_parallel_size != selected_gpu_count:
        return "gpu_count_mismatch"
    maximum = runtime.gpu_policy.max_gpus_per_instance
    if isinstance(maximum, int) and selected_gpu_count > maximum:
        return "gpu_count_exceeds_maximum"
    if context_length not in runtime.context_policy.allowed_steps:
        return "context_length_not_allowed"
    return None


def _runtime(value: object, index: int) -> RuntimeCompatibility:
    field = f"runtimes[{index}]"
    data = _mapping(value, field)
    _closed_with_optional(data, _REQUIRED_RUNTIME_KEYS, _OPTIONAL_RUNTIME_KEYS, field)
    name = _string(data["name"], f"{field}.name")
    version = _string(data["version"], f"{field}.version")
    launcher = _string(data["launcher"], f"{field}.launcher")
    if launcher not in _LAUNCHERS:
        raise RuntimeCompatibilityError(f"{field}.launcher is invalid")
    minimum_driver_version = (
        _string(
            data["minimum_driver_version"],
            f"{field}.minimum_driver_version",
            pattern=re.compile(r"[0-9]+(?:\.[0-9]+){0,3}"),
        )
        if "minimum_driver_version" in data
        else None
    )
    image_digest_value = data.get("image_digest")
    if image_digest_value is None:
        image_digest = None
    else:
        image_digest = _string(
            image_digest_value,
            f"{field}.image_digest",
            pattern=_IMAGE_DIGEST,
        )
    if launcher == "container" and image_digest is None:
        raise RuntimeCompatibilityError(f"{field}.image_digest is required for container")
    if launcher == "docker" and image_digest is None:
        raise RuntimeCompatibilityError(f"{field}.image_digest is required for docker")
    if launcher == "process" and image_digest is not None:
        raise RuntimeCompatibilityError(f"{field}.image_digest is forbidden for process")
    resource_value = data.get("container_resources")
    if launcher == "container":
        resources = _mapping(resource_value, f"{field}.container_resources")
        _closed(
            resources,
            frozenset(
                {
                    "port",
                    "shm_bytes_per_gpu",
                    "tmpfs_bytes",
                    "memory_bytes_per_gpu",
                    "nano_cpus_per_gpu",
                    "pids_limit",
                }
            ),
            f"{field}.container_resources",
        )
        container_resources = RuntimeContainerResources(
            port=_positive_integer(resources["port"], f"{field}.container_resources.port"),
            shm_bytes_per_gpu=_positive_integer(
                resources["shm_bytes_per_gpu"], f"{field}.container_resources.shm_bytes_per_gpu"
            ),
            tmpfs_bytes=_positive_integer(
                resources["tmpfs_bytes"], f"{field}.container_resources.tmpfs_bytes"
            ),
            memory_bytes_per_gpu=_positive_integer(
                resources["memory_bytes_per_gpu"],
                f"{field}.container_resources.memory_bytes_per_gpu",
            ),
            nano_cpus_per_gpu=_positive_integer(
                resources["nano_cpus_per_gpu"], f"{field}.container_resources.nano_cpus_per_gpu"
            ),
            pids_limit=_positive_integer(
                resources["pids_limit"], f"{field}.container_resources.pids_limit"
            ),
        )
        if container_resources.port > 65535:
            raise RuntimeCompatibilityError(f"{field}.container_resources.port is invalid")
    elif resource_value is not None:
        raise RuntimeCompatibilityError(f"{field}.container_resources requires container launcher")
    else:
        container_resources = None

    executable_value = data["executable"]
    if not isinstance(executable_value, str):
        raise RuntimeCompatibilityError(f"{field}.executable must be a string")
    executable = Path(executable_value)
    if not executable.is_absolute() or ".." in executable.parts:
        raise RuntimeCompatibilityError(f"{field}.executable must be an absolute normalized path")

    architectures_value = data["supported_architectures"]
    if not isinstance(architectures_value, list) or not architectures_value:
        raise RuntimeCompatibilityError(f"{field}.supported_architectures must be non-empty")
    architectures = tuple(
        _string(item, f"{field}.supported_architectures") for item in architectures_value
    )
    if len(set(architectures)) != len(architectures):
        raise RuntimeCompatibilityError(f"{field}.supported_architectures must be unique")

    parser_values = _mapping(data["reasoning_parsers"], f"{field}.reasoning_parsers")
    registered_parsers = _REGISTERED_REASONING_PARSERS.get((name, version), frozenset())
    reasoning_parsers: dict[str, str] = {}
    for architecture, value in parser_values.items():
        architecture = _string(architecture, f"{field}.reasoning_parsers.architecture")
        parser = _string(value, f"{field}.reasoning_parsers.parser")
        if parser not in registered_parsers:
            raise RuntimeCompatibilityError(f"{field}.reasoning_parsers parser is not registered")
        reasoning_parsers[architecture] = parser

    tool_parser_values = _mapping(data["tool_call_parsers"], f"{field}.tool_call_parsers")
    registered_tool_parsers = _REGISTERED_TOOL_CALL_PARSERS.get((name, version), frozenset())
    tool_call_parsers: dict[str, str] = {}
    for architecture, value in tool_parser_values.items():
        architecture = _string(architecture, f"{field}.tool_call_parsers.architecture")
        parser = _string(value, f"{field}.tool_call_parsers.parser")
        if parser not in registered_tool_parsers:
            raise RuntimeCompatibilityError(f"{field}.tool_call_parsers parser is not registered")
        tool_call_parsers[architecture] = parser

    request_capabilities = _request_capabilities(
        data["request_capabilities"], f"{field}.request_capabilities"
    )

    utilization = _mapping(data["gpu_memory_utilization"], f"{field}.gpu_memory_utilization")
    _closed(utilization, frozenset({"minimum", "maximum"}), f"{field}.gpu_memory_utilization")
    minimum = _number(utilization["minimum"], f"{field}.gpu_memory_utilization.minimum")
    maximum = _number(utilization["maximum"], f"{field}.gpu_memory_utilization.maximum")
    if not 0 < minimum <= maximum <= 1:
        raise RuntimeCompatibilityError(f"{field}.gpu_memory_utilization range is invalid")

    gpu_policy = _gpu_policy(data["gpu_policy"], f"{field}.gpu_policy")
    tp_policy = _tp_policy(data["tp_policy"], f"{field}.tp_policy")
    context_policy = _context_policy(data["context_policy"], f"{field}.context_policy")
    failure_signatures = _failure_signatures(
        data["failure_signatures"], f"{field}.failure_signatures"
    )
    metrics = _metrics(data["metrics"], f"{field}.metrics") if "metrics" in data else None

    argument_values = _mapping(data["arguments"], f"{field}.arguments")
    _closed_with_optional(
        argument_values,
        _ARGUMENT_KEYS,
        _OPTIONAL_ARGUMENT_KEYS,
        f"{field}.arguments",
    )
    arguments = RuntimeArguments(
        tool_call_parser=_string(
            argument_values["tool_call_parser"],
            f"{field}.arguments.tool_call_parser",
            pattern=_SAFE_FLAG,
        ),
        enable_auto_tool_choice=_optional_string(
            argument_values["enable_auto_tool_choice"],
            f"{field}.arguments.enable_auto_tool_choice",
            pattern=_SAFE_FLAG,
        ),
        reasoning_parser=_string(
            argument_values["reasoning_parser"],
            f"{field}.arguments.reasoning_parser",
            pattern=_SAFE_FLAG,
        ),
        serve=_string(argument_values["serve"], f"{field}.arguments.serve"),
        model_path=_optional_string(
            argument_values.get("model_path"),
            f"{field}.arguments.model_path",
            pattern=_SAFE_FLAG,
        ),
        host=_string(argument_values["host"], f"{field}.arguments.host", pattern=_SAFE_FLAG),
        port=_string(argument_values["port"], f"{field}.arguments.port", pattern=_SAFE_FLAG),
        tensor_parallel_size=_string(
            argument_values["tensor_parallel_size"],
            f"{field}.arguments.tensor_parallel_size",
            pattern=_SAFE_FLAG,
        ),
        max_model_len=_string(
            argument_values["max_model_len"],
            f"{field}.arguments.max_model_len",
            pattern=_SAFE_FLAG,
        ),
        gpu_memory_utilization=_string(
            argument_values["gpu_memory_utilization"],
            f"{field}.arguments.gpu_memory_utilization",
            pattern=_SAFE_FLAG,
        ),
        served_model_name=_string(
            argument_values["served_model_name"],
            f"{field}.arguments.served_model_name",
            pattern=_SAFE_FLAG,
        ),
        disable_request_logging=_optional_string(
            argument_values["disable_request_logging"],
            f"{field}.arguments.disable_request_logging",
            pattern=_SAFE_FLAG,
        ),
        enable_metrics=_optional_string(
            argument_values["enable_metrics"],
            f"{field}.arguments.enable_metrics",
            pattern=_SAFE_FLAG,
        ),
    )
    all_arguments = (
        arguments.tool_call_parser,
        arguments.enable_auto_tool_choice,
        arguments.reasoning_parser,
        arguments.serve,
        arguments.model_path,
        arguments.host,
        arguments.port,
        arguments.tensor_parallel_size,
        arguments.max_model_len,
        arguments.gpu_memory_utilization,
        arguments.served_model_name,
        arguments.disable_request_logging,
        arguments.enable_metrics,
    )
    present_arguments = tuple(argument for argument in all_arguments if argument is not None)
    if len(set(present_arguments)) != len(present_arguments):
        raise RuntimeCompatibilityError(f"{field}.arguments must be unique")
    if "--trust-remote-code" in present_arguments:
        raise RuntimeCompatibilityError(f"{field}.arguments contains a forbidden flag")

    return RuntimeCompatibility(
        name=name,
        minimum_driver_version=minimum_driver_version,
        version=version,
        launcher=launcher,
        image_digest=image_digest,
        container_resources=container_resources,
        executable=executable,
        supported_architectures=architectures,
        reasoning_parsers=MappingProxyType(reasoning_parsers),
        tool_call_parsers=MappingProxyType(tool_call_parsers),
        request_capabilities=request_capabilities,
        gpu_memory_utilization_minimum=minimum,
        gpu_memory_utilization_maximum=maximum,
        gpu_policy=gpu_policy,
        tp_policy=tp_policy,
        context_policy=context_policy,
        failure_signatures=failure_signatures,
        arguments=arguments,
        metrics=metrics,
    )


def _reasoning_effort(value: object, field: str) -> str:
    if not isinstance(value, str) or value not in REASONING_EFFORTS:
        raise RuntimeCompatibilityError(f"{field} must be a reasoning effort")
    return value


def _reasoning_models(value: object) -> Mapping[str, ModelReasoningCompatibility]:
    models: dict[str, ModelReasoningCompatibility] = {}
    for architecture, policy in _mapping(value, "catalog.reasoning_models").items():
        field = "catalog.reasoning_models.policy"
        architecture = _string(architecture, "catalog.reasoning_models.architecture")
        data = _mapping(policy, field)
        _closed(data, frozenset({"efforts", "aliases", "can_disable"}), field)
        raw_efforts = data["efforts"]
        if not isinstance(raw_efforts, list) or not raw_efforts:
            raise RuntimeCompatibilityError(f"{field}.efforts must be non-empty")
        efforts = tuple(_reasoning_effort(item, f"{field}.efforts") for item in raw_efforts)
        if "none" in efforts or len(set(efforts)) != len(efforts):
            raise RuntimeCompatibilityError(f"{field}.efforts must be unique and exclude none")
        aliases = {
            _reasoning_effort(key, f"{field}.aliases.key"): _reasoning_effort(
                target, f"{field}.aliases.target"
            )
            for key, target in _mapping(data["aliases"], f"{field}.aliases").items()
        }
        if any(target not in efforts for target in aliases.values()):
            raise RuntimeCompatibilityError(f"{field}.aliases targets must be in efforts")
        can_disable = data["can_disable"]
        if not isinstance(can_disable, bool):
            raise RuntimeCompatibilityError(f"{field}.can_disable must be a boolean")
        models[architecture] = ModelReasoningCompatibility(
            efforts, MappingProxyType(aliases), can_disable
        )
    return MappingProxyType(models)


def load_runtime_compatibility(path: Path) -> RuntimeCompatibilityCatalog:
    """Load and fully validate the one supported JSON-compatible YAML document."""

    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeCompatibilityError("runtime compatibility catalog is unreadable") from error
    root = _mapping(payload, "catalog")
    _closed_with_optional(
        root, frozenset({"schema_version", "runtimes"}), frozenset({"reasoning_models"}), "catalog"
    )
    if root["schema_version"] != 1:
        raise RuntimeCompatibilityError("catalog.schema_version must be 1")
    values = root["runtimes"]
    if not isinstance(values, list) or not values:
        raise RuntimeCompatibilityError("catalog.runtimes must be non-empty")
    runtimes = tuple(_runtime(value, index) for index, value in enumerate(values))
    by_name = {runtime.name: runtime for runtime in runtimes}
    if len(by_name) != len(runtimes):
        raise RuntimeCompatibilityError("catalog runtime names must be unique")
    # Older catalogs declare no model-specific overrides; invalid declarations still fail closed.
    return RuntimeCompatibilityCatalog(
        1, MappingProxyType(by_name), _reasoning_models(root.get("reasoning_models", {}))
    )

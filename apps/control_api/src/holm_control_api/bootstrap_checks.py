"""Initialization system detection: one explicit, data-driven decision table.

Every one of the seven cards has exactly one entry in
``BOOTSTRAP_CHECK_RULES``, and every rule returns one of three outcomes.
The table is the point: a reviewer can read the whole classification without
following branches through a request handler, and
``assert_bootstrap_check_table_complete`` fails when the enum, the rule table
and the card label table stop agreeing in either direction.

Two distinctions this module refuses to collapse:

*   **"读不到" is not "读到了零".** ``UNAVAILABLE`` (NVML could not be used at
    all) and ``NO_DEVICES`` (NVML worked and this machine has no GPU) are both
    failures, but they are different failures with different remedies.
    Collapsing them produces the misleading
    "0 张 GPU" conclusion.
*   **"尚未部署" is neither pass nor fail.** Seven of the nine platform
    components may have no implementation in the current build. Calling that a failure
    would make the platform impossible to install; calling it "ready" is the
    false green that detection must avoid. It is a warning: initialization allows
    continuing past a warning as long as the impact is stated.

The disk and memory thresholds below are **this project's own numbers with no
universal host baseline behind them** -- the deployment guidance only says
the model directory "建议与系统盘
分离" and never gives a figure. They are named constants so the eventual
installer has exactly one place to calibrate, and the gap is
left for installation-time calibration.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from holm_control_api.system_status import (
    AgentResponse,
    AgentUnavailableResponse,
    GpuDiscoveryState,
    GpuHealthState,
    HostEnvironmentId,
    HostEnvironmentItem,
    HostEnvironmentReason,
    KnownString,
    KnownUInt64,
    SystemComponentsResponse,
    SystemComponentStatus,
    UInt64Observation,
)

#: 项目默认阈值，不代表通用硬件要求；安装时需统一标定。
MINIMUM_AVAILABLE_DISK_BYTES: Final[int] = 200 * 1024**3
#: 本项目自定，无基线依据；低于此值只警告，不阻止初始化。
RECOMMENDED_AVAILABLE_DISK_BYTES: Final[int] = 1024**4
#: 本项目自定，无基线依据。
MINIMUM_TOTAL_MEMORY_BYTES: Final[int] = 32 * 1024**3
#: 本项目自定，无基线依据；低于此值只警告，不阻止初始化。
RECOMMENDED_TOTAL_MEMORY_BYTES: Final[int] = 64 * 1024**3


class BootstrapCheckId(StrEnum):
    """The seven initialization detection cards, as stable wire identifiers."""

    GPU = "gpu"
    NVIDIA_DRIVER = "nvidia_driver"
    DOCKER_COMPOSE = "docker_compose"
    NVIDIA_CONTAINER_TOOLKIT = "nvidia_container_toolkit"
    DISK = "disk"
    MEMORY = "memory"
    HOLM_COMPONENTS = "holm_components"


class BootstrapCheckOutcome(StrEnum):
    """The three detection states. Only ``FAILED`` blocks completing initialization."""

    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"


class BootstrapCheckReason(StrEnum):
    """Stable reason codes; the localized sentence belongs to the web client."""

    AGENT_UNAVAILABLE = "agent_unavailable"

    GPU_DEVICES_PRESENT = "gpu_devices_present"
    GPU_DISCOVERY_UNAVAILABLE = "gpu_discovery_unavailable"
    GPU_NO_DEVICES = "gpu_no_devices"
    GPU_INVENTORY_INCONSISTENT = "gpu_inventory_inconsistent"
    GPU_UNHEALTHY_DEVICES = "gpu_unhealthy_devices"
    GPU_HEALTH_UNKNOWN = "gpu_health_unknown"

    DRIVER_MEETS_MINIMUM = "driver_meets_minimum"
    DRIVER_BELOW_MINIMUM = "driver_below_minimum"
    DRIVER_MINIMUM_UNDETERMINED = "driver_minimum_undetermined"
    DRIVER_VERSION_UNKNOWN = "driver_version_unknown"

    CUDA_VERSION_UNKNOWN = "cuda_version_unknown"

    DISK_SUFFICIENT = "disk_sufficient"
    DISK_BELOW_RECOMMENDED = "disk_below_recommended"
    DISK_BELOW_MINIMUM = "disk_below_minimum"
    DISK_CAPACITY_UNKNOWN = "disk_capacity_unknown"

    MEMORY_SUFFICIENT = "memory_sufficient"
    MEMORY_BELOW_RECOMMENDED = "memory_below_recommended"
    MEMORY_BELOW_MINIMUM = "memory_below_minimum"
    MEMORY_CAPACITY_UNKNOWN = "memory_capacity_unknown"

    DOCKER_COMPOSE_MEETS_MINIMUM = "docker_compose_meets_minimum"
    DOCKER_BELOW_MINIMUM = "docker_below_minimum"
    COMPOSE_BELOW_MINIMUM = "compose_below_minimum"
    DOCKER_VERSION_UNDETERMINED = "docker_version_undetermined"
    COMPOSE_VERSION_UNDETERMINED = "compose_version_undetermined"
    TOOLKIT_MEETS_MINIMUM = "toolkit_meets_minimum"
    TOOLKIT_BELOW_MINIMUM = "toolkit_below_minimum"
    TOOLKIT_VERSION_UNDETERMINED = "toolkit_version_undetermined"

    COMPONENTS_READY = "components_ready"
    COMPONENTS_NOT_DEPLOYED = "components_not_deployed"
    COMPONENTS_ABNORMAL = "components_abnormal"


@dataclass(frozen=True, slots=True)
class BootstrapEnvironment:
    """One Agent observation and the component report evaluated from it."""

    agent: AgentResponse
    components: SystemComponentsResponse


@dataclass(frozen=True, slots=True)
class BootstrapCheckResult:
    """One detection card's verdict with its non-empty reason list."""

    id: BootstrapCheckId
    outcome: BootstrapCheckOutcome
    reasons: tuple[BootstrapCheckReason, ...]

    def __post_init__(self) -> None:
        if not self.reasons:
            raise ValueError("a detection verdict must state at least one reason")


type BootstrapCheckEvaluator = Callable[
    [BootstrapEnvironment],
    tuple[BootstrapCheckOutcome, tuple[BootstrapCheckReason, ...]],
]

type _Verdict = tuple[BootstrapCheckOutcome, tuple[BootstrapCheckReason, ...]]


def _agent_unavailable() -> _Verdict:
    return BootstrapCheckOutcome.FAILED, (BootstrapCheckReason.AGENT_UNAVAILABLE,)


def _evaluate_gpu(environment: BootstrapEnvironment) -> _Verdict:
    agent = environment.agent
    if isinstance(agent, AgentUnavailableResponse):
        return _agent_unavailable()
    inventory = agent.gpus
    if inventory.discovery_state is GpuDiscoveryState.UNAVAILABLE:
        # Deliberately NOT folded into "no devices": nothing was discovered
        # because discovery itself could not run.
        return BootstrapCheckOutcome.FAILED, (BootstrapCheckReason.GPU_DISCOVERY_UNAVAILABLE,)
    if inventory.discovery_state is GpuDiscoveryState.NO_DEVICES:
        return BootstrapCheckOutcome.FAILED, (BootstrapCheckReason.GPU_NO_DEVICES,)
    if not inventory.devices:
        # READY with an empty device list is a protocol inconsistency, not an
        # honest "this machine has no GPU"; saying so keeps the two apart.
        return BootstrapCheckOutcome.FAILED, (BootstrapCheckReason.GPU_INVENTORY_INCONSISTENT,)
    reasons: list[BootstrapCheckReason] = []
    if any(device.health is GpuHealthState.UNHEALTHY for device in inventory.devices):
        reasons.append(BootstrapCheckReason.GPU_UNHEALTHY_DEVICES)
    if any(device.health is GpuHealthState.UNKNOWN for device in inventory.devices):
        # An unreadable health state is reported as unreadable rather than
        # silently counted as healthy.
        reasons.append(BootstrapCheckReason.GPU_HEALTH_UNKNOWN)
    if reasons:
        return BootstrapCheckOutcome.WARNING, tuple(reasons)
    return BootstrapCheckOutcome.PASSED, (BootstrapCheckReason.GPU_DEVICES_PRESENT,)


def _host_item(
    environment: BootstrapEnvironment, identifier: HostEnvironmentId
) -> HostEnvironmentItem:
    # An absent/duplicate item is contract drift, never an implicit pass.
    matches = [
        item for item in environment.components.host_environment.items if item.id is identifier
    ]
    if len(matches) != 1:
        raise ValueError("bootstrap host environment item coverage mismatch")
    return matches[0]


def _evaluate_nvidia_driver(environment: BootstrapEnvironment) -> _Verdict:
    if isinstance(environment.agent, AgentUnavailableResponse):
        return _agent_unavailable()
    driver = _host_item(environment, HostEnvironmentId.NVIDIA_DRIVER)
    if not isinstance(driver.current, KnownString):
        return BootstrapCheckOutcome.FAILED, (BootstrapCheckReason.DRIVER_VERSION_UNKNOWN,)
    if not isinstance(environment.components.host_environment.cuda_driver_version, KnownString):
        return BootstrapCheckOutcome.FAILED, (BootstrapCheckReason.CUDA_VERSION_UNKNOWN,)
    if driver.reason is HostEnvironmentReason.BELOW_MINIMUM:
        return BootstrapCheckOutcome.FAILED, (BootstrapCheckReason.DRIVER_BELOW_MINIMUM,)
    if driver.reason is HostEnvironmentReason.MEETS_MINIMUM:
        return BootstrapCheckOutcome.PASSED, (BootstrapCheckReason.DRIVER_MEETS_MINIMUM,)
    return BootstrapCheckOutcome.WARNING, (BootstrapCheckReason.DRIVER_MINIMUM_UNDETERMINED,)


def _evaluate_capacity(
    observed: UInt64Observation,
    *,
    minimum: int,
    recommended: int,
    sufficient: BootstrapCheckReason,
    below_recommended: BootstrapCheckReason,
    below_minimum: BootstrapCheckReason,
    unknown: BootstrapCheckReason,
) -> _Verdict:
    if not isinstance(observed, KnownUInt64):
        # An unreadable capacity is a failure, not an implicit pass: nothing
        # here is allowed to treat "读不到" as "够用".
        return BootstrapCheckOutcome.FAILED, (unknown,)
    if observed.value < minimum:
        return BootstrapCheckOutcome.FAILED, (below_minimum,)
    if observed.value < recommended:
        return BootstrapCheckOutcome.WARNING, (below_recommended,)
    return BootstrapCheckOutcome.PASSED, (sufficient,)


def _evaluate_disk(environment: BootstrapEnvironment) -> _Verdict:
    agent = environment.agent
    if isinstance(agent, AgentUnavailableResponse):
        return _agent_unavailable()
    return _evaluate_capacity(
        agent.host.available_disk_bytes,
        minimum=MINIMUM_AVAILABLE_DISK_BYTES,
        recommended=RECOMMENDED_AVAILABLE_DISK_BYTES,
        sufficient=BootstrapCheckReason.DISK_SUFFICIENT,
        below_recommended=BootstrapCheckReason.DISK_BELOW_RECOMMENDED,
        below_minimum=BootstrapCheckReason.DISK_BELOW_MINIMUM,
        unknown=BootstrapCheckReason.DISK_CAPACITY_UNKNOWN,
    )


def _evaluate_memory(environment: BootstrapEnvironment) -> _Verdict:
    agent = environment.agent
    if isinstance(agent, AgentUnavailableResponse):
        return _agent_unavailable()
    return _evaluate_capacity(
        agent.host.total_memory_bytes,
        minimum=MINIMUM_TOTAL_MEMORY_BYTES,
        recommended=RECOMMENDED_TOTAL_MEMORY_BYTES,
        sufficient=BootstrapCheckReason.MEMORY_SUFFICIENT,
        below_recommended=BootstrapCheckReason.MEMORY_BELOW_RECOMMENDED,
        below_minimum=BootstrapCheckReason.MEMORY_BELOW_MINIMUM,
        unknown=BootstrapCheckReason.MEMORY_CAPACITY_UNKNOWN,
    )


def _evaluate_host_versions(
    environment: BootstrapEnvironment,
    rules: tuple[tuple[HostEnvironmentId, BootstrapCheckReason, BootstrapCheckReason], ...],
    passed: BootstrapCheckReason,
) -> _Verdict:
    if isinstance(environment.agent, AgentUnavailableResponse):
        return _agent_unavailable()
    failed = False
    reasons = []
    for identifier, below, undetermined in rules:
        item = _host_item(environment, identifier)
        if item.reason is HostEnvironmentReason.BELOW_MINIMUM:
            failed = True
            reasons.append(below)
        elif item.reason is not HostEnvironmentReason.MEETS_MINIMUM:
            reasons.append(undetermined)
    if failed:
        return BootstrapCheckOutcome.FAILED, tuple(reasons)
    if reasons:
        # Prototype deviation D9: missing container tools must not block the
        # development preview. Unknown is a warning; a known old version fails.
        return BootstrapCheckOutcome.WARNING, tuple(reasons)
    return BootstrapCheckOutcome.PASSED, (passed,)


def _evaluate_docker_compose(environment: BootstrapEnvironment) -> _Verdict:
    return _evaluate_host_versions(
        environment,
        (
            (
                HostEnvironmentId.DOCKER,
                BootstrapCheckReason.DOCKER_BELOW_MINIMUM,
                BootstrapCheckReason.DOCKER_VERSION_UNDETERMINED,
            ),
            (
                HostEnvironmentId.COMPOSE,
                BootstrapCheckReason.COMPOSE_BELOW_MINIMUM,
                BootstrapCheckReason.COMPOSE_VERSION_UNDETERMINED,
            ),
        ),
        BootstrapCheckReason.DOCKER_COMPOSE_MEETS_MINIMUM,
    )


def _evaluate_toolkit(environment: BootstrapEnvironment) -> _Verdict:
    return _evaluate_host_versions(
        environment,
        (
            (
                HostEnvironmentId.NVIDIA_CONTAINER_TOOLKIT,
                BootstrapCheckReason.TOOLKIT_BELOW_MINIMUM,
                BootstrapCheckReason.TOOLKIT_VERSION_UNDETERMINED,
            ),
        ),
        BootstrapCheckReason.TOOLKIT_MEETS_MINIMUM,
    )


def _evaluate_platform_components(environment: BootstrapEnvironment) -> _Verdict:
    statuses = tuple(component.status for component in environment.components.components)
    if any(
        status in {SystemComponentStatus.DEGRADED, SystemComponentStatus.UNAVAILABLE}
        for status in statuses
    ):
        return BootstrapCheckOutcome.FAILED, (BootstrapCheckReason.COMPONENTS_ABNORMAL,)
    if any(status is SystemComponentStatus.NOT_DEPLOYED for status in statuses):
        return BootstrapCheckOutcome.WARNING, (BootstrapCheckReason.COMPONENTS_NOT_DEPLOYED,)
    return BootstrapCheckOutcome.PASSED, (BootstrapCheckReason.COMPONENTS_READY,)


BOOTSTRAP_CHECK_RULES: Final[Mapping[BootstrapCheckId, BootstrapCheckEvaluator]] = MappingProxyType(
    {
        BootstrapCheckId.GPU: _evaluate_gpu,
        BootstrapCheckId.NVIDIA_DRIVER: _evaluate_nvidia_driver,
        BootstrapCheckId.DOCKER_COMPOSE: _evaluate_docker_compose,
        BootstrapCheckId.NVIDIA_CONTAINER_TOOLKIT: _evaluate_toolkit,
        BootstrapCheckId.DISK: _evaluate_disk,
        BootstrapCheckId.MEMORY: _evaluate_memory,
        BootstrapCheckId.HOLM_COMPONENTS: _evaluate_platform_components,
    }
)

#: The contract card label each identifier stands for. Kept beside the rules so
#: the completeness assertion can compare against the baseline sentence itself
#: instead of against a list somebody retyped.
PDD_CHECK_LABELS: Final[Mapping[BootstrapCheckId, str]] = MappingProxyType(
    {
        BootstrapCheckId.GPU: "GPU",
        BootstrapCheckId.NVIDIA_DRIVER: "NVIDIA 驱动",
        BootstrapCheckId.DOCKER_COMPOSE: "Docker／Compose",
        BootstrapCheckId.NVIDIA_CONTAINER_TOOLKIT: "NVIDIA Container Toolkit",
        BootstrapCheckId.DISK: "磁盘",
        BootstrapCheckId.MEMORY: "内存",
        BootstrapCheckId.HOLM_COMPONENTS: "Holm 组件",
    }
)


def assert_bootstrap_check_table_complete(
    expected_labels: Collection[str],
    rules: Mapping[BootstrapCheckId, BootstrapCheckEvaluator] = BOOTSTRAP_CHECK_RULES,
    labels: Mapping[BootstrapCheckId, str] = PDD_CHECK_LABELS,
) -> None:
    """Fail when the enum, the rule table and card labels stop agreeing.

    Both directions matter: a missing card silently drops a check, and an extra
    one shows the user a verdict the baseline never asked for.
    """
    expected = frozenset(expected_labels)
    if len(expected) != len(tuple(expected_labels)):
        raise AssertionError("the expected detection card labels contain duplicates")
    enum_ids = frozenset(BootstrapCheckId)
    rule_ids = frozenset(rules)
    label_ids = frozenset(labels)
    if enum_ids != rule_ids or enum_ids != label_ids:
        raise AssertionError(
            "Bootstrap detection table drift: "
            f"rules_missing={sorted(identifier.value for identifier in enum_ids - rule_ids)}, "
            f"rules_unexpected={sorted(identifier.value for identifier in rule_ids - enum_ids)}, "
            f"labels_missing={sorted(identifier.value for identifier in enum_ids - label_ids)}, "
            f"labels_unexpected={sorted(identifier.value for identifier in label_ids - enum_ids)}"
        )
    observed = frozenset(labels.values())
    if len(observed) != len(labels):
        raise AssertionError("two detection cards claim the same label")
    if observed != expected:
        raise AssertionError(
            "Bootstrap detection cards differ from expected labels: "
            f"missing={sorted(expected - observed)}, unexpected={sorted(observed - expected)}"
        )


def evaluate_bootstrap_checks(
    environment: BootstrapEnvironment,
) -> tuple[BootstrapCheckResult, ...]:
    """Run every rule in table order and return one verdict per detection card."""
    results: list[BootstrapCheckResult] = []
    for identifier, evaluate in BOOTSTRAP_CHECK_RULES.items():
        outcome, reasons = evaluate(environment)
        results.append(BootstrapCheckResult(id=identifier, outcome=outcome, reasons=reasons))
    return tuple(results)


def blocking_check_ids(
    results: Collection[BootstrapCheckResult],
) -> tuple[BootstrapCheckId, ...]:
    """Return the failing cards. Only failures block completion."""
    return tuple(result.id for result in results if result.outcome is BootstrapCheckOutcome.FAILED)


def build_environment(
    *,
    agent: AgentResponse,
    components: SystemComponentsResponse,
) -> BootstrapEnvironment:
    """Assemble the observation the table reads, from one Agent call."""
    return BootstrapEnvironment(
        agent=agent,
        components=components,
    )


__all__ = [
    "BOOTSTRAP_CHECK_RULES",
    "MINIMUM_AVAILABLE_DISK_BYTES",
    "MINIMUM_TOTAL_MEMORY_BYTES",
    "PDD_CHECK_LABELS",
    "RECOMMENDED_AVAILABLE_DISK_BYTES",
    "RECOMMENDED_TOTAL_MEMORY_BYTES",
    "BootstrapCheckEvaluator",
    "BootstrapCheckId",
    "BootstrapCheckOutcome",
    "BootstrapCheckReason",
    "BootstrapCheckResult",
    "BootstrapEnvironment",
    "assert_bootstrap_check_table_complete",
    "blocking_check_ids",
    "build_environment",
    "evaluate_bootstrap_checks",
]

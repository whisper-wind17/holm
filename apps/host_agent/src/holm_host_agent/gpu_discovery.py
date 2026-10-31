"""Translate the narrow NVML call surface into stable GPU discovery data."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from itertools import combinations
from typing import Protocol
from uuid import uuid4

import pynvml
from holm_contracts.agent.v1 import agent_pb2


class NvmlErrorKind(StrEnum):
    """Sanitized NVML failure classes safe to use in control flow."""

    LIBRARY_UNAVAILABLE = "library_unavailable"
    DRIVER_UNAVAILABLE = "driver_unavailable"
    PERMISSION_DENIED = "permission_denied"
    NOT_SUPPORTED = "not_supported"
    FUNCTION_UNAVAILABLE = "function_unavailable"
    OTHER = "other"


class NvmlCallError(RuntimeError):
    """An NVML failure without raw exception text or host paths."""

    def __init__(self, kind: NvmlErrorKind) -> None:
        super().__init__(kind.value)
        self.kind = kind


class NvmlLibrary(Protocol):
    """The exact low-level NVML call boundary used by production translation."""

    def initialize(self) -> None: ...

    def shutdown(self) -> None: ...

    def system_get_driver_version(self) -> str: ...

    def system_get_cuda_driver_version(self) -> int: ...

    def device_get_count(self) -> int: ...

    def device_get_handle_by_index(self, index: int) -> object: ...

    def device_get_uuid(self, handle: object) -> str: ...

    def device_get_name(self, handle: object) -> str: ...

    def device_get_memory_total(self, handle: object) -> int: ...

    def device_get_utilization_percent(self, handle: object) -> float: ...

    def device_get_memory_used(self, handle: object) -> int: ...

    def device_get_temperature_celsius(self, handle: object) -> float: ...

    def device_get_power_watts(self, handle: object) -> float: ...

    def device_get_compute_running_process_pids(self, handle: object) -> tuple[int, ...]: ...

    def device_get_uncorrected_volatile_ecc_errors(self, handle: object) -> int: ...

    def device_get_pci_bus_id(self, handle: object) -> str: ...

    def device_get_topology_common_ancestor(self, first: object, second: object) -> int: ...

    def device_get_p2p_read_status(self, first: object, second: object) -> int: ...

    def nvlink_max_links(self) -> int: ...

    def device_get_nvlink_state(self, handle: object, link: int) -> bool: ...

    def device_get_nvlink_remote_pci_bus_id(self, handle: object, link: int) -> str: ...


def _classify_pynvml_error(error: pynvml.NVMLError) -> NvmlErrorKind:
    if isinstance(error, pynvml.NVMLError_LibraryNotFound):
        return NvmlErrorKind.LIBRARY_UNAVAILABLE
    if isinstance(error, pynvml.NVMLError_DriverNotLoaded):
        return NvmlErrorKind.DRIVER_UNAVAILABLE
    if isinstance(error, pynvml.NVMLError_NoPermission):
        return NvmlErrorKind.PERMISSION_DENIED
    if isinstance(error, pynvml.NVMLError_NotSupported):
        return NvmlErrorKind.NOT_SUPPORTED
    if isinstance(error, pynvml.NVMLError_FunctionNotFound):
        return NvmlErrorKind.FUNCTION_UNAVAILABLE
    return NvmlErrorKind.OTHER


def _nvml_call[T](operation: Callable[[], T]) -> T:
    try:
        return operation()
    except pynvml.NVMLError as error:
        raise NvmlCallError(_classify_pynvml_error(error)) from None


def _string_result(value: object) -> str:
    if not isinstance(value, str):
        raise NvmlCallError(NvmlErrorKind.OTHER)
    return value


def _integer_result(value: object) -> int:
    if not isinstance(value, int):
        raise NvmlCallError(NvmlErrorKind.OTHER)
    return value


class PynvmlLibrary:
    """Production adapter kept deliberately one-to-one with pynvml calls."""

    def initialize(self) -> None:
        _nvml_call(pynvml.nvmlInit)

    def shutdown(self) -> None:
        _nvml_call(pynvml.nvmlShutdown)

    def system_get_driver_version(self) -> str:
        return _string_result(_nvml_call(pynvml.nvmlSystemGetDriverVersion))

    def system_get_cuda_driver_version(self) -> int:
        return _integer_result(_nvml_call(pynvml.nvmlSystemGetCudaDriverVersion_v2))

    def device_get_count(self) -> int:
        return _integer_result(_nvml_call(pynvml.nvmlDeviceGetCount))

    def device_get_handle_by_index(self, index: int) -> object:
        return _nvml_call(lambda: pynvml.nvmlDeviceGetHandleByIndex(index))

    def device_get_uuid(self, handle: object) -> str:
        return _string_result(_nvml_call(lambda: pynvml.nvmlDeviceGetUUID(handle)))

    def device_get_name(self, handle: object) -> str:
        return _string_result(_nvml_call(lambda: pynvml.nvmlDeviceGetName(handle)))

    def device_get_memory_total(self, handle: object) -> int:
        memory = _nvml_call(lambda: pynvml.nvmlDeviceGetMemoryInfo(handle))
        return int(memory.total)

    def device_get_utilization_percent(self, handle: object) -> float:
        utilization = _nvml_call(lambda: pynvml.nvmlDeviceGetUtilizationRates(handle))
        return float(utilization.gpu)

    def device_get_memory_used(self, handle: object) -> int:
        memory = _nvml_call(lambda: pynvml.nvmlDeviceGetMemoryInfo(handle))
        return int(memory.used)

    def device_get_temperature_celsius(self, handle: object) -> float:
        return float(
            _nvml_call(lambda: pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU))
        )

    def device_get_power_watts(self, handle: object) -> float:
        milliwatts = _nvml_call(lambda: pynvml.nvmlDeviceGetPowerUsage(handle))
        return float(milliwatts) / 1000.0

    def device_get_compute_running_process_pids(self, handle: object) -> tuple[int, ...]:
        processes = _nvml_call(lambda: pynvml.nvmlDeviceGetComputeRunningProcesses(handle))
        pids: list[int] = []
        try:
            for process in processes:
                pid = process.pid
                if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
                    raise NvmlCallError(NvmlErrorKind.OTHER)
                pids.append(pid)
        except (AttributeError, TypeError):
            raise NvmlCallError(NvmlErrorKind.OTHER) from None
        return tuple(pids)

    def device_get_uncorrected_volatile_ecc_errors(self, handle: object) -> int:
        return _integer_result(
            _nvml_call(
                lambda: pynvml.nvmlDeviceGetTotalEccErrors(
                    handle,
                    pynvml.NVML_MEMORY_ERROR_TYPE_UNCORRECTED,
                    pynvml.NVML_VOLATILE_ECC,
                )
            )
        )

    def device_get_pci_bus_id(self, handle: object) -> str:
        pci_info = _nvml_call(lambda: pynvml.nvmlDeviceGetPciInfo(handle))
        return str(pci_info.busId)

    def device_get_topology_common_ancestor(self, first: object, second: object) -> int:
        return _integer_result(
            _nvml_call(lambda: pynvml.nvmlDeviceGetTopologyCommonAncestor(first, second))
        )

    def device_get_p2p_read_status(self, first: object, second: object) -> int:
        return _integer_result(_nvml_call(lambda: pynvml.nvmlDeviceGetP2PStatus(first, second, 0)))

    def nvlink_max_links(self) -> int:
        return int(pynvml.NVML_NVLINK_MAX_LINKS)

    def device_get_nvlink_state(self, handle: object, link: int) -> bool:
        return bool(_nvml_call(lambda: pynvml.nvmlDeviceGetNvLinkState(handle, link)))

    def device_get_nvlink_remote_pci_bus_id(self, handle: object, link: int) -> str:
        pci_info = _nvml_call(lambda: pynvml.nvmlDeviceGetNvLinkRemotePciInfo(handle, link))
        return str(pci_info.busId)


class DiscoveryState(StrEnum):
    READY = "ready"
    NO_DEVICES = "no_devices"
    UNAVAILABLE = "unavailable"


class DiscoveryFailureReason(StrEnum):
    NONE = "none"
    NO_DEVICES = "no_devices"
    LIBRARY_UNAVAILABLE = "library_unavailable"
    DRIVER_UNAVAILABLE = "driver_unavailable"
    PERMISSION_DENIED = "permission_denied"
    INITIALIZATION_FAILED = "initialization_failed"
    ENUMERATION_FAILED = "enumeration_failed"
    SNAPSHOT_CHANGED = "snapshot_changed"


class HealthState(StrEnum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class HealthReason(StrEnum):
    ECC_ERRORS_DETECTED = "ecc_errors_detected"
    QUERY_UNSUPPORTED = "query_unsupported"
    QUERY_FAILED = "query_failed"


class TopologyState(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    UNSUPPORTED = "unsupported"
    QUERY_FAILED = "query_failed"


class PcieRelation(StrEnum):
    INTERNAL = "internal"
    SINGLE_SWITCH = "single_switch"
    MULTIPLE_SWITCHES = "multiple_switches"
    HOST_BRIDGE = "host_bridge"
    NUMA_NODE = "numa_node"
    SYSTEM = "system"


class _RawPcieRelation(IntEnum):
    INTERNAL = 0
    SINGLE_SWITCH = 10
    MULTIPLE_SWITCHES = 20
    HOST_BRIDGE = 30
    NUMA_NODE = 40
    SYSTEM = 50


class _RawP2pStatus(IntEnum):
    OK = 0
    CHIPSET_NOT_SUPPORTED = 1
    GPU_NOT_SUPPORTED = 2
    IOH_TOPOLOGY_NOT_SUPPORTED = 3
    DISABLED = 4
    NOT_SUPPORTED = 5
    UNKNOWN = 6


@dataclass(frozen=True, slots=True)
class DiscoveredGpu:
    uuid: str
    display_index: int
    model: str
    memory_bytes: int
    health: HealthState
    health_reasons: tuple[HealthReason, ...]
    utilization_percent: float | None
    memory_used_bytes: int | None
    temperature_celsius: float | None
    power_watts: float | None
    handle: object
    pci_bus_id: str


@dataclass(frozen=True, slots=True)
class TopologyLink:
    gpu_a_uuid: str
    gpu_b_uuid: str
    pcie_state: TopologyState
    pcie_relation: PcieRelation | None
    nvlink_state: TopologyState
    p2p_read_state: TopologyState


@dataclass(frozen=True, slots=True)
class GpuInventory:
    state: DiscoveryState
    failure_reason: DiscoveryFailureReason
    snapshot_id: str | None
    devices: tuple[DiscoveredGpu, ...]
    topology_links: tuple[TopologyLink, ...]

    def __post_init__(self) -> None:
        uuids = tuple(device.uuid for device in self.devices)
        if len(set(uuids)) != len(uuids):
            raise ValueError("GPU inventory UUIDs must be unique")
        expected_pairs = {tuple(sorted(pair)) for pair in combinations(uuids, 2)}
        actual_pairs = {(link.gpu_a_uuid, link.gpu_b_uuid) for link in self.topology_links}
        if actual_pairs != expected_pairs or len(actual_pairs) != len(self.topology_links):
            raise ValueError("GPU topology must contain every normalized pair exactly once")
        if self.state is DiscoveryState.READY and not self.devices:
            raise ValueError("ready discovery requires at least one GPU")
        if self.state is not DiscoveryState.READY and self.devices:
            raise ValueError("failed or empty discovery cannot expose a partial device list")


_DISCOVERY_STATE_PROTO = {
    DiscoveryState.READY: agent_pb2.GPU_DISCOVERY_STATE_READY,
    DiscoveryState.NO_DEVICES: agent_pb2.GPU_DISCOVERY_STATE_NO_DEVICES,
    DiscoveryState.UNAVAILABLE: agent_pb2.GPU_DISCOVERY_STATE_UNAVAILABLE,
}
_DISCOVERY_REASON_PROTO = {
    DiscoveryFailureReason.NONE: agent_pb2.GPU_DISCOVERY_FAILURE_REASON_UNSPECIFIED,
    DiscoveryFailureReason.NO_DEVICES: agent_pb2.GPU_DISCOVERY_FAILURE_REASON_NO_DEVICES,
    DiscoveryFailureReason.LIBRARY_UNAVAILABLE: (
        agent_pb2.GPU_DISCOVERY_FAILURE_REASON_LIBRARY_UNAVAILABLE
    ),
    DiscoveryFailureReason.DRIVER_UNAVAILABLE: (
        agent_pb2.GPU_DISCOVERY_FAILURE_REASON_DRIVER_UNAVAILABLE
    ),
    DiscoveryFailureReason.PERMISSION_DENIED: (
        agent_pb2.GPU_DISCOVERY_FAILURE_REASON_PERMISSION_DENIED
    ),
    DiscoveryFailureReason.INITIALIZATION_FAILED: (
        agent_pb2.GPU_DISCOVERY_FAILURE_REASON_INITIALIZATION_FAILED
    ),
    DiscoveryFailureReason.ENUMERATION_FAILED: (
        agent_pb2.GPU_DISCOVERY_FAILURE_REASON_ENUMERATION_FAILED
    ),
    DiscoveryFailureReason.SNAPSHOT_CHANGED: (
        agent_pb2.GPU_DISCOVERY_FAILURE_REASON_SNAPSHOT_CHANGED
    ),
}
_HEALTH_STATE_PROTO = {
    HealthState.HEALTHY: agent_pb2.GPU_HEALTH_STATE_HEALTHY,
    HealthState.UNHEALTHY: agent_pb2.GPU_HEALTH_STATE_UNHEALTHY,
    HealthState.UNKNOWN: agent_pb2.GPU_HEALTH_STATE_UNKNOWN,
}
_HEALTH_REASON_PROTO = {
    HealthReason.ECC_ERRORS_DETECTED: agent_pb2.GPU_HEALTH_REASON_ECC_ERRORS_DETECTED,
    HealthReason.QUERY_UNSUPPORTED: agent_pb2.GPU_HEALTH_REASON_QUERY_UNSUPPORTED,
    HealthReason.QUERY_FAILED: agent_pb2.GPU_HEALTH_REASON_QUERY_FAILED,
}
_TOPOLOGY_STATE_PROTO = {
    TopologyState.PRESENT: agent_pb2.TOPOLOGY_STATE_PRESENT,
    TopologyState.ABSENT: agent_pb2.TOPOLOGY_STATE_ABSENT,
    TopologyState.UNSUPPORTED: agent_pb2.TOPOLOGY_STATE_UNSUPPORTED,
    TopologyState.QUERY_FAILED: agent_pb2.TOPOLOGY_STATE_QUERY_FAILED,
}
_PCIE_RELATION_PROTO = {
    PcieRelation.INTERNAL: agent_pb2.PCIE_RELATION_INTERNAL,
    PcieRelation.SINGLE_SWITCH: agent_pb2.PCIE_RELATION_SINGLE_SWITCH,
    PcieRelation.MULTIPLE_SWITCHES: agent_pb2.PCIE_RELATION_MULTIPLE_SWITCHES,
    PcieRelation.HOST_BRIDGE: agent_pb2.PCIE_RELATION_HOST_BRIDGE,
    PcieRelation.NUMA_NODE: agent_pb2.PCIE_RELATION_NUMA_NODE,
    PcieRelation.SYSTEM: agent_pb2.PCIE_RELATION_SYSTEM,
}


def _empty_inventory(
    state: DiscoveryState,
    reason: DiscoveryFailureReason,
    *,
    snapshot_id: str | None = None,
) -> GpuInventory:
    return GpuInventory(
        state=state,
        failure_reason=reason,
        snapshot_id=snapshot_id,
        devices=(),
        topology_links=(),
    )


def _initialization_failure(error: NvmlCallError) -> DiscoveryFailureReason:
    return {
        NvmlErrorKind.LIBRARY_UNAVAILABLE: DiscoveryFailureReason.LIBRARY_UNAVAILABLE,
        NvmlErrorKind.DRIVER_UNAVAILABLE: DiscoveryFailureReason.DRIVER_UNAVAILABLE,
        NvmlErrorKind.PERMISSION_DENIED: DiscoveryFailureReason.PERMISSION_DENIED,
    }.get(error.kind, DiscoveryFailureReason.INITIALIZATION_FAILED)


def _health(library: NvmlLibrary, handle: object) -> tuple[HealthState, tuple[HealthReason, ...]]:
    try:
        uncorrected_errors = library.device_get_uncorrected_volatile_ecc_errors(handle)
    except NvmlCallError as error:
        if error.kind in {NvmlErrorKind.NOT_SUPPORTED, NvmlErrorKind.FUNCTION_UNAVAILABLE}:
            return HealthState.UNKNOWN, (HealthReason.QUERY_UNSUPPORTED,)
        return HealthState.UNKNOWN, (HealthReason.QUERY_FAILED,)
    if uncorrected_errors > 0:
        return HealthState.UNHEALTHY, (HealthReason.ECC_ERRORS_DETECTED,)
    return HealthState.HEALTHY, ()


def _pcie(
    library: NvmlLibrary, first: object, second: object
) -> tuple[TopologyState, PcieRelation | None]:
    try:
        raw_relation = _RawPcieRelation(library.device_get_topology_common_ancestor(first, second))
    except NvmlCallError as error:
        if error.kind in {NvmlErrorKind.NOT_SUPPORTED, NvmlErrorKind.FUNCTION_UNAVAILABLE}:
            return TopologyState.UNSUPPORTED, None
        return TopologyState.QUERY_FAILED, None
    except ValueError:
        return TopologyState.QUERY_FAILED, None
    return TopologyState.PRESENT, PcieRelation(raw_relation.name.lower())


def _p2p(library: NvmlLibrary, first: object, second: object) -> TopologyState:
    try:
        status = _RawP2pStatus(library.device_get_p2p_read_status(first, second))
    except NvmlCallError as error:
        if error.kind in {NvmlErrorKind.NOT_SUPPORTED, NvmlErrorKind.FUNCTION_UNAVAILABLE}:
            return TopologyState.UNSUPPORTED
        return TopologyState.QUERY_FAILED
    except ValueError:
        return TopologyState.QUERY_FAILED
    if status is _RawP2pStatus.OK:
        return TopologyState.PRESENT
    if status is _RawP2pStatus.NOT_SUPPORTED:
        return TopologyState.UNSUPPORTED
    if status is _RawP2pStatus.UNKNOWN:
        return TopologyState.QUERY_FAILED
    return TopologyState.ABSENT


@dataclass(frozen=True, slots=True)
class _NvlinkObservation:
    state: TopologyState
    remote_pci_bus_ids: frozenset[str]


def _nvlink_observation(library: NvmlLibrary, device: DiscoveredGpu) -> _NvlinkObservation:
    remote_bus_ids: set[str] = set()
    try:
        link_count = library.nvlink_max_links()
        for link in range(link_count):
            if library.device_get_nvlink_state(device.handle, link):
                remote_bus_ids.add(library.device_get_nvlink_remote_pci_bus_id(device.handle, link))
    except NvmlCallError as error:
        if error.kind in {NvmlErrorKind.NOT_SUPPORTED, NvmlErrorKind.FUNCTION_UNAVAILABLE}:
            return _NvlinkObservation(TopologyState.UNSUPPORTED, frozenset())
        return _NvlinkObservation(TopologyState.QUERY_FAILED, frozenset())
    if remote_bus_ids:
        return _NvlinkObservation(TopologyState.PRESENT, frozenset(remote_bus_ids))
    return _NvlinkObservation(TopologyState.ABSENT, frozenset())


def _nvlink_pair(
    first: DiscoveredGpu,
    second: DiscoveredGpu,
    observations: dict[str, _NvlinkObservation],
) -> TopologyState:
    first_observation = observations[first.uuid]
    second_observation = observations[second.uuid]
    states = {first_observation.state, second_observation.state}
    if TopologyState.QUERY_FAILED in states:
        return TopologyState.QUERY_FAILED
    if TopologyState.UNSUPPORTED in states:
        return TopologyState.UNSUPPORTED
    if (
        second.pci_bus_id in first_observation.remote_pci_bus_ids
        or first.pci_bus_id in second_observation.remote_pci_bus_ids
    ):
        return TopologyState.PRESENT
    return TopologyState.ABSENT


def _topology(library: NvmlLibrary, devices: tuple[DiscoveredGpu, ...]) -> tuple[TopologyLink, ...]:
    nvlink_observations = {device.uuid: _nvlink_observation(library, device) for device in devices}
    links: list[TopologyLink] = []
    for first, second in combinations(sorted(devices, key=lambda item: item.uuid), 2):
        pcie_state, pcie_relation = _pcie(library, first.handle, second.handle)
        links.append(
            TopologyLink(
                gpu_a_uuid=first.uuid,
                gpu_b_uuid=second.uuid,
                pcie_state=pcie_state,
                pcie_relation=pcie_relation,
                nvlink_state=_nvlink_pair(first, second, nvlink_observations),
                p2p_read_state=_p2p(library, first.handle, second.handle),
            )
        )
    return tuple(links)


def _read_device(library: NvmlLibrary, display_index: int) -> DiscoveredGpu:
    handle = library.device_get_handle_by_index(display_index)
    gpu_uuid = library.device_get_uuid(handle)
    model = library.device_get_name(handle)
    memory_bytes = library.device_get_memory_total(handle)
    pci_bus_id = library.device_get_pci_bus_id(handle)
    if not gpu_uuid.startswith("GPU-") or any(character.isspace() for character in gpu_uuid):
        raise NvmlCallError(NvmlErrorKind.OTHER)
    if not model or memory_bytes <= 0 or not pci_bus_id:
        raise NvmlCallError(NvmlErrorKind.OTHER)
    health, reasons = _health(library, handle)

    def optional_metric[T](read: Callable[[], T], valid: Callable[[T], bool]) -> T | None:
        try:
            value = read()
        except (NvmlCallError, AttributeError, TypeError, ValueError):
            return None
        return value if valid(value) else None

    return DiscoveredGpu(
        uuid=gpu_uuid,
        display_index=display_index,
        model=model,
        memory_bytes=memory_bytes,
        health=health,
        health_reasons=reasons,
        utilization_percent=optional_metric(
            lambda: library.device_get_utilization_percent(handle),
            lambda value: 0 <= value <= 100,
        ),
        memory_used_bytes=optional_metric(
            lambda: library.device_get_memory_used(handle),
            lambda value: 0 <= value <= memory_bytes,
        ),
        temperature_celsius=optional_metric(
            lambda: library.device_get_temperature_celsius(handle),
            lambda value: -273.15 <= value <= 500,
        ),
        power_watts=optional_metric(
            lambda: library.device_get_power_watts(handle),
            lambda value: value >= 0,
        ),
        handle=handle,
        pci_bus_id=pci_bus_id,
    )


def _verify_device_set(library: NvmlLibrary, expected_uuids: set[str]) -> bool:
    count = library.device_get_count()
    observed = {
        library.device_get_uuid(library.device_get_handle_by_index(index)) for index in range(count)
    }
    return len(observed) == count and observed == expected_uuids


def discover_gpus(
    library: NvmlLibrary,
    *,
    snapshot_id_factory: Callable[[], str] = lambda: str(uuid4()),
) -> GpuInventory:
    """Discover one complete device/topology snapshot via the low-level NVML boundary."""
    try:
        library.initialize()
    except NvmlCallError as error:
        return _empty_inventory(DiscoveryState.UNAVAILABLE, _initialization_failure(error))

    snapshot_id = snapshot_id_factory()
    try:
        count = library.device_get_count()
        if count == 0:
            return _empty_inventory(
                DiscoveryState.NO_DEVICES,
                DiscoveryFailureReason.NO_DEVICES,
                snapshot_id=snapshot_id,
            )
        devices = tuple(_read_device(library, index) for index in range(count))
        expected_uuids = {device.uuid for device in devices}
        if len(expected_uuids) != len(devices):
            return _empty_inventory(
                DiscoveryState.UNAVAILABLE,
                DiscoveryFailureReason.SNAPSHOT_CHANGED,
            )
        topology_links = _topology(library, devices)
        if not _verify_device_set(library, expected_uuids):
            return _empty_inventory(
                DiscoveryState.UNAVAILABLE,
                DiscoveryFailureReason.SNAPSHOT_CHANGED,
            )
        return GpuInventory(
            state=DiscoveryState.READY,
            failure_reason=DiscoveryFailureReason.NONE,
            snapshot_id=snapshot_id,
            devices=devices,
            topology_links=topology_links,
        )
    except NvmlCallError:
        return _empty_inventory(
            DiscoveryState.UNAVAILABLE, DiscoveryFailureReason.ENUMERATION_FAILED
        )
    finally:
        with suppress(NvmlCallError):
            library.shutdown()


def inventory_to_proto(inventory: GpuInventory) -> agent_pb2.ListGPUsResponse:
    """Serialize a validated inventory without exposing opaque handles or host paths."""
    response = agent_pb2.ListGPUsResponse(
        discovery_state=_DISCOVERY_STATE_PROTO[inventory.state],
        failure_reason=_DISCOVERY_REASON_PROTO[inventory.failure_reason],
    )
    if inventory.snapshot_id is not None:
        response.snapshot_id = inventory.snapshot_id
    for device in inventory.devices:
        message = response.devices.add(
            uuid=device.uuid,
            display_index=device.display_index,
            model=device.model,
            memory_bytes=device.memory_bytes,
            health=_HEALTH_STATE_PROTO[device.health],
            health_reasons=tuple(_HEALTH_REASON_PROTO[reason] for reason in device.health_reasons),
        )
        if device.utilization_percent is not None:
            message.utilization_percent = device.utilization_percent
        if device.memory_used_bytes is not None:
            message.memory_used_bytes = device.memory_used_bytes
        if device.temperature_celsius is not None:
            message.temperature_celsius = device.temperature_celsius
        if device.power_watts is not None:
            message.power_watts = device.power_watts
    for link in inventory.topology_links:
        message = response.topology_links.add(
            gpu_a_uuid=link.gpu_a_uuid,
            gpu_b_uuid=link.gpu_b_uuid,
            pcie_state=_TOPOLOGY_STATE_PROTO[link.pcie_state],
            nvlink_state=_TOPOLOGY_STATE_PROTO[link.nvlink_state],
            p2p_read_state=_TOPOLOGY_STATE_PROTO[link.p2p_read_state],
        )
        if link.pcie_relation is not None:
            message.pcie_relation = _PCIE_RELATION_PROTO[link.pcie_relation]
    return response


@dataclass(frozen=True, slots=True)
class DriverVersionObservation:
    value: str | None
    unknown_reason: int


def read_driver_version(library: NvmlLibrary) -> DriverVersionObservation:
    """Read Driver version with an explicit distinction for empty source values."""
    try:
        library.initialize()
    except NvmlCallError:
        return DriverVersionObservation(
            value=None,
            unknown_reason=agent_pb2.OBSERVATION_UNKNOWN_REASON_SOURCE_UNAVAILABLE,
        )
    try:
        value = library.system_get_driver_version().strip()
        if not value:
            return DriverVersionObservation(
                value=None,
                unknown_reason=agent_pb2.OBSERVATION_UNKNOWN_REASON_EMPTY_VALUE,
            )
        return DriverVersionObservation(
            value=value,
            unknown_reason=agent_pb2.OBSERVATION_UNKNOWN_REASON_UNSPECIFIED,
        )
    except NvmlCallError:
        return DriverVersionObservation(
            value=None,
            unknown_reason=agent_pb2.OBSERVATION_UNKNOWN_REASON_QUERY_FAILED,
        )
    finally:
        with suppress(NvmlCallError):
            library.shutdown()

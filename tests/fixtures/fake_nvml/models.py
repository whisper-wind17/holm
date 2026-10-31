"""Typed, in-memory models for the standard-library Fake NVML fixture."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import combinations
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from holm_host_agent.gpu_discovery import NvmlErrorKind

FIXTURE_REVIEW_STATUS: Final = "待真机复核"


class FixtureId(StrEnum):
    """TST 3.2 Fake NVML fixture identifiers."""

    FX_NV_01 = "FX-NV-01"
    FX_NV_02 = "FX-NV-02"
    FX_NV_03 = "FX-NV-03"
    FX_NV_04 = "FX-NV-04"
    FX_NV_05 = "FX-NV-05"
    FX_NV_06 = "FX-NV-06"
    FX_NV_07 = "FX-NV-07"
    FX_NV_08 = "FX-NV-08"


class GpuHealth(StrEnum):
    """Health values returned by the constructed device inventory."""

    HEALTHY = "Healthy"
    UNHEALTHY = "Unhealthy"
    UNKNOWN = "Unknown"


class QueryFailure(StrEnum):
    """Low-level NVML query failures injected before production translation."""

    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class ExpectedInstanceState(StrEnum):
    """Expected downstream states carried as fixture data, not state-machine logic."""

    RUNNING = "Running"
    UNHEALTHY = "Unhealthy"


@dataclass(frozen=True, slots=True, eq=False)
class GpuDevice:
    """One GPU whose identity and equality are defined only by its stable UUID."""

    uuid: str
    display_index: int
    model: str
    memory_bytes: int
    health: GpuHealth = GpuHealth.HEALTHY
    health_reason: str | None = None
    health_query_failure: QueryFailure | None = None
    utilization_percent: float = 42.0
    memory_used_bytes: int | None = None
    temperature_celsius: float = 55.0
    power_watts: float = 120.0

    def __post_init__(self) -> None:
        if not self.uuid.startswith("GPU-") or any(character.isspace() for character in self.uuid):
            raise ValueError("GPU UUID must be a non-empty GPU-prefixed token")
        if self.display_index < 0:
            raise ValueError("GPU display index must be non-negative")
        if not self.model:
            raise ValueError("GPU model must be non-empty")
        if self.memory_bytes <= 0:
            raise ValueError("GPU memory must be positive")
        if (
            self.memory_used_bytes is not None
            and not 0 <= self.memory_used_bytes <= self.memory_bytes
        ):
            raise ValueError("GPU used memory must fit within total memory")
        if self.health is GpuHealth.UNHEALTHY and not self.health_reason:
            raise ValueError("Unhealthy GPU must expose a health reason")
        if self.health is GpuHealth.UNKNOWN and self.health_query_failure is None:
            raise ValueError("Unknown GPU health requires an injected query failure")
        if self.health is not GpuHealth.UNKNOWN and self.health_query_failure is not None:
            raise ValueError("only Unknown GPU health can inject a query failure")
        if self.health is GpuHealth.HEALTHY and self.health_reason is not None:
            raise ValueError("Healthy GPU cannot expose a health reason")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, GpuDevice):
            return NotImplemented
        return self.uuid == other.uuid

    def __hash__(self) -> int:
        return hash(self.uuid)


@dataclass(frozen=True, slots=True)
class InitializationFailure:
    """A structured initialization failure supplied by a Fake NVML snapshot."""

    code: str
    message: str
    blocks_completion: bool = True

    def __post_init__(self) -> None:
        if not self.code or not self.message:
            raise ValueError("initialization failure code and message must be non-empty")


@dataclass(frozen=True, slots=True)
class NvmlTopologyLink:
    """Raw pairwise values returned by the Fake NVML call surface."""

    gpu_a_uuid: str
    gpu_b_uuid: str
    pcie_ancestor: int = 50
    p2p_read_status: int = 0
    nvlink_connected: bool = False
    pcie_query_failure: QueryFailure | None = None
    p2p_query_failure: QueryFailure | None = None

    def __post_init__(self) -> None:
        if self.gpu_a_uuid >= self.gpu_b_uuid:
            raise ValueError("Fake NVML topology endpoints must be UUID-normalized")


@dataclass(frozen=True, slots=True)
class NvmlSnapshot:
    """An immutable point-in-time inventory keyed internally by GPU UUID."""

    devices: tuple[GpuDevice, ...]
    failure_items: tuple[InitializationFailure, ...] = ()
    topology_links: tuple[NvmlTopologyLink, ...] = ()
    nvlink_query_failure: QueryFailure | None = None
    _devices_by_uuid: Mapping[str, GpuDevice] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        devices_by_uuid = {device.uuid: device for device in self.devices}
        if len(devices_by_uuid) != len(self.devices):
            raise ValueError("GPU UUIDs must be unique within an NVML snapshot")
        if len({device.display_index for device in self.devices}) != len(self.devices):
            raise ValueError("GPU display indices must be unique within one snapshot")
        if not self.devices and not any(item.blocks_completion for item in self.failure_items):
            raise ValueError("zero-device discovery must expose a blocking failure item")
        device_uuids = set(devices_by_uuid)
        topology_pairs = {(link.gpu_a_uuid, link.gpu_b_uuid) for link in self.topology_links}
        expected_pairs = {tuple(sorted(pair)) for pair in combinations(device_uuids, 2)}
        if topology_pairs != expected_pairs or len(topology_pairs) != len(self.topology_links):
            raise ValueError("Fake NVML topology must contain every normalized pair exactly once")
        object.__setattr__(self, "_devices_by_uuid", MappingProxyType(devices_by_uuid))

    @property
    def can_complete_initialization(self) -> bool:
        """Whether this injected discovery result permits initialization to complete."""

        return not any(item.blocks_completion for item in self.failure_items)

    @property
    def device_uuids(self) -> tuple[str, ...]:
        """Stable identities in injected order."""

        return tuple(device.uuid for device in self.devices)

    def ordered_for_display(self) -> tuple[GpuDevice, ...]:
        """Return the current display order without turning the index into identity."""

        return tuple(sorted(self.devices, key=lambda device: device.display_index))

    def device_by_uuid(self, gpu_uuid: str) -> GpuDevice:
        """Resolve a device only through its stable UUID primary key."""

        return self._devices_by_uuid[gpu_uuid]


class FakeNvml:
    """Inject raw NVML calls while retaining the original scenario cursor API."""

    def __init__(
        self,
        snapshots: Iterable[NvmlSnapshot],
        *,
        initialization_error: NvmlErrorKind | None = None,
        driver_version: str = "fixture-driver-1",
        cuda_driver_version: int = 12_080,
    ) -> None:
        timeline = tuple(snapshots)
        if not timeline:
            raise ValueError("Fake NVML requires at least one snapshot")
        self._timeline = timeline
        self._position = 0
        self._initialization_error = initialization_error
        self._driver_version = driver_version
        self._cuda_driver_version = cuda_driver_version
        self._initialized = False

    @property
    def current_snapshot(self) -> NvmlSnapshot:
        """The active point in the injected timeline."""

        return self._timeline[self._position]

    def list_devices(self) -> tuple[GpuDevice, ...]:
        """List devices in their mutable display order."""

        return self.current_snapshot.ordered_for_display()

    def device_by_uuid(self, gpu_uuid: str) -> GpuDevice:
        """Look up a device by UUID; numeric display indices are not accepted."""

        return self.current_snapshot.device_by_uuid(gpu_uuid)

    def advance(self) -> NvmlSnapshot:
        """Advance to the next injected state transition."""

        if self._position + 1 >= len(self._timeline):
            raise RuntimeError("Fake NVML timeline is exhausted")
        self._position += 1
        return self.current_snapshot

    def initialize(self) -> None:
        """Mirror ``nvmlInit`` and inject only its sanitized error class."""
        if self._initialization_error is not None:
            from holm_host_agent.gpu_discovery import NvmlCallError

            raise NvmlCallError(self._initialization_error)
        self._initialized = True

    def shutdown(self) -> None:
        self._initialized = False

    def system_get_driver_version(self) -> str:
        self._require_initialized()
        return self._driver_version

    def system_get_cuda_driver_version(self) -> int:
        self._require_initialized()
        return self._cuda_driver_version

    def device_get_count(self) -> int:
        self._require_initialized()
        return len(self.current_snapshot.devices)

    def device_get_handle_by_index(self, index: int) -> object:
        self._require_initialized()
        return self.current_snapshot.ordered_for_display()[index].uuid

    def device_get_uuid(self, handle: object) -> str:
        return self._device_for_handle(handle).uuid

    def device_get_name(self, handle: object) -> str:
        return self._device_for_handle(handle).model

    def device_get_memory_total(self, handle: object) -> int:
        return self._device_for_handle(handle).memory_bytes

    def device_get_utilization_percent(self, handle: object) -> float:
        return self._device_for_handle(handle).utilization_percent

    def device_get_memory_used(self, handle: object) -> int:
        device = self._device_for_handle(handle)
        return (
            device.memory_bytes // 4
            if device.memory_used_bytes is None
            else device.memory_used_bytes
        )

    def device_get_temperature_celsius(self, handle: object) -> float:
        return self._device_for_handle(handle).temperature_celsius

    def device_get_power_watts(self, handle: object) -> float:
        return self._device_for_handle(handle).power_watts

    def device_get_compute_running_process_pids(self, handle: object) -> tuple[int, ...]:
        self._device_for_handle(handle)
        return ()

    def device_get_uncorrected_volatile_ecc_errors(self, handle: object) -> int:
        device = self._device_for_handle(handle)
        if device.health_query_failure is not None:
            self._raise_query_failure(device.health_query_failure)
        return 1 if device.health is GpuHealth.UNHEALTHY else 0

    def device_get_pci_bus_id(self, handle: object) -> str:
        device = self._device_for_handle(handle)
        ordinal = self.current_snapshot.devices.index(device)
        return f"00000000:{ordinal + 1:02X}:00.0"

    def device_get_topology_common_ancestor(self, first: object, second: object) -> int:
        link = self._topology_link(first, second)
        if link.pcie_query_failure is not None:
            self._raise_query_failure(link.pcie_query_failure)
        return link.pcie_ancestor

    def device_get_p2p_read_status(self, first: object, second: object) -> int:
        link = self._topology_link(first, second)
        if link.p2p_query_failure is not None:
            self._raise_query_failure(link.p2p_query_failure)
        return link.p2p_read_status

    def nvlink_max_links(self) -> int:
        return max(len(self.current_snapshot.devices) - 1, 1)

    def device_get_nvlink_state(self, handle: object, link: int) -> bool:
        self._device_for_handle(handle)
        if self.current_snapshot.nvlink_query_failure is not None:
            self._raise_query_failure(self.current_snapshot.nvlink_query_failure)
        return link < len(self._nvlink_peers(handle))

    def device_get_nvlink_remote_pci_bus_id(self, handle: object, link: int) -> str:
        peers = self._nvlink_peers(handle)
        return self.device_get_pci_bus_id(peers[link])

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise RuntimeError("Fake NVML must be initialized before use")

    def _device_for_handle(self, handle: object) -> GpuDevice:
        self._require_initialized()
        if not isinstance(handle, str):
            raise TypeError("Fake NVML handle must be a UUID token")
        return self.current_snapshot.device_by_uuid(handle)

    def _topology_link(self, first: object, second: object) -> NvmlTopologyLink:
        endpoints = tuple(sorted((self.device_get_uuid(first), self.device_get_uuid(second))))
        return next(
            link
            for link in self.current_snapshot.topology_links
            if (link.gpu_a_uuid, link.gpu_b_uuid) == endpoints
        )

    def _nvlink_peers(self, handle: object) -> tuple[str, ...]:
        gpu_uuid = self.device_get_uuid(handle)
        peers: list[str] = []
        for link in self.current_snapshot.topology_links:
            if not link.nvlink_connected or gpu_uuid not in {link.gpu_a_uuid, link.gpu_b_uuid}:
                continue
            peers.append(link.gpu_b_uuid if link.gpu_a_uuid == gpu_uuid else link.gpu_a_uuid)
        return tuple(sorted(peers))

    @staticmethod
    def _raise_query_failure(failure: QueryFailure) -> None:
        from holm_host_agent.gpu_discovery import NvmlCallError, NvmlErrorKind

        kind = (
            NvmlErrorKind.NOT_SUPPORTED
            if failure is QueryFailure.UNSUPPORTED
            else NvmlErrorKind.OTHER
        )
        raise NvmlCallError(kind)


@dataclass(frozen=True, slots=True)
class DeploymentExpectation:
    """A downstream deployment probe attached to a topology fixture."""

    case_id: str
    gpu_uuids: tuple[str, ...]
    tensor_parallel_size: int
    accepted: bool = True
    rejection_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.case_id:
            raise ValueError("deployment expectation case ID must be non-empty")
        if not self.gpu_uuids or len(set(self.gpu_uuids)) != len(self.gpu_uuids):
            raise ValueError("deployment expectation requires unique GPU UUIDs")
        if self.tensor_parallel_size != len(self.gpu_uuids):
            raise ValueError("TP size must match the number of expected GPU UUIDs")
        if self.accepted and self.rejection_reason is not None:
            raise ValueError("accepted deployment cannot have a rejection reason")
        if not self.accepted and not self.rejection_reason:
            raise ValueError("rejected deployment must expose a reason")


@dataclass(frozen=True, slots=True)
class InstanceTransitionExpectation:
    """Expected binding projection after a supplied inventory transition."""

    instance_id: str
    state_before: ExpectedInstanceState
    state_after: ExpectedInstanceState
    allocation_gpu_uuids_before: tuple[str, ...]
    allocation_gpu_uuids_after: tuple[str, ...]
    automatic_replacement_gpu_uuids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.instance_id:
            raise ValueError("instance ID must be non-empty")
        if not self.allocation_gpu_uuids_before:
            raise ValueError("instance transition requires an initial GPU Allocation")
        for values in (
            self.allocation_gpu_uuids_before,
            self.allocation_gpu_uuids_after,
            self.automatic_replacement_gpu_uuids,
        ):
            if len(set(values)) != len(values):
                raise ValueError("instance transition GPU UUIDs must be unique")


@dataclass(frozen=True, slots=True)
class FakeNvmlScenario:
    """One isolated TST scenario and the expectations it enables downstream."""

    fixture_id: FixtureId
    snapshots: tuple[NvmlSnapshot, ...]
    deployment_expectations: tuple[DeploymentExpectation, ...] = ()
    instance_transition: InstanceTransitionExpectation | None = None
    review_status: str = field(default=FIXTURE_REVIEW_STATUS, init=False)

    def __post_init__(self) -> None:
        if not self.snapshots:
            raise ValueError("Fake NVML scenario requires at least one snapshot")

    def open(self) -> FakeNvml:
        """Create a fresh mutable cursor so tests cannot share timeline state."""

        return FakeNvml(self.snapshots)

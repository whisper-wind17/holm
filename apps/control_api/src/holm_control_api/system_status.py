"""Stable HTTP contracts and aggregation rules for the System API."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Final, Literal, Protocol

from holm_contracts import VersionPayload
from holm_contracts.agent.v1 import agent_pb2
from holm_contracts.release import HostRequirements
from holm_domain import (
    Component,
    ComponentStatus,
    LedgerHealth,
    decode_ledger_backlog_reason,
)
from holm_domain.ledger_health import LedgerBacklogDetails, decode_ledger_backlog_details
from holm_persistence import LatestHeartbeat, SystemStatusSnapshot
from holm_runtimes import RuntimeCompatibilityCatalog
from pydantic import BaseModel, ConfigDict, Field

from holm_control_api.agent_client import (
    AgentSnapshot,
    AgentUnavailable,
    AgentUnavailableReason,
    HostAgentClient,
)
from holm_control_api.component_probe import ComponentProbeClient, ProbeObservation
from holm_control_api.version_comparison import compare_versions

HEARTBEAT_STALE_AFTER_SECONDS: Final[float] = 30.0
SCHEDULER_OLD_JOB_AFTER_SECONDS: Final[float] = 60.0


class _ClosedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ObservationState(StrEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"


class ObservationUnknownReason(StrEnum):
    NOT_COLLECTED = "not_collected"
    SOURCE_UNAVAILABLE = "source_unavailable"
    EMPTY_VALUE = "empty_value"
    QUERY_FAILED = "query_failed"
    NOT_DEPLOYED = "not_deployed"
    PROTOCOL_INVALID = "protocol_invalid"


class KnownUInt64(_ClosedResponse):
    state: Literal[ObservationState.KNOWN] = ObservationState.KNOWN
    value: int = Field(ge=0)


class UnknownUInt64(_ClosedResponse):
    state: Literal[ObservationState.UNKNOWN] = ObservationState.UNKNOWN
    reason: ObservationUnknownReason


type UInt64Observation = Annotated[KnownUInt64 | UnknownUInt64, Field(discriminator="state")]


class KnownString(_ClosedResponse):
    state: Literal[ObservationState.KNOWN] = ObservationState.KNOWN
    value: str = Field(min_length=1)


class UnknownString(_ClosedResponse):
    state: Literal[ObservationState.UNKNOWN] = ObservationState.UNKNOWN
    reason: ObservationUnknownReason


type StringObservation = Annotated[KnownString | UnknownString, Field(discriminator="state")]


class UnknownValue(_ClosedResponse):
    state: Literal[ObservationState.UNKNOWN] = ObservationState.UNKNOWN
    reason: ObservationUnknownReason


class AgentAccessStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class GpuDiscoveryState(StrEnum):
    READY = "ready"
    NO_DEVICES = "no_devices"
    UNAVAILABLE = "unavailable"


class GpuDiscoveryFailureReason(StrEnum):
    NONE = "none"
    NO_DEVICES = "no_devices"
    LIBRARY_UNAVAILABLE = "library_unavailable"
    DRIVER_UNAVAILABLE = "driver_unavailable"
    PERMISSION_DENIED = "permission_denied"
    INITIALIZATION_FAILED = "initialization_failed"
    ENUMERATION_FAILED = "enumeration_failed"
    SNAPSHOT_CHANGED = "snapshot_changed"
    PROTOCOL_INVALID = "protocol_invalid"


class GpuHealthState(StrEnum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class GpuHealthReason(StrEnum):
    ECC_ERRORS_DETECTED = "ecc_errors_detected"
    QUERY_UNSUPPORTED = "query_unsupported"
    QUERY_FAILED = "query_failed"
    PROTOCOL_INVALID = "protocol_invalid"


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


class HostInfoResponse(_ClosedResponse):
    logical_cpu_count: UInt64Observation
    total_memory_bytes: UInt64Observation
    total_disk_bytes: UInt64Observation
    available_disk_bytes: UInt64Observation
    driver_version: StringObservation
    cuda_driver_version: StringObservation


class GpuDeviceResponse(_ClosedResponse):
    uuid: str = Field(min_length=1)
    display_index: int | None = Field(default=None, ge=0)
    model: str = Field(min_length=1)
    memory_bytes: int = Field(ge=0)
    health: GpuHealthState
    health_reasons: tuple[GpuHealthReason, ...]


class GpuTopologyLinkResponse(_ClosedResponse):
    gpu_a_uuid: str = Field(min_length=1)
    gpu_b_uuid: str = Field(min_length=1)
    pcie_state: TopologyState
    pcie_relation: PcieRelation | None = None
    nvlink_state: TopologyState
    p2p_read_state: TopologyState


class GpuInventoryResponse(_ClosedResponse):
    discovery_state: GpuDiscoveryState
    failure_reason: GpuDiscoveryFailureReason
    snapshot_id: str | None = None
    devices: tuple[GpuDeviceResponse, ...]
    topology_links: tuple[GpuTopologyLinkResponse, ...]


class AgentUnavailableResponse(_ClosedResponse):
    status: Literal[AgentAccessStatus.UNAVAILABLE] = AgentAccessStatus.UNAVAILABLE
    reason: AgentUnavailableReason


class AgentAvailableResponse(_ClosedResponse):
    status: Literal[AgentAccessStatus.AVAILABLE] = AgentAccessStatus.AVAILABLE
    host: HostInfoResponse
    gpus: GpuInventoryResponse


type AgentResponse = Annotated[
    AgentAvailableResponse | AgentUnavailableResponse,
    Field(discriminator="status"),
]


class SystemServerResponse(_ClosedResponse):
    observed_at: datetime
    agent: AgentResponse


class SystemComponentId(StrEnum):
    EDGE = "Edge"
    CONTROL_PLANE = "Control Plane"
    TELEMETRY_COLLECTOR = "Telemetry Collector"
    GATEWAY = "Gateway"
    SCHEDULER = "Scheduler"
    DOWNLOAD_MANAGER = "Download Manager"
    REQUEST_LEDGER_WRITER = "Request Ledger Writer"
    MAINTENANCE = "Maintenance"
    DATABASE = "Database"
    HOST_AGENT = "Host Agent"


class ComponentSignalSource(StrEnum):
    SELF = "self"
    DATABASE = "database"
    HEARTBEAT = "heartbeat"
    JOBS = "jobs"
    LEDGER_BACKLOG = "ledger_backlog"
    AGENT = "agent"
    PROBE = "probe"


class SystemComponentStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    NOT_DEPLOYED = "not_deployed"


class SystemComponentReason(StrEnum):
    PROBE_READY = "probe_ready"
    PROBE_UNREACHABLE = "probe_unreachable"
    GATEWAY_ROUTE_SNAPSHOT_UNAVAILABLE = "gateway_route_snapshot_unavailable"
    GATEWAY_LEDGER_UNAVAILABLE = "gateway_ledger_unavailable"
    AGENT_AVAILABLE = "agent_available"
    AGENT_UNAVAILABLE = "agent_unavailable"
    SELF_READY = "self_ready"
    DEPENDENCY_READY = "dependency_ready"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    HEARTBEAT_FRESH = "heartbeat_fresh"
    HEARTBEAT_STALE = "heartbeat_stale"
    HEARTBEAT_DEGRADED = "heartbeat_degraded"
    HEARTBEAT_STOPPING = "heartbeat_stopping"
    NO_HEARTBEAT = "no_heartbeat"
    NO_SIGNAL_SOURCE = "no_signal_source"
    SIGNAL_UNAVAILABLE = "signal_unavailable"
    SCHEDULER_BACKLOG = "scheduler_backlog"
    SCHEDULER_LEASES_EXPIRED = "scheduler_leases_expired"
    LEDGER_BACKLOG = "ledger_backlog"
    LEDGER_UNAVAILABLE = "ledger_unavailable"


class KnownTimestamp(_ClosedResponse):
    state: Literal[ObservationState.KNOWN] = ObservationState.KNOWN
    value: datetime


type TimestampObservation = Annotated[KnownTimestamp | UnknownValue, Field(discriminator="state")]


class ComponentDetailKind(StrEnum):
    NONE = "none"
    DATABASE = "database"
    REQUEST_LEDGER_WRITER = "request_ledger_writer"


class NoComponentDetails(_ClosedResponse):
    kind: Literal[ComponentDetailKind.NONE] = ComponentDetailKind.NONE


class RequestLedgerWriterDetails(_ClosedResponse):
    kind: Literal[ComponentDetailKind.REQUEST_LEDGER_WRITER] = (
        ComponentDetailKind.REQUEST_LEDGER_WRITER
    )
    pending_event_count: UInt64Observation
    oldest_event_at: TimestampObservation


class DatabaseDetails(_ClosedResponse):
    kind: Literal[ComponentDetailKind.DATABASE] = ComponentDetailKind.DATABASE
    schema_revision: StringObservation


type ComponentDetails = Annotated[
    NoComponentDetails | RequestLedgerWriterDetails | DatabaseDetails,
    Field(discriminator="kind"),
]


class SystemComponentResponse(_ClosedResponse):
    id: SystemComponentId
    status: SystemComponentStatus
    reason: SystemComponentReason
    version: StringObservation
    signal_sources: tuple[ComponentSignalSource, ...]
    details: ComponentDetails


class PlatformAggregateStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"


class ComponentSummaryResponse(_ClosedResponse):
    status: PlatformAggregateStatus
    ready_count: int = Field(ge=0)
    abnormal_count: int = Field(ge=0)
    not_deployed_count: int = Field(ge=0)


class PlatformVersionResponse(_ClosedResponse):
    build_version: str


class HostEnvironmentId(StrEnum):
    NVIDIA_DRIVER = "nvidia_driver"
    DOCKER = "docker"
    COMPOSE = "compose"
    NVIDIA_CONTAINER_TOOLKIT = "nvidia_container_toolkit"


class HostEnvironmentStatus(StrEnum):
    SATISFIED = "satisfied"
    UNSATISFIED = "unsatisfied"
    UNKNOWN = "unknown"


class HostEnvironmentReason(StrEnum):
    MEETS_MINIMUM = "meets_minimum"
    BELOW_MINIMUM = "below_minimum"
    AGENT_UNAVAILABLE = "agent_unavailable"
    CURRENT_UNAVAILABLE = "current_unavailable"
    MINIMUM_NOT_DECLARED = "minimum_not_declared"
    VERSION_UNPARSEABLE = "version_unparseable"


class DriverCompatibility(StrEnum):
    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"
    UNKNOWN = "unknown"


class DriverCompatibilityReason(StrEnum):
    MEETS_MINIMUM = "meets_minimum"
    BELOW_MINIMUM = "below_minimum"
    AGENT_UNAVAILABLE = "agent_unavailable"
    DRIVER_UNAVAILABLE = "driver_unavailable"
    MINIMUM_NOT_DECLARED = "minimum_not_declared"
    VERSION_UNPARSEABLE = "version_unparseable"


class HostEnvironmentItem(_ClosedResponse):
    id: HostEnvironmentId
    current: StringObservation
    minimum: str | None
    status: HostEnvironmentStatus
    reason: HostEnvironmentReason


class HostEnvironmentResponse(_ClosedResponse):
    cuda_driver_version: StringObservation
    items: tuple[HostEnvironmentItem, ...]


class InferenceEngineResponse(_ClosedResponse):
    name: str
    version: str
    launcher: str
    image_digest: str | None
    active_instance_count: int | None = Field(ge=0)
    minimum_driver_version: str | None
    driver_compatibility: DriverCompatibility
    reason: DriverCompatibilityReason


class SystemComponentsResponse(_ClosedResponse):
    observed_at: datetime
    platform: PlatformVersionResponse
    summary: ComponentSummaryResponse
    components: tuple[SystemComponentResponse, ...]
    host_environment: HostEnvironmentResponse
    inference_engines: tuple[InferenceEngineResponse, ...]


@dataclass(frozen=True, slots=True)
class ComponentSignalRule:
    sources: tuple[ComponentSignalSource, ...]
    heartbeat_component: Component | None = None

    def __post_init__(self) -> None:
        if len(self.sources) != len(set(self.sources)):
            raise ValueError("component signal sources must be unique")
        has_heartbeat_source = ComponentSignalSource.HEARTBEAT in self.sources
        if has_heartbeat_source != (self.heartbeat_component is not None):
            raise ValueError("heartbeat source and heartbeat component must be configured together")


COMPONENT_SIGNAL_RULES: Final[Mapping[SystemComponentId, ComponentSignalRule]] = MappingProxyType(
    {
        SystemComponentId.EDGE: ComponentSignalRule((ComponentSignalSource.PROBE,)),
        SystemComponentId.CONTROL_PLANE: ComponentSignalRule((ComponentSignalSource.SELF,)),
        SystemComponentId.TELEMETRY_COLLECTOR: ComponentSignalRule(
            (ComponentSignalSource.HEARTBEAT,),
            Component.TELEMETRY_WORKER,
        ),
        SystemComponentId.GATEWAY: ComponentSignalRule((ComponentSignalSource.PROBE,)),
        SystemComponentId.SCHEDULER: ComponentSignalRule(
            (ComponentSignalSource.HEARTBEAT, ComponentSignalSource.JOBS),
            Component.INSTANCE_CONTROLLER,
        ),
        SystemComponentId.DOWNLOAD_MANAGER: ComponentSignalRule(
            (ComponentSignalSource.HEARTBEAT,),
            Component.DOWNLOAD_WORKER,
        ),
        SystemComponentId.REQUEST_LEDGER_WRITER: ComponentSignalRule(
            (ComponentSignalSource.HEARTBEAT, ComponentSignalSource.LEDGER_BACKLOG),
            Component.LEDGER_WRITER,
        ),
        SystemComponentId.MAINTENANCE: ComponentSignalRule(
            (ComponentSignalSource.HEARTBEAT,),
            Component.MAINTENANCE_WORKER,
        ),
        SystemComponentId.DATABASE: ComponentSignalRule((ComponentSignalSource.DATABASE,)),
        SystemComponentId.HOST_AGENT: ComponentSignalRule((ComponentSignalSource.AGENT,)),
    }
)


def assert_component_signal_map_complete(
    expected_component_ids: Collection[str],
    mapping: Mapping[SystemComponentId, ComponentSignalRule] = COMPONENT_SIGNAL_RULES,
) -> None:
    expected = frozenset(expected_component_ids)
    enum_ids = frozenset(component.value for component in SystemComponentId)
    mapping_ids = frozenset(
        component.value if isinstance(component, SystemComponentId) else str(component)
        for component in mapping
    )
    if enum_ids != expected or mapping_ids != expected:
        raise AssertionError(
            "System component contract drift: "
            f"enum_missing={sorted(expected - enum_ids)}, "
            f"enum_unexpected={sorted(enum_ids - expected)}, "
            f"mapping_missing={sorted(expected - mapping_ids)}, "
            f"mapping_unexpected={sorted(mapping_ids - expected)}"
        )


class SystemDatabaseReader(Protocol):
    async def read(self) -> SystemStatusSnapshot:
        """Return database version, DB-clock heartbeat ages, and scheduler signals."""


_UNKNOWN_REASON_MAP: Final[Mapping[int, ObservationUnknownReason]] = MappingProxyType(
    {
        agent_pb2.OBSERVATION_UNKNOWN_REASON_NOT_COLLECTED: (
            ObservationUnknownReason.NOT_COLLECTED
        ),
        agent_pb2.OBSERVATION_UNKNOWN_REASON_SOURCE_UNAVAILABLE: (
            ObservationUnknownReason.SOURCE_UNAVAILABLE
        ),
        agent_pb2.OBSERVATION_UNKNOWN_REASON_EMPTY_VALUE: ObservationUnknownReason.EMPTY_VALUE,
        agent_pb2.OBSERVATION_UNKNOWN_REASON_QUERY_FAILED: ObservationUnknownReason.QUERY_FAILED,
    }
)


def uint_observation(value: agent_pb2.ObservedUInt64) -> UInt64Observation:
    """Convert one protobuf unsigned observation into its response shape."""
    if value.state == agent_pb2.KNOWLEDGE_STATE_KNOWN and value.HasField("value"):
        return KnownUInt64(value=value.value)
    return UnknownUInt64(reason=ObservationUnknownReason.PROTOCOL_INVALID)


def string_observation(value: agent_pb2.ObservedString) -> StringObservation:
    """Convert one protobuf string observation into its response shape."""
    if value.state == agent_pb2.KNOWLEDGE_STATE_KNOWN and value.HasField("value") and value.value:
        return KnownString(value=value.value)
    return UnknownString(
        reason=_UNKNOWN_REASON_MAP.get(
            value.unknown_reason, ObservationUnknownReason.PROTOCOL_INVALID
        )
    )


_DISCOVERY_STATE_MAP: Final[Mapping[int, GpuDiscoveryState]] = MappingProxyType(
    {
        agent_pb2.GPU_DISCOVERY_STATE_READY: GpuDiscoveryState.READY,
        agent_pb2.GPU_DISCOVERY_STATE_NO_DEVICES: GpuDiscoveryState.NO_DEVICES,
        agent_pb2.GPU_DISCOVERY_STATE_UNAVAILABLE: GpuDiscoveryState.UNAVAILABLE,
    }
)
_DISCOVERY_REASON_MAP: Final[Mapping[int, GpuDiscoveryFailureReason]] = MappingProxyType(
    {
        agent_pb2.GPU_DISCOVERY_FAILURE_REASON_UNSPECIFIED: GpuDiscoveryFailureReason.NONE,
        agent_pb2.GPU_DISCOVERY_FAILURE_REASON_NO_DEVICES: GpuDiscoveryFailureReason.NO_DEVICES,
        agent_pb2.GPU_DISCOVERY_FAILURE_REASON_LIBRARY_UNAVAILABLE: (
            GpuDiscoveryFailureReason.LIBRARY_UNAVAILABLE
        ),
        agent_pb2.GPU_DISCOVERY_FAILURE_REASON_DRIVER_UNAVAILABLE: (
            GpuDiscoveryFailureReason.DRIVER_UNAVAILABLE
        ),
        agent_pb2.GPU_DISCOVERY_FAILURE_REASON_PERMISSION_DENIED: (
            GpuDiscoveryFailureReason.PERMISSION_DENIED
        ),
        agent_pb2.GPU_DISCOVERY_FAILURE_REASON_INITIALIZATION_FAILED: (
            GpuDiscoveryFailureReason.INITIALIZATION_FAILED
        ),
        agent_pb2.GPU_DISCOVERY_FAILURE_REASON_ENUMERATION_FAILED: (
            GpuDiscoveryFailureReason.ENUMERATION_FAILED
        ),
        agent_pb2.GPU_DISCOVERY_FAILURE_REASON_SNAPSHOT_CHANGED: (
            GpuDiscoveryFailureReason.SNAPSHOT_CHANGED
        ),
    }
)
_HEALTH_STATE_MAP: Final[Mapping[int, GpuHealthState]] = MappingProxyType(
    {
        agent_pb2.GPU_HEALTH_STATE_HEALTHY: GpuHealthState.HEALTHY,
        agent_pb2.GPU_HEALTH_STATE_UNHEALTHY: GpuHealthState.UNHEALTHY,
        agent_pb2.GPU_HEALTH_STATE_UNKNOWN: GpuHealthState.UNKNOWN,
    }
)
_HEALTH_REASON_MAP: Final[Mapping[int, GpuHealthReason]] = MappingProxyType(
    {
        agent_pb2.GPU_HEALTH_REASON_ECC_ERRORS_DETECTED: GpuHealthReason.ECC_ERRORS_DETECTED,
        agent_pb2.GPU_HEALTH_REASON_QUERY_UNSUPPORTED: GpuHealthReason.QUERY_UNSUPPORTED,
        agent_pb2.GPU_HEALTH_REASON_QUERY_FAILED: GpuHealthReason.QUERY_FAILED,
    }
)
_TOPOLOGY_STATE_MAP: Final[Mapping[int, TopologyState]] = MappingProxyType(
    {
        agent_pb2.TOPOLOGY_STATE_PRESENT: TopologyState.PRESENT,
        agent_pb2.TOPOLOGY_STATE_ABSENT: TopologyState.ABSENT,
        agent_pb2.TOPOLOGY_STATE_UNSUPPORTED: TopologyState.UNSUPPORTED,
        agent_pb2.TOPOLOGY_STATE_QUERY_FAILED: TopologyState.QUERY_FAILED,
    }
)
_PCIE_RELATION_MAP: Final[Mapping[int, PcieRelation]] = MappingProxyType(
    {
        agent_pb2.PCIE_RELATION_INTERNAL: PcieRelation.INTERNAL,
        agent_pb2.PCIE_RELATION_SINGLE_SWITCH: PcieRelation.SINGLE_SWITCH,
        agent_pb2.PCIE_RELATION_MULTIPLE_SWITCHES: PcieRelation.MULTIPLE_SWITCHES,
        agent_pb2.PCIE_RELATION_HOST_BRIDGE: PcieRelation.HOST_BRIDGE,
        agent_pb2.PCIE_RELATION_NUMA_NODE: PcieRelation.NUMA_NODE,
        agent_pb2.PCIE_RELATION_SYSTEM: PcieRelation.SYSTEM,
    }
)


def _topology_state(value: int) -> TopologyState:
    return _TOPOLOGY_STATE_MAP.get(value, TopologyState.QUERY_FAILED)


def agent_response(
    observation: AgentSnapshot | AgentUnavailable,
) -> AgentAvailableResponse | AgentUnavailableResponse:
    """Render one Agent observation; the single host/GPU projection in the API."""
    if isinstance(observation, AgentUnavailable):
        return AgentUnavailableResponse(reason=observation.reason)
    host = observation.host_info
    inventory = observation.gpu_inventory
    devices = tuple(
        GpuDeviceResponse(
            uuid=device.uuid,
            display_index=device.display_index if device.HasField("display_index") else None,
            model=device.model,
            memory_bytes=device.memory_bytes,
            health=_HEALTH_STATE_MAP.get(device.health, GpuHealthState.UNKNOWN),
            health_reasons=tuple(
                _HEALTH_REASON_MAP.get(reason, GpuHealthReason.PROTOCOL_INVALID)
                for reason in device.health_reasons
            ),
        )
        for device in inventory.devices
    )
    links = tuple(
        GpuTopologyLinkResponse(
            gpu_a_uuid=link.gpu_a_uuid,
            gpu_b_uuid=link.gpu_b_uuid,
            pcie_state=_topology_state(link.pcie_state),
            pcie_relation=(
                _PCIE_RELATION_MAP.get(link.pcie_relation)
                if link.HasField("pcie_relation")
                else None
            ),
            nvlink_state=_topology_state(link.nvlink_state),
            p2p_read_state=_topology_state(link.p2p_read_state),
        )
        for link in inventory.topology_links
    )
    return AgentAvailableResponse(
        host=HostInfoResponse(
            logical_cpu_count=uint_observation(host.logical_cpu_count),
            total_memory_bytes=uint_observation(host.total_memory_bytes),
            total_disk_bytes=uint_observation(host.total_disk_bytes),
            available_disk_bytes=uint_observation(host.available_disk_bytes),
            driver_version=string_observation(host.driver_version),
            cuda_driver_version=string_observation(host.cuda_driver_version),
        ),
        gpus=GpuInventoryResponse(
            discovery_state=_DISCOVERY_STATE_MAP.get(
                inventory.discovery_state, GpuDiscoveryState.UNAVAILABLE
            ),
            failure_reason=_DISCOVERY_REASON_MAP.get(
                inventory.failure_reason, GpuDiscoveryFailureReason.PROTOCOL_INVALID
            ),
            snapshot_id=inventory.snapshot_id if inventory.HasField("snapshot_id") else None,
            devices=devices,
            topology_links=links,
        ),
    )


def _known_version(value: str) -> KnownString:
    return KnownString(value=value)


def _unknown_version(reason: ObservationUnknownReason) -> UnknownString:
    return UnknownString(reason=reason)


def _heartbeat_state(
    heartbeat: LatestHeartbeat | None,
    *,
    database_available: bool,
) -> tuple[SystemComponentStatus, SystemComponentReason, StringObservation]:
    if not database_available:
        return (
            SystemComponentStatus.UNAVAILABLE,
            SystemComponentReason.SIGNAL_UNAVAILABLE,
            _unknown_version(ObservationUnknownReason.SOURCE_UNAVAILABLE),
        )
    if heartbeat is None:
        return (
            SystemComponentStatus.NOT_DEPLOYED,
            SystemComponentReason.NO_HEARTBEAT,
            _unknown_version(ObservationUnknownReason.NOT_DEPLOYED),
        )
    version = _known_version(heartbeat.version)
    if heartbeat.status is ComponentStatus.STOPPING:
        return SystemComponentStatus.DEGRADED, SystemComponentReason.HEARTBEAT_STOPPING, version
    if heartbeat.status is ComponentStatus.DEGRADED:
        return SystemComponentStatus.DEGRADED, SystemComponentReason.HEARTBEAT_DEGRADED, version
    if heartbeat.age_seconds > HEARTBEAT_STALE_AFTER_SECONDS:
        return SystemComponentStatus.DEGRADED, SystemComponentReason.HEARTBEAT_STALE, version
    return SystemComponentStatus.READY, SystemComponentReason.HEARTBEAT_FRESH, version


def ledger_backlog_component_state(
    status: SystemComponentStatus,
    reason: SystemComponentReason,
    backlog: LedgerHealth | None,
) -> tuple[SystemComponentStatus, SystemComponentReason]:
    """Fold the Ledger backlog verdict into one component's state.

    The backlog can only make a component look worse: an Available Ledger never
    upgrades a component whose heartbeat is already telling a different story,
    and a missing verdict is treated as no signal rather than as good news.
    """

    if backlog is None or backlog is LedgerHealth.AVAILABLE:
        return status, reason
    if backlog is LedgerHealth.UNAVAILABLE:
        return SystemComponentStatus.UNAVAILABLE, SystemComponentReason.LEDGER_UNAVAILABLE
    if status is SystemComponentStatus.UNAVAILABLE:
        return status, reason
    return SystemComponentStatus.DEGRADED, SystemComponentReason.LEDGER_BACKLOG


def _component_details(
    component_id: SystemComponentId,
    snapshot: SystemStatusSnapshot | None,
    backlog: LedgerBacklogDetails | None,
) -> ComponentDetails:
    if component_id is SystemComponentId.REQUEST_LEDGER_WRITER:
        return RequestLedgerWriterDetails(
            pending_event_count=(
                KnownUInt64(value=backlog.pending_event_count)
                if backlog is not None and backlog.pending_event_count is not None
                else UnknownUInt64(reason=ObservationUnknownReason.NOT_COLLECTED)
            ),
            oldest_event_at=(
                KnownTimestamp(value=backlog.oldest_event_at)
                if backlog is not None and backlog.oldest_event_at is not None
                else UnknownValue(
                    reason=(
                        ObservationUnknownReason.EMPTY_VALUE
                        if backlog is not None and backlog.pending_event_count == 0
                        else ObservationUnknownReason.NOT_COLLECTED
                    )
                )
            ),
        )
    if component_id is SystemComponentId.DATABASE:
        return DatabaseDetails(
            schema_revision=(
                KnownString(value=snapshot.schema_revision)
                if snapshot is not None and snapshot.schema_revision is not None
                else UnknownString(
                    reason=(
                        ObservationUnknownReason.SOURCE_UNAVAILABLE
                        if snapshot is None
                        else ObservationUnknownReason.NOT_COLLECTED
                    )
                )
            )
        )
    return NoComponentDetails()


def summarize_component_statuses(
    statuses: Iterable[SystemComponentStatus],
) -> ComponentSummaryResponse:
    """Classify component states without collapsing the explicit not-deployed state."""
    materialized = tuple(statuses)
    ready_count = sum(status is SystemComponentStatus.READY for status in materialized)
    abnormal_count = sum(
        status in {SystemComponentStatus.DEGRADED, SystemComponentStatus.UNAVAILABLE}
        for status in materialized
    )
    not_deployed_count = sum(
        status is SystemComponentStatus.NOT_DEPLOYED for status in materialized
    )
    return ComponentSummaryResponse(
        status=(
            PlatformAggregateStatus.DEGRADED if abnormal_count else PlatformAggregateStatus.READY
        ),
        ready_count=ready_count,
        abnormal_count=abnormal_count,
        not_deployed_count=not_deployed_count,
    )


def _host_current(agent: AgentSnapshot | AgentUnavailable, field: str | None) -> StringObservation:
    if isinstance(agent, AgentUnavailable):
        return UnknownString(reason=ObservationUnknownReason.SOURCE_UNAVAILABLE)
    if field is None:
        return UnknownString(reason=ObservationUnknownReason.NOT_COLLECTED)
    return string_observation(getattr(agent.host_info, field))


def _minimum_reason(
    agent: AgentSnapshot | AgentUnavailable,
    current: StringObservation,
    minimum: str | None,
) -> HostEnvironmentReason:
    if isinstance(agent, AgentUnavailable):
        return HostEnvironmentReason.AGENT_UNAVAILABLE
    if isinstance(current, UnknownString):
        return HostEnvironmentReason.CURRENT_UNAVAILABLE
    if minimum is None:
        return HostEnvironmentReason.MINIMUM_NOT_DECLARED
    comparison = compare_versions(current.value, minimum)
    if comparison is None:
        return HostEnvironmentReason.VERSION_UNPARSEABLE
    return (
        HostEnvironmentReason.MEETS_MINIMUM
        if comparison >= 0
        else HostEnvironmentReason.BELOW_MINIMUM
    )


def host_environment_response(
    agent: AgentSnapshot | AgentUnavailable,
    requirements: HostRequirements | None,
) -> HostEnvironmentResponse:
    items = []
    for item_id, field in (
        (HostEnvironmentId.NVIDIA_DRIVER, "driver_version"),
        (HostEnvironmentId.DOCKER, "docker_version"),
        (HostEnvironmentId.COMPOSE, "compose_version"),
        (HostEnvironmentId.NVIDIA_CONTAINER_TOOLKIT, "toolkit_version"),
    ):
        current = _host_current(agent, field)
        minimum = None if requirements is None else getattr(requirements, item_id.value)
        reason = _minimum_reason(agent, current, minimum)
        status = {
            HostEnvironmentReason.MEETS_MINIMUM: HostEnvironmentStatus.SATISFIED,
            HostEnvironmentReason.BELOW_MINIMUM: HostEnvironmentStatus.UNSATISFIED,
        }.get(reason, HostEnvironmentStatus.UNKNOWN)
        items.append(
            HostEnvironmentItem(
                id=item_id,
                current=current,
                minimum=minimum,
                status=status,
                reason=reason,
            )
        )
    return HostEnvironmentResponse(
        cuda_driver_version=_host_current(agent, "cuda_driver_version"),
        items=tuple(items),
    )


def inference_engines_response(
    agent: AgentSnapshot | AgentUnavailable,
    catalog: RuntimeCompatibilityCatalog | None,
    snapshot: SystemStatusSnapshot | None,
) -> tuple[InferenceEngineResponse, ...]:
    if catalog is None:
        return ()
    driver = _host_current(agent, "driver_version")
    engines = []
    for runtime in catalog.runtimes.values():
        host_reason = _minimum_reason(agent, driver, runtime.minimum_driver_version)
        reason = (
            DriverCompatibilityReason.DRIVER_UNAVAILABLE
            if host_reason is HostEnvironmentReason.CURRENT_UNAVAILABLE
            else DriverCompatibilityReason(host_reason.value)
        )
        status = {
            DriverCompatibilityReason.MEETS_MINIMUM: DriverCompatibility.COMPATIBLE,
            DriverCompatibilityReason.BELOW_MINIMUM: DriverCompatibility.INCOMPATIBLE,
        }.get(reason, DriverCompatibility.UNKNOWN)
        # Explicit projection excludes executable paths and other internal runtime configuration.
        engines.append(
            InferenceEngineResponse(
                name=runtime.name,
                version=runtime.version,
                launcher=runtime.launcher,
                image_digest=runtime.image_digest,
                active_instance_count=(
                    None
                    if snapshot is None
                    else snapshot.active_instance_counts.get(runtime.name, 0)
                ),
                minimum_driver_version=runtime.minimum_driver_version,
                driver_compatibility=status,
                reason=reason,
            )
        )
    return tuple(engines)


class SystemStatusService:
    __slots__ = (
        "_agent",
        "_database",
        "_ledger_health",
        "_ledger_details",
        "_version",
        "_probes",
        "_host_requirements",
        "_runtime_catalog",
    )

    def __init__(
        self,
        *,
        agent: HostAgentClient,
        database: SystemDatabaseReader,
        version: VersionPayload,
        probes: ComponentProbeClient | None = None,
        host_requirements: HostRequirements | None = None,
        runtime_catalog: RuntimeCompatibilityCatalog | None = None,
        ledger_health: Callable[[], LedgerHealth] | None = None,
        ledger_details: Callable[[], LedgerBacklogDetails | None] | None = None,
    ) -> None:
        self._host_requirements = host_requirements
        self._runtime_catalog = runtime_catalog
        self._probes = probes or ComponentProbeClient()
        self._agent = agent
        self._database = database
        self._version = version
        self._ledger_health = ledger_health
        self._ledger_details = ledger_details

    async def read_server(self) -> SystemServerResponse:
        observation = await self._agent.observe()
        return SystemServerResponse(
            observed_at=datetime.now(UTC),
            agent=agent_response(observation),
        )

    async def _read_database(self) -> SystemStatusSnapshot | None:
        try:
            return await self._database.read()
        except Exception:
            return None

    async def read_components(
        self, *, observation: AgentSnapshot | AgentUnavailable | None = None
    ) -> SystemComponentsResponse:
        async def observed_agent() -> AgentSnapshot | AgentUnavailable:
            # Bootstrap shares its observation so host facts cannot disagree
            # between the detection cards and the component evaluation.
            return observation if observation is not None else await self._agent.observe()

        snapshot, agent, (gateway, edge) = await asyncio.gather(
            self._read_database(), observed_agent(), self._probes.read()
        )
        probes = {SystemComponentId.GATEWAY: gateway, SystemComponentId.EDGE: edge}
        backlog = None if self._ledger_health is None else self._ledger_health()
        details = None if self._ledger_details is None else self._ledger_details()
        components = tuple(
            self._component(component_id, rule, snapshot, backlog, details, agent, probes)
            for component_id, rule in COMPONENT_SIGNAL_RULES.items()
        )
        return SystemComponentsResponse(
            observed_at=datetime.now(UTC),
            platform=PlatformVersionResponse(build_version=self._version.build_version),
            summary=summarize_component_statuses(component.status for component in components),
            components=components,
            host_environment=host_environment_response(agent, self._host_requirements),
            inference_engines=inference_engines_response(agent, self._runtime_catalog, snapshot),
        )

    def _component(
        self,
        component_id: SystemComponentId,
        rule: ComponentSignalRule,
        snapshot: SystemStatusSnapshot | None,
        ledger_backlog: LedgerHealth | None,
        ledger_details: LedgerBacklogDetails | None,
        agent: AgentSnapshot | AgentUnavailable,
        probes: Mapping[SystemComponentId, ProbeObservation | None],
    ) -> SystemComponentResponse:
        remaining_sources = set(rule.sources)
        if not remaining_sources:
            status = SystemComponentStatus.NOT_DEPLOYED
            reason = SystemComponentReason.NO_SIGNAL_SOURCE
            version: StringObservation = _unknown_version(ObservationUnknownReason.NOT_DEPLOYED)
        elif ComponentSignalSource.SELF in remaining_sources:
            remaining_sources.remove(ComponentSignalSource.SELF)
            status = SystemComponentStatus.READY
            reason = SystemComponentReason.SELF_READY
            version = _known_version(self._version.build_version)
        elif ComponentSignalSource.PROBE in remaining_sources:
            remaining_sources.remove(ComponentSignalSource.PROBE)
            probe = probes[component_id]
            if probe is None:
                status = SystemComponentStatus.NOT_DEPLOYED
                reason = SystemComponentReason.NO_SIGNAL_SOURCE
                version = _unknown_version(ObservationUnknownReason.NOT_DEPLOYED)
            else:
                reason = SystemComponentReason(probe.reason)
                status = (
                    SystemComponentStatus.READY
                    if reason is SystemComponentReason.PROBE_READY
                    else SystemComponentStatus.UNAVAILABLE
                )
                version = (
                    _known_version(probe.build_version)
                    if probe.build_version is not None
                    else _unknown_version(
                        ObservationUnknownReason.NOT_COLLECTED
                        if component_id is SystemComponentId.EDGE
                        else ObservationUnknownReason.SOURCE_UNAVAILABLE
                    )
                )
        elif ComponentSignalSource.AGENT in remaining_sources:
            remaining_sources.remove(ComponentSignalSource.AGENT)
            available = isinstance(agent, AgentSnapshot)
            status = SystemComponentStatus.READY if available else SystemComponentStatus.UNAVAILABLE
            reason = (
                SystemComponentReason.AGENT_AVAILABLE
                if available
                else SystemComponentReason.AGENT_UNAVAILABLE
            )
            # The current observation RPC does not supply an Agent build version.
            version = _unknown_version(ObservationUnknownReason.NOT_COLLECTED)
        elif ComponentSignalSource.DATABASE in remaining_sources:
            remaining_sources.remove(ComponentSignalSource.DATABASE)
            if snapshot is None:
                status = SystemComponentStatus.UNAVAILABLE
                reason = SystemComponentReason.DEPENDENCY_UNAVAILABLE
                version = _unknown_version(ObservationUnknownReason.SOURCE_UNAVAILABLE)
            else:
                status = SystemComponentStatus.READY
                reason = SystemComponentReason.DEPENDENCY_READY
                version = _known_version(snapshot.postgres_version)
        elif ComponentSignalSource.HEARTBEAT in remaining_sources:
            remaining_sources.remove(ComponentSignalSource.HEARTBEAT)
            if rule.heartbeat_component is None:
                raise AssertionError("heartbeat signal has no component binding")
            heartbeat = (
                None if snapshot is None else snapshot.heartbeats.get(rule.heartbeat_component)
            )
            status, reason, version = _heartbeat_state(
                heartbeat,
                database_available=snapshot is not None,
            )
            uses_ledger_backlog = ComponentSignalSource.LEDGER_BACKLOG in remaining_sources
            if uses_ledger_backlog:
                remaining_sources.remove(ComponentSignalSource.LEDGER_BACKLOG)
            if uses_ledger_backlog and heartbeat is not None:
                # The Control API's read-only local probe stays current while
                # the Ledger Writer is stopped (the smoke path that creates a
                # backlog).  Heartbeat reasons remain the fallback for
                # deployments that have not wired that local probe yet.
                backlog = ledger_backlog
                if backlog is None and heartbeat.age_seconds <= HEARTBEAT_STALE_AFTER_SECONDS:
                    backlog = decode_ledger_backlog_reason(heartbeat.reason)
                if (
                    ledger_details is None
                    and heartbeat.age_seconds <= HEARTBEAT_STALE_AFTER_SECONDS
                ):
                    ledger_details = decode_ledger_backlog_details(heartbeat.reason)
                status, reason = ledger_backlog_component_state(status, reason, backlog)
            uses_job_signals = ComponentSignalSource.JOBS in remaining_sources
            if uses_job_signals:
                remaining_sources.remove(ComponentSignalSource.JOBS)
            if uses_job_signals and snapshot is not None and status is SystemComponentStatus.READY:
                if snapshot.expired_lease_count:
                    status = SystemComponentStatus.DEGRADED
                    reason = SystemComponentReason.SCHEDULER_LEASES_EXPIRED
                elif (
                    snapshot.oldest_executable_job_age_seconds is not None
                    and snapshot.oldest_executable_job_age_seconds > SCHEDULER_OLD_JOB_AFTER_SECONDS
                ):
                    status = SystemComponentStatus.DEGRADED
                    reason = SystemComponentReason.SCHEDULER_BACKLOG
        else:
            raise AssertionError(
                "component declares signal sources without a state evaluator: "
                f"{sorted(source.value for source in remaining_sources)}"
            )
        if remaining_sources:
            raise AssertionError(
                "component state evaluator did not consume declared signal sources: "
                f"{sorted(source.value for source in remaining_sources)}"
            )
        return SystemComponentResponse(
            id=component_id,
            status=status,
            reason=reason,
            version=version,
            signal_sources=rule.sources,
            details=_component_details(component_id, snapshot, ledger_details),
        )


__all__ = [
    "AgentAvailableResponse",
    "AgentResponse",
    "AgentUnavailableResponse",
    "HostInfoResponse",
    "GpuInventoryResponse",
    "StringObservation",
    "UInt64Observation",
    "agent_response",
    "ledger_backlog_component_state",
    "string_observation",
    "uint_observation",
    "COMPONENT_SIGNAL_RULES",
    "HEARTBEAT_STALE_AFTER_SECONDS",
    "SCHEDULER_OLD_JOB_AFTER_SECONDS",
    "AgentAccessStatus",
    "ComponentSignalRule",
    "ComponentSignalSource",
    "GpuDiscoveryFailureReason",
    "GpuDiscoveryState",
    "GpuHealthState",
    "ObservationState",
    "ObservationUnknownReason",
    "PlatformAggregateStatus",
    "SystemComponentId",
    "SystemComponentReason",
    "SystemComponentResponse",
    "SystemComponentStatus",
    "SystemComponentsResponse",
    "SystemDatabaseReader",
    "SystemServerResponse",
    "SystemStatusService",
    "assert_component_signal_map_complete",
    "summarize_component_statuses",
]

"""Standard-library Fake Host Agent fixture for M0-06D.

All behavior is ``待真机复核`` and must be reconciled with the certified Host
Agent and Docker behavior during the applicable G1 test window.  This package
never imports a Docker/NVML client, opens the Docker socket, or executes a
command.
"""

from .agent import (
    DOWNLOAD_WORKER_UID,
    INSTANCE_CONTROLLER_UID,
    RPC_ALLOWLIST,
    TELEMETRY_WORKER_UID,
    FakeHostAgent,
    is_immutable_image_digest,
)
from .models import (
    FIXTURE_REVIEW_STATUS,
    MAX_LOG_LINE_CHARACTERS,
    MAX_LOG_PAGE_SIZE,
    PLATFORM_LABEL,
    ArbitraryMount,
    AuditDenialReason,
    ContainerStatus,
    DenialAuditRecord,
    FixtureId,
    LaunchRejected,
    LaunchRejectionReason,
    LaunchSpec,
    LifecycleAction,
    LifecycleEvent,
    LogCursor,
    LogEntry,
    LogPage,
    PeerKind,
    PermissionDenied,
    RemovalRejected,
    RemovalRejectionReason,
    RpcMethod,
    RuntimeInspection,
)
from .version_queries import (
    FX_HA_COMPOSE_VERSION,
    FX_HA_COMPOSE_VERSION_PREFIXED,
    FX_HA_DOCKER_VERSION,
    FX_HA_TOOLKIT_VERSION,
    RawVersionQueryFixture,
)

__all__ = [
    "DOWNLOAD_WORKER_UID",
    "FIXTURE_REVIEW_STATUS",
    "INSTANCE_CONTROLLER_UID",
    "MAX_LOG_LINE_CHARACTERS",
    "MAX_LOG_PAGE_SIZE",
    "PLATFORM_LABEL",
    "RPC_ALLOWLIST",
    "TELEMETRY_WORKER_UID",
    "ArbitraryMount",
    "AuditDenialReason",
    "ContainerStatus",
    "DenialAuditRecord",
    "FakeHostAgent",
    "FX_HA_COMPOSE_VERSION",
    "FX_HA_COMPOSE_VERSION_PREFIXED",
    "FX_HA_DOCKER_VERSION",
    "FX_HA_TOOLKIT_VERSION",
    "FixtureId",
    "LaunchRejected",
    "LaunchRejectionReason",
    "LaunchSpec",
    "LifecycleAction",
    "LifecycleEvent",
    "LogCursor",
    "LogEntry",
    "LogPage",
    "PeerKind",
    "PermissionDenied",
    "RemovalRejected",
    "RemovalRejectionReason",
    "RpcMethod",
    "RawVersionQueryFixture",
    "RuntimeInspection",
    "is_immutable_image_digest",
]

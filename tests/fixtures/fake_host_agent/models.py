"""Typed models for the standard-library Fake Host Agent fixture.

This module models only the injection and assertion points required by
``FX-HA-01`` through ``FX-HA-06``.  It is not a production Launch Spec or
Host Agent implementation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

FIXTURE_REVIEW_STATUS: Final = "待真机复核"
PLATFORM_LABEL: Final = "holm"
MAX_LOG_LINE_CHARACTERS: Final = 65_536
MAX_LOG_PAGE_SIZE: Final = 100


class FixtureId(StrEnum):
    """TST 3.3 Fake Host Agent fixture identifiers."""

    FX_HA_01 = "FX-HA-01"
    FX_HA_02 = "FX-HA-02"
    FX_HA_03 = "FX-HA-03"
    FX_HA_04 = "FX-HA-04"
    FX_HA_05 = "FX-HA-05"
    FX_HA_06 = "FX-HA-06"


class RpcMethod(StrEnum):
    """The finite RPC surface understood by this fixture."""

    GET_HOST_INFO = "GetHostInfo"
    LIST_GPUS = "ListGPUs"
    ENSURE_RUNTIME = "EnsureRuntime"
    STOP_RUNTIME = "StopRuntime"
    REMOVE_RUNTIME = "RemoveRuntime"
    INSPECT_RUNTIME = "InspectRuntime"
    LIST_RUNTIMES = "ListRuntimes"
    STREAM_LOGS = "StreamLogs"
    VALIDATE_PATH = "ValidatePath"


class PeerKind(StrEnum):
    """Sanitized caller categories safe to retain in denial audit records."""

    DOWNLOAD_WORKER = "download_worker"
    TELEMETRY_WORKER = "telemetry_worker"
    INSTANCE_CONTROLLER = "instance_controller"
    CONTAINER_ROOT = "container_root"
    UNKNOWN = "unknown"


class AuditDenialReason(StrEnum):
    """Stable, content-free reasons for rejected RPC authorization."""

    CONTAINER_ROOT_FORBIDDEN = "container_root_forbidden"
    UNKNOWN_UID = "unknown_uid"
    METHOD_NOT_ALLOWED = "method_not_allowed"


@dataclass(frozen=True, slots=True)
class DenialAuditRecord:
    """A deliberately small audit allowlist that never retains request data."""

    event: str
    peer_kind: PeerKind
    reason: AuditDenialReason
    rpc_method: RpcMethod | None


class ContainerStatus(StrEnum):
    """Container states needed by the lifecycle fixture."""

    RUNNING = "running"
    STOPPED = "stopped"


class LifecycleAction(StrEnum):
    """Observable ordered lifecycle operations."""

    CREATE = "create"
    GRACEFUL_STOP = "graceful_stop"
    KILL_AFTER_TIMEOUT = "kill_after_timeout"
    REMOVE = "remove"


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    """One content-free lifecycle assertion point."""

    runtime_id: str
    action: LifecycleAction
    timeout_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class ArbitraryMount:
    """A caller-supplied mount that the Host Agent boundary must reject."""

    source: str
    target: str
    read_only: bool = True


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    """Minimal fixture input, not the production Runtime Launch Spec contract."""

    runtime_id: str
    image_digest: str
    gpu_uuids: tuple[str, ...]
    model_path: str = "/fixture/model"
    shell: str | None = None
    docker_parameters: Mapping[str, str] | None = None
    mounts: tuple[ArbitraryMount, ...] = ()

    def __post_init__(self) -> None:
        if not self.runtime_id:
            raise ValueError("runtime_id must be non-empty")
        if not self.gpu_uuids:
            raise ValueError("gpu_uuids must be non-empty")
        if len(set(self.gpu_uuids)) != len(self.gpu_uuids):
            raise ValueError("gpu_uuids must not contain duplicates")
        if self.docker_parameters is not None:
            object.__setattr__(
                self,
                "docker_parameters",
                MappingProxyType(dict(self.docker_parameters)),
            )


@dataclass(frozen=True, slots=True)
class RuntimeInspection:
    """InspectRuntime fields plus fixture identity and observed GPU ownership."""

    runtime_id: str
    status: ContainerStatus
    exit_code: int | None
    oom_killed: bool | None
    internal_address: str
    gpu_uuids: tuple[str, ...] | None


class LaunchRejectionReason(StrEnum):
    """Injectable and boundary rejection reasons without raw input values."""

    IMAGE_DIGEST_NOT_ALLOWED = "image_digest_not_allowed"
    GPU_OCCUPIED_BY_OTHER_PLATFORM = "gpu_occupied_by_other_platform"
    MOUNT_VALIDATION_FAILED = "mount_validation_failed"
    SHELL_NOT_ALLOWED = "shell_not_allowed"
    DOCKER_PARAMETERS_NOT_ALLOWED = "docker_parameters_not_allowed"
    ARBITRARY_MOUNT_NOT_ALLOWED = "arbitrary_mount_not_allowed"
    RUNTIME_SPEC_CONFLICT = "runtime_spec_conflict"


class RemovalRejectionReason(StrEnum):
    """Reasons RemoveRuntime can refuse a fixture container."""

    PLATFORM_LABEL_REQUIRED = "platform_label_required"
    CONTAINER_MUST_BE_STOPPED = "container_must_be_stopped"


class PermissionDenied(RuntimeError):
    """Raised after an RPC denial has been recorded in the audit sink."""

    def __init__(self, reason: AuditDenialReason) -> None:
        self.reason = reason
        super().__init__("Host Agent RPC permission denied")


class LaunchRejected(RuntimeError):
    """Raised for a sanitized Launch Spec or injected startup rejection."""

    def __init__(self, reason: LaunchRejectionReason) -> None:
        self.reason = reason
        super().__init__("Runtime launch rejected")


class RemovalRejected(RuntimeError):
    """Raised when RemoveRuntime ownership or state preconditions fail."""

    def __init__(self, reason: RemovalRejectionReason) -> None:
        self.reason = reason
        super().__init__("Runtime removal rejected")


@dataclass(frozen=True, slots=True)
class LogEntry:
    """One bounded retained line with its monotonic per-Runtime sequence."""

    sequence: int
    text: str
    truncated: bool
    original_character_count: int


@dataclass(frozen=True, slots=True)
class LogCursor:
    """A cursor bound to one Runtime so it cannot cross log streams."""

    runtime_id: str
    offset: int


@dataclass(frozen=True, slots=True)
class LogPage:
    """One bounded page returned by StreamLogs."""

    entries: tuple[LogEntry, ...]
    next_cursor: LogCursor | None

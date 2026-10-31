"""Pure in-memory Fake Host Agent for TST ``FX-HA-01`` through ``FX-HA-06``."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, NoReturn

from .models import (
    FIXTURE_REVIEW_STATUS,
    MAX_LOG_LINE_CHARACTERS,
    MAX_LOG_PAGE_SIZE,
    PLATFORM_LABEL,
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

DOWNLOAD_WORKER_UID: Final = 11_001
TELEMETRY_WORKER_UID: Final = 11_002
INSTANCE_CONTROLLER_UID: Final = 11_003

_UID_TO_PEER: Final[Mapping[int, PeerKind]] = MappingProxyType(
    {
        DOWNLOAD_WORKER_UID: PeerKind.DOWNLOAD_WORKER,
        TELEMETRY_WORKER_UID: PeerKind.TELEMETRY_WORKER,
        INSTANCE_CONTROLLER_UID: PeerKind.INSTANCE_CONTROLLER,
    }
)

# This is intentionally a positive allowlist. There is no fallback branch that grants
# an unlisted method, so extending RpcMethod alone cannot grant any caller access.
RPC_ALLOWLIST: Final[Mapping[PeerKind, frozenset[RpcMethod]]] = MappingProxyType(
    {
        PeerKind.DOWNLOAD_WORKER: frozenset(
            {
                RpcMethod.GET_HOST_INFO,
                RpcMethod.VALIDATE_PATH,
            }
        ),
        PeerKind.TELEMETRY_WORKER: frozenset(
            {
                RpcMethod.GET_HOST_INFO,
                RpcMethod.LIST_GPUS,
                RpcMethod.INSPECT_RUNTIME,
            }
        ),
        PeerKind.INSTANCE_CONTROLLER: frozenset(
            {
                RpcMethod.ENSURE_RUNTIME,
                RpcMethod.STOP_RUNTIME,
                RpcMethod.REMOVE_RUNTIME,
                RpcMethod.INSPECT_RUNTIME,
                RpcMethod.LIST_RUNTIMES,
                RpcMethod.STREAM_LOGS,
            }
        ),
    }
)

_IMAGE_DIGEST_PATTERN: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")


def is_immutable_image_digest(value: str) -> bool:
    """Return whether a value is a canonical immutable sha256 image digest."""

    return _IMAGE_DIGEST_PATTERN.fullmatch(value) is not None


@dataclass(slots=True)
class _RuntimeState:
    spec: LaunchSpec
    status: ContainerStatus
    exit_code: int | None
    oom_killed: bool | None
    internal_address: str
    platform_label: str | None
    observed_gpu_uuids: tuple[str, ...] | None
    graceful_stop_succeeds: bool = True


class FakeHostAgent:
    """An isolated Fake Host Agent with no Docker, socket, NVML, or shell access."""

    review_status: Final = FIXTURE_REVIEW_STATUS
    fixture_ids: Final = tuple(FixtureId)

    def __init__(
        self,
        *,
        allowed_image_digests: Iterable[str],
        max_log_line_characters: int = 4_096,
    ) -> None:
        digests = frozenset(allowed_image_digests)
        if not digests:
            raise ValueError("Fake Host Agent requires at least one allowed image digest")
        if not all(is_immutable_image_digest(digest) for digest in digests):
            raise ValueError("image allowlist entries must be immutable sha256 digests")
        if not 1 <= max_log_line_characters <= MAX_LOG_LINE_CHARACTERS:
            raise ValueError("max_log_line_characters exceeds the fixture hard limit")

        self._allowed_image_digests = digests
        self._max_log_line_characters = max_log_line_characters
        self._audit_records: list[DenialAuditRecord] = []
        self._runtimes: dict[str, _RuntimeState] = {}
        self._lifecycle_events: list[LifecycleEvent] = []
        self._gpu_occupied_by_other_platform: set[str] = set()
        self._mount_validation_failures: set[str] = set()
        self._logs: dict[str, list[LogEntry]] = {}

    @property
    def audit_records(self) -> tuple[DenialAuditRecord, ...]:
        """Return an immutable view of sanitized denial audit records."""

        return tuple(self._audit_records)

    @property
    def lifecycle_events(self) -> tuple[LifecycleEvent, ...]:
        """Return the ordered lifecycle calls made against the fixture."""

        return tuple(self._lifecycle_events)

    @property
    def runtime_count(self) -> int:
        """Return the number of currently retained fixture containers."""

        return len(self._runtimes)

    @property
    def retained_log_characters(self) -> int:
        """Return retained log payload size after per-line bounding."""

        return sum(len(entry.text) for entries in self._logs.values() for entry in entries)

    @property
    def max_log_line_characters(self) -> int:
        """Expose the configured retention bound for downstream assertions."""

        return self._max_log_line_characters

    def require_allowed(self, peer_uid: int, method_name: str) -> RpcMethod:
        """Authorize one method by explicit membership, auditing every denial."""

        peer_kind = self._peer_kind(peer_uid)
        try:
            method = RpcMethod(method_name)
        except ValueError:
            method = None

        if peer_kind is PeerKind.CONTAINER_ROOT:
            self._deny(peer_kind, AuditDenialReason.CONTAINER_ROOT_FORBIDDEN, method)
        if peer_kind is PeerKind.UNKNOWN:
            self._deny(peer_kind, AuditDenialReason.UNKNOWN_UID, method)
        if method is None or method not in RPC_ALLOWLIST.get(peer_kind, frozenset()):
            self._deny(peer_kind, AuditDenialReason.METHOD_NOT_ALLOWED, method)
        return method

    def ensure_runtime(self, spec: LaunchSpec) -> RuntimeInspection:
        """Idempotently retain a valid Runtime or raise one injected safe failure."""

        self._validate_boundary_fields(spec)
        existing = self._runtimes.get(spec.runtime_id)
        if existing is not None:
            if existing.spec != spec:
                raise LaunchRejected(LaunchRejectionReason.RUNTIME_SPEC_CONFLICT)
            return self._inspection(existing)

        if spec.runtime_id in self._mount_validation_failures:
            raise LaunchRejected(LaunchRejectionReason.MOUNT_VALIDATION_FAILED)
        if any(gpu_uuid in self._gpu_occupied_by_other_platform for gpu_uuid in spec.gpu_uuids):
            raise LaunchRejected(LaunchRejectionReason.GPU_OCCUPIED_BY_OTHER_PLATFORM)

        state = _RuntimeState(
            spec=spec,
            status=ContainerStatus.RUNNING,
            exit_code=None,
            oom_killed=None,
            internal_address=f"runtime-{spec.runtime_id}.holm-runtime",
            platform_label=PLATFORM_LABEL,
            observed_gpu_uuids=spec.gpu_uuids,
        )
        self._runtimes[spec.runtime_id] = state
        self._lifecycle_events.append(
            LifecycleEvent(runtime_id=spec.runtime_id, action=LifecycleAction.CREATE)
        )
        return self._inspection(state)

    def inject_gpu_occupied_by_other_platform(self, gpu_uuid: str) -> None:
        """Inject the FX-HA-03 conflicting platform-label condition."""

        self._gpu_occupied_by_other_platform.add(gpu_uuid)

    def inject_mount_validation_failure(self, runtime_id: str) -> None:
        """Inject a typed model/cache mount validation failure."""

        self._mount_validation_failures.add(runtime_id)

    def inject_graceful_stop_timeout(self, runtime_id: str) -> None:
        """Make the next stop require Kill after its graceful timeout."""

        self._runtime(runtime_id).graceful_stop_succeeds = False

    def stop_runtime(
        self,
        runtime_id: str,
        *,
        timeout_seconds: float,
    ) -> RuntimeInspection:
        """Simulate graceful stop and deterministically inject timeout Kill."""

        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        state = self._runtime(runtime_id)
        if state.status is ContainerStatus.STOPPED:
            return self._inspection(state)

        self._lifecycle_events.append(
            LifecycleEvent(
                runtime_id=runtime_id,
                action=LifecycleAction.GRACEFUL_STOP,
                timeout_seconds=timeout_seconds,
            )
        )
        if state.graceful_stop_succeeds:
            state.status = ContainerStatus.STOPPED
            state.exit_code = 0 if state.exit_code is None else state.exit_code
            state.oom_killed = False if state.oom_killed is None else state.oom_killed
            return self._inspection(state)

        self._lifecycle_events.append(
            LifecycleEvent(
                runtime_id=runtime_id,
                action=LifecycleAction.KILL_AFTER_TIMEOUT,
                timeout_seconds=timeout_seconds,
            )
        )
        state.status = ContainerStatus.STOPPED
        state.exit_code = 137 if state.exit_code is None else state.exit_code
        state.oom_killed = False if state.oom_killed is None else state.oom_killed
        return self._inspection(state)

    def remove_runtime(self, runtime_id: str) -> None:
        """Remove only a stopped container carrying the platform ownership label."""

        state = self._runtime(runtime_id)
        if state.platform_label != PLATFORM_LABEL:
            raise RemovalRejected(RemovalRejectionReason.PLATFORM_LABEL_REQUIRED)
        if state.status is not ContainerStatus.STOPPED:
            raise RemovalRejected(RemovalRejectionReason.CONTAINER_MUST_BE_STOPPED)
        del self._runtimes[runtime_id]
        self._logs.pop(runtime_id, None)
        self._lifecycle_events.append(
            LifecycleEvent(runtime_id=runtime_id, action=LifecycleAction.REMOVE)
        )

    def inspect_runtime(self, runtime_id: str) -> RuntimeInspection:
        """Return state, exit code, OOM flag, and fixture-internal address."""

        return self._inspection(self._runtime(runtime_id))

    def list_runtimes(self) -> tuple[RuntimeInspection, ...]:
        """Return every Runtime retained by this Agent fixture."""

        return tuple(self._inspection(state) for state in self._runtimes.values())

    def inject_observed_gpu_uuids(self, runtime_id: str, gpu_uuids: tuple[str, ...] | None) -> None:
        """Set a GPU observation independently from the launch request."""

        if gpu_uuids is not None and len(set(gpu_uuids)) != len(gpu_uuids):
            raise ValueError("observed gpu_uuids must be unique")
        self._runtime(runtime_id).observed_gpu_uuids = gpu_uuids

    def inject_container(
        self,
        spec: LaunchSpec,
        *,
        status: ContainerStatus,
        platform_label: str | None,
        exit_code: int | None = None,
        oom_killed: bool | None = None,
    ) -> None:
        """Inject an existing container for lifecycle precondition checks."""

        if spec.runtime_id in self._runtimes:
            raise ValueError("runtime_id is already present")
        self._runtimes[spec.runtime_id] = _RuntimeState(
            spec=spec,
            status=status,
            exit_code=exit_code,
            oom_killed=oom_killed,
            internal_address=f"runtime-{spec.runtime_id}.holm-runtime",
            platform_label=platform_label,
            observed_gpu_uuids=spec.gpu_uuids,
        )

    def inject_exit(
        self,
        runtime_id: str,
        *,
        exit_code: int,
        oom_killed: bool | None,
    ) -> None:
        """Inject independently configurable exit code and OOMKilled observations."""

        state = self._runtime(runtime_id)
        state.status = ContainerStatus.STOPPED
        state.exit_code = exit_code
        state.oom_killed = oom_killed

    def inject_logs(self, runtime_id: str, lines: Iterable[str]) -> None:
        """Retain only a bounded prefix of each injected line, including ANSI text."""

        entries = self._logs.setdefault(runtime_id, [])
        for line in lines:
            original_character_count = len(line)
            bounded_text = line[: self._max_log_line_characters]
            entries.append(
                LogEntry(
                    sequence=len(entries),
                    text=bounded_text,
                    truncated=original_character_count > len(bounded_text),
                    original_character_count=original_character_count,
                )
            )

    def stream_logs(
        self,
        runtime_id: str,
        *,
        cursor: LogCursor | None = None,
        page_size: int = 50,
    ) -> LogPage:
        """Return a bounded, non-overlapping page after the supplied cursor."""

        if not 1 <= page_size <= MAX_LOG_PAGE_SIZE:
            raise ValueError("page_size exceeds the fixture hard limit")
        if cursor is not None and cursor.runtime_id != runtime_id:
            raise ValueError("log cursor belongs to another Runtime")
        entries = self._logs.get(runtime_id, [])
        start = 0 if cursor is None else cursor.offset
        if not 0 <= start <= len(entries):
            raise ValueError("log cursor offset is outside the retained stream")
        end = min(start + page_size, len(entries))
        next_cursor = LogCursor(runtime_id=runtime_id, offset=end) if end < len(entries) else None
        return LogPage(entries=tuple(entries[start:end]), next_cursor=next_cursor)

    @staticmethod
    def _peer_kind(peer_uid: int) -> PeerKind:
        if peer_uid == 0:
            return PeerKind.CONTAINER_ROOT
        return _UID_TO_PEER.get(peer_uid, PeerKind.UNKNOWN)

    def _deny(
        self,
        peer_kind: PeerKind,
        reason: AuditDenialReason,
        method: RpcMethod | None,
    ) -> NoReturn:
        self._audit_records.append(
            DenialAuditRecord(
                event="host_agent_rpc_denied",
                peer_kind=peer_kind,
                reason=reason,
                rpc_method=method,
            )
        )
        raise PermissionDenied(reason)

    def _validate_boundary_fields(self, spec: LaunchSpec) -> None:
        if spec.shell is not None:
            raise LaunchRejected(LaunchRejectionReason.SHELL_NOT_ALLOWED)
        if spec.docker_parameters:
            raise LaunchRejected(LaunchRejectionReason.DOCKER_PARAMETERS_NOT_ALLOWED)
        if spec.mounts:
            raise LaunchRejected(LaunchRejectionReason.ARBITRARY_MOUNT_NOT_ALLOWED)
        if (
            not is_immutable_image_digest(spec.image_digest)
            or spec.image_digest not in self._allowed_image_digests
        ):
            raise LaunchRejected(LaunchRejectionReason.IMAGE_DIGEST_NOT_ALLOWED)

    def _runtime(self, runtime_id: str) -> _RuntimeState:
        try:
            return self._runtimes[runtime_id]
        except KeyError as error:
            raise KeyError("Runtime is not present in Fake Host Agent") from error

    @staticmethod
    def _inspection(state: _RuntimeState) -> RuntimeInspection:
        return RuntimeInspection(
            runtime_id=state.spec.runtime_id,
            status=state.status,
            exit_code=state.exit_code,
            oom_killed=state.oom_killed,
            internal_address=state.internal_address,
            gpu_uuids=state.observed_gpu_uuids,
        )

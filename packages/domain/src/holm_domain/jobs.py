"""Framework-independent contracts for PostgreSQL-backed jobs and heartbeats."""

from __future__ import annotations

from collections.abc import Mapping, Set
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final
from uuid import UUID

EXTERNAL_REGISTRATION_JOB_TYPE: Final[str] = "download.register_external"
DOWNLOAD_RESOLUTION_JOB_TYPE: Final[str] = "download.resolve"
DOWNLOAD_CLEANUP_JOB_TYPE: Final[str] = "download.cleanup"


class JobStatus(StrEnum):
    """Persisted job states matching the database values."""

    QUEUED = "Queued"
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"


JOB_TRANSITIONS: Final[Mapping[JobStatus, Set[JobStatus]]] = MappingProxyType(
    {
        JobStatus.QUEUED: frozenset({JobStatus.RUNNING}),
        JobStatus.RUNNING: frozenset({JobStatus.QUEUED, JobStatus.SUCCEEDED, JobStatus.FAILED}),
        JobStatus.SUCCEEDED: frozenset(),
        JobStatus.FAILED: frozenset(),
    }
)


class Component(StrEnum):
    """Deployable processes that publish worker heartbeats."""

    DOWNLOAD_WORKER = "download-worker"
    INSTANCE_CONTROLLER = "instance-controller"
    TELEMETRY_WORKER = "telemetry-worker"
    LEDGER_WRITER = "ledger-writer"
    MAINTENANCE_WORKER = "maintenance-worker"


class ComponentStatus(StrEnum):
    """A worker's own short health statement, before system-status aggregation."""

    HEALTHY = "Healthy"
    DEGRADED = "Degraded"
    STOPPING = "Stopping"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded exponential retry policy shared by repositories and tests."""

    max_attempts: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or self.max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        if self.base_delay_seconds <= 0:
            raise ValueError("base_delay_seconds must be positive")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must not be below the base delay")

    def delay_after(self, failed_attempt: int) -> float:
        """Return the delay after a one-based failed attempt."""
        if isinstance(failed_attempt, bool) or not 1 <= failed_attempt <= self.max_attempts:
            raise ValueError("failed_attempt is outside the bounded policy")
        return min(
            self.max_delay_seconds,
            self.base_delay_seconds * float(2 ** (failed_attempt - 1)),
        )


@dataclass(frozen=True, slots=True)
class JobExecutionKey:
    """The mandatory idempotency identity supplied to every job handler."""

    job_id: UUID
    target_object_version: int

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, UUID):
            raise TypeError("job_id must be a UUID")
        if (
            isinstance(self.target_object_version, bool)
            or not isinstance(self.target_object_version, int)
            or self.target_object_version < 0
        ):
            raise ValueError("target_object_version must be a non-negative integer")


__all__ = [
    "EXTERNAL_REGISTRATION_JOB_TYPE",
    "DOWNLOAD_RESOLUTION_JOB_TYPE",
    "DOWNLOAD_CLEANUP_JOB_TYPE",
    "JOB_TRANSITIONS",
    "Component",
    "ComponentStatus",
    "JobExecutionKey",
    "JobStatus",
    "RetryPolicy",
]

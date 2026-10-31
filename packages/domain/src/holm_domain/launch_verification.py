"""Content-free durable authorization for exactly one Runtime launch attempt."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from holm_domain.model_instances import ModelInstanceStatus


class LaunchVerificationResult(StrEnum):
    PENDING = "Pending"
    READY = "Ready"
    MISSING = "Missing"
    CORRUPTED = "Corrupted"
    FAILED = "Failed"


@dataclass(frozen=True, slots=True)
class LaunchVerification:
    request_id: UUID
    instance_id: UUID
    artifact_id: UUID
    config_version: int
    status: ModelInstanceStatus
    attempt: int
    requested_at: datetime
    result: LaunchVerificationResult
    authorized: bool = False

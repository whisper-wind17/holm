"""Closed metadata vocabulary for Request Records."""

from __future__ import annotations

from enum import StrEnum


class RequestRecordStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"
    REJECTED = "rejected"


class RequestRecordState(StrEnum):
    """Event-sourced projection states for the partitioned Usage record."""

    RECEIVED = "received"
    IN_PROGRESS = "in_progress"
    REJECTED = "rejected"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"
    UNKNOWN = "unknown"


class RequestFinishReason(StrEnum):
    STOP = "stop"
    LENGTH = "length"
    TOOL_CALLS = "tool_calls"
    CONTENT_FILTER = "content_filter"
    OTHER = "other"


def normalize_finish_reason(value: object) -> RequestFinishReason | None:
    """Map an upstream value into the closed persisted allowlist."""

    if value is None:
        return None
    if not isinstance(value, str):
        return RequestFinishReason.OTHER
    try:
        return RequestFinishReason(value)
    except ValueError:
        return RequestFinishReason.OTHER


__all__ = [
    "RequestFinishReason",
    "RequestRecordState",
    "RequestRecordStatus",
    "normalize_finish_reason",
]

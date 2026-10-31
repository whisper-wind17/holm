"""Stable cleanup outcomes; no raw Agent errors cross persistence."""

from enum import StrEnum


class CleanupFailure(StrEnum):
    AGENT_UNAVAILABLE = "runtime_agent_unavailable"
    UNSUPPORTED = "runtime_remove_unsupported"
    OWNERSHIP_CONFLICT = "runtime_ownership_conflict"
    STATE_CONFLICT = "runtime_state_conflict"
    CACHE_UNSAFE = "runtime_cache_unsafe"
    CACHE_FAILED = "runtime_cache_cleanup_failed"
    REMOVE_FAILED = "runtime_remove_failed"
    INVALID_RESPONSE = "runtime_agent_invalid_response"
    INSTANCE_EXISTS = "instance_still_exists"

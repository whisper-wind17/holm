"""Closed vocabulary and thresholds for Ledger backlog protection.

Backlog protection distinguishes three states by the age of the oldest unacknowledged Ledger
event: ``Available`` (<= 30 s), ``Delayed`` (30 s - 5 min, calls continue and
administrators see the delay) and ``Unavailable`` (> 5 min, SQLite fsync
failure, WAL corruption or a critical disk, at which point the Gateway rejects
new requests *before* forwarding). The vocabulary lives here so the Gateway,
the Ledger Writer and the Control API all classify against one definition.

Nothing in this module performs I/O: callers supply the measured age, the
observed filesystem numbers and the Spool Writer's own state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

# These defaults are the product contract; callers may only
# override them for tests and smoke runs, never to relax production behaviour.
LEDGER_DELAYED_AFTER_SECONDS: Final[float] = 30.0
LEDGER_UNAVAILABLE_AFTER_SECONDS: Final[float] = 300.0
# "Critical disk" takes whichever of the two conditions triggers first.
LEDGER_DISK_CRITICAL_FREE_RATIO: Final[float] = 0.05
LEDGER_DISK_CRITICAL_FREE_BYTES: Final[int] = 5 * 1024 * 1024 * 1024

# The Ledger Writer publishes its backlog verdict through the existing
# component heartbeat ``reason`` column; this prefix keeps that free-text column
# machine readable without inventing a second transport.
LEDGER_BACKLOG_REASON_PREFIX: Final[str] = "ledger_backlog="


class LedgerHealth(StrEnum):
    """The three ledger backlog states, ordered from healthy to worst."""

    AVAILABLE = "available"
    DELAYED = "delayed"
    UNAVAILABLE = "unavailable"


_SEVERITY: Final[dict[LedgerHealth, int]] = {
    LedgerHealth.AVAILABLE: 0,
    LedgerHealth.DELAYED: 1,
    LedgerHealth.UNAVAILABLE: 2,
}


def _validate_thresholds(delayed_after: float, unavailable_after: float) -> None:
    for value, name in ((delayed_after, "delayed"), (unavailable_after, "unavailable")):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} threshold must be a number of seconds")
        if value <= 0:
            raise ValueError(f"{name} threshold must be positive")
    if delayed_after > unavailable_after:
        raise ValueError("delayed threshold must not exceed the unavailable threshold")


def classify_backlog_age(
    age_seconds: float | None,
    *,
    delayed_after: float = LEDGER_DELAYED_AFTER_SECONDS,
    unavailable_after: float = LEDGER_UNAVAILABLE_AFTER_SECONDS,
) -> LedgerHealth:
    """Map the oldest unacknowledged event's age onto a backlog state.

    ``None`` means nothing is waiting for acknowledgement at all. The
    state boundaries are exact: 30 s is still Available and
    300 s is still Delayed; only strictly greater ages escalate.
    """

    _validate_thresholds(delayed_after, unavailable_after)
    if age_seconds is None:
        return LedgerHealth.AVAILABLE
    if isinstance(age_seconds, bool) or not isinstance(age_seconds, (int, float)):
        raise ValueError("backlog age must be a number of seconds")
    if age_seconds > unavailable_after:
        return LedgerHealth.UNAVAILABLE
    if age_seconds > delayed_after:
        return LedgerHealth.DELAYED
    return LedgerHealth.AVAILABLE


def is_disk_critical(
    *,
    total_bytes: int,
    free_bytes: int,
    minimum_free_ratio: float = LEDGER_DISK_CRITICAL_FREE_RATIO,
    minimum_free_bytes: int = LEDGER_DISK_CRITICAL_FREE_BYTES,
) -> bool:
    """Apply the "whichever triggers first" critical-disk rule."""

    if total_bytes <= 0:
        # An unreadable or zero-sized filesystem cannot be shown to be safe.
        return True
    if free_bytes < 0:
        raise ValueError("free bytes cannot be negative")
    return free_bytes < minimum_free_bytes or free_bytes < total_bytes * minimum_free_ratio


def worst_ledger_health(*states: LedgerHealth) -> LedgerHealth:
    """Combine independent backlog signals; any Unavailable source wins."""

    worst = LedgerHealth.AVAILABLE
    for state in states:
        if _SEVERITY[state] > _SEVERITY[worst]:
            worst = state
    return worst


@dataclass(frozen=True, slots=True)
class LedgerBacklogDetails:
    """Optional measurements; absent values must never imply an empty Ledger."""

    pending_event_count: int | None = None
    oldest_event_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.pending_event_count is not None and (
            type(self.pending_event_count) is not int or self.pending_event_count < 0
        ):
            raise ValueError("invalid pending event count")
        if self.oldest_event_at is not None and self.oldest_event_at.utcoffset() is None:
            raise ValueError("oldest event timestamp must include a timezone")


def encode_ledger_backlog_reason(
    health: LedgerHealth,
    details: LedgerBacklogDetails | None = None,
) -> str:
    """Render the backlog verdict for the component heartbeat ``reason``."""

    reason = f"{LEDGER_BACKLOG_REASON_PREFIX}{LedgerHealth(health).value}"
    if details is None:
        return reason
    count = "unknown" if details.pending_event_count is None else str(details.pending_event_count)
    oldest = (
        "unknown"
        if details.oldest_event_at is None
        else details.oldest_event_at.astimezone(UTC).isoformat()
    )
    return f"{reason};pending_event_count={count};oldest_event_at={oldest}"


def decode_ledger_backlog_reason(reason: str | None) -> LedgerHealth | None:
    """Parse a heartbeat ``reason``; unknown or absent text yields no signal."""

    if reason is None or not reason.startswith(LEDGER_BACKLOG_REASON_PREFIX):
        return None
    try:
        return LedgerHealth(reason[len(LEDGER_BACKLOG_REASON_PREFIX) :].split(";", 1)[0])
    except ValueError:
        return None


def decode_ledger_backlog_details(reason: str | None) -> LedgerBacklogDetails | None:
    """Legacy verdict-only reasons carry no measurements; malformed fields stay unknown."""
    if decode_ledger_backlog_reason(reason) is None or reason is None:
        return None
    fields = reason.split(";")
    if len(fields) != 3:
        return None
    if not fields[1].startswith("pending_event_count=") or not fields[2].startswith(
        "oldest_event_at="
    ):
        return None
    count = fields[1].split("=", 1)[1]
    oldest = fields[2].split("=", 1)[1]
    try:
        return LedgerBacklogDetails(
            pending_event_count=None if count == "unknown" else int(count),
            oldest_event_at=None if oldest == "unknown" else datetime.fromisoformat(oldest),
        )
    except ValueError:
        return None


__all__ = [
    "LedgerBacklogDetails",
    "decode_ledger_backlog_details",
    "LEDGER_BACKLOG_REASON_PREFIX",
    "LEDGER_DELAYED_AFTER_SECONDS",
    "LEDGER_DISK_CRITICAL_FREE_BYTES",
    "LEDGER_DISK_CRITICAL_FREE_RATIO",
    "LEDGER_UNAVAILABLE_AFTER_SECONDS",
    "LedgerHealth",
    "classify_backlog_age",
    "decode_ledger_backlog_reason",
    "encode_ledger_backlog_reason",
    "is_disk_critical",
    "worst_ledger_health",
]

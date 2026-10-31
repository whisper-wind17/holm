"""Pure per-request Ledger lifecycle decisions."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from holm_ledger.models import TERMINAL_EVENT_TYPES, EventType, LedgerEvent


class SequenceAction(StrEnum):
    WRITE = "write"
    IGNORE = "ignore"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class SequenceDecision:
    action: SequenceAction
    reason: str


def decide_event_sequence(
    existing_events: Sequence[LedgerEvent], incoming_event: LedgerEvent
) -> SequenceDecision:
    """Decide a lifecycle transition without I/O or mutation.

    A request starts with Received, then becomes Rejected or InProgress. An
    InProgress request ends exactly once. Older/replayed events are ignored only
    after a terminal event is already durable.
    """

    if not existing_events:
        if incoming_event.event_type is EventType.RECEIVED:
            return SequenceDecision(SequenceAction.WRITE, "initial_received")
        return SequenceDecision(SequenceAction.REJECT, "first_event_must_be_received")

    request_id = existing_events[0].request_id
    if incoming_event.request_id != request_id or any(
        event.request_id != request_id for event in existing_events
    ):
        return SequenceDecision(SequenceAction.REJECT, "request_id_mismatch")

    highest = max(existing_events, key=lambda event: event.event_seq)
    terminal_exists = any(event.event_type in TERMINAL_EVENT_TYPES for event in existing_events)

    if incoming_event.event_seq <= highest.event_seq:
        if terminal_exists:
            return SequenceDecision(SequenceAction.IGNORE, "stale_after_terminal")
        return SequenceDecision(SequenceAction.REJECT, "event_seq_not_increasing")

    if terminal_exists:
        if incoming_event.event_type is EventType.IN_PROGRESS:
            return SequenceDecision(SequenceAction.REJECT, "terminal_cannot_return_to_in_progress")
        return SequenceDecision(SequenceAction.REJECT, "terminal_event_is_final")

    if highest.event_type is EventType.RECEIVED:
        # Unknown after Received is the Ledger Writer's convergence for a request whose
        # Gateway died before InProgress became durable.
        if incoming_event.event_type in {
            EventType.REJECTED,
            EventType.ABORTED,
            EventType.IN_PROGRESS,
            EventType.UNKNOWN,
        }:
            return SequenceDecision(SequenceAction.WRITE, "received_transition")
        return SequenceDecision(
            SequenceAction.REJECT, "received_requires_rejected_in_progress_or_unknown"
        )

    if highest.event_type is EventType.IN_PROGRESS:
        if incoming_event.event_type in TERMINAL_EVENT_TYPES - {EventType.REJECTED}:
            return SequenceDecision(SequenceAction.WRITE, "terminal_transition")
        return SequenceDecision(SequenceAction.REJECT, "in_progress_requires_terminal")

    return SequenceDecision(SequenceAction.REJECT, "invalid_existing_lifecycle")


__all__ = ["SequenceAction", "SequenceDecision", "decide_event_sequence"]

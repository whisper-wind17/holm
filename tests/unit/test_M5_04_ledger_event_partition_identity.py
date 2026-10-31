"""M5-04 Request Ledger partition identity model checks."""

from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest
from holm_domain import RequestRecordState
from holm_ledger import ReceivedEvent, RequestOperation, RequestProtocol, validate_event
from pydantic import ValidationError

NOW = datetime(2026, 9, 17, 10, 0, tzinfo=UTC)
REQUEST_ID = UUID("74000000-0000-4000-8000-000000000001")


def _received_values() -> dict[str, object]:
    return {
        "request_id": REQUEST_ID,
        "event_seq": 1,
        "created_at": NOW,
        "received_at": NOW,
        "protocol": RequestProtocol.OPENAI,
        "operation": RequestOperation.CHAT_COMPLETIONS,
        "endpoint": "/v1/chat/completions",
        "stream": False,
        "input_bytes": 12,
    }


def test_M5_04_received_at_is_required_and_received_matches_created_at() -> None:
    values = _received_values()
    del values["received_at"]
    with pytest.raises(ValidationError):
        ReceivedEvent(**values)

    values = _received_values()
    values["received_at"] = NOW + timedelta(microseconds=1)
    with pytest.raises(ValidationError, match="must equal created_at"):
        ReceivedEvent(**values)


def test_M5_04_received_at_requires_utc_even_when_offset_is_aware() -> None:
    values = _received_values()
    values["received_at"] = NOW.astimezone(timezone(timedelta(hours=8)))
    with pytest.raises(ValidationError, match="must use UTC"):
        ReceivedEvent(**values)


def test_M5_04_partitioned_record_state_vocabulary_is_closed() -> None:
    assert {state.value for state in RequestRecordState} == {
        "received",
        "in_progress",
        "rejected",
        "completed",
        "failed",
        "aborted",
        "unknown",
    }


def test_M5_04_body_sentinel_is_rejected_before_any_persistence_boundary() -> None:
    values = _received_values()
    values["messages"] = "M5_04_BODY_SENTINEL"
    with pytest.raises(ValidationError):
        validate_event(values)

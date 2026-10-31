"""M5-01 closed event schemas and pure lifecycle decisions."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from holm_domain import ErrorCode, RequestFinishReason
from holm_ledger import (
    AbortedEvent,
    CompletedEvent,
    EventType,
    FailedEvent,
    InProgressEvent,
    ReceivedEvent,
    RejectedEvent,
    RequestOperation,
    RequestProtocol,
    SequenceAction,
    UnknownEvent,
    decide_event_sequence,
    deserialize_event,
    serialize_event,
    validate_event,
)
from pydantic import ValidationError

REQUEST_ID = UUID("00000000-0000-4000-8000-000000000001")
USER_ID = UUID("00000000-0000-4000-8000-000000000002")
KEY_ID = UUID("00000000-0000-4000-8000-000000000003")
SERVICE_ID = UUID("00000000-0000-4000-8000-000000000004")
INSTANCE_ID = UUID("00000000-0000-4000-8000-000000000005")
NOW = datetime(2026, 9, 17, 10, 0, tzinfo=UTC)


def _received(*, seq: int = 1) -> ReceivedEvent:
    return ReceivedEvent(
        request_id=REQUEST_ID,
        event_seq=seq,
        created_at=NOW,
        received_at=NOW,
        protocol=RequestProtocol.OPENAI,
        operation=RequestOperation.CHAT_COMPLETIONS,
        endpoint="/v1/chat/completions",
        stream=False,
        input_bytes=128,
    )


def _in_progress(*, seq: int = 2) -> InProgressEvent:
    return InProgressEvent(
        request_id=REQUEST_ID,
        event_seq=seq,
        created_at=NOW,
        received_at=NOW,
        user_id=USER_ID,
        api_key_id=KEY_ID,
        service_id=SERVICE_ID,
        instance_id=INSTANCE_ID,
        requested_max_tokens=64,
    )


def _completed(*, seq: int = 3) -> CompletedEvent:
    return CompletedEvent(
        request_id=REQUEST_ID,
        event_seq=seq,
        created_at=NOW,
        received_at=NOW,
        input_tokens=3,
        output_tokens=5,
        ttft_ms=4,
        duration_ms=8,
        finish_reason=RequestFinishReason.STOP,
        http_status=200,
    )


def _all_events() -> tuple[object, ...]:
    return (
        _received(),
        RejectedEvent(
            request_id=REQUEST_ID,
            event_seq=2,
            created_at=NOW,
            received_at=NOW,
            error_code=ErrorCode.INVALID_API_KEY,
            http_status=401,
        ),
        _in_progress(),
        _completed(),
        FailedEvent(
            request_id=REQUEST_ID,
            event_seq=3,
            created_at=NOW,
            received_at=NOW,
            input_tokens=3,
            duration_ms=8,
            error_code=ErrorCode.BACKEND_UNAVAILABLE,
            http_status=503,
        ),
        AbortedEvent(
            request_id=REQUEST_ID,
            event_seq=3,
            created_at=NOW,
            received_at=NOW,
            duration_ms=8,
            client_disconnected=True,
        ),
        UnknownEvent(
            request_id=REQUEST_ID,
            event_seq=3,
            created_at=NOW,
            received_at=NOW,
            last_confirmed_event_type=EventType.IN_PROGRESS,
            last_confirmed_event_seq=2,
        ),
    )


@pytest.mark.parametrize("event", _all_events())
def test_M5_01_each_closed_event_round_trips(event: object) -> None:
    validated = validate_event(event)
    assert deserialize_event(serialize_event(validated)) == validated


@pytest.mark.parametrize("event", _all_events())
def test_M5_01_each_event_rejects_missing_required_field(event: object) -> None:
    value = validate_event(event).model_dump(mode="json")
    required = next(
        name
        for name, field in type(validate_event(event)).model_fields.items()
        if field.is_required()
        and name not in {"request_id", "event_seq", "created_at", "event_type"}
    )
    del value[required]
    with pytest.raises(ValidationError):
        validate_event(value)


@pytest.mark.parametrize("body_field", ["messages", "prompt", "content", "body"])
def test_M5_01_body_shaped_unknown_fields_are_rejected(body_field: str) -> None:
    value = _received().model_dump(mode="json")
    value[body_field] = "M5_01_BODY_SENTINEL"
    with pytest.raises(ValidationError):
        validate_event(value)


def test_M5_01_sequence_accepts_received_in_progress_completed() -> None:
    received = _received()
    in_progress = _in_progress()
    completed = _completed()

    assert decide_event_sequence((), received).action is SequenceAction.WRITE
    assert decide_event_sequence((received,), in_progress).action is SequenceAction.WRITE
    assert decide_event_sequence((received, in_progress), completed).action is SequenceAction.WRITE


def test_M5_01_sequence_accepts_received_rejected() -> None:
    received = _received()
    rejected = RejectedEvent(
        request_id=REQUEST_ID,
        event_seq=2,
        created_at=NOW,
        received_at=NOW,
        error_code=ErrorCode.INVALID_API_KEY,
        http_status=401,
    )
    assert decide_event_sequence((received,), rejected).action is SequenceAction.WRITE


def test_M5_01_sequence_rejects_first_non_received_and_non_increasing() -> None:
    assert decide_event_sequence((), _in_progress()).action is SequenceAction.REJECT
    assert (
        decide_event_sequence((_received(),), _in_progress(seq=1)).action is SequenceAction.REJECT
    )


def test_M5_01_TC_LG_06_stale_after_terminal_is_ignored() -> None:
    events = (_received(), _in_progress(), _completed())
    assert decide_event_sequence(events, _in_progress(seq=3)).action is SequenceAction.IGNORE
    assert decide_event_sequence(events, _in_progress(seq=2)).action is SequenceAction.IGNORE


def test_M5_01_TC_LG_06_terminal_cannot_regress_or_repeat() -> None:
    events = (_received(), _in_progress(), _completed())
    assert decide_event_sequence(events, _in_progress(seq=4)).action is SequenceAction.REJECT
    assert decide_event_sequence(events, _completed(seq=4)).action is SequenceAction.REJECT

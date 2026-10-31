"""Closed, metadata-only Request Ledger event models."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from holm_domain import (
    GATEWAY_ERROR_CODES,
    ErrorCode,
    MeteringSource,
    MetricsCapability,
    RequestFinishReason,
)
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    TypeAdapter,
    field_validator,
    model_validator,
)

_SQLiteInteger = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
_PositiveSQLiteInteger = Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]
_HttpStatus = Annotated[int, Field(strict=True, ge=100, le=599)]


class EventType(StrEnum):
    RECEIVED = "Received"
    REJECTED = "Rejected"
    IN_PROGRESS = "InProgress"
    COMPLETED = "Completed"
    FAILED = "Failed"
    ABORTED = "Aborted"
    UNKNOWN = "Unknown"


class RequestProtocol(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"


class RequestOperation(StrEnum):
    CHAT_COMPLETIONS = "chat.completions"
    MESSAGES = "messages"


class _EventBase(BaseModel):
    """Fields duplicated into table columns so every payload is self-describing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    event_seq: _PositiveSQLiteInteger
    created_at: AwareDatetime
    received_at: AwareDatetime

    @field_validator("created_at", "received_at")
    @classmethod
    def _timestamp_is_utc(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("Ledger timestamps must use UTC")
        return value


class ReceivedEvent(_EventBase):
    event_type: Literal[EventType.RECEIVED] = EventType.RECEIVED
    protocol: RequestProtocol
    operation: RequestOperation
    endpoint: Literal["/v1/chat/completions", "/v1/messages"]
    stream: StrictBool
    input_bytes: _SQLiteInteger

    @model_validator(mode="after")
    def _received_time_matches_created_time(self) -> ReceivedEvent:
        if self.received_at != self.created_at:
            raise ValueError("Received received_at must equal created_at")
        return self


class RejectedEvent(_EventBase):
    event_type: Literal[EventType.REJECTED] = EventType.REJECTED
    error_code: ErrorCode
    http_status: _HttpStatus
    user_id: UUID | None = None
    api_key_id: UUID | None = None
    service_id: UUID | None = None
    instance_id: UUID | None = None

    @field_validator("error_code")
    @classmethod
    def _gateway_error_only(cls, value: ErrorCode) -> ErrorCode:
        if value not in GATEWAY_ERROR_CODES:
            raise ValueError("error_code must be a stable Gateway error code")
        return value


class InProgressEvent(_EventBase):
    event_type: Literal[EventType.IN_PROGRESS] = EventType.IN_PROGRESS
    user_id: UUID
    api_key_id: UUID
    service_id: UUID
    instance_id: UUID
    requested_max_tokens: _SQLiteInteger | None


class _MeteredEvent(_EventBase):
    """A terminal event that records which metering level produced its Tokens."""

    metering_source: MeteringSource | None = None
    metrics_capability: MetricsCapability | None = None


class CompletedEvent(_MeteredEvent):
    # Token counts are optional. A Runtime that completes normally but
    # reports no Usage, and whose Tokenizer Bundle cannot be used either, is a
    # completed request with unknown Tokens - not a failed one.  Rewriting it to
    # Failed(backend_unavailable) misreported a successful inference.
    event_type: Literal[EventType.COMPLETED] = EventType.COMPLETED
    input_tokens: _SQLiteInteger | None = None
    output_tokens: _SQLiteInteger | None = None
    ttft_ms: _SQLiteInteger | None = None
    duration_ms: _SQLiteInteger
    finish_reason: RequestFinishReason | None = None
    http_status: _HttpStatus

    @model_validator(mode="after")
    def _unavailable_metering_keeps_tokens_null(self) -> CompletedEvent:
        if self.metering_source is MeteringSource.UNAVAILABLE and (
            self.input_tokens is not None or self.output_tokens is not None
        ):
            raise ValueError("unavailable metering cannot carry Token counts")
        return self


class FailedEvent(_MeteredEvent):
    event_type: Literal[EventType.FAILED] = EventType.FAILED
    input_tokens: _SQLiteInteger | None = None
    output_tokens: _SQLiteInteger | None = None
    ttft_ms: _SQLiteInteger | None = None
    duration_ms: _SQLiteInteger
    error_code: ErrorCode
    http_status: _HttpStatus

    @field_validator("error_code")
    @classmethod
    def _gateway_error_only(cls, value: ErrorCode) -> ErrorCode:
        if value not in GATEWAY_ERROR_CODES:
            raise ValueError("error_code must be a stable Gateway error code")
        return value


class AbortedEvent(_MeteredEvent):
    # Queued requests can terminate directly from Received.
    user_id: UUID | None = None
    api_key_id: UUID | None = None
    service_id: UUID | None = None
    instance_id: UUID | None = None
    event_type: Literal[EventType.ABORTED] = EventType.ABORTED
    input_tokens: _SQLiteInteger | None = None
    output_tokens: _SQLiteInteger | None = None
    duration_ms: _SQLiteInteger
    client_disconnected: Literal[True]


class UnknownEvent(_EventBase):
    event_type: Literal[EventType.UNKNOWN] = EventType.UNKNOWN
    last_confirmed_event_type: Literal[EventType.RECEIVED, EventType.IN_PROGRESS]
    last_confirmed_event_seq: _PositiveSQLiteInteger
    user_id: UUID | None = None
    api_key_id: UUID | None = None
    service_id: UUID | None = None
    instance_id: UUID | None = None
    input_tokens: _SQLiteInteger | None = None
    output_tokens: _SQLiteInteger | None = None

    @model_validator(mode="after")
    def _last_event_precedes_unknown(self) -> UnknownEvent:
        if self.last_confirmed_event_seq >= self.event_seq:
            raise ValueError("last_confirmed_event_seq must precede the Unknown event")
        return self


type LedgerEvent = Annotated[
    ReceivedEvent
    | RejectedEvent
    | InProgressEvent
    | CompletedEvent
    | FailedEvent
    | AbortedEvent
    | UnknownEvent,
    Field(discriminator="event_type"),
]

LEDGER_EVENT_ADAPTER: TypeAdapter[LedgerEvent] = TypeAdapter(LedgerEvent)


def validate_event(value: object) -> LedgerEvent:
    """Validate an untrusted event without accepting arbitrary dictionaries."""

    return LEDGER_EVENT_ADAPTER.validate_python(value)


def serialize_event(event: LedgerEvent) -> str:
    """Serialize only a previously validated closed event model."""

    return LEDGER_EVENT_ADAPTER.dump_json(event).decode("utf-8")


def deserialize_event(payload_json: str) -> LedgerEvent:
    """Validate persisted JSON against the same closed discriminated union."""

    return LEDGER_EVENT_ADAPTER.validate_json(payload_json)


TERMINAL_EVENT_TYPES = frozenset(
    {
        EventType.REJECTED,
        EventType.COMPLETED,
        EventType.FAILED,
        EventType.ABORTED,
        EventType.UNKNOWN,
    }
)


__all__ = [
    "AbortedEvent",
    "CompletedEvent",
    "EventType",
    "FailedEvent",
    "InProgressEvent",
    "LEDGER_EVENT_ADAPTER",
    "LedgerEvent",
    "ReceivedEvent",
    "RejectedEvent",
    "RequestOperation",
    "RequestProtocol",
    "TERMINAL_EVENT_TYPES",
    "UnknownEvent",
    "deserialize_event",
    "serialize_event",
    "validate_event",
]

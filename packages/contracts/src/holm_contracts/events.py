"""Pydantic serialization contract for Domain Events."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from holm_common.time import format_rfc3339_utc, require_utc
from holm_domain.events import DomainEvent
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_serializer, field_validator

PositiveStrictInt = Annotated[int, Field(strict=True, gt=0)]


class DomainEventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: PositiveStrictInt
    event: str = Field(min_length=1)
    aggregate_id: UUID
    version: PositiveStrictInt
    occurred_at: datetime
    payload: dict[str, JsonValue]

    @field_validator("occurred_at")
    @classmethod
    def validate_utc(cls, value: datetime) -> datetime:
        return require_utc(value)

    @field_serializer("occurred_at")
    def serialize_utc(self, value: datetime) -> str:
        return format_rfc3339_utc(value)

    @classmethod
    def from_domain(cls, event: DomainEvent) -> DomainEventEnvelope:
        return cls(
            id=event.id,
            event=event.event,
            aggregate_id=event.aggregate_id,
            version=event.version,
            occurred_at=event.occurred_at,
            payload=event.payload_json(),
        )

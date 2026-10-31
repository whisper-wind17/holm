"""Immutable framework-independent Domain Event values."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from uuid import UUID

type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type FrozenJsonValue = JsonScalar | tuple[FrozenJsonValue, ...] | Mapping[str, FrozenJsonValue]


def _freeze_json(value: object) -> FrozenJsonValue:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Domain Event payload numbers must be finite")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, FrozenJsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("Domain Event payload keys must be strings")
            frozen[key] = _freeze_json(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    raise ValueError("Domain Event payload must contain only JSON values")


def _thaw_json(value: FrozenJsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def freeze_json_object(value: Mapping[str, object]) -> Mapping[str, FrozenJsonValue]:
    """Copy and freeze a JSON object at a trusted construction boundary."""
    frozen = _freeze_json(value)
    if not isinstance(frozen, Mapping):
        raise ValueError("Domain Event payload must be an object")
    return frozen


def thaw_json_object(value: Mapping[str, FrozenJsonValue]) -> dict[str, JsonValue]:
    """Return a detached mutable representation of a frozen JSON object."""
    return {key: _thaw_json(item) for key, item in value.items()}


@dataclass(frozen=True, slots=True)
class DomainEvent:
    id: int
    event: str
    aggregate_id: UUID
    version: int
    occurred_at: datetime
    payload: Mapping[str, FrozenJsonValue]

    def __post_init__(self) -> None:
        if isinstance(self.id, bool) or not isinstance(self.id, int) or self.id <= 0:
            raise ValueError("Domain Event ID must be a positive integer")
        if not self.event or self.event != self.event.strip():
            raise ValueError("Domain Event name must be non-empty and trimmed")
        if not isinstance(self.aggregate_id, UUID):
            raise ValueError("Domain Event aggregate ID must be a UUID")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version <= 0:
            raise ValueError("Domain Event version must be a positive integer")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() != timedelta(0):
            raise ValueError("Domain Event time must be UTC")
        object.__setattr__(self, "payload", freeze_json_object(self.payload))

    def payload_json(self) -> dict[str, JsonValue]:
        """Return a detached mutable JSON representation for serialization."""
        return thaw_json_object(self.payload)


__all__ = [
    "DomainEvent",
    "FrozenJsonValue",
    "JsonScalar",
    "JsonValue",
    "freeze_json_object",
    "thaw_json_object",
]

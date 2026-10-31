"""Private typed carriers shared only by the OpenAI and vLLM adapters."""

from __future__ import annotations

from dataclasses import dataclass, field

from holm_protocols import (
    JsonArray,
    JsonMember,
    JsonObject,
    JsonScalar,
    JsonValue,
    ProtocolExtension,
    freeze_json,
    thaw_json,
)


def object_value(value: JsonObject, name: str) -> JsonValue | None:
    return next((member.value for member in value.members if member.name == name), None)


@dataclass(frozen=True, slots=True)
class OpenAIRequestExtension(ProtocolExtension):
    client_requested_stream_usage: bool


@dataclass(frozen=True, slots=True)
class OpenAIResponseExtension(ProtocolExtension):
    payload: JsonObject = field(repr=False)
    public_model_name: str


@dataclass(frozen=True, slots=True)
class RawSseLine:
    value: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class DataSseLine:
    payload: JsonValue | None = field(default=None, repr=False)
    done: bool = False


type OpenAISseLine = RawSseLine | DataSseLine


@dataclass(frozen=True, slots=True)
class OpenAISseExtension(ProtocolExtension):
    lines: tuple[OpenAISseLine, ...] = field(repr=False)
    public_model_name: str


__all__ = [
    "DataSseLine",
    "JsonArray",
    "JsonMember",
    "JsonObject",
    "JsonScalar",
    "JsonValue",
    "OpenAIRequestExtension",
    "OpenAIResponseExtension",
    "OpenAISseExtension",
    "OpenAISseLine",
    "RawSseLine",
    "freeze_json",
    "object_value",
    "thaw_json",
]

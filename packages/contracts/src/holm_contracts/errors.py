"""Stable error Envelope and OpenAPI component generation."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from holm_domain.errors import ErrorCode
from pydantic import BaseModel, ConfigDict, Field, JsonValue


class _ClosedContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ErrorBody(_ClosedContract):
    code: ErrorCode
    message: str = Field(min_length=1)
    details: dict[str, JsonValue]
    request_id: UUID


class ErrorEnvelope(_ClosedContract):
    error: ErrorBody


def error_envelope_openapi_components() -> dict[str, object]:
    """Return deterministic OpenAPI 3.1 components for the error Envelope."""
    schema: dict[str, Any] = ErrorEnvelope.model_json_schema(
        mode="serialization", ref_template="#/components/schemas/{model}"
    )
    definitions = schema.pop("$defs", {})
    schemas = {**definitions, "ErrorEnvelope": schema}
    return {"components": {"schemas": dict(sorted(schemas.items()))}}

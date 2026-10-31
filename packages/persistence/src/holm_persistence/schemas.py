"""Frozen PostgreSQL Schema namespace names."""

from typing import Final

CORE_SCHEMA: Final[str] = "core"
USAGE_SCHEMA: Final[str] = "usage"
TELEMETRY_SCHEMA: Final[str] = "telemetry"

SCHEMA_NAMES: Final[tuple[str, str, str]] = (CORE_SCHEMA, USAGE_SCHEMA, TELEMETRY_SCHEMA)

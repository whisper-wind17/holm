"""UTC wall-clock and canonical RFC 3339 helpers."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Final

_RFC3339_UTC: Final[re.Pattern[str]] = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|\+00:00)$"
)


class UTCDateTimeError(ValueError):
    """Raised when a value cannot satisfy the UTC time contract."""


def utc_now() -> datetime:
    """Return the current timezone-aware UTC wall-clock time."""
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """Normalize a timezone-aware datetime to UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise UTCDateTimeError("timezone-aware datetime required")
    return value.astimezone(UTC)


def require_utc(value: datetime) -> datetime:
    """Require a timezone-aware datetime whose offset is UTC."""
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise UTCDateTimeError("UTC datetime required")
    return value.astimezone(UTC)


def format_rfc3339_utc(value: datetime) -> str:
    """Serialize an aware datetime as canonical RFC 3339 UTC with `Z`."""
    normalized = as_utc(value)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_rfc3339_utc(value: str) -> datetime:
    """Parse RFC 3339 text that explicitly identifies the UTC offset."""
    if _RFC3339_UTC.fullmatch(value) is None:
        raise UTCDateTimeError("invalid RFC 3339 UTC datetime")
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise UTCDateTimeError("invalid RFC 3339 UTC datetime") from None
    return require_utc(parsed)

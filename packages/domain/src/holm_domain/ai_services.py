"""Framework-independent AI Service and API Key states."""

from __future__ import annotations

from enum import StrEnum


class ApiKeyStatus(StrEnum):
    """Persisted API Key lifecycle states."""

    ACTIVE = "Active"
    REVOKED = "Revoked"
    EXPIRED = "Expired"


__all__ = ["ApiKeyStatus"]

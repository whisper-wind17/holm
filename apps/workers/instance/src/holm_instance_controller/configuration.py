"""Fail-closed file configuration for the instance controller."""

from __future__ import annotations

from pathlib import Path

from holm_common import SecretFileError, read_secret_file
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError


class InstanceControllerConfigurationError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("instance controller configuration rejected")


def load_database_url(path: Path) -> URL:
    try:
        raw = read_secret_file(path)
    except SecretFileError as error:
        raise InstanceControllerConfigurationError(
            f"database_secret_unavailable:{error.code}"
        ) from None
    try:
        value = raw.decode("utf-8").strip()
        url = make_url(value)
    except (ArgumentError, UnicodeDecodeError, ValueError, TypeError):
        raise InstanceControllerConfigurationError("database_url_invalid") from None
    if not value or url.drivername != "postgresql+psycopg" or not url.username:
        raise InstanceControllerConfigurationError("database_url_invalid")
    return url


__all__ = ["InstanceControllerConfigurationError", "load_database_url"]

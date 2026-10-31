"""Programmatic Alembic entry points that keep connection credentials in memory."""

from __future__ import annotations

import math
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from holm_persistence.accounts import METADATA
from holm_persistence.role_grants import verify_table_grants

DEFAULT_MIGRATION_LOCK_TIMEOUT_SECONDS: Final[float] = 30.0
MIGRATION_LOCK_CLASS_ID: Final[int] = 5_263_187
MIGRATION_LOCK_OBJECT_ID: Final[int] = 1
LOCK_NOT_AVAILABLE_SQLSTATE: Final[str] = "55P03"


class MigrationLockTimeout(RuntimeError):
    """The database did not grant the bounded Migration lock in time."""


def _timeout_milliseconds(timeout_seconds: float) -> int:
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise TypeError("Migration lock timeout must be a number")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("Migration lock timeout must be finite and greater than zero")
    return math.ceil(timeout_seconds * 1000)


def _set_lock_timeout(connection: Connection, value: str) -> None:
    connection.execute(
        text("SELECT set_config('lock_timeout', :value, false)"),
        {"value": value},
    ).scalar_one()
    connection.commit()


@contextmanager
def migration_lock(
    connection: Connection,
    *,
    timeout_seconds: float = DEFAULT_MIGRATION_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Hold the database-level Migration lock with a bounded server-side wait."""
    timeout_milliseconds = _timeout_milliseconds(timeout_seconds)
    if connection.in_transaction():
        raise RuntimeError("Migration lock requires a connection without an active transaction")

    previous_lock_timeout = str(connection.exec_driver_sql("SHOW lock_timeout").scalar_one())
    connection.commit()
    acquired = False
    try:
        _set_lock_timeout(connection, f"{timeout_milliseconds}ms")
        try:
            connection.execute(
                text("SELECT pg_advisory_lock(:class_id, :object_id)"),
                {
                    "class_id": MIGRATION_LOCK_CLASS_ID,
                    "object_id": MIGRATION_LOCK_OBJECT_ID,
                },
            ).one()
            connection.commit()
            acquired = True
        except DBAPIError as error:
            connection.rollback()
            if getattr(error.orig, "sqlstate", None) == LOCK_NOT_AVAILABLE_SQLSTATE:
                raise MigrationLockTimeout(
                    "database Migration lock was not acquired before the configured timeout"
                ) from None
            raise

        yield
    finally:
        if connection.in_transaction():
            connection.rollback()
        try:
            if acquired:
                released = bool(
                    connection.execute(
                        text("SELECT pg_advisory_unlock(:class_id, :object_id)"),
                        {
                            "class_id": MIGRATION_LOCK_CLASS_ID,
                            "object_id": MIGRATION_LOCK_OBJECT_ID,
                        },
                    ).scalar_one()
                )
                connection.commit()
                if not released:
                    raise RuntimeError("database Migration lock ownership was lost")
        finally:
            if connection.in_transaction():
                connection.rollback()
            _set_lock_timeout(connection, previous_lock_timeout)


def _config(
    connection: Connection,
    config_path: Path,
    *,
    remove_version_table_on_base: bool = False,
) -> Config:
    if not config_path.is_file():
        raise FileNotFoundError(config_path)
    return Config(
        config_path,
        attributes={
            "connection": connection,
            "remove_version_table_on_base": remove_version_table_on_base,
        },
    )


def upgrade_to_head(
    connection: Connection,
    config_path: Path,
    *,
    lock_timeout_seconds: float = DEFAULT_MIGRATION_LOCK_TIMEOUT_SECONDS,
) -> None:
    """Synchronously upgrade to head during the bounded maintenance workflow."""
    verify_table_grants(METADATA)
    with migration_lock(connection, timeout_seconds=lock_timeout_seconds):
        command.upgrade(_config(connection, config_path), "head")


def downgrade_to_base(
    connection: Connection,
    config_path: Path,
    *,
    lock_timeout_seconds: float = DEFAULT_MIGRATION_LOCK_TIMEOUT_SECONDS,
) -> None:
    """Synchronously return to the object-free Alembic base."""
    with migration_lock(connection, timeout_seconds=lock_timeout_seconds):
        command.downgrade(
            _config(
                connection,
                config_path,
                remove_version_table_on_base=True,
            ),
            "base",
        )

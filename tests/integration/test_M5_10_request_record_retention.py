"""M5-10 Request Record month-partition retention against PostgreSQL 16."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from holm_persistence import (
    create_database_engine,
    ensure_request_record_partitions,
)
from holm_persistence.request_record_ledger_repository import (
    drop_expired_request_record_partitions,
)
from migration_test_support import upgrade_head
from sqlalchemy.engine import URL


def _async_url(fixture: EphemeralPostgres) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username="ubuntu",
        host=str(fixture.socket_directory),
        port=fixture.port,
        database="postgres",
    )


@pytest.fixture(scope="module", autouse=True)
def _migrated_database(ephemeral_postgres: EphemeralPostgres) -> Iterator[None]:
    upgrade_head(ephemeral_postgres.engine)
    yield


def _partitions(fixture: EphemeralPostgres) -> tuple[str, ...]:
    with fixture.engine.connect() as connection:
        return tuple(
            connection.execute(
                sa.text(
                    "SELECT child.relname FROM pg_inherits "
                    "JOIN pg_class AS parent ON parent.oid = inhparent "
                    "JOIN pg_class AS child ON child.oid = inhrelid "
                    "JOIN pg_namespace AS namespace ON namespace.oid = child.relnamespace "
                    "WHERE namespace.nspname = 'usage' "
                    "AND parent.relname = 'request_records' ORDER BY child.relname"
                )
            ).scalars()
        )


def test_M5_10_partition_drop_requires_verified_upper_bound_strictly_before_cutoff(
    ephemeral_postgres: EphemeralPostgres,
    _migrated_database: None,
) -> None:
    del _migrated_database

    async def scenario() -> tuple[tuple[str, ...], tuple[str, ...]]:
        engine = create_database_engine(_async_url(ephemeral_postgres), connect_timeout_seconds=5)
        try:
            await ensure_request_record_partitions(
                engine,
                (
                    datetime(2026, 7, 15, tzinfo=UTC),
                    datetime(2026, 8, 15, tzinfo=UTC),
                ),
            )
            async with engine.begin() as connection:
                # The old-looking name deliberately carries a recent bound.
                # Name-only deletion would irreversibly remove retained data.
                await connection.execute(
                    sa.text(
                        "CREATE TABLE usage.request_records_202001 "
                        "PARTITION OF usage.request_records "
                        "FOR VALUES FROM ('2026-10-01T00:00:00+00:00') "
                        "TO ('2026-11-01T00:00:00+00:00')"
                    )
                )
            boundary = await drop_expired_request_record_partitions(
                engine,
                retention_days=50,
                now=datetime(2026, 9, 20, tzinfo=UTC),
            )
            expired = await drop_expired_request_record_partitions(
                engine,
                retention_days=30,
                now=datetime(2026, 9, 20, tzinfo=UTC),
            )
            return boundary, expired
        finally:
            await engine.dispose()

    boundary, expired = asyncio.run(scenario())
    assert boundary == ()
    assert expired == ("request_records_202607",)
    assert _partitions(ephemeral_postgres) == (
        "request_records_202001",
        "request_records_202608",
    )

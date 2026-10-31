"""M5-07: both Request Record projections carry the metering provenance columns."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final
from uuid import UUID

import pytest
from holm_domain import MeteringSource, MetricsCapability
from migration_test_support import PostgresFixture, upgrade_head
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

_COLUMNS: Final[tuple[str, ...]] = ("metering_source", "metrics_capability")
_SCHEMAS: Final[tuple[str, ...]] = ("core", "usage")
_RECEIVED_AT: Final[datetime] = datetime(2026, 9, 18, 9, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def migrated(ephemeral_postgres: PostgresFixture) -> Engine:
    upgrade_head(ephemeral_postgres.engine)
    return ephemeral_postgres.engine


def _columns(connection: Connection, schema: str) -> dict[str, str]:
    rows = connection.execute(
        text(
            """
            SELECT column_name, is_nullable
            FROM information_schema.columns
            WHERE table_schema = :schema
              AND table_name = 'request_records'
              AND column_name = ANY(:columns)
            """
        ),
        {"schema": schema, "columns": list(_COLUMNS)},
    ).all()
    return {str(row[0]): str(row[1]) for row in rows}


def test_M5_07_both_projections_gain_nullable_metering_columns(migrated: Engine) -> None:
    with migrated.connect() as connection:
        for schema in _SCHEMAS:
            assert _columns(connection, schema) == {
                "metering_source": "YES",
                "metrics_capability": "YES",
            }


def test_M5_07_pre_existing_shaped_row_keeps_null_metering(migrated: Engine) -> None:
    request_id = UUID("57000000-0000-4000-8000-0000000000a1")
    with migrated.begin() as connection:
        _insert_core_record(connection, request_id)
        row = connection.execute(
            text(
                "SELECT metering_source, metrics_capability "
                "FROM core.request_records WHERE request_id = :request_id"
            ),
            {"request_id": request_id},
        ).one()
        assert row == (None, None)
        connection.rollback()


def test_M5_07_metering_columns_accept_only_the_closed_vocabulary(migrated: Engine) -> None:
    accepted = UUID("57000000-0000-4000-8000-0000000000a2")
    with migrated.begin() as connection:
        _insert_core_record(
            connection,
            accepted,
            metering_source=MeteringSource.TOKENIZER_FALLBACK.value,
            metrics_capability=MetricsCapability.PARTIAL.value,
        )
        row = connection.execute(
            text(
                "SELECT metering_source, metrics_capability "
                "FROM core.request_records WHERE request_id = :request_id"
            ),
            {"request_id": accepted},
        ).one()
        assert row == ("tokenizer_fallback", "partial")
        connection.rollback()

    rejected = UUID("57000000-0000-4000-8000-0000000000a3")
    with migrated.begin() as connection:
        with pytest.raises(IntegrityError):
            _insert_core_record(connection, rejected, metering_source="guessed")
        connection.rollback()


def test_M5_07_usage_projection_rejects_an_unknown_capability(migrated: Engine) -> None:
    with migrated.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS usage.request_records_202609 "
                "PARTITION OF usage.request_records "
                "FOR VALUES FROM ('2026-09-01T00:00:00+00') TO ('2026-10-01T00:00:00+00')"
            )
        )
    with migrated.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    """
                    INSERT INTO usage.request_records (
                        received_at, request_id, event_seq, record_state,
                        updated_at, metrics_capability
                    ) VALUES (
                        :received_at, :request_id, 1, 'completed', :received_at, 'maybe'
                    )
                    """
                ),
                {
                    "received_at": _RECEIVED_AT,
                    "request_id": UUID("57000000-0000-4000-8000-0000000000a4"),
                },
            )
        connection.rollback()


def _insert_core_record(
    connection: Connection,
    request_id: UUID,
    *,
    metering_source: str | None = None,
    metrics_capability: str | None = None,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO core.request_records (
                request_id, received_at, completed_at, stream, status,
                http_status, duration_ms, metering_source, metrics_capability
            ) VALUES (
                :request_id, :received_at, :received_at, false, 'completed',
                200, 12, :metering_source, :metrics_capability
            )
            """
        ),
        {
            "request_id": request_id,
            "received_at": _RECEIVED_AT,
            "metering_source": metering_source,
            "metrics_capability": metrics_capability,
        },
    )

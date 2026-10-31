"""FIX-10: nullable service latency percentiles survive upgrade and minute queries."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from conftest import EphemeralPostgres
from holm_persistence import (
    ServiceMinuteMetric,
    TelemetryMinuteRepository,
    create_database_engine,
    create_session_factory,
)
from migration_test_support import ALEMBIC_CONFIG_PATH
from sqlalchemy.engine import URL

SERVICE_ID = UUID("00000000-0000-7000-8000-000000001010")
BUCKET_AT = datetime(2026, 9, 23, 12, tzinfo=UTC)


def test_FIX_10_TC_TM_09_existing_minute_row_stays_null_and_new_row_round_trips(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    with ephemeral_postgres.engine.connect() as connection:
        config = Config(ALEMBIC_CONFIG_PATH, attributes={"connection": connection})
        command.upgrade(config, "20260923_0028")
        connection.execute(
            sa.text(
                "SELECT core.holm_ensure_month_partition("
                "'telemetry.service_metrics_1m', :month_start)"
            ),
            {"month_start": BUCKET_AT.date().replace(day=1)},
        )
        connection.execute(
            sa.text(
                "INSERT INTO telemetry.service_metrics_1m "
                "(bucket_at, service_id, quality, telemetry_status, e2e_latency_p95_ms) "
                "VALUES (:bucket_at, :service_id, 'full', 'available', 200)"
            ),
            {"bucket_at": BUCKET_AT, "service_id": SERVICE_ID},
        )
        connection.commit()
        command.upgrade(config, "head")
        old_row = connection.execute(
            sa.text(
                "SELECT e2e_latency_p50_ms, e2e_latency_p95_ms, e2e_latency_p99_ms "
                "FROM telemetry.service_metrics_1m WHERE service_id = :service_id"
            ),
            {"service_id": SERVICE_ID},
        ).one()
        assert old_row == (None, 200.0, None)

    async def scenario() -> tuple[tuple[float | None, float | None, float | None], ...]:
        url = URL.create(
            "postgresql+psycopg",
            username="ubuntu",
            host=str(ephemeral_postgres.socket_directory),
            port=ephemeral_postgres.port,
            database="postgres",
        )
        engine = create_database_engine(url, connect_timeout_seconds=5)
        factory = create_session_factory(engine)
        try:
            async with factory() as session, session.begin():
                await TelemetryMinuteRepository(session).upsert(
                    services=(
                        ServiceMinuteMetric(
                            bucket_at=BUCKET_AT + timedelta(minutes=1),
                            service_id=SERVICE_ID,
                            quality="full",
                            telemetry_status="available",
                            e2e_latency_p50_ms=100.0,
                            e2e_latency_p95_ms=200.0,
                            e2e_latency_p99_ms=300.0,
                        ),
                    )
                )
            async with factory() as session:
                rows = await TelemetryMinuteRepository(session).list_services(
                    SERVICE_ID, since=BUCKET_AT
                )
            return tuple(
                (row.e2e_latency_p50_ms, row.e2e_latency_p95_ms, row.e2e_latency_p99_ms)
                for row in rows
            )
        finally:
            await engine.dispose()

    assert asyncio.run(scenario()) == ((None, 200.0, None), (100.0, 200.0, 300.0))

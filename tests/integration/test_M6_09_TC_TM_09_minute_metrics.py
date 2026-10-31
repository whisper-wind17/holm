"""M6-09 / TC-TM-09 partitioned minute telemetry persistence."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from holm_persistence import (
    GpuMinuteMetric,
    InstanceMinuteMetric,
    ServiceMinuteMetric,
    TelemetryMinuteRepository,
    create_database_engine,
    create_session_factory,
    drop_expired_telemetry_partitions,
)
from migration_test_support import upgrade_head
from sqlalchemy.engine import URL

INSTANCE_ID = UUID("00000000-0000-7000-8000-000000006091")
SERVICE_ID = UUID("00000000-0000-7000-8000-000000006092")


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


def test_M6_09_TC_TM_09_real_postgres_upsert_is_idempotent_and_sparse(
    ephemeral_postgres: EphemeralPostgres,
    _migrated_database: None,
) -> None:
    del _migrated_database
    bucket_at = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)

    async def scenario() -> tuple[int, float | None, str, str]:
        engine = create_database_engine(_async_url(ephemeral_postgres), connect_timeout_seconds=5)
        factory = create_session_factory(engine)
        try:
            async with factory() as session, session.begin():
                repository = TelemetryMinuteRepository(session)
                first = await repository.upsert(
                    instances=(
                        InstanceMinuteMetric(
                            bucket_at=bucket_at,
                            instance_id=INSTANCE_ID,
                            quality="full",
                            telemetry_status="available",
                            running_last=0.0,
                        ),
                    ),
                    services=(
                        ServiceMinuteMetric(
                            bucket_at=bucket_at,
                            service_id=SERVICE_ID,
                            quality="partial",
                            telemetry_status="unavailable",
                        ),
                    ),
                    gpus=(
                        GpuMinuteMetric(
                            bucket_at=bucket_at,
                            gpu_id="GPU-M6-09",
                            quality="full",
                            telemetry_status="available",
                            utilization_last=0.0,
                            health_last="healthy",
                        ),
                    ),
                )
                second = await repository.upsert(
                    instances=(
                        InstanceMinuteMetric(
                            bucket_at=bucket_at,
                            instance_id=INSTANCE_ID,
                            quality="partial",
                            telemetry_status="delayed",
                            running_last=None,
                        ),
                    )
                )
            async with factory() as session:
                rows = await TelemetryMinuteRepository(session).list_instances(
                    INSTANCE_ID,
                    since=datetime(2026, 8, 1, tzinfo=UTC),
                )
                counts_list: list[int] = []
                for table in (
                    "instance_metrics_1m",
                    "service_metrics_1m",
                    "gpu_metrics_1m",
                ):
                    count = await session.scalar(sa.text(f"SELECT count(*) FROM telemetry.{table}"))
                    counts_list.append(int(count or 0))
                counts = tuple(counts_list)
            assert first == 3
            assert second == 1
            assert counts == (1, 1, 1)
            return len(rows), rows[0].running_last, rows[0].quality, rows[0].telemetry_status
        finally:
            await engine.dispose()

    assert asyncio.run(scenario()) == (1, None, "partial", "delayed")


def test_M6_09_TC_TM_09_retention_drops_only_wholly_expired_month_partitions(
    ephemeral_postgres: EphemeralPostgres,
    _migrated_database: None,
) -> None:
    del _migrated_database

    async def scenario() -> tuple[str, ...]:
        engine = create_database_engine(_async_url(ephemeral_postgres), connect_timeout_seconds=5)
        factory = create_session_factory(engine)
        try:
            async with factory() as session, session.begin():
                repository = TelemetryMinuteRepository(session)
                for bucket_at in (
                    datetime(2026, 7, 15, tzinfo=UTC),
                    datetime(2026, 8, 15, tzinfo=UTC),
                ):
                    await repository.upsert(
                        gpus=(
                            GpuMinuteMetric(
                                bucket_at=bucket_at,
                                gpu_id="GPU-M6-09-retention",
                                quality="partial",
                                telemetry_status="unavailable",
                            ),
                        )
                    )
            return await drop_expired_telemetry_partitions(
                engine,
                now=datetime(2026, 9, 20, tzinfo=UTC),
            )
        finally:
            await engine.dispose()

    dropped = asyncio.run(scenario())
    assert dropped == ("gpu_metrics_1m_202607",)
    with ephemeral_postgres.engine.connect() as connection:
        partitions = tuple(
            connection.execute(
                sa.text(
                    "SELECT child.relname FROM pg_inherits "
                    "JOIN pg_class AS parent ON parent.oid = inhparent "
                    "JOIN pg_class AS child ON child.oid = inhrelid "
                    "JOIN pg_namespace AS namespace ON namespace.oid = child.relnamespace "
                    "WHERE namespace.nspname = 'telemetry' "
                    "AND parent.relname = 'gpu_metrics_1m' ORDER BY child.relname"
                )
            ).scalars()
        )
    assert partitions == ("gpu_metrics_1m_202608",)


def test_M6_09_TC_TM_09_wide_tables_have_no_body_or_high_cardinality_columns(
    ephemeral_postgres: EphemeralPostgres,
    _migrated_database: None,
) -> None:
    del _migrated_database
    with ephemeral_postgres.engine.connect() as connection:
        columns = {
            str(name)
            for name in connection.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'telemetry' AND table_name LIKE '%_metrics_1m'"
                )
            ).scalars()
        }
    forbidden = {
        "request_id",
        "user_id",
        "api_key_id",
        "prompt",
        "messages",
        "request_body",
        "response_body",
        "model_output",
    }
    assert columns.isdisjoint(forbidden)

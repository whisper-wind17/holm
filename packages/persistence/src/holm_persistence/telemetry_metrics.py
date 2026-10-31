"""Partitioned minute telemetry records and retention operations."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Final, Literal, TypeVar
from uuid import UUID

import sqlalchemy as sa
from holm_common import as_utc
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from holm_persistence.accounts import METADATA
from holm_persistence.schemas import TELEMETRY_SCHEMA

INSTANCE_METRICS_1M_TABLE_NAME: Final = "instance_metrics_1m"
SERVICE_METRICS_1M_TABLE_NAME: Final = "service_metrics_1m"
GPU_METRICS_1M_TABLE_NAME: Final = "gpu_metrics_1m"
TELEMETRY_RETENTION_DAYS: Final = 30

TelemetryQualityValue = Literal["full", "partial"]
TelemetryStatusValue = Literal["available", "delayed", "unavailable"]

_LOGGER: Final = logging.getLogger("holm.persistence.telemetry")
_MANAGED_TABLES: Final = (
    INSTANCE_METRICS_1M_TABLE_NAME,
    SERVICE_METRICS_1M_TABLE_NAME,
    GPU_METRICS_1M_TABLE_NAME,
)


INSTANCE_METRICS_1M: Final = sa.Table(
    INSTANCE_METRICS_1M_TABLE_NAME,
    METADATA,
    sa.Column("bucket_at", sa.DateTime(timezone=True), primary_key=True),
    sa.Column("instance_id", sa.Uuid(as_uuid=True), primary_key=True),
    sa.Column("quality", sa.Text(), nullable=False),
    sa.Column("telemetry_status", sa.Text(), nullable=False),
    sa.Column("ttft_p50_ms", sa.Float),
    sa.Column("ttft_p95_ms", sa.Float),
    sa.Column("ttft_p99_ms", sa.Float),
    sa.Column("output_tokens_delta", sa.Float),
    sa.Column("output_tps_avg", sa.Float),
    sa.Column("output_tps_max", sa.Float),
    sa.Column("requests_delta", sa.Float),
    sa.Column("running_avg", sa.Float),
    sa.Column("running_max", sa.Float),
    sa.Column("running_last", sa.Float),
    sa.Column("waiting_avg", sa.Float),
    sa.Column("waiting_max", sa.Float),
    sa.Column("waiting_last", sa.Float),
    sa.Column("queue_time_p95_ms", sa.Float),
    sa.Column("kv_cache_avg", sa.Float),
    sa.Column("kv_cache_max", sa.Float),
    sa.Column("kv_cache_last", sa.Float),
    sa.Column("errors_delta", sa.Float),
    sa.Column("error_rate", sa.Float),
    schema=TELEMETRY_SCHEMA,
    postgresql_partition_by="RANGE (bucket_at)",
)

SERVICE_METRICS_1M: Final = sa.Table(
    SERVICE_METRICS_1M_TABLE_NAME,
    METADATA,
    sa.Column("bucket_at", sa.DateTime(timezone=True), primary_key=True),
    sa.Column("service_id", sa.Uuid(as_uuid=True), primary_key=True),
    sa.Column("quality", sa.Text(), nullable=False),
    sa.Column("telemetry_status", sa.Text(), nullable=False),
    sa.Column("ttft_p50_ms", sa.Float),
    sa.Column("ttft_p95_ms", sa.Float),
    sa.Column("ttft_p99_ms", sa.Float),
    sa.Column("tpot_p50_ms", sa.Float),
    sa.Column("tpot_p95_ms", sa.Float),
    sa.Column("e2e_latency_p50_ms", sa.Float),
    sa.Column("e2e_latency_p95_ms", sa.Float),
    sa.Column("e2e_latency_p99_ms", sa.Float),
    sa.Column("output_tokens_delta", sa.Float),
    sa.Column("output_tps_avg", sa.Float),
    sa.Column("output_tps_max", sa.Float),
    sa.Column("inflight_avg", sa.Float),
    sa.Column("inflight_max", sa.Float),
    sa.Column("inflight_last", sa.Float),
    sa.Column("request_rate_avg", sa.Float),
    sa.Column("request_rate_max", sa.Float),
    sa.Column("accepted_requests", sa.BigInteger),
    sa.Column("rejected_requests", sa.BigInteger),
    sa.Column("client_disconnects", sa.BigInteger),
    sa.Column("error_requests", sa.BigInteger),
    sa.Column("error_rate", sa.Float),
    schema=TELEMETRY_SCHEMA,
    postgresql_partition_by="RANGE (bucket_at)",
)

GPU_METRICS_1M: Final = sa.Table(
    GPU_METRICS_1M_TABLE_NAME,
    METADATA,
    sa.Column("bucket_at", sa.DateTime(timezone=True), primary_key=True),
    sa.Column("gpu_id", sa.Text(), primary_key=True),
    sa.Column("quality", sa.Text(), nullable=False),
    sa.Column("telemetry_status", sa.Text(), nullable=False),
    sa.Column("utilization_avg", sa.Float),
    sa.Column("utilization_max", sa.Float),
    sa.Column("utilization_last", sa.Float),
    sa.Column("memory_used_bytes_avg", sa.Float),
    sa.Column("memory_used_bytes_max", sa.BigInteger),
    sa.Column("memory_used_bytes_last", sa.BigInteger),
    sa.Column("temperature_celsius_avg", sa.Float),
    sa.Column("temperature_celsius_max", sa.Float),
    sa.Column("temperature_celsius_last", sa.Float),
    sa.Column("power_watts_avg", sa.Float),
    sa.Column("power_watts_max", sa.Float),
    sa.Column("power_watts_last", sa.Float),
    sa.Column("health_last", sa.Text),
    schema=TELEMETRY_SCHEMA,
    postgresql_partition_by="RANGE (bucket_at)",
)


@dataclass(frozen=True, slots=True)
class InstanceMinuteMetric:
    bucket_at: datetime
    instance_id: UUID
    quality: TelemetryQualityValue
    telemetry_status: TelemetryStatusValue
    ttft_p50_ms: float | None = None
    ttft_p95_ms: float | None = None
    ttft_p99_ms: float | None = None
    output_tokens_delta: float | None = None
    output_tps_avg: float | None = None
    output_tps_max: float | None = None
    requests_delta: float | None = None
    running_avg: float | None = None
    running_max: float | None = None
    running_last: float | None = None
    waiting_avg: float | None = None
    waiting_max: float | None = None
    waiting_last: float | None = None
    queue_time_p95_ms: float | None = None
    kv_cache_avg: float | None = None
    kv_cache_max: float | None = None
    kv_cache_last: float | None = None
    errors_delta: float | None = None
    error_rate: float | None = None


@dataclass(frozen=True, slots=True)
class ServiceMinuteMetric:
    bucket_at: datetime
    service_id: UUID
    quality: TelemetryQualityValue
    telemetry_status: TelemetryStatusValue
    ttft_p50_ms: float | None = None
    ttft_p95_ms: float | None = None
    ttft_p99_ms: float | None = None
    tpot_p50_ms: float | None = None
    tpot_p95_ms: float | None = None
    e2e_latency_p50_ms: float | None = None
    e2e_latency_p95_ms: float | None = None
    e2e_latency_p99_ms: float | None = None
    output_tokens_delta: float | None = None
    output_tps_avg: float | None = None
    output_tps_max: float | None = None
    inflight_avg: float | None = None
    inflight_max: float | None = None
    inflight_last: float | None = None
    request_rate_avg: float | None = None
    request_rate_max: float | None = None
    accepted_requests: int | None = None
    rejected_requests: int | None = None
    client_disconnects: int | None = None
    error_requests: int | None = None
    error_rate: float | None = None


@dataclass(frozen=True, slots=True)
class GpuMinuteMetric:
    bucket_at: datetime
    gpu_id: str
    quality: TelemetryQualityValue
    telemetry_status: TelemetryStatusValue
    utilization_avg: float | None = None
    utilization_max: float | None = None
    utilization_last: float | None = None
    memory_used_bytes_avg: float | None = None
    memory_used_bytes_max: int | None = None
    memory_used_bytes_last: int | None = None
    temperature_celsius_avg: float | None = None
    temperature_celsius_max: float | None = None
    temperature_celsius_last: float | None = None
    power_watts_avg: float | None = None
    power_watts_max: float | None = None
    power_watts_last: float | None = None
    health_last: str | None = None


MinuteMetric = TypeVar("MinuteMetric", InstanceMinuteMetric, ServiceMinuteMetric, GpuMinuteMetric)


def _month_bounds(value: datetime) -> tuple[datetime, datetime]:
    start = as_utc(value).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


def _partition_statement(table_name: str, bucket_at: datetime) -> sa.TextClause:
    if table_name not in _MANAGED_TABLES:
        raise ValueError("unmanaged telemetry table")
    start, _ = _month_bounds(bucket_at)
    return sa.text(
        "SELECT core.holm_ensure_month_partition(:parent_name, :month_start)"
    ).bindparams(parent_name=f"telemetry.{table_name}", month_start=start.date())


class TelemetryMinuteRepository:
    """Upsert and query sparse minute-wide telemetry records."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self,
        *,
        instances: Iterable[InstanceMinuteMetric] = (),
        services: Iterable[ServiceMinuteMetric] = (),
        gpus: Iterable[GpuMinuteMetric] = (),
    ) -> int:
        groups = (
            (INSTANCE_METRICS_1M, "instance_id", tuple(instances)),
            (SERVICE_METRICS_1M, "service_id", tuple(services)),
            (GPU_METRICS_1M, "gpu_id", tuple(gpus)),
        )
        written = 0
        for table, identity_name, records in groups:
            for bucket_at in sorted({record.bucket_at for record in records}):
                await self._session.execute(_partition_statement(table.name, bucket_at))
            for record in records:
                values = asdict(record)
                statement = postgresql_insert(table).values(**values)
                update_values = {
                    column.name: statement.excluded[column.name]
                    for column in table.columns
                    if column.name not in {"bucket_at", identity_name}
                }
                await self._session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[table.c.bucket_at, table.c[identity_name]],
                        set_=update_values,
                    )
                )
                written += 1
        return written

    async def list_instances(
        self, instance_id: UUID, *, since: datetime
    ) -> tuple[InstanceMinuteMetric, ...]:
        return await self._list(
            INSTANCE_METRICS_1M,
            InstanceMinuteMetric,
            INSTANCE_METRICS_1M.c.instance_id == instance_id,
            since,
        )

    async def list_services(
        self, service_id: UUID, *, since: datetime
    ) -> tuple[ServiceMinuteMetric, ...]:
        return await self._list(
            SERVICE_METRICS_1M,
            ServiceMinuteMetric,
            SERVICE_METRICS_1M.c.service_id == service_id,
            since,
        )

    async def list_gpus(self, *, since: datetime) -> tuple[GpuMinuteMetric, ...]:
        return await self._list(GPU_METRICS_1M, GpuMinuteMetric, sa.true(), since)

    async def _list(
        self,
        table: sa.Table,
        record_type: type[MinuteMetric],
        identity_clause: sa.ColumnElement[bool],
        since: datetime,
    ) -> tuple[MinuteMetric, ...]:
        rows = (
            await self._session.execute(
                sa.select(table)
                .where(table.c.bucket_at >= as_utc(since), identity_clause)
                .order_by(table.c.bucket_at)
            )
        ).mappings()
        return tuple(record_type(**dict(row)) for row in rows)


async def drop_expired_telemetry_partitions(
    engine: AsyncEngine,
    *,
    retention_days: int = TELEMETRY_RETENTION_DAYS,
    now: datetime,
) -> tuple[str, ...]:
    """Drop only catalog-verified managed partitions wholly before the cutoff."""

    if isinstance(retention_days, bool) or retention_days <= 0:
        raise ValueError("telemetry retention days must be positive")
    cutoff = as_utc(now) - timedelta(days=retention_days)
    dropped: list[str] = []
    async with engine.begin() as connection:
        for table_name in _MANAGED_TABLES:
            names = await connection.scalar(
                sa.text("SELECT core.holm_drop_expired_month_partitions(:parent_name, :cutoff)"),
                {"parent_name": f"telemetry.{table_name}", "cutoff": cutoff},
            )
            dropped.extend(names or ())
    return tuple(dropped)


__all__ = [
    "GPU_METRICS_1M",
    "GPU_METRICS_1M_TABLE_NAME",
    "GpuMinuteMetric",
    "INSTANCE_METRICS_1M",
    "INSTANCE_METRICS_1M_TABLE_NAME",
    "InstanceMinuteMetric",
    "SERVICE_METRICS_1M",
    "SERVICE_METRICS_1M_TABLE_NAME",
    "ServiceMinuteMetric",
    "TELEMETRY_RETENTION_DAYS",
    "TelemetryMinuteRepository",
    "drop_expired_telemetry_partitions",
]

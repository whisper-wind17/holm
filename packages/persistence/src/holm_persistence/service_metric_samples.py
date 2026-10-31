"""Allowlisted cumulative Gateway service samples; no request identifiers or content."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from holm_persistence.accounts import METADATA
from holm_persistence.schemas import TELEMETRY_SCHEMA

SERVICE_METRIC_BUCKET_BOUNDS: Final[tuple[float, ...]] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    20.0,
    30.0,
    60.0,
    120.0,
    300.0,
    float("inf"),
)
MAX_GATEWAY_SERVICE_SAMPLES: Final = 1024

GATEWAY_SERVICE_METRIC_SAMPLES: Final = sa.Table(
    "gateway_service_metric_samples",
    METADATA,
    sa.Column("service_id", sa.Uuid(as_uuid=True), primary_key=True),
    sa.Column("gateway_start_id", sa.Uuid(as_uuid=True), nullable=False),
    sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("ttft_buckets", sa.ARRAY(sa.BigInteger()), nullable=False),
    sa.Column("tpot_buckets", sa.ARRAY(sa.BigInteger()), nullable=False),
    sa.Column("e2e_buckets", sa.ARRAY(sa.BigInteger()), nullable=False),
    sa.Column("output_tokens_total", sa.BigInteger(), nullable=False),
    sa.Column("accepted_requests", sa.BigInteger(), nullable=False),
    sa.Column("rejected_requests", sa.BigInteger(), nullable=False),
    sa.Column("client_disconnects", sa.BigInteger(), nullable=False),
    sa.Column("gateway_errors", sa.BigInteger(), nullable=False),
    sa.Column("runtime_errors", sa.BigInteger(), nullable=False),
    sa.Column("timeout_errors", sa.BigInteger(), nullable=False),
    sa.Column("inflight", sa.Integer(), nullable=False),
    schema=TELEMETRY_SCHEMA,
)


@dataclass(frozen=True, slots=True)
class GatewayServiceMetricSample:
    service_id: UUID
    gateway_start_id: UUID
    sampled_at: datetime
    ttft_buckets: tuple[int, ...]
    tpot_buckets: tuple[int, ...]
    e2e_buckets: tuple[int, ...]
    output_tokens_total: int
    accepted_requests: int
    rejected_requests: int
    client_disconnects: int
    gateway_errors: int
    runtime_errors: int
    timeout_errors: int
    inflight: int

    @property
    def error_requests(self) -> int:
        return self.gateway_errors + self.runtime_errors + self.timeout_errors


class GatewayServiceMetricSampleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, samples: tuple[GatewayServiceMetricSample, ...]) -> None:
        if not samples:
            return
        rows = [asdict(sample) for sample in samples]
        statement = postgresql_insert(GATEWAY_SERVICE_METRIC_SAMPLES).values(rows)
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[GATEWAY_SERVICE_METRIC_SAMPLES.c.service_id],
                set_={
                    column.name: statement.excluded[column.name]
                    for column in GATEWAY_SERVICE_METRIC_SAMPLES.columns
                    if column.name != "service_id"
                },
                where=GATEWAY_SERVICE_METRIC_SAMPLES.c.sampled_at <= statement.excluded.sampled_at,
            )
        )

    async def list_all(self) -> tuple[GatewayServiceMetricSample, ...]:
        rows = (
            await self._session.execute(
                sa.select(GATEWAY_SERVICE_METRIC_SAMPLES)
                .order_by(GATEWAY_SERVICE_METRIC_SAMPLES.c.sampled_at.desc())
                .limit(MAX_GATEWAY_SERVICE_SAMPLES)
            )
        ).mappings()
        return tuple(
            GatewayServiceMetricSample(
                **{
                    **dict(row),
                    "ttft_buckets": tuple(row["ttft_buckets"]),
                    "tpot_buckets": tuple(row["tpot_buckets"]),
                    "e2e_buckets": tuple(row["e2e_buckets"]),
                }
            )
            for row in rows
        )

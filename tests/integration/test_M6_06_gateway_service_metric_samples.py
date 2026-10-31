"""M6-06 PostgreSQL cumulative sample channel and zero-body schema."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import sqlalchemy as sa
from conftest import EphemeralPostgres
from holm_contracts.agent.v1 import agent_pb2
from holm_persistence import (
    GATEWAY_SERVICE_METRIC_SAMPLES,
    SERVICE_METRIC_BUCKET_BOUNDS,
    AiServiceRecord,
    GatewayServiceMetricSample,
    GatewayServiceMetricSampleRepository,
    ModelInstanceRecord,
    create_database_engine,
    create_session_factory,
)
from holm_telemetry import LEGACY_VLLM_METRIC_MAPPING, TelemetryCollector, TelemetryStatus
from migration_test_support import upgrade_head
from sqlalchemy.engine import URL

SERVICE_ID = UUID("00000000-0000-7000-8000-000000006066")
START_ID = UUID("00000000-0000-7000-8000-000000006067")
INSTANCE_ID = UUID("00000000-0000-7000-8000-000000006068")


class _Gpus:
    async def observe_gpus(self) -> agent_pb2.ListGPUsResponse:
        return agent_pb2.ListGPUsResponse(discovery_state=agent_pb2.GPU_DISCOVERY_STATE_READY)


class _Instances:
    async def list(self) -> tuple[ModelInstanceRecord, ...]:
        return ()


class _Services:
    def __init__(self, at: datetime) -> None:
        self._record = AiServiceRecord(
            name="FX service",
            id=SERVICE_ID,
            public_model_name="m606",
            description="",
            enabled=True,
            primary_instance_id=INSTANCE_ID,
            route_version=1,
            created_at=at,
            updated_at=at,
            deleted_at=None,
        )

    async def list(self) -> tuple[AiServiceRecord, ...]:
        return (self._record,)


def test_M6_06_real_postgres_sample_upsert_and_allowlisted_columns(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    upgrade_head(ephemeral_postgres.engine)
    allowed = {
        "service_id",
        "gateway_start_id",
        "sampled_at",
        "ttft_buckets",
        "tpot_buckets",
        "e2e_buckets",
        "output_tokens_total",
        "accepted_requests",
        "rejected_requests",
        "client_disconnects",
        "gateway_errors",
        "runtime_errors",
        "timeout_errors",
        "inflight",
    }
    with ephemeral_postgres.engine.connect() as connection:
        columns = {
            str(name)
            for name in connection.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'telemetry' "
                    "AND table_name = 'gateway_service_metric_samples'"
                )
            ).scalars()
        }
    assert columns == allowed
    assert set(GATEWAY_SERVICE_METRIC_SAMPLES.c.keys()) == allowed

    now = datetime.now(UTC) - timedelta(seconds=10)
    empty_buckets = (0,) * len(SERVICE_METRIC_BUCKET_BOUNDS)
    sample = GatewayServiceMetricSample(
        service_id=SERVICE_ID,
        gateway_start_id=START_ID,
        sampled_at=now,
        ttft_buckets=empty_buckets,
        tpot_buckets=empty_buckets,
        e2e_buckets=empty_buckets,
        output_tokens_total=0,
        accepted_requests=0,
        rejected_requests=0,
        client_disconnects=0,
        gateway_errors=0,
        runtime_errors=0,
        timeout_errors=0,
        inflight=0,
    )
    url = URL.create(
        "postgresql+psycopg",
        username="ubuntu",
        host=str(ephemeral_postgres.socket_directory),
        port=ephemeral_postgres.port,
        database="postgres",
    )

    async def scenario() -> None:
        engine = create_database_engine(url, connect_timeout_seconds=5)
        factory = create_session_factory(engine)
        try:
            async with factory() as session, session.begin():
                repository = GatewayServiceMetricSampleRepository(session)
                await repository.upsert((sample,))
                await repository.upsert(
                    (replace(sample, sampled_at=now + timedelta(seconds=5), accepted_requests=3),)
                )
            async with factory() as session:
                saved = await GatewayServiceMetricSampleRepository(session).list_all()
            assert len(saved) == 1
            assert saved[0].accepted_requests == 3
            assert saved[0].ttft_buckets == empty_buckets
            assert saved[0].sampled_at == now + timedelta(seconds=5)

            collector = TelemetryCollector(
                gpus=_Gpus(),
                instances=_Instances(),
                ai_services=_Services(now),
                session_factory=factory,
                metric_mapping_resolver=lambda _runtime: LEGACY_VLLM_METRIC_MAPPING,
            )
            await collector.collect_once()
            newer = replace(
                sample,
                sampled_at=now + timedelta(seconds=10),
                ttft_buckets=tuple(1 for _ in empty_buckets),
                output_tokens_total=30,
                accepted_requests=6,
            )
            async with factory() as session, session.begin():
                await GatewayServiceMetricSampleRepository(session).upsert((newer,))
            await collector.collect_once()
            snapshot = collector.store.service(SERVICE_ID, now_monotonic=time.monotonic())
            assert snapshot.telemetry_status is TelemetryStatus.AVAILABLE
            assert snapshot.current.output_tps_avg == 6
            assert snapshot.current.request_rate_avg == 0.6
            assert snapshot.current.ttft_p95_ms is not None
        finally:
            await engine.dispose()

    asyncio.run(scenario())

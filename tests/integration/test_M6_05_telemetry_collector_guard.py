"""M6-05 real-PostgreSQL duplicate collector fail-loud guard."""

from __future__ import annotations

import asyncio
import logging

import pytest
from conftest import EphemeralPostgres
from holm_contracts.agent.v1 import agent_pb2
from holm_persistence import create_database_engine, create_session_factory
from holm_telemetry import (
    LEGACY_VLLM_METRIC_MAPPING,
    CollectorAlreadyRunning,
    PostgresHeartbeatGuard,
    RuntimeMetricMapping,
    TelemetryCollector,
)
from migration_test_support import upgrade_head
from sqlalchemy.engine import URL


class _Gpus:
    async def observe_gpus(self) -> agent_pb2.ListGPUsResponse:
        return agent_pb2.ListGPUsResponse(discovery_state=agent_pb2.GPU_DISCOVERY_STATE_READY)


class _Instances:
    async def list(self):  # type: ignore[no-untyped-def]
        return ()


class _Services:
    async def list(self):  # type: ignore[no-untyped-def]
        return ()


def _test_metric_mapping_resolver(_runtime_type: str) -> RuntimeMetricMapping:
    return LEGACY_VLLM_METRIC_MAPPING


def test_M6_05_TC_TM_11_second_collector_is_rejected_with_critical_event(
    ephemeral_postgres: EphemeralPostgres,
    caplog: pytest.LogCaptureFixture,
) -> None:
    upgrade_head(ephemeral_postgres.engine)
    database_url = URL.create(
        "postgresql+psycopg",
        username="ubuntu",
        host=str(ephemeral_postgres.socket_directory),
        port=ephemeral_postgres.port,
        database="postgres",
    )

    async def scenario() -> None:
        engine = create_database_engine(database_url, connect_timeout_seconds=5)
        factory = create_session_factory(engine)
        first = TelemetryCollector(
            gpus=_Gpus(),
            instances=_Instances(),
            ai_services=_Services(),
            guard=PostgresHeartbeatGuard(
                session_factory=factory,
                build_version="1.0.0+m605",
                stale_after_seconds=30,
            ),
            metric_mapping_resolver=_test_metric_mapping_resolver,
            interval_seconds=30,
            jitter_seconds=0,
        )
        second = TelemetryCollector(
            gpus=_Gpus(),
            instances=_Instances(),
            ai_services=_Services(),
            guard=PostgresHeartbeatGuard(
                session_factory=factory,
                build_version="1.0.0+m605",
                stale_after_seconds=30,
            ),
            metric_mapping_resolver=_test_metric_mapping_resolver,
            interval_seconds=30,
            jitter_seconds=0,
        )
        unguarded_control = TelemetryCollector(
            gpus=_Gpus(),
            instances=_Instances(),
            ai_services=_Services(),
            metric_mapping_resolver=_test_metric_mapping_resolver,
            interval_seconds=30,
            jitter_seconds=0,
        )
        try:
            await first.start()
            with (
                caplog.at_level(logging.CRITICAL, logger="holm.telemetry.collector"),
                pytest.raises(CollectorAlreadyRunning),
            ):
                await second.start()
            await second.stop()
            with pytest.raises(CollectorAlreadyRunning):
                await second.start()
            # Positive control: without the guard, a second loop starts. Removing
            # the guard from ``second`` therefore makes the rejection assertion red.
            await unguarded_control.start()
        finally:
            await unguarded_control.stop()
            await second.stop()
            await first.stop()
            await engine.dispose()

    asyncio.run(scenario())

    rejected = [
        record
        for record in caplog.records
        if getattr(record, "event_code", None) == "duplicate_collector_rejected"
    ]
    assert len(rejected) == 2
    assert all(record.levelno == logging.CRITICAL for record in rejected)

"""M6-11 Overview exposes computed request metrics without changing their units."""

import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import UUID

from holm_contracts.agent.v1 import agent_pb2
from holm_control_api.telemetry_routes import OverviewValuesResponse
from holm_persistence import (
    SERVICE_METRIC_BUCKET_BOUNDS,
    AiServiceRecord,
    GatewayServiceMetricSample,
    ModelInstanceRecord,
)
from holm_telemetry import LEGACY_VLLM_METRIC_MAPPING, OverviewValues, TelemetryCollector

SERVICE_WITH_TRAFFIC = UUID("00000000-0000-7000-8000-000000006111")
IDLE_SERVICE = UUID("00000000-0000-7000-8000-000000006112")
GATEWAY_START = UUID("00000000-0000-7000-8000-000000006113")
INSTANCE_ID = UUID("00000000-0000-7000-8000-000000006114")


class _NoGpus:
    async def observe_gpus(self) -> agent_pb2.ListGPUsResponse:
        return agent_pb2.ListGPUsResponse(discovery_state=agent_pb2.GPU_DISCOVERY_STATE_READY)


class _NoInstances:
    async def list(self) -> tuple[ModelInstanceRecord, ...]:
        return ()


class _Services:
    async def list(self) -> tuple[AiServiceRecord, ...]:
        now = datetime.now(UTC)
        return tuple(
            AiServiceRecord(
                name="FX service",
                id=service_id,
                public_model_name=f"m611-{index}",
                description="",
                enabled=True,
                primary_instance_id=INSTANCE_ID,
                route_version=1,
                created_at=now,
                updated_at=now,
                deleted_at=None,
            )
            for index, service_id in enumerate((SERVICE_WITH_TRAFFIC, IDLE_SERVICE))
        )


def _collector() -> TelemetryCollector:
    return TelemetryCollector(
        gpus=_NoGpus(),
        instances=_NoInstances(),
        ai_services=_Services(),
        metric_mapping_resolver=lambda _runtime: LEGACY_VLLM_METRIC_MAPPING,
    )


def _sample(*, sampled_at: datetime, inflight: int = 0) -> GatewayServiceMetricSample:
    empty_buckets = (0,) * len(SERVICE_METRIC_BUCKET_BOUNDS)
    return GatewayServiceMetricSample(
        service_id=SERVICE_WITH_TRAFFIC,
        gateway_start_id=GATEWAY_START,
        sampled_at=sampled_at,
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
        inflight=inflight,
    )


def test_M6_11_TC_TM_05_06_overview_response_exposes_ttft_and_error_rate() -> None:
    values = OverviewValues(
        gpu_allocated=None,
        gpu_available=None,
        gpu_total=None,
        requests_running=3,
        requests_waiting=2,
        services_available=1,
        services_starting=0,
        services_unavailable=0,
        services_total=1,
        ttft_p95_ms=128.0,
        error_rate=0.25,
    )
    response = OverviewValuesResponse.from_value(values).model_dump()
    assert response["requests_running"] == 3
    assert response["ttft_p95_ms"] == 128.0
    assert response["error_rate"] == 0.25


def test_M6_11_TC_UI_08_overview_missing_metrics_are_null() -> None:
    values = OverviewValues(None, None, None, None, None, 0, 0, 0, 0)
    response = OverviewValuesResponse.from_value(values).model_dump()
    assert response["ttft_p95_ms"] is None
    assert response["error_rate"] is None


def test_M6_11_TC_TM_05_overview_ignores_idle_service_without_sample() -> None:
    collector = _collector()
    baseline = _sample(sampled_at=datetime.now(UTC) - timedelta(seconds=6))
    current = replace(
        baseline,
        sampled_at=baseline.sampled_at + timedelta(seconds=5),
        accepted_requests=4,
        runtime_errors=1,
        inflight=3,
    )

    async def scenario() -> None:
        with patch.object(
            collector, "_safe_service_samples", new=AsyncMock(return_value=(baseline,))
        ):
            await collector.collect_once()
        with patch.object(
            collector, "_safe_service_samples", new=AsyncMock(return_value=(current,))
        ):
            await collector.collect_once()

    asyncio.run(scenario())
    overview = collector.store.overview(now_monotonic=time.monotonic()).values
    assert overview.requests_running == 3
    assert overview.error_rate == 0.25


def test_M6_11_TC_TM_05_overview_sample_read_failure_is_unknown() -> None:
    collector = _collector()
    sample = _sample(sampled_at=datetime.now(UTC), inflight=3)

    async def scenario() -> None:
        with patch.object(
            collector, "_safe_service_samples", new=AsyncMock(return_value=(sample,))
        ):
            await collector.collect_once()
        with patch.object(collector, "_safe_service_samples", new=AsyncMock(return_value=None)):
            await collector.collect_once()

    asyncio.run(scenario())
    overview = collector.store.overview(now_monotonic=time.monotonic()).values
    assert overview.requests_running is None
    assert overview.error_rate is None

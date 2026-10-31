"""UI-03 / TC-TM-05/06: FX-UI-03 reuses M6-06 cumulative counter fixtures."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fixtures.asgi_client import AsgiTestClient
from holm_control_api.telemetry_routes import ServiceTelemetryResponse, create_telemetry_router
from holm_control_api.telemetry_service import TelemetryRange, TelemetryService
from holm_telemetry import TelemetryStore
from test_M6_06_TC_TM_05_06_service_metrics import SERVICE_A, SERVICE_B, START_B, _sample


def test_UI_03_TC_TM_05_06_merges_buckets_and_divides_summed_counts() -> None:
    store = TelemetryStore()
    now = datetime.now(UTC)
    start = now - timedelta(minutes=10)
    samples = (
        _sample(SERVICE_A, sampled_at=start),
        _sample(
            SERVICE_A,
            sampled_at=start + timedelta(seconds=5),
            observations=(0.01,) * 99,
            accepted=99,
            runtime_errors=1,
        ),
        _sample(
            SERVICE_A,
            sampled_at=now,
            observations=(0.01,) * 99 + (1.0,),
            accepted=100,
            runtime_errors=2,
        ),
        _sample(SERVICE_B, sampled_at=start),
        _sample(SERVICE_B, sampled_at=now, observations=(10.0,), accepted=1, runtime_errors=1),
    )
    for sample in samples:
        store.record_service(sample, observed_monotonic=0, now=now)
    result = store.service_request_window(SERVICE_A, sampled_at=now)
    assert result.requests == 100
    assert result.error_rate == 0.02  # not the mean of 1/99 and 1/1
    assert result.ttft_p95_ms == pytest.approx(5 + 5 * 95 / 99)
    assert result.started_at == start
    snapshot = store.service(SERVICE_A, now_monotonic=0)
    point_p95s = [p.values.ttft_p95_ms for p in snapshot.series if p.delta is not None]
    assert all(value is not None for value in point_p95s)
    assert result.ttft_p95_ms != pytest.approx(sum(value or 0 for value in point_p95s) / 2)
    response = ServiceTelemetryResponse.from_snapshot(snapshot)
    assert response.window_1h.requests == 100
    assert response.window_1h.ttft_p95_ms == result.ttft_p95_ms
    assert response.current.ttft_p95_ms == snapshot.current.ttft_p95_ms
    assert len(response.series) == len(samples) - 2


def test_UI_03_TC_TM_05_unknown_idle_reset_and_expired_window() -> None:
    store = TelemetryStore()
    now = datetime.now(UTC)
    start = now - timedelta(seconds=15)
    assert store.service_request_window(SERVICE_A, sampled_at=now).requests is None
    first = _sample(SERVICE_A, sampled_at=start)
    store.record_service(first, observed_monotonic=0, now=now)
    baseline = store.service_request_window(SERVICE_A, sampled_at=now)
    assert baseline.requests is None
    assert baseline.started_at == start
    idle = replace(first, sampled_at=start + timedelta(seconds=5))
    store.record_service(idle, observed_monotonic=5, now=now)
    assert store.service(SERVICE_A, now_monotonic=5).current.output_tps_avg == 0
    idle_window = store.service_request_window(SERVICE_A, sampled_at=now)
    assert idle_window.requests == 0
    assert idle_window.ttft_p95_ms is None
    assert idle_window.error_rate is None
    restart = replace(
        first,
        sampled_at=start + timedelta(seconds=10),
        gateway_start_id=START_B,
        accepted_requests=500,
    )
    store.record_service(restart, observed_monotonic=10, now=now)
    assert store.service_request_window(SERVICE_A, sampled_at=now).requests == 0
    store.record_service(
        replace(restart, sampled_at=now, accepted_requests=504, runtime_errors=1),
        observed_monotonic=15,
        now=now,
    )
    result = store.service_request_window(SERVICE_A, sampled_at=now)
    assert result.requests == 4
    assert result.error_rate == 0.25
    assert (result.requests, result.error_rate, result.started_at) == store.overview_request_window(
        sampled_at=now
    )
    expired = store.service_request_window(SERVICE_A, sampled_at=now + timedelta(hours=2))
    assert expired.requests is None
    assert expired.started_at is None


def test_UI_03_TC_TM_05_skips_counter_decrease_old_and_future_samples() -> None:
    store = TelemetryStore()
    now = datetime.now(UTC)
    for sample in (
        _sample(SERVICE_A, sampled_at=now - timedelta(hours=2)),
        _sample(
            SERVICE_A, sampled_at=now - timedelta(hours=2) + timedelta(seconds=5), accepted=100
        ),
        _sample(SERVICE_A, sampled_at=now - timedelta(minutes=5)),
        _sample(SERVICE_A, sampled_at=now, accepted=10, runtime_errors=2),
        _sample(SERVICE_A, sampled_at=now + timedelta(seconds=5), accepted=100),
    ):
        store.record_service(sample, observed_monotonic=0, now=now)
    window = store.service_request_window(SERVICE_A, sampled_at=now)
    assert window.requests == 10
    assert window.error_rate == 0.2
    assert window.started_at == now - timedelta(minutes=5)


def test_UI_03_TC_TM_05_response_keeps_chart_range_and_uses_ring_for_summary() -> None:
    store = TelemetryStore()
    now = datetime.now(UTC)
    for sample in (
        _sample(SERVICE_A, sampled_at=now - timedelta(seconds=5)),
        _sample(SERVICE_A, sampled_at=now, accepted=10, runtime_errors=1),
    ):
        store.record_service(sample, observed_monotonic=0, now=now)
    recent = store.service(SERVICE_A, now_monotonic=0)
    historical = replace(recent, current=replace(recent.current, ttft_p95_ms=999))
    source = Mock(spec=TelemetryService)
    source.service = AsyncMock(side_effect=[historical, recent])
    app = FastAPI()
    app.include_router(create_telemetry_router(cast(TelemetryService, source)))

    async def scenario() -> None:
        response = await AsgiTestClient(app).get(f"/api/v1/telemetry/services/{SERVICE_A}?range=6h")
        assert response.status == 200
        payload = response.json()
        assert payload["window_1h"]["requests"] == 10
        assert payload["window_1h"]["error_rate"] == 0.1
        assert payload["current"]["ttft_p95_ms"] == 999
        assert len(payload["series"]) == 2
        assert source.service.await_args_list[0].args == (SERVICE_A, TelemetryRange.SIX_HOURS)
        assert source.service.await_args_list[1].args == (SERVICE_A, TelemetryRange.ONE_HOUR)

    asyncio.run(scenario())

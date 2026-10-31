"""UI-01 / TC-TM-05: FX-UI-01 uses the existing service-counter fixture."""

import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import UUID

from holm_control_api.telemetry_routes import OverviewValuesResponse
from holm_telemetry import TelemetryStore
from test_M6_11_TC_TM_05_06_overview_response import IDLE_SERVICE, _collector, _sample


def test_UI_01_TC_TM_05_window_sums_services_and_points_skips_reset_and_old_points() -> None:
    store = TelemetryStore()
    now = datetime.now(UTC)
    start = now - timedelta(minutes=20)
    first = _sample(sampled_at=start)
    other = replace(first, service_id=IDLE_SERVICE)
    samples = (
        replace(first, sampled_at=now - timedelta(hours=2)),
        replace(
            first, sampled_at=now - timedelta(hours=2) + timedelta(seconds=5), accepted_requests=500
        ),
        first,  # counter reset: this window must not contribute a negative delta
        replace(
            first, sampled_at=start + timedelta(seconds=5), accepted_requests=10, runtime_errors=2
        ),
        replace(
            first, sampled_at=start + timedelta(seconds=10), accepted_requests=20, runtime_errors=4
        ),
        other,
        replace(other, sampled_at=now, accepted_requests=80, runtime_errors=1),
    )
    for sample in samples:
        store.record_service(sample, observed_monotonic=0, now=now)
    assert store.overview_request_window(sampled_at=now) == (100, 0.05, start)


def test_UI_01_TC_TM_05_missing_zero_and_gateway_restart_are_distinct() -> None:
    store = TelemetryStore()
    now = datetime.now(UTC)
    assert store.overview_request_window(sampled_at=now) == (None, None, None)
    first = _sample(sampled_at=now - timedelta(seconds=10))
    store.record_service(first, observed_monotonic=0, now=now)
    assert store.overview_request_window(sampled_at=now) == (None, None, first.sampled_at)
    idle = replace(first, sampled_at=now - timedelta(seconds=5))
    store.record_service(idle, observed_monotonic=5, now=now)
    restart = replace(first, sampled_at=now, gateway_start_id=UUID(int=42), accepted_requests=100)
    store.record_service(restart, observed_monotonic=10, now=now)
    assert store.overview_request_window(sampled_at=now) == (0, None, first.sampled_at)


def test_UI_01_TC_TM_05_collector_response_and_read_failure() -> None:
    collector = _collector()
    now = datetime.now(UTC)
    baseline = _sample(sampled_at=now - timedelta(seconds=5))
    current = replace(baseline, sampled_at=now, accepted_requests=7, runtime_errors=1)

    async def scenario() -> None:
        for sample in (baseline, current):
            with patch.object(
                collector, "_safe_service_samples", new=AsyncMock(return_value=(sample,))
            ):
                await collector.collect_once()
        values = collector.store.overview(now_monotonic=time.monotonic()).values
        response = OverviewValuesResponse.from_value(values)
        assert response.requests_1h == 7
        assert response.error_rate_1h == 1 / 7
        assert response.window_started_at == baseline.sampled_at
        with patch.object(collector, "_safe_service_samples", new=AsyncMock(return_value=None)):
            await collector.collect_once()
        failed = collector.store.overview(now_monotonic=time.monotonic()).values
        assert failed.requests_1h is None
        assert failed.error_rate_1h is None

    asyncio.run(scenario())

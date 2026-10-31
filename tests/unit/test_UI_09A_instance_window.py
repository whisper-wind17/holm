"""UI-09A: FX-UI-09A synthetic cumulative Runtime counters, PDD 9.4/15.3."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from fastapi import FastAPI
from fixtures.asgi_client import AsgiTestClient
from holm_control_api.telemetry_routes import (
    InstanceTelemetryResponse,
    create_telemetry_router,
)
from holm_control_api.telemetry_service import TelemetryRange, TelemetryService
from holm_telemetry import CanonicalRuntimeMetric, TelemetryStore
from holm_telemetry.metrics import RuntimeMetricSource, RuntimeMetricType

INSTANCE = UUID("00000000-0000-7000-8000-000000000909")
MAPPING: dict[CanonicalRuntimeMetric, RuntimeMetricSource | None] = {
    metric: None for metric in CanonicalRuntimeMetric
} | {
    CanonicalRuntimeMetric.REQUESTS_TOTAL: RuntimeMetricSource(
        "requests", RuntimeMetricType.COUNTER
    ),
    CanonicalRuntimeMetric.ERRORS_TOTAL: RuntimeMetricSource("errors", RuntimeMetricType.COUNTER),
    CanonicalRuntimeMetric.TTFT_SECONDS: RuntimeMetricSource("ttft", RuntimeMetricType.HISTOGRAM),
}


def record(
    store: TelemetryStore,
    at: datetime,
    requests: int,
    errors: int | None,
    buckets: tuple[int, int, int],
) -> None:
    lines = ["# TYPE requests counter", f"requests {requests}", "# TYPE ttft histogram"]
    for bound, count in zip(("0.1", "1", "+Inf"), buckets, strict=True):
        lines.append(f'ttft_bucket{{le="{bound}"}} {count}')
    mapping = MAPPING.copy()
    if errors is None:
        mapping[CanonicalRuntimeMetric.ERRORS_TOTAL] = None
    else:
        lines.extend(("# TYPE errors counter", f"errors {errors}"))
    store.record_instance(
        INSTANCE,
        "\n".join(lines).encode(),
        sampled_at=at,
        observed_monotonic=at.timestamp(),
        metric_mapping=mapping,
    )


def test_UI_09A_TC_TM_03_05_merges_buckets_and_counts() -> None:
    store = TelemetryStore()
    now = datetime.now(UTC)
    start = now - timedelta(seconds=10)
    record(store, start, 0, 0, (0, 0, 0))
    record(store, now - timedelta(seconds=5), 99, 1, (99, 99, 99))
    record(store, now, 100, 2, (99, 100, 100))
    result = store.instance_request_window(INSTANCE, sampled_at=now)
    assert result.requests == 100
    assert result.error_rate == 0.02
    assert result.ttft_p95_ms == pytest.approx(100 * 95 / 99)
    assert result.started_at == start
    snapshot = store.instance(INSTANCE, now_monotonic=now.timestamp())
    p95s = [point.values.ttft_p95_seconds for point in snapshot.series[1:]]
    assert result.ttft_p95_ms != pytest.approx(sum(value or 0 for value in p95s) * 500)
    response = InstanceTelemetryResponse.from_snapshot(INSTANCE, snapshot)
    assert response.window_1h.requests == 100
    assert response.current.ttft_p95_seconds == snapshot.current.ttft_p95_seconds
    assert len(response.series) == 3
    assert set(response.window_1h.model_dump()) == {
        "requests",
        "error_rate",
        "ttft_p95_ms",
        "started_at",
    }
    assert "raw" not in response.model_dump_json()


def test_UI_09A_TC_TM_03_04_unknown_idle_reset_and_expired_window() -> None:
    store = TelemetryStore()
    now = datetime.now(UTC)
    assert store.instance_request_window(INSTANCE, sampled_at=now).requests is None
    record(store, now - timedelta(seconds=20), 10, 2, (10, 10, 10))
    baseline = store.instance_request_window(INSTANCE, sampled_at=now)
    assert baseline.requests is baseline.ttft_p95_ms is baseline.error_rate is None
    record(store, now - timedelta(seconds=15), 10, 2, (10, 10, 10))
    idle = store.instance_request_window(INSTANCE, sampled_at=now)
    assert idle.requests == 0
    assert idle.ttft_p95_ms is idle.error_rate is None
    record(store, now - timedelta(seconds=10), 0, 0, (0, 0, 0))
    record(store, now, 5, 1, (5, 5, 5))
    result = store.instance_request_window(INSTANCE, sampled_at=now)
    assert result.requests == 5
    assert result.error_rate == 0.2
    assert result.ttft_p95_ms == pytest.approx(95)
    expired = store.instance_request_window(INSTANCE, sampled_at=now + timedelta(hours=2))
    assert expired.requests is expired.started_at is None


def test_UI_09A_TC_TM_02_unsupported_error_counter_stays_distinguishable() -> None:
    store = TelemetryStore()
    now = datetime.now(UTC)
    record(store, now - timedelta(seconds=5), 0, None, (0, 0, 0))
    record(store, now, 5, None, (5, 5, 5))
    response = InstanceTelemetryResponse.from_snapshot(
        INSTANCE,
        store.instance(INSTANCE, now_monotonic=now.timestamp()),
    )
    assert response.window_1h.requests == 5
    assert response.window_1h.error_rate is None
    assert response.window_1h.ttft_p95_ms == pytest.approx(95)
    assert CanonicalRuntimeMetric.ERRORS_TOTAL in response.current.unsupported


@pytest.mark.parametrize("query_range", [TelemetryRange.ONE_HOUR, TelemetryRange.SIX_HOURS])
def test_UI_09A_TC_TM_09_api_preserves_current_and_series(query_range: TelemetryRange) -> None:
    store = TelemetryStore()
    now = datetime.now(UTC)
    record(store, now - timedelta(seconds=5), 0, 0, (0, 0, 0))
    record(store, now, 5, 1, (5, 5, 5))
    recent = store.instance(INSTANCE, now_monotonic=now.timestamp())
    historical = replace(recent, window_1h=type(recent.window_1h)())
    source = Mock(spec=TelemetryService)
    source.instance = AsyncMock(
        side_effect=[recent] if query_range is TelemetryRange.ONE_HOUR else [historical, recent]
    )
    app = FastAPI()
    app.include_router(create_telemetry_router(cast(TelemetryService, source)))

    async def scenario() -> None:
        response = await AsgiTestClient(app).get(
            f"/api/v1/telemetry/instances/{INSTANCE}?range={query_range.value}"
        )
        assert response.status == 200
        payload = response.json()
        assert payload["window_1h"]["requests"] == 5
        assert payload["window_1h"]["error_rate"] == 0.2
        assert payload["window_1h"]["ttft_p95_ms"] == pytest.approx(95)
        original = InstanceTelemetryResponse.from_snapshot(INSTANCE, recent).model_dump(mode="json")
        assert payload["current"] == original["current"]
        assert payload["series"] == original["series"]
        assert source.instance.await_args_list[0].args == (INSTANCE, query_range)
        assert source.instance.await_args_list[-1].args == (INSTANCE, TelemetryRange.ONE_HOUR)

    asyncio.run(scenario())

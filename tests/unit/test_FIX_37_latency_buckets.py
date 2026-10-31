"""FX-FIX-37: synthetic timings from 60s/266s monitoring examples."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from holm_domain import RequestRecordStatus
from holm_gateway.service_metrics import GatewayServiceMetrics
from holm_telemetry import TelemetryStore
from holm_telemetry.collector import _service_delta, _service_percentile

SERVICE_ID = UUID("00000000-0000-7000-8000-000000000037")
NOW = datetime(2026, 10, 28, tzinfo=UTC)


def _finish(metrics: GatewayServiceMetrics, seconds: float) -> None:
    metrics.finished(
        SERVICE_ID,
        forwarded=True,
        status=RequestRecordStatus.COMPLETED,
        http_status=200,
        error_code=None,
        duration_seconds=seconds * 2,
        first_token_seconds=seconds,
        last_token_seconds=seconds * 2,
        output_tokens=2,
    )


@pytest.mark.parametrize(
    ("seconds", "lower", "upper"),
    [
        (15.0, 10.0, 20.0),
        (25.0, 20.0, 30.0),
        (60.0, 30.0, 60.0),
        (90.0, 60.0, 120.0),
        (266.0, 120.0, 300.0),
    ],
)
def test_FIX_37_TC_TM_03_long_latency_percentiles(
    seconds: float, lower: float, upper: float
) -> None:
    metrics = GatewayServiceMetrics()
    metrics.forwarded(SERVICE_ID)
    baseline = metrics.snapshot(sampled_at=NOW)[0]
    _finish(metrics, seconds)
    first = metrics.snapshot(sampled_at=NOW)[0]
    # A second request puts the specified duration directly in the E2E histogram.
    metrics.finished(
        SERVICE_ID,
        forwarded=False,
        status=RequestRecordStatus.COMPLETED,
        http_status=200,
        error_code=None,
        duration_seconds=seconds,
        first_token_seconds=None,
        output_tokens=None,
    )
    sample = metrics.snapshot(sampled_at=NOW + timedelta(seconds=5))[0]
    delta = _service_delta(baseline, sample)
    assert delta is not None
    assert len(sample.ttft_buckets) == len(sample.tpot_buckets) == len(sample.e2e_buckets) == 17
    for buckets in (delta.ttft_buckets, delta.tpot_buckets):
        for quantile in (0.5, 0.95, 0.99):
            value = _service_percentile(buckets, quantile)
            assert value is not None and lower < value <= upper

    # Isolate the second request's E2E delta through the public store path.
    store = TelemetryStore()
    store.record_service(first, observed_monotonic=0, now=NOW)
    store.record_service(sample, observed_monotonic=5, now=sample.sampled_at)
    values = store.service(SERVICE_ID, now_monotonic=5).current
    for value in (values.e2e_latency_p50_ms, values.e2e_latency_p95_ms, values.e2e_latency_p99_ms):
        assert value is not None and lower * 1000 < value <= upper * 1000


def test_FIX_37_TC_TM_03_overflow_keeps_maximum_finite_bound() -> None:
    metrics = GatewayServiceMetrics()
    _finish(metrics, 301)
    sample = metrics.snapshot(sampled_at=NOW)[0]
    for buckets in (sample.ttft_buckets, sample.tpot_buckets, sample.e2e_buckets):
        for quantile in (0.5, 0.95, 0.99):
            assert _service_percentile(buckets, quantile) == 300.0


def test_FIX_37_TC_TM_04_old_buckets_drop_one_window_then_recover() -> None:
    metrics = GatewayServiceMetrics()
    metrics.forwarded(SERVICE_ID)
    baseline = metrics.snapshot(sampled_at=NOW)[0]
    old = replace(baseline, ttft_buckets=(0,) * 12, tpot_buckets=(0,) * 12, e2e_buckets=(0,) * 12)
    _finish(metrics, 30)
    current = metrics.snapshot(sampled_at=NOW + timedelta(seconds=5))[0]
    assert _service_delta(old, current) is None
    store = TelemetryStore()
    store.record_service(old, observed_monotonic=0, now=old.sampled_at)
    store.record_service(current, observed_monotonic=5, now=current.sampled_at)
    values = store.service(SERVICE_ID, now_monotonic=5).current
    assert values.ttft_p99_ms is None
    assert values.tpot_p95_ms is None
    assert values.e2e_latency_p99_ms is None
    _finish(metrics, 133)
    following = metrics.snapshot(sampled_at=NOW + timedelta(seconds=10))[0]
    assert _service_delta(current, following) is not None
    store.record_service(following, observed_monotonic=10, now=following.sampled_at)
    recovered = store.service(SERVICE_ID, now_monotonic=10).current
    assert recovered.ttft_p99_ms == pytest.approx(298_200)
    assert recovered.tpot_p95_ms == pytest.approx(291_000)
    assert recovered.e2e_latency_p99_ms == pytest.approx(298_200)

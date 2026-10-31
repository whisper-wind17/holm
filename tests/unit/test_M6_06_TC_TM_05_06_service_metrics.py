"""M6-06 / TC-TM-05, TC-TM-06 service sampling calculations."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from holm_control_api.telemetry_routes import ServiceMetricValuesResponse
from holm_domain import ErrorCode, RequestRecordStatus
from holm_gateway.service_metrics import GatewayServiceMetrics
from holm_persistence import SERVICE_METRIC_BUCKET_BOUNDS, GatewayServiceMetricSample
from holm_telemetry import (
    TelemetryStatus,
    TelemetryStore,
    histogram_percentile_delta,
)
from holm_telemetry.collector import TelemetryCollector

SERVICE_A = UUID("00000000-0000-7000-8000-000000006061")
SERVICE_B = UUID("00000000-0000-7000-8000-000000006062")
START_A = UUID("00000000-0000-7000-8000-000000006063")
START_B = UUID("00000000-0000-7000-8000-000000006064")
NOW = datetime(2026, 9, 23, 10, tzinfo=UTC)


def _buckets(*observations: float) -> tuple[int, ...]:
    return tuple(
        sum(value <= bound for value in observations) for bound in SERVICE_METRIC_BUCKET_BOUNDS
    )


def _sample(
    service_id: UUID,
    *,
    start_id: UUID = START_A,
    sampled_at: datetime = NOW,
    observations: tuple[float, ...] = (),
    output: int = 0,
    accepted: int = 0,
    rejected: int = 0,
    disconnects: int = 0,
    gateway_errors: int = 0,
    runtime_errors: int = 0,
    timeout_errors: int = 0,
) -> GatewayServiceMetricSample:
    return GatewayServiceMetricSample(
        service_id=service_id,
        gateway_start_id=start_id,
        sampled_at=sampled_at,
        ttft_buckets=_buckets(*observations),
        tpot_buckets=_buckets(),
        e2e_buckets=_buckets(),
        output_tokens_total=output,
        accepted_requests=accepted,
        rejected_requests=rejected,
        client_disconnects=disconnects,
        gateway_errors=gateway_errors,
        runtime_errors=runtime_errors,
        timeout_errors=timeout_errors,
        inflight=0,
    )


def test_M6_06_TC_TM_05_gateway_results_are_finite_and_exclude_4xx_disconnects() -> None:
    metrics = GatewayServiceMetrics()
    metrics.forwarded(SERVICE_A)
    metrics.finished(
        SERVICE_A,
        forwarded=True,
        status=RequestRecordStatus.COMPLETED,
        http_status=200,
        error_code=None,
        duration_seconds=0.25,
        first_token_seconds=0.05,
        last_token_seconds=0.15,
        output_tokens=11,
    )
    metrics.finished(
        SERVICE_A,
        forwarded=False,
        status=RequestRecordStatus.REJECTED,
        http_status=400,
        error_code=ErrorCode.INVALID_REQUEST,
        duration_seconds=0.01,
        first_token_seconds=None,
        output_tokens=None,
    )
    metrics.finished(
        SERVICE_A,
        forwarded=False,
        status=RequestRecordStatus.ABORTED,
        http_status=499,
        error_code=None,
        duration_seconds=0.01,
        first_token_seconds=None,
        output_tokens=None,
    )
    metrics.finished(
        SERVICE_A,
        forwarded=False,
        status=RequestRecordStatus.FAILED,
        http_status=503,
        error_code=ErrorCode.BACKEND_UNAVAILABLE,
        duration_seconds=0.1,
        first_token_seconds=None,
        output_tokens=None,
    )
    sample = metrics.snapshot(sampled_at=NOW)[0]
    assert (sample.accepted_requests, sample.rejected_requests, sample.client_disconnects) == (
        2,
        1,
        1,
    )
    assert (sample.runtime_errors, sample.output_tokens_total, sample.inflight) == (1, 11, 0)
    assert sample.ttft_buckets[-1] == 1
    assert sample.tpot_buckets[-1] == 1
    assert sample.e2e_buckets[-1] == 4


def test_M6_06_TC_TM_05_rates_error_denominator_and_restart_rebaseline() -> None:
    store = TelemetryStore()
    store.record_service(_sample(SERVICE_A), observed_monotonic=100, now=NOW)
    after = _sample(
        SERVICE_A,
        sampled_at=NOW + timedelta(seconds=5),
        observations=(0.1,),
        output=50,
        accepted=4,
        rejected=2,
        disconnects=1,
        runtime_errors=1,
        timeout_errors=1,
    )
    store.record_service(after, observed_monotonic=105, now=after.sampled_at)
    current = store.service(SERVICE_A, now_monotonic=105).current
    assert current.output_tps_avg == 10
    assert current.request_rate_avg == 0.8
    assert (current.accepted_requests, current.rejected_requests, current.client_disconnects) == (
        4,
        2,
        1,
    )
    assert (current.error_requests, current.error_rate) == (2, 0.5)
    assert current.ttft_p95_ms is not None

    restart = replace(_sample(SERVICE_A, start_id=START_B), sampled_at=NOW + timedelta(seconds=10))
    store.record_service(restart, observed_monotonic=110, now=restart.sampled_at)
    current = store.service(SERVICE_A, now_monotonic=110).current
    assert current.output_tps_avg is None
    assert current.request_rate_avg is None
    assert current.ttft_p95_ms is None
    resumed = replace(restart, sampled_at=NOW + timedelta(seconds=15), accepted_requests=1)
    store.record_service(resumed, observed_monotonic=115, now=resumed.sampled_at)
    assert store.service(SERVICE_A, now_monotonic=115).current.request_rate_avg == 0.2


def test_M6_06_TC_TM_06_platform_ttft_merges_bucket_increments() -> None:
    store = TelemetryStore()
    store.record_service(_sample(SERVICE_A), observed_monotonic=100, now=NOW)
    store.record_service(_sample(SERVICE_B), observed_monotonic=100, now=NOW)
    first = _sample(SERVICE_A, sampled_at=NOW + timedelta(seconds=5), observations=(0.01,) * 19)
    second = _sample(SERVICE_B, sampled_at=NOW + timedelta(seconds=5), observations=(1.0,))
    store.record_service(first, observed_monotonic=105, now=first.sampled_at)
    store.record_service(second, observed_monotonic=105, now=second.sampled_at)
    platform = store.platform_ttft_p95_ms(now_monotonic=105)
    first_p95 = store.service(SERVICE_A, now_monotonic=105).current.ttft_p95_ms
    second_p95 = store.service(SERVICE_B, now_monotonic=105).current.ttft_p95_ms
    assert platform == pytest.approx(10)
    assert first_p95 is not None and second_p95 is not None
    assert platform != pytest.approx((first_p95 + second_p95) / 2)
    next_first = _sample(
        SERVICE_A,
        sampled_at=NOW + timedelta(seconds=10),
        observations=(0.01,) * 20,
    )
    store.record_service(next_first, observed_monotonic=110, now=next_first.sampled_at)
    assert store.platform_ttft_p95_ms(now_monotonic=110) == pytest.approx(9.75)


def test_M6_06_stale_service_sample_changes_telemetry_only() -> None:
    store = TelemetryStore()
    store.record_service(_sample(SERVICE_A), observed_monotonic=100, now=NOW)
    assert store.service(SERVICE_A, now_monotonic=111).telemetry_status is TelemetryStatus.DELAYED
    assert (
        store.service(SERVICE_A, now_monotonic=131).telemetry_status is TelemetryStatus.UNAVAILABLE
    )
    assert store.service(SERVICE_A, now_monotonic=131).current.output_tps_avg is None


def test_FIX_10_TC_TM_05_service_e2e_percentiles_share_bucket_delta_formula() -> None:
    store = TelemetryStore()
    baseline = _sample(SERVICE_A)
    store.record_service(baseline, observed_monotonic=100, now=NOW)
    observations = (0.01, 0.02, 0.1, 0.5, 1.0)
    latest = replace(
        baseline,
        sampled_at=NOW + timedelta(seconds=5),
        e2e_buckets=_buckets(*observations),
    )
    store.record_service(latest, observed_monotonic=105, now=latest.sampled_at)
    current = store.service(SERVICE_A, now_monotonic=105).current
    expected = tuple(
        histogram_percentile_delta(
            tuple(zip(SERVICE_METRIC_BUCKET_BOUNDS, latest.e2e_buckets, strict=True)),
            tuple(zip(SERVICE_METRIC_BUCKET_BOUNDS, baseline.e2e_buckets, strict=True)),
            quantile,
        )
        for quantile in (0.5, 0.95, 0.99)
    )
    actual = (
        current.e2e_latency_p50_ms,
        current.e2e_latency_p95_ms,
        current.e2e_latency_p99_ms,
    )
    assert actual == pytest.approx(tuple(value * 1000 for value in expected if value is not None))
    assert actual[0] <= actual[1] <= actual[2]
    response = ServiceMetricValuesResponse.from_value(current)
    assert (
        response.e2e_latency_p50_ms,
        response.e2e_latency_p95_ms,
        response.e2e_latency_p99_ms,
    ) == actual

    collector = object.__new__(TelemetryCollector)
    collector._store = store  # type: ignore[attr-defined]
    minute = collector._service_minute_metric(  # type: ignore[attr-defined]
        SERVICE_A, bucket_at=NOW, observed_monotonic=105
    )
    assert (
        minute.e2e_latency_p50_ms,
        minute.e2e_latency_p95_ms,
        minute.e2e_latency_p99_ms,
    ) == pytest.approx(actual)


def test_FIX_10_TC_TM_05_service_e2e_percentiles_without_samples_are_null() -> None:
    store = TelemetryStore()
    baseline = _sample(SERVICE_A)
    store.record_service(baseline, observed_monotonic=100, now=NOW)
    current = store.service(SERVICE_A, now_monotonic=100).current
    assert (
        current.e2e_latency_p50_ms,
        current.e2e_latency_p95_ms,
        current.e2e_latency_p99_ms,
    ) == (None, None, None)
    collector = object.__new__(TelemetryCollector)
    collector._store = store  # type: ignore[attr-defined]
    minute = collector._service_minute_metric(  # type: ignore[attr-defined]
        SERVICE_A, bucket_at=NOW, observed_monotonic=100
    )
    assert (
        minute.e2e_latency_p50_ms,
        minute.e2e_latency_p95_ms,
        minute.e2e_latency_p99_ms,
    ) == (None, None, None)

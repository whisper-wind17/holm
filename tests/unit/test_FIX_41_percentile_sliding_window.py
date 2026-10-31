"""FX-FIX-41: synthetic 5s samples and sparse completions.

No Runtime, GPU, or request bodies are needed; histograms use production bounds.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from holm_persistence import SERVICE_METRIC_BUCKET_BOUNDS, GatewayServiceMetricSample
from holm_telemetry import (
    CanonicalRuntimeMetric,
    RuntimeMetricDeriver,
    RuntimeMetrics,
    RuntimeMetricSource,
    RuntimeMetricType,
    TelemetryCollector,
    TelemetryStore,
    histogram_percentile_delta,
)

SERVICE_ID = UUID("00000000-0000-7000-8000-000000000041")
INSTANCE_ID = UUID("00000000-0000-7000-8000-000000000042")
START = datetime(2026, 10, 29, tzinfo=UTC)
SERVICE_PERCENTILES = (
    "ttft_p50_ms",
    "ttft_p95_ms",
    "ttft_p99_ms",
    "tpot_p50_ms",
    "tpot_p95_ms",
    "e2e_latency_p50_ms",
    "e2e_latency_p95_ms",
    "e2e_latency_p99_ms",
)
INSTANCE_PERCENTILES = (
    "ttft_p50_seconds",
    "ttft_p95_seconds",
    "ttft_p99_seconds",
    "queue_time_p50_seconds",
    "queue_time_p95_seconds",
)
MAPPING = {
    metric: RuntimeMetricSource(metric.value, RuntimeMetricType.HISTOGRAM)
    if metric in (CanonicalRuntimeMetric.TTFT_SECONDS, CanonicalRuntimeMetric.QUEUE_TIME_SECONDS)
    else RuntimeMetricSource(metric.value, RuntimeMetricType.COUNTER)
    if metric
    in (
        CanonicalRuntimeMetric.OUTPUT_TOKENS_TOTAL,
        CanonicalRuntimeMetric.REQUESTS_TOTAL,
        CanonicalRuntimeMetric.ERRORS_TOTAL,
    )
    else RuntimeMetricSource(metric.value, RuntimeMetricType.GAUGE)
    if metric
    in (
        CanonicalRuntimeMetric.REQUESTS_RUNNING,
        CanonicalRuntimeMetric.REQUESTS_WAITING,
        CanonicalRuntimeMetric.KV_CACHE_USAGE_RATIO,
    )
    else None
    for metric in CanonicalRuntimeMetric
}


def _buckets(observations: tuple[float, ...]) -> tuple[int, ...]:
    return tuple(
        sum(value <= bound for value in observations) for bound in SERVICE_METRIC_BUCKET_BOUNDS
    )


def _service(second: float, observations: tuple[float, ...]) -> GatewayServiceMetricSample:
    return GatewayServiceMetricSample(
        service_id=SERVICE_ID,
        gateway_start_id=INSTANCE_ID,
        sampled_at=START + timedelta(seconds=second),
        ttft_buckets=_buckets(observations),
        tpot_buckets=_buckets(observations),
        e2e_buckets=_buckets(observations),
        output_tokens_total=len(observations) * 10,
        accepted_requests=len(observations),
        rejected_requests=0,
        client_disconnects=0,
        gateway_errors=0,
        runtime_errors=len(observations) // 3,
        timeout_errors=0,
        inflight=2,
    )


def _runtime(observations: tuple[float, ...]) -> RuntimeMetrics:
    buckets = tuple(zip(SERVICE_METRIC_BUCKET_BOUNDS, _buckets(observations), strict=True))
    return RuntimeMetrics(
        running=2,
        waiting=1,
        kv_cache_usage_ratio=0.25,
        output_tokens_total=len(observations) * 10,
        ttft_buckets=buckets,
        queue_time_buckets=buckets,
        model_names=(),
        requests_total=len(observations),
        errors_total=len(observations) // 3,
    )


def _record(store: TelemetryStore, second: float, observations: tuple[float, ...]) -> None:
    service = _service(second, observations)
    store.record_service(service, observed_monotonic=second, now=service.sampled_at)
    raw = _runtime(observations)
    lines = []
    for metric, value in (
        (CanonicalRuntimeMetric.TTFT_SECONDS, raw.ttft_buckets),
        (CanonicalRuntimeMetric.QUEUE_TIME_SECONDS, raw.queue_time_buckets),
    ):
        lines.append(f"# TYPE {metric.value} histogram")
        lines.extend(f'{metric.value}_bucket{{le="{bound}"}} {count}' for bound, count in value)
    for metric, count in (
        (CanonicalRuntimeMetric.OUTPUT_TOKENS_TOTAL, raw.output_tokens_total),
        (CanonicalRuntimeMetric.REQUESTS_TOTAL, raw.requests_total),
        (CanonicalRuntimeMetric.ERRORS_TOTAL, raw.errors_total),
        (CanonicalRuntimeMetric.REQUESTS_RUNNING, raw.running),
        (CanonicalRuntimeMetric.REQUESTS_WAITING, raw.waiting),
        (CanonicalRuntimeMetric.KV_CACHE_USAGE_RATIO, raw.kv_cache_usage_ratio),
    ):
        source = MAPPING[metric]
        assert source is not None
        lines.extend(
            (f"# TYPE {metric.value} {source.metric_type.value}", f"{metric.value} {count}")
        )
    store.record_instance(
        INSTANCE_ID,
        ("\n".join(lines).replace('le="inf"', 'le="+Inf"') + "\n").encode(),
        sampled_at=service.sampled_at,
        observed_monotonic=second,
        metric_mapping=MAPPING,
    )


def _expected(observations: tuple[float, ...], quantile: float) -> float | None:
    return histogram_percentile_delta(
        _runtime(observations).ttft_buckets,
        _runtime(()).ttft_buckets,
        quantile,
    )


def _assert_percentiles(
    store: TelemetryStore, second: float, observations: tuple[float, ...]
) -> None:
    service = store.service(SERVICE_ID, now_monotonic=second)
    instance = store.instance(INSTANCE_ID, now_monotonic=second)
    assert service.current == service.series[-1].values
    assert instance.current == instance.series[-1].values
    for fields, current, scale in (
        (SERVICE_PERCENTILES, service.current, 1000),
        (INSTANCE_PERCENTILES, instance.current, 1),
    ):
        for name in fields:
            quantile = 0.5 if "p50" in name else 0.95 if "p95" in name else 0.99
            expected = _expected(observations, quantile)
            if expected is None:
                assert getattr(current, name) is None
            else:
                assert getattr(current, name) == pytest.approx(expected * scale)


def test_FIX_41_TC_TM_03_sparse_requests_keep_all_percentiles_for_three_minutes() -> None:
    store = TelemetryStore()
    observations: tuple[float, ...] = ()
    completions: list[tuple[int, float]] = []
    _record(store, 0, observations)
    _assert_percentiles(store, 0, ())
    for second in range(5, 186, 5):
        completed = second % 20 == 5
        if completed:
            value = 1.0 if second % 40 == 5 else 0.01
            observations += (value,)
            completions.append((second, value))
        _record(store, second, observations)
        recent = tuple(value for at, value in completions if second - 60 < at <= second)
        _assert_percentiles(store, second, recent)
        service = store.service(SERVICE_ID, now_monotonic=second).current
        instance = store.instance(INSTANCE_ID, now_monotonic=second).current
        assert service.output_tps_avg == instance.output_tps == (2 if completed else 0)
        assert service.accepted_requests == int(completed)
        assert service.request_rate_avg == (0.2 if completed else 0)
        assert service.error_requests == int(completed and len(observations) % 3 == 0)
        assert service.error_rate == (service.error_requests if completed else None)
        assert service.inflight_last == instance.running == 2
        assert instance.waiting == 1
        assert instance.kv_cache_usage_ratio == 0.25
        platform = store.platform_ttft_p95_ms(now_monotonic=second)
        assert platform == (
            pytest.approx(_expected((observations[-1],), 0.95) * 1000) if completed else None
        )


@pytest.mark.parametrize("second", [64.5, 65.0, 70.0])
def test_FIX_41_TC_TM_03_window_is_open_on_left_and_idle_expires(second: float) -> None:
    store = TelemetryStore()
    _record(store, 0, ())
    _record(store, 5, (1.0,))
    _record(store, 10, (1.0,))
    _record(store, second, (1.0,))
    _assert_percentiles(store, second, (1.0,) if second < 65 else ())
    service_hour = store.service_request_window(
        SERVICE_ID, sampled_at=START + timedelta(seconds=second)
    )
    instance_hour = store.instance_request_window(
        INSTANCE_ID, sampled_at=START + timedelta(seconds=second)
    )
    assert service_hour.requests == instance_hour.requests == 1
    assert service_hour.ttft_p95_ms == instance_hour.ttft_p95_ms == pytest.approx(975)
    assert store.overview_request_window(sampled_at=START + timedelta(seconds=second)) == (
        1,
        0,
        START,
    )
    assert store.platform_ttft_p95_ms(now_monotonic=second) is None


def test_FIX_41_current_percentiles_use_last_point_but_tps_keeps_history() -> None:
    store = TelemetryStore()
    _record(store, 0, ())
    _record(store, 5, (1.0,))
    _record(store, 10, ())
    snapshot = store.instance(INSTANCE_ID, now_monotonic=10)
    assert all(getattr(snapshot.current, field) is None for field in INSTANCE_PERCENTILES)
    assert snapshot.series[-1].values.output_tps is None
    assert snapshot.current.output_tps == 2
    assert snapshot.current.running == 2
    assert snapshot.current.waiting == 1
    assert snapshot.current.kv_cache_usage_ratio == 0.25

    store.record_instance(
        INSTANCE_ID,
        b"",
        sampled_at=START + timedelta(seconds=15),
        observed_monotonic=15,
        metric_mapping={metric: None for metric in CanonicalRuntimeMetric},
    )
    unsupported = store.instance(INSTANCE_ID, now_monotonic=15).current
    assert all(getattr(unsupported, field) is None for field in INSTANCE_PERCENTILES)
    assert unsupported.output_tps is None
    assert unsupported.unsupported is not None
    assert CanonicalRuntimeMetric.TTFT_SECONDS in unsupported.unsupported
    assert CanonicalRuntimeMetric.QUEUE_TIME_SECONDS in unsupported.unsupported


@pytest.mark.parametrize("reset", ["counter", "histogram", "bounds", "restart"])
def test_FIX_41_TC_TM_04_service_rebaseline_cannot_reuse_older_buckets(reset: str) -> None:
    store = TelemetryStore()
    _record(store, 0, ())
    _record(store, 5, (1.0,))
    sample = _service(10, (1.0,))
    if reset == "counter":
        sample = replace(sample, output_tokens_total=0)
    elif reset == "histogram":
        sample = replace(sample, ttft_buckets=_buckets(()))
    elif reset == "bounds":
        sample = replace(sample, ttft_buckets=(0,) * 12)
    else:
        sample = replace(sample, gateway_start_id=SERVICE_ID)
    store.record_service(sample, observed_monotonic=10, now=sample.sampled_at)
    assert all(
        getattr(store.service(SERVICE_ID, now_monotonic=10).current, field) is None
        for field in SERVICE_PERCENTILES
    )
    baseline = replace(
        sample,
        sampled_at=START + timedelta(seconds=15),
        ttft_buckets=_buckets(()),
        tpot_buckets=_buckets(()),
        e2e_buckets=_buckets(()),
    )
    store.record_service(baseline, observed_monotonic=15, now=baseline.sampled_at)
    resumed = replace(
        baseline,
        sampled_at=START + timedelta(seconds=20),
        ttft_buckets=_buckets((0.01,)),
        tpot_buckets=_buckets((0.01,)),
        e2e_buckets=_buckets((0.01,)),
    )
    store.record_service(resumed, observed_monotonic=20, now=resumed.sampled_at)
    current = store.service(SERVICE_ID, now_monotonic=20).current
    assert (
        current.ttft_p95_ms
        == current.tpot_p95_ms
        == current.e2e_latency_p95_ms
        == pytest.approx(9.75)
    )


@pytest.mark.parametrize("reset", ["counter", "histogram", "bounds", "missing"])
def test_FIX_41_TC_TM_04_instance_rebaseline_cannot_reuse_older_buckets(reset: str) -> None:
    deriver = RuntimeMetricDeriver()
    deriver.derive(_runtime(()), observed_monotonic=0)
    deriver.derive(_runtime((1.0,)), observed_monotonic=5)
    sample = _runtime((1.0,))
    if reset == "counter":
        sample = replace(sample, output_tokens_total=0)
    elif reset == "histogram":
        sample = replace(
            sample,
            ttft_buckets=_runtime(()).ttft_buckets,
            queue_time_buckets=_runtime(()).queue_time_buckets,
        )
    elif reset == "bounds":
        sample = replace(
            sample,
            ttft_buckets=((0.1, 0), (float("inf"), 0)),
            queue_time_buckets=((0.1, 0), (float("inf"), 0)),
        )
    else:
        sample = replace(sample, ttft_buckets=(), queue_time_buckets=())
    current = deriver.derive(sample, observed_monotonic=10)
    assert all(getattr(current, field) is None for field in INSTANCE_PERCENTILES)
    # A new baseline followed by one fast request must not resurrect the slow request.
    baseline = replace(
        sample,
        ttft_buckets=_runtime(()).ttft_buckets,
        queue_time_buckets=_runtime(()).queue_time_buckets,
    )
    deriver.derive(baseline, observed_monotonic=15)
    fast = _runtime((0.01,))
    resumed = replace(
        baseline, ttft_buckets=fast.ttft_buckets, queue_time_buckets=fast.queue_time_buckets
    )
    current = deriver.derive(resumed, observed_monotonic=20)
    assert current.ttft_p95_seconds == current.queue_time_p95_seconds == pytest.approx(0.00975)


def test_FIX_41_TC_TM_03_aligned_minute_and_hour_aggregates_keep_raw_deltas() -> None:
    store = TelemetryStore()
    observations: tuple[float, ...] = ()
    # No completion at either boundary: the rolling and calendar windows contain
    # the same requests despite their different endpoint inclusion rules.
    for second in range(-5, 66, 5):
        if second in (5, 25, 45):
            observations += (0.01 if second != 25 else 1.0,)
        _record(store, second, observations)
    collector = object.__new__(TelemetryCollector)
    collector._store = store
    service = collector._service_minute_metric(SERVICE_ID, bucket_at=START, observed_monotonic=65)
    instance = collector._instance_minute_metric(
        INSTANCE_ID, bucket_at=START, observed_monotonic=65
    )
    service_point = store.service(SERVICE_ID, now_monotonic=65).series[-2].values
    instance_point = store.instance(INSTANCE_ID, now_monotonic=65).series[-2].values
    for name in SERVICE_PERCENTILES:
        assert getattr(service, name) == getattr(service_point, name)
    for quantile in ("p50", "p95", "p99"):
        assert getattr(instance, f"ttft_{quantile}_ms") == pytest.approx(
            getattr(instance_point, f"ttft_{quantile}_seconds") * 1000
        )
    assert instance.queue_time_p95_ms == pytest.approx(instance_point.queue_time_p95_seconds * 1000)
    assert service.accepted_requests == instance.requests_delta == 3
    assert service.output_tokens_delta == instance.output_tokens_delta == 30
    assert service.output_tps_avg == instance.output_tps_avg == 0.5
    assert service.error_rate == instance.error_rate == pytest.approx(1 / 3)
    assert service.inflight_last == instance.running_last == 2
    for window in (
        store.service_request_window(SERVICE_ID, sampled_at=START + timedelta(seconds=65)),
        store.instance_request_window(INSTANCE_ID, sampled_at=START + timedelta(seconds=65)),
    ):
        assert window.requests == 3
        assert window.error_rate == pytest.approx(1 / 3)
        assert window.ttft_p95_ms == service.ttft_p95_ms
    # Expiring a request in the current curve does not change the completed minute.
    _assert_percentiles(store, 65, (1.0, 0.01))

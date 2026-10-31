"""M6-08A production TTFT P99 derivation and no-data semantics."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from uuid import UUID

import pytest
from holm_telemetry import (
    LEGACY_VLLM_METRIC_MAPPING,
    RuntimeMetricDeriver,
    RuntimeMetrics,
    TelemetryStore,
)

INSTANCE_ID = UUID("00000000-0000-7000-8000-000000060800")
SAMPLED_AT = datetime(2026, 9, 22, tzinfo=UTC)


def _metrics(
    *,
    output_tokens_total: float,
    ttft_buckets: tuple[tuple[float, float], ...],
) -> RuntimeMetrics:
    return RuntimeMetrics(
        running=0.0,
        waiting=0.0,
        kv_cache_usage_ratio=0.0,
        output_tokens_total=output_tokens_total,
        ttft_buckets=ttft_buckets,
        queue_time_buckets=(),
        model_names=("M6-08A-fixture",),
    )


def test_M6_08A_ttft_p99_uses_the_existing_histogram_delta_derivation() -> None:
    deriver = RuntimeMetricDeriver()
    empty = ((0.1, 0.0), (0.5, 0.0), (1.0, 0.0), (math.inf, 0.0))
    populated = ((0.1, 50.0), (0.5, 90.0), (1.0, 100.0), (math.inf, 100.0))

    baseline = deriver.derive(
        _metrics(output_tokens_total=100.0, ttft_buckets=empty),
        observed_monotonic=10.0,
    )
    derived = deriver.derive(
        _metrics(output_tokens_total=160.0, ttft_buckets=populated),
        observed_monotonic=15.0,
    )

    assert baseline.ttft_p99_seconds is None
    assert derived.ttft_p50_seconds == pytest.approx(0.1)
    assert derived.ttft_p95_seconds == pytest.approx(0.75)
    assert derived.ttft_p99_seconds == pytest.approx(0.95)
    p50 = derived.ttft_p50_seconds
    p95 = derived.ttft_p95_seconds
    p99 = derived.ttft_p99_seconds
    assert p50 is not None
    assert p95 is not None
    assert p99 is not None
    assert p50 <= p95 <= p99


def test_M6_08A_ring_current_carries_p99_but_empty_and_baseline_stay_none() -> None:
    store = TelemetryStore()
    empty = store.instance(INSTANCE_ID, now_monotonic=0.0)
    baseline = _vllm_payload(ttft=(0, 0, 0, 0), output_tokens=100)
    populated = _vllm_payload(ttft=(50, 90, 100, 100), output_tokens=160)

    assert empty.current.ttft_p99_seconds is None
    store.record_instance(
        INSTANCE_ID,
        baseline,
        sampled_at=SAMPLED_AT,
        observed_monotonic=10.0,
        metric_mapping=LEGACY_VLLM_METRIC_MAPPING,
    )
    assert store.instance(INSTANCE_ID, now_monotonic=10.0).current.ttft_p99_seconds is None

    store.record_instance(
        INSTANCE_ID,
        populated,
        sampled_at=SAMPLED_AT,
        observed_monotonic=15.0,
        metric_mapping=LEGACY_VLLM_METRIC_MAPPING,
    )
    snapshot = store.instance(INSTANCE_ID, now_monotonic=15.0)

    assert snapshot.series[-1].values.ttft_p99_seconds == pytest.approx(0.95)
    assert snapshot.current.ttft_p99_seconds == pytest.approx(0.95)


def test_M6_08A_ttft_p99_is_none_for_counter_reset_first_window() -> None:
    deriver = RuntimeMetricDeriver()
    populated = ((0.1, 50.0), (0.5, 90.0), (1.0, 100.0), (math.inf, 100.0))
    reset = ((0.1, 1.0), (0.5, 1.0), (1.0, 1.0), (math.inf, 1.0))
    deriver.derive(
        _metrics(output_tokens_total=160.0, ttft_buckets=populated),
        observed_monotonic=10.0,
    )

    first_after_reset = deriver.derive(
        _metrics(output_tokens_total=1.0, ttft_buckets=reset),
        observed_monotonic=15.0,
    )

    assert first_after_reset.ttft_p99_seconds is None


def _vllm_payload(*, ttft: tuple[int, int, int, int], output_tokens: int) -> bytes:
    lines = (
        "# TYPE vllm:generation_tokens_total counter",
        f"vllm:generation_tokens_total {output_tokens}",
        "# TYPE vllm:time_to_first_token_seconds histogram",
        f'vllm:time_to_first_token_seconds_bucket{{le="0.1"}} {ttft[0]}',
        f'vllm:time_to_first_token_seconds_bucket{{le="0.5"}} {ttft[1]}',
        f'vllm:time_to_first_token_seconds_bucket{{le="1"}} {ttft[2]}',
        f'vllm:time_to_first_token_seconds_bucket{{le="+Inf"}} {ttft[3]}',
    )
    return ("\n".join(lines) + "\n").encode()

"""M6-09 / TC-TM-09 minute aggregation and range source selection."""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from holm_contracts.agent.v1 import agent_pb2
from holm_control_api import telemetry_service as service_module
from holm_control_api.telemetry_service import TelemetryRange, TelemetryService
from holm_persistence import InstanceMinuteMetric
from holm_telemetry import (
    LEGACY_VLLM_METRIC_MAPPING,
    TelemetryCollector,
    TelemetryQuality,
    TelemetryStore,
    aggregate_counter,
    aggregate_gauge,
    histogram_percentile_delta,
    minute_quality,
)
from holm_telemetry import collector as collector_module

INSTANCE_ID = UUID("00000000-0000-7000-8000-000000006009")
BUCKET_AT = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def test_M6_09_TC_TM_09_gauge_counter_and_histogram_minute_formulas() -> None:
    gauge = aggregate_gauge((None, 0.0, 2.0, 4.0))
    counter = aggregate_counter(
        (
            (BUCKET_AT, 100.0),
            (BUCKET_AT + timedelta(seconds=10), 120.0),
            (BUCKET_AT + timedelta(seconds=20), 150.0),
        )
    )
    reset = aggregate_counter(
        (
            (BUCKET_AT, 100.0),
            (BUCKET_AT + timedelta(seconds=10), 5.0),
            (BUCKET_AT + timedelta(seconds=20), 15.0),
        )
    )
    previous = ((0.1, 0.0), (0.5, 0.0), (math.inf, 0.0))
    current = ((0.1, 5.0), (0.5, 10.0), (math.inf, 10.0))

    assert gauge.average == 2.0
    assert gauge.maximum == 4.0
    assert gauge.last == 4.0
    assert counter.delta == 50.0
    assert counter.rate_average == 2.5
    assert counter.rate_maximum == 3.0
    assert reset.delta == 10.0
    assert reset.rate_average == 1.0
    assert histogram_percentile_delta(current, previous, 0.5) == pytest.approx(0.1)
    assert histogram_percentile_delta(current, previous, 0.95) == pytest.approx(0.46)


def test_M6_09_TC_TM_09_partial_and_no_data_are_distinct_from_real_zero() -> None:
    assert minute_quality(9) is TelemetryQuality.PARTIAL
    assert minute_quality(10) is TelemetryQuality.FULL
    assert aggregate_gauge((None, None)).last is None
    assert aggregate_gauge((0.0, 0.0)).last == 0.0
    assert aggregate_counter(((BUCKET_AT, None),)).delta is None
    assert (
        aggregate_counter(((BUCKET_AT, 10.0), (BUCKET_AT + timedelta(seconds=5), 10.0))).delta
        == 0.0
    )


class _Instances:
    async def get(self, instance_id: UUID) -> object:
        assert instance_id == INSTANCE_ID
        return object()


class _SessionContext:
    async def __aenter__(self) -> _SessionContext:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    def begin(self) -> _SessionContext:
        return self


class _Repository:
    ranges: list[datetime] = []

    def __init__(self, session: object) -> None:
        del session

    async def list_instances(
        self, instance_id: UUID, *, since: datetime
    ) -> tuple[InstanceMinuteMetric, ...]:
        assert instance_id == INSTANCE_ID
        self.ranges.append(since)
        return (
            InstanceMinuteMetric(
                bucket_at=BUCKET_AT,
                instance_id=INSTANCE_ID,
                quality="partial",
                telemetry_status="delayed",
                running_last=0.0,
                ttft_p99_ms=990.0,
            ),
        )


def test_M6_09_TC_TM_09_1h_uses_ring_and_long_ranges_use_minute_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = TelemetryStore()
    store.record_instance(
        INSTANCE_ID,
        b"vllm:num_requests_running 0\n",
        sampled_at=BUCKET_AT,
        observed_monotonic=100.0,
        metric_mapping=LEGACY_VLLM_METRIC_MAPPING,
    )
    service = object.__new__(TelemetryService)
    service._instances = _Instances()  # type: ignore[attr-defined]
    service._collector = SimpleNamespace(store=store)  # type: ignore[attr-defined]
    service._session_factory = lambda: _SessionContext()  # type: ignore[attr-defined]
    service._clock = lambda: BUCKET_AT + timedelta(hours=8)  # type: ignore[attr-defined]
    monkeypatch.setattr(service_module, "TelemetryMinuteRepository", _Repository)
    _Repository.ranges = []

    async def scenario() -> tuple[int, tuple[str, ...]]:
        ring = await service.instance(INSTANCE_ID, TelemetryRange.ONE_HOUR)
        sources = []
        for query_range in (
            TelemetryRange.SIX_HOURS,
            TelemetryRange.TWENTY_FOUR_HOURS,
            TelemetryRange.SEVEN_DAYS,
        ):
            snapshot = await service.instance(INSTANCE_ID, query_range)
            assert snapshot.series[0].quality is TelemetryQuality.PARTIAL
            assert snapshot.series[0].values.running == 0.0
            assert snapshot.series[0].values.ttft_p99_seconds == pytest.approx(0.99)
            sources.append(query_range.value)
        return len(ring.series), tuple(sources)

    ring_count, sources = asyncio.run(scenario())
    assert ring_count == 1
    assert sources == ("6h", "24h", "7d")
    assert _Repository.ranges == [
        BUCKET_AT + timedelta(hours=2),
        BUCKET_AT - timedelta(hours=16),
        BUCKET_AT - timedelta(days=6, hours=16),
    ]


class _MinuteRepository:
    writes: list[dict[str, object]] = []

    def __init__(self, session: object) -> None:
        del session

    async def upsert(self, **records: object) -> int:
        self.writes.append(records)
        return sum(len(cast(tuple[object, ...], values)) for values in records.values())


def test_M6_09_TC_TM_09_collector_flushes_all_three_targets_on_minute_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_id = UUID("00000000-0000-7000-8000-000000006010")
    collector = TelemetryCollector(
        gpus=cast(Any, object()),
        instances=cast(Any, object()),
        ai_services=cast(Any, object()),
        metric_mapping_resolver=lambda _: LEGACY_VLLM_METRIC_MAPPING,
        session_factory=lambda: cast(Any, _SessionContext()),
    )
    collector._known_instance_ids = {INSTANCE_ID}  # type: ignore[attr-defined]
    collector._known_service_ids = {service_id}  # type: ignore[attr-defined]
    monkeypatch.setattr(collector_module, "TelemetryMinuteRepository", _MinuteRepository)
    _MinuteRepository.writes = []
    payload = b"# TYPE vllm:num_requests_running gauge\nvllm:num_requests_running 0\n"
    for ordinal in range(10):
        sampled_at = BUCKET_AT + timedelta(seconds=ordinal * 5)
        collector.store.record_instance(
            INSTANCE_ID,
            payload,
            sampled_at=sampled_at,
            observed_monotonic=100.0 + ordinal * 5,
            metric_mapping=LEGACY_VLLM_METRIC_MAPPING,
        )
        collector.store.record_gpu(
            agent_pb2.GpuDevice(
                uuid="GPU-M6-09",
                utilization_percent=0.0,
                memory_used_bytes=0,
                health=agent_pb2.GPU_HEALTH_STATE_HEALTHY,
            ),
            sampled_at=sampled_at,
            observed_monotonic=100.0 + ordinal * 5,
        )

    async def scenario() -> None:
        await collector._roll_completed_minute(  # type: ignore[attr-defined]
            sampled_at=BUCKET_AT + timedelta(seconds=55),
            observed_monotonic=155.0,
        )
        await collector._roll_completed_minute(  # type: ignore[attr-defined]
            sampled_at=BUCKET_AT + timedelta(minutes=1),
            observed_monotonic=160.0,
        )

    asyncio.run(scenario())
    assert collector.minute_write_failures_total == 0
    assert len(_MinuteRepository.writes) == 1
    write = _MinuteRepository.writes[0]
    instance = cast(tuple[Any, ...], write["instances"])[0]
    service = cast(tuple[Any, ...], write["services"])[0]
    gpu = cast(tuple[Any, ...], write["gpus"])[0]
    assert instance.quality == "full"
    assert instance.running_last == 0.0
    assert service.quality == "partial"
    assert service.telemetry_status == "unavailable"
    assert gpu.quality == "full"
    assert gpu.utilization_last == 0.0

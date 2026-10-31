"""Framework-independent in-process telemetry collector and bounded query state."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
import time
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol, cast
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from holm_contracts.agent.v1 import agent_pb2
from holm_domain import (
    AiServiceRuntimeStatus,
    Component,
    ComponentStatus,
    ModelInstanceStatus,
    derive_ai_service_runtime_status,
)
from holm_persistence import (
    SERVICE_METRIC_BUCKET_BOUNDS,
    AiServiceRecord,
    AsyncSessionFactory,
    ComponentHeartbeatRepository,
    GatewayServiceMetricSample,
    GatewayServiceMetricSampleRepository,
    GpuMinuteMetric,
    InstanceMinuteMetric,
    ModelInstanceRecord,
    ServiceMinuteMetric,
    TelemetryMinuteRepository,
)
from sqlalchemy.exc import SQLAlchemyError

from holm_telemetry.metrics import (
    LEGACY_VLLM_METRIC_MAPPING,
    MAX_METRICS_BYTES,
    PERCENTILE_WINDOW_SECONDS,
    CanonicalRuntimeMetric,
    DerivedRuntimeMetrics,
    RingBuffer,
    RuntimeMetricDeriver,
    RuntimeMetricMapping,
    RuntimeMetrics,
    TelemetryQuality,
    TelemetryStatus,
    TelemetryStatusTracker,
    aggregate_counter,
    aggregate_gauge,
    histogram_percentile_delta,
    minute_quality,
    parse_runtime_metrics,
)

COLLECTION_INTERVAL_SECONDS: Final = 5.0
COLLECTION_JITTER_SECONDS: Final = 0.5
RUNTIME_TIMEOUT_SECONDS: Final = 2.0
DEFAULT_COLLECTOR_INSTANCE_ID: Final = "control-api-telemetry"

_LOGGER = logging.getLogger("holm.telemetry.collector")


def _seconds_to_ms(value: float | None) -> float | None:
    return None if value is None else value * 1_000


class CollectorAlreadyRunning(RuntimeError):
    """A fresh heartbeat proves this deployment already has a collector."""


class GpuInventorySource(Protocol):
    async def observe_gpus(self) -> agent_pb2.ListGPUsResponse | None:
        """Return the bounded Host Agent GPU observation, or no observation."""


class ModelInstanceSource(Protocol):
    async def list(self) -> tuple[ModelInstanceRecord, ...]: ...


class AiServiceSource(Protocol):
    async def list(self) -> tuple[AiServiceRecord, ...]: ...


class CollectorGuard(Protocol):
    async def start(self) -> None: ...

    async def heartbeat(self) -> None: ...

    async def stop(self) -> None: ...


@dataclass(frozen=True, slots=True)
class InstanceTelemetryPoint:
    sampled_at: datetime
    values: DerivedRuntimeMetrics
    quality: TelemetryQuality = TelemetryQuality.FULL
    telemetry_status: TelemetryStatus = TelemetryStatus.AVAILABLE
    raw: RuntimeMetrics | None = None


@dataclass(frozen=True, slots=True)
class GpuMetricValues:
    utilization_percent: float | None
    memory_used_bytes: int | None
    temperature_celsius: float | None
    power_watts: float | None
    health: str | None = None


@dataclass(frozen=True, slots=True)
class GpuTelemetryPoint:
    sampled_at: datetime
    values: GpuMetricValues
    quality: TelemetryQuality = TelemetryQuality.FULL
    telemetry_status: TelemetryStatus = TelemetryStatus.AVAILABLE


@dataclass(frozen=True, slots=True)
class ServiceRequestWindow:
    """Retained-hour counters and merged histogram, unknown until a delta exists."""

    requests: int | None = None
    error_rate: float | None = None
    ttft_p95_ms: float | None = None
    started_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class InstanceTelemetrySnapshot:
    sampled_at: datetime | None
    telemetry_status: TelemetryStatus
    current: DerivedRuntimeMetrics
    series: tuple[InstanceTelemetryPoint, ...]
    quality: TelemetryQuality = TelemetryQuality.PARTIAL
    window_1h: ServiceRequestWindow = ServiceRequestWindow()


@dataclass(frozen=True, slots=True)
class GpuTelemetrySnapshot:
    uuid: str
    sampled_at: datetime | None
    telemetry_status: TelemetryStatus
    current: GpuMetricValues
    series: tuple[GpuTelemetryPoint, ...]
    quality: TelemetryQuality = TelemetryQuality.PARTIAL


@dataclass(frozen=True, slots=True)
class ServiceMetricValues:
    ttft_p50_ms: float | None = None
    ttft_p95_ms: float | None = None
    ttft_p99_ms: float | None = None
    tpot_p50_ms: float | None = None
    tpot_p95_ms: float | None = None
    e2e_latency_p50_ms: float | None = None
    e2e_latency_p95_ms: float | None = None
    e2e_latency_p99_ms: float | None = None
    output_tps_avg: float | None = None
    output_tps_max: float | None = None
    inflight_avg: float | None = None
    inflight_max: float | None = None
    inflight_last: float | None = None
    request_rate_avg: float | None = None
    accepted_requests: int | None = None
    rejected_requests: int | None = None
    client_disconnects: int | None = None
    error_requests: int | None = None
    error_rate: float | None = None


@dataclass(frozen=True, slots=True)
class ServiceTelemetryPoint:
    sampled_at: datetime
    values: ServiceMetricValues
    quality: TelemetryQuality
    telemetry_status: TelemetryStatus
    delta: _ServiceDelta | None = None


@dataclass(frozen=True, slots=True)
class _ServiceDelta:
    elapsed_seconds: float
    ttft_buckets: tuple[int, ...]
    tpot_buckets: tuple[int, ...]
    e2e_buckets: tuple[int, ...]
    output_tokens: int
    accepted: int
    rejected: int
    disconnects: int
    errors: int


@dataclass(frozen=True, slots=True)
class ServiceTelemetrySnapshot:
    service_id: UUID
    sampled_at: datetime | None
    telemetry_status: TelemetryStatus
    current: ServiceMetricValues
    series: tuple[ServiceTelemetryPoint, ...]
    quality: TelemetryQuality = TelemetryQuality.PARTIAL
    window_1h: ServiceRequestWindow = ServiceRequestWindow()


@dataclass(frozen=True, slots=True)
class OverviewValues:
    gpu_allocated: int | None
    gpu_available: int | None
    gpu_total: int | None
    requests_running: float | None
    requests_waiting: float | None
    services_available: int | None
    services_starting: int | None
    services_unavailable: int | None
    services_total: int | None
    ttft_p95_ms: float | None = None
    error_rate: float | None = None
    requests_1h: int | None = None
    error_rate_1h: float | None = None
    window_started_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class OverviewSnapshot:
    sampled_at: datetime | None
    telemetry_status: TelemetryStatus
    values: OverviewValues


@dataclass(slots=True)
class _InstanceState:
    series: RingBuffer[InstanceTelemetryPoint]
    status: TelemetryStatusTracker
    deriver: RuntimeMetricDeriver


@dataclass(slots=True)
class _GpuState:
    series: RingBuffer[GpuTelemetryPoint]
    status: TelemetryStatusTracker
    disappeared: bool = False


@dataclass(slots=True)
class _ServiceState:
    series: RingBuffer[ServiceTelemetryPoint]
    status: TelemetryStatusTracker
    previous: GatewayServiceMetricSample | None = None
    last_observed_monotonic: float | None = None


_EMPTY_INSTANCE_VALUES = DerivedRuntimeMetrics(None, None, None, None, None, None, None, None, None)
_EMPTY_GPU_VALUES = GpuMetricValues(None, None, None, None, None)
_EMPTY_OVERVIEW_VALUES = OverviewValues(None, None, None, None, None, None, None, None, None)


def _service_delta(
    previous: GatewayServiceMetricSample | None,
    current: GatewayServiceMetricSample,
) -> _ServiceDelta | None:
    if previous is None or previous.gateway_start_id != current.gateway_start_id:
        return None
    elapsed = (current.sampled_at - previous.sampled_at).total_seconds()
    if elapsed <= 0:
        return None
    before = (
        previous.output_tokens_total,
        previous.accepted_requests,
        previous.rejected_requests,
        previous.client_disconnects,
        previous.gateway_errors,
        previous.runtime_errors,
        previous.timeout_errors,
    )
    after = (
        current.output_tokens_total,
        current.accepted_requests,
        current.rejected_requests,
        current.client_disconnects,
        current.gateway_errors,
        current.runtime_errors,
        current.timeout_errors,
    )
    arrays = (
        (previous.ttft_buckets, current.ttft_buckets),
        (previous.tpot_buckets, current.tpot_buckets),
        (previous.e2e_buckets, current.e2e_buckets),
    )
    if any(new < old for old, new in zip(before, after, strict=True)) or any(
        len(old) != len(SERVICE_METRIC_BUCKET_BOUNDS)
        or len(new) != len(SERVICE_METRIC_BUCKET_BOUNDS)
        or any(next_value < old_value for old_value, next_value in zip(old, new, strict=True))
        for old, new in arrays
    ):
        return None
    return _ServiceDelta(
        elapsed_seconds=elapsed,
        ttft_buckets=tuple(new - old for old, new in zip(*arrays[0], strict=True)),
        tpot_buckets=tuple(new - old for old, new in zip(*arrays[1], strict=True)),
        e2e_buckets=tuple(new - old for old, new in zip(*arrays[2], strict=True)),
        output_tokens=current.output_tokens_total - previous.output_tokens_total,
        accepted=current.accepted_requests - previous.accepted_requests,
        rejected=current.rejected_requests - previous.rejected_requests,
        disconnects=current.client_disconnects - previous.client_disconnects,
        errors=current.error_requests - previous.error_requests,
    )


def _service_percentile(buckets: tuple[int, ...], quantile: float) -> float | None:
    return histogram_percentile_delta(
        tuple(zip(SERVICE_METRIC_BUCKET_BOUNDS, buckets, strict=True)),
        tuple((bound, 0) for bound in SERVICE_METRIC_BUCKET_BOUNDS),
        quantile,
    )


def _service_values(
    sample: GatewayServiceMetricSample,
    delta: _ServiceDelta | None,
    history: tuple[ServiceTelemetryPoint, ...],
) -> ServiceMetricValues:
    if delta is None:
        return ServiceMetricValues(
            inflight_avg=float(sample.inflight),
            inflight_max=float(sample.inflight),
            inflight_last=float(sample.inflight),
        )
    cutoff = sample.sampled_at - timedelta(seconds=PERCENTILE_WINDOW_SECONDS)
    ttft, tpot, e2e = list(delta.ttft_buckets), list(delta.tpot_buckets), list(delta.e2e_buckets)
    for point in reversed(history):
        if point.sampled_at <= cutoff or point.delta is None:
            break
        for total, buckets in (
            (ttft, point.delta.ttft_buckets),
            (tpot, point.delta.tpot_buckets),
            (e2e, point.delta.e2e_buckets),
        ):
            for index, value in enumerate(buckets):
                total[index] += value
    output_rate = delta.output_tokens / delta.elapsed_seconds
    request_rate = delta.accepted / delta.elapsed_seconds
    return ServiceMetricValues(
        ttft_p50_ms=_seconds_to_ms(_service_percentile(tuple(ttft), 0.5)),
        ttft_p95_ms=_seconds_to_ms(_service_percentile(tuple(ttft), 0.95)),
        ttft_p99_ms=_seconds_to_ms(_service_percentile(tuple(ttft), 0.99)),
        tpot_p50_ms=_seconds_to_ms(_service_percentile(tuple(tpot), 0.5)),
        tpot_p95_ms=_seconds_to_ms(_service_percentile(tuple(tpot), 0.95)),
        e2e_latency_p50_ms=_seconds_to_ms(_service_percentile(tuple(e2e), 0.5)),
        e2e_latency_p95_ms=_seconds_to_ms(_service_percentile(tuple(e2e), 0.95)),
        e2e_latency_p99_ms=_seconds_to_ms(_service_percentile(tuple(e2e), 0.99)),
        output_tps_avg=output_rate,
        output_tps_max=output_rate,
        inflight_avg=float(sample.inflight),
        inflight_max=float(sample.inflight),
        inflight_last=float(sample.inflight),
        request_rate_avg=request_rate,
        accepted_requests=delta.accepted,
        rejected_requests=delta.rejected,
        client_disconnects=delta.disconnects,
        error_requests=delta.errors,
        error_rate=None if delta.accepted == 0 else delta.errors / delta.accepted,
    )


class TelemetryStore:
    """Single-event-loop Ring Buffers; no telemetry data is persisted."""

    def __init__(self) -> None:
        self._instances: dict[UUID, _InstanceState] = {}
        self._gpus: dict[str, _GpuState] = {}
        self._services: dict[UUID, _ServiceState] = {}
        self._overview_values = _EMPTY_OVERVIEW_VALUES
        self._overview_sampled_at: datetime | None = None
        self._overview_status = TelemetryStatusTracker()

    def reconcile_instances(self, known_ids: set[UUID], running_ids: set[UUID]) -> None:
        for deleted_id in self._instances.keys() - known_ids:
            del self._instances[deleted_id]
        for instance_id in known_ids:
            state = self._instance_state(instance_id)
            if instance_id not in running_ids:
                state.status.failure()

    def record_instance(
        self,
        instance_id: UUID,
        metrics: bytes,
        *,
        sampled_at: datetime,
        observed_monotonic: float,
        metric_mapping: RuntimeMetricMapping = LEGACY_VLLM_METRIC_MAPPING,
    ) -> None:
        state = self._instance_state(instance_id)
        raw = parse_runtime_metrics(metrics, metric_mapping)
        derived = state.deriver.derive(raw, observed_monotonic=observed_monotonic)
        state.series.append(InstanceTelemetryPoint(sampled_at, derived, raw=raw))
        state.status.success(observed_monotonic)

    def fail_instance(self, instance_id: UUID) -> None:
        self._instance_state(instance_id).status.failure()

    def fail_instances(self) -> None:
        for state in self._instances.values():
            state.status.failure()

    def reconcile_gpus(self, known_uuids: set[str]) -> None:
        for missing_uuid in self._gpus.keys() - known_uuids:
            state = self._gpus[missing_uuid]
            state.disappeared = True
            state.status.failure()

    def record_gpu(
        self,
        device: agent_pb2.GpuDevice,
        *,
        sampled_at: datetime,
        observed_monotonic: float,
    ) -> None:
        state = self._gpu_state(device.uuid)
        state.disappeared = False
        state.series.append(
            GpuTelemetryPoint(
                sampled_at,
                GpuMetricValues(
                    utilization_percent=(
                        device.utilization_percent
                        if device.HasField("utilization_percent")
                        else None
                    ),
                    memory_used_bytes=(
                        device.memory_used_bytes if device.HasField("memory_used_bytes") else None
                    ),
                    temperature_celsius=(
                        device.temperature_celsius
                        if device.HasField("temperature_celsius")
                        else None
                    ),
                    power_watts=device.power_watts if device.HasField("power_watts") else None,
                    health=agent_pb2.GpuHealthState.Name(device.health)
                    .lower()
                    .removeprefix("gpu_health_state_"),
                ),
            )
        )
        state.status.success(observed_monotonic)

    def fail_gpus(self) -> None:
        for state in self._gpus.values():
            state.status.failure()

    def reconcile_services(self, known_ids: set[UUID]) -> None:
        for deleted_id in self._services.keys() - known_ids:
            del self._services[deleted_id]

    def record_service(
        self,
        sample: GatewayServiceMetricSample,
        *,
        observed_monotonic: float,
        now: datetime,
    ) -> None:
        state = self._services.setdefault(
            sample.service_id, _ServiceState(RingBuffer(), TelemetryStatusTracker())
        )
        previous = state.previous
        if previous is not None and sample.sampled_at <= previous.sampled_at:
            return
        state.previous = sample
        state.last_observed_monotonic = observed_monotonic
        age = max(0.0, (now - sample.sampled_at).total_seconds())
        state.status.success(observed_monotonic - age)
        delta = _service_delta(previous, sample)
        state.series.append(
            ServiceTelemetryPoint(
                sampled_at=sample.sampled_at,
                values=_service_values(sample, delta, state.series.values()),
                quality=TelemetryQuality.FULL,
                telemetry_status=TelemetryStatus.AVAILABLE,
                delta=delta,
            )
        )

    def fail_services(self) -> None:
        for state in self._services.values():
            state.status.failure()

    def service(self, service_id: UUID, *, now_monotonic: float) -> ServiceTelemetrySnapshot:
        state = self._services.get(service_id)
        if state is None:
            return ServiceTelemetrySnapshot(
                service_id, None, TelemetryStatus.UNAVAILABLE, ServiceMetricValues(), ()
            )
        series = state.series.values()
        status = state.status.status(now_monotonic)
        return ServiceTelemetrySnapshot(
            service_id=service_id,
            sampled_at=series[-1].sampled_at if series else None,
            telemetry_status=status,
            current=ServiceMetricValues()
            if status is TelemetryStatus.UNAVAILABLE
            else series[-1].values,
            series=series,
            quality=series[-1].quality if series else TelemetryQuality.PARTIAL,
            window_1h=self.service_request_window(service_id, sampled_at=datetime.now(UTC)),
        )

    def instance_request_window(
        self, instance_id: UUID, *, sampled_at: datetime
    ) -> ServiceRequestWindow:
        """Merge adjacent raw Runtime deltas in the retained hour.

        Resets establish a new baseline. Missing error counters remain unknown;
        they must not turn an unsupported error rate into a measured zero.
        """
        state = self._instances.get(instance_id)
        if state is None:
            return ServiceRequestWindow()
        cutoff = sampled_at - timedelta(hours=1)
        points = tuple(
            point for point in state.series.values() if cutoff <= point.sampled_at <= sampled_at
        )
        started_at = next((point.sampled_at for point in points if point.raw is not None), None)
        requests = errors = 0.0
        found = False
        errors_known = True
        buckets: dict[float, float] = {}
        for previous_point, current_point in zip(points, points[1:], strict=False):
            previous, current = previous_point.raw, current_point.raw
            if previous is None or current is None:
                continue
            if previous.requests_total is None or current.requests_total is None:
                continue
            request_delta = current.requests_total - previous.requests_total
            error_delta = (
                current.errors_total - previous.errors_total
                if current.errors_total is not None and previous.errors_total is not None
                else None
            )
            same_bounds = tuple(bound for bound, _ in current.ttft_buckets) == tuple(
                bound for bound, _ in previous.ttft_buckets
            )
            bucket_delta = (
                tuple(
                    (bound, count - old_count)
                    for (bound, count), (_, old_count) in zip(
                        current.ttft_buckets, previous.ttft_buckets, strict=True
                    )
                )
                if same_bounds
                else ()
            )
            if (
                request_delta < 0
                or (error_delta is not None and error_delta < 0)
                or any(count < 0 for _, count in bucket_delta)
            ):
                continue
            found = True
            requests += request_delta
            errors_known = errors_known and error_delta is not None
            errors += error_delta or 0
            for bound, count in bucket_delta:
                buckets[bound] = buckets.get(bound, 0) + count
        merged = tuple(sorted(buckets.items()))
        return ServiceRequestWindow(
            requests=int(requests) if found else None,
            error_rate=errors / requests if errors_known and requests > 0 else None,
            ttft_p95_ms=_seconds_to_ms(
                histogram_percentile_delta(merged, tuple((bound, 0) for bound, _ in merged), 0.95)
            )
            if requests > 0
            else None,
            started_at=started_at,
        )

    def service_request_window(
        self, service_id: UUID, *, sampled_at: datetime
    ) -> ServiceRequestWindow:
        """Merge only this service's retained deltas, using the Overview time window."""
        state = self._services.get(service_id)
        if state is None:
            return ServiceRequestWindow()
        cutoff = sampled_at - timedelta(hours=1)
        points = tuple(
            point for point in state.series.values() if cutoff <= point.sampled_at <= sampled_at
        )
        started_at = min((point.sampled_at for point in points), default=None)
        deltas = tuple(point.delta for point in points if point.delta is not None)
        if not deltas:
            return ServiceRequestWindow(started_at=started_at)
        accepted = sum(delta.accepted for delta in deltas)
        errors = sum(delta.errors for delta in deltas)
        buckets = tuple(
            sum(delta.ttft_buckets[index] for delta in deltas)
            for index in range(len(SERVICE_METRIC_BUCKET_BOUNDS))
        )
        return ServiceRequestWindow(
            requests=accepted,
            error_rate=errors / accepted if accepted > 0 else None,
            ttft_p95_ms=_seconds_to_ms(_service_percentile(buckets, 0.95))
            if accepted > 0
            else None,
            started_at=started_at,
        )

    def overview_request_window(
        self, *, sampled_at: datetime
    ) -> tuple[int | None, float | None, datetime | None]:
        """Sum accepted/error deltas in the retained hour, never average rates.

        A first/reset sample has no delta. It establishes the observed start,
        but cannot turn an otherwise unknown request count into a measured zero.
        """
        cutoff = sampled_at - timedelta(hours=1)
        started_at: datetime | None = None
        accepted = errors = 0
        found = False
        for state in self._services.values():
            for point in state.series.values():
                if not cutoff <= point.sampled_at <= sampled_at:
                    continue
                started_at = (
                    point.sampled_at if started_at is None else min(started_at, point.sampled_at)
                )
                if point.delta is None:
                    continue
                found = True
                accepted += point.delta.accepted
                errors += point.delta.errors
        return (
            accepted if found else None,
            errors / accepted if found and accepted > 0 else None,
            started_at,
        )

    def platform_ttft_p95_ms(self, *, now_monotonic: float) -> float | None:
        bucket_sums = [0] * len(SERVICE_METRIC_BUCKET_BOUNDS)
        found = False
        for state in self._services.values():
            if state.status.status(now_monotonic) is TelemetryStatus.UNAVAILABLE:
                continue
            if state.last_observed_monotonic != now_monotonic:
                continue
            points = state.series.values()
            delta = points[-1].delta if points else None
            if delta is None:
                continue
            found = True
            for index, value in enumerate(delta.ttft_buckets):
                bucket_sums[index] += value
        if not found:
            return None
        return _seconds_to_ms(
            histogram_percentile_delta(
                tuple(zip(SERVICE_METRIC_BUCKET_BOUNDS, bucket_sums, strict=True)),
                tuple((bound, 0) for bound in SERVICE_METRIC_BUCKET_BOUNDS),
                0.95,
            )
        )

    def record_overview(
        self,
        values: OverviewValues,
        *,
        sampled_at: datetime,
        observed_monotonic: float,
    ) -> None:
        self._overview_values = values
        self._overview_sampled_at = sampled_at
        self._overview_status.success(observed_monotonic)

    def fail_overview(self) -> None:
        self._overview_status.failure()

    def instance(self, instance_id: UUID, *, now_monotonic: float) -> InstanceTelemetrySnapshot:
        state = self._instances.get(instance_id)
        if state is None:
            return InstanceTelemetrySnapshot(
                None, TelemetryStatus.UNAVAILABLE, _EMPTY_INSTANCE_VALUES, ()
            )
        series = state.series.values()
        status = state.status.status(now_monotonic)
        current = self._instance_current(series)
        if status is TelemetryStatus.UNAVAILABLE:
            current = _EMPTY_INSTANCE_VALUES
        return InstanceTelemetrySnapshot(
            sampled_at=series[-1].sampled_at if series else None,
            telemetry_status=status,
            current=current,
            series=series,
            quality=series[-1].quality if series else TelemetryQuality.PARTIAL,
            window_1h=self.instance_request_window(instance_id, sampled_at=datetime.now(UTC)),
        )

    def gpus(self, *, now_monotonic: float) -> tuple[GpuTelemetrySnapshot, ...]:
        snapshots: list[GpuTelemetrySnapshot] = []
        for uuid, state in sorted(self._gpus.items()):
            series = state.series.values()
            status = (
                TelemetryStatus.UNAVAILABLE
                if state.disappeared
                else state.status.status(now_monotonic)
            )
            current = series[-1].values if series else _EMPTY_GPU_VALUES
            if status is TelemetryStatus.UNAVAILABLE:
                current = _EMPTY_GPU_VALUES
            snapshots.append(
                GpuTelemetrySnapshot(
                    uuid=uuid,
                    sampled_at=series[-1].sampled_at if series else None,
                    telemetry_status=status,
                    current=current,
                    series=series,
                    quality=series[-1].quality if series else TelemetryQuality.PARTIAL,
                )
            )
        return tuple(snapshots)

    def overview(self, *, now_monotonic: float) -> OverviewSnapshot:
        status = self._overview_status.status(now_monotonic)
        return OverviewSnapshot(
            sampled_at=self._overview_sampled_at,
            telemetry_status=status,
            values=(
                _EMPTY_OVERVIEW_VALUES
                if status is TelemetryStatus.UNAVAILABLE
                else self._overview_values
            ),
        )

    def _instance_state(self, instance_id: UUID) -> _InstanceState:
        return self._instances.setdefault(
            instance_id,
            _InstanceState(RingBuffer(), TelemetryStatusTracker(), RuntimeMetricDeriver()),
        )

    def _gpu_state(self, uuid: str) -> _GpuState:
        return self._gpus.setdefault(uuid, _GpuState(RingBuffer(), TelemetryStatusTracker()))

    @staticmethod
    def _instance_current(
        series: tuple[InstanceTelemetryPoint, ...],
    ) -> DerivedRuntimeMetrics:
        if not series:
            return _EMPTY_INSTANCE_VALUES

        def latest(
            name: str, canonical: CanonicalRuntimeMetric, *, allow_history: bool = False
        ) -> float | None:
            if canonical in (series[-1].values.unsupported or ()):
                return None
            # Percentiles already carry a bounded window; do not resurrect expired values.
            for point in reversed(series if allow_history else series[-1:]):
                value = getattr(point.values, name)
                if value is not None:
                    return cast(float, value)
            return None

        return DerivedRuntimeMetrics(
            running=series[-1].values.running,
            waiting=series[-1].values.waiting,
            kv_cache_usage_ratio=series[-1].values.kv_cache_usage_ratio,
            output_tps=latest(
                "output_tps", CanonicalRuntimeMetric.OUTPUT_TOKENS_TOTAL, allow_history=True
            ),
            ttft_p50_seconds=latest("ttft_p50_seconds", CanonicalRuntimeMetric.TTFT_SECONDS),
            ttft_p95_seconds=latest("ttft_p95_seconds", CanonicalRuntimeMetric.TTFT_SECONDS),
            ttft_p99_seconds=latest("ttft_p99_seconds", CanonicalRuntimeMetric.TTFT_SECONDS),
            queue_time_p50_seconds=latest(
                "queue_time_p50_seconds", CanonicalRuntimeMetric.QUEUE_TIME_SECONDS
            ),
            queue_time_p95_seconds=latest(
                "queue_time_p95_seconds", CanonicalRuntimeMetric.QUEUE_TIME_SECONDS
            ),
            unsupported=series[-1].values.unsupported,
        )


_RUNTIME_ALIAS: Final[re.Pattern[str]] = re.compile(
    r"runtime-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
)


def runtime_metrics_url(endpoint: str) -> str:
    normalized = endpoint if "://" in endpoint else f"http://{endpoint}"
    parsed = urlsplit(normalized)
    if (
        parsed.scheme != "http"
        or (
            parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            # hostname lowercases input; check the authority to require canonical aliases.
            and _RUNTIME_ALIAS.fullmatch(parsed.netloc.partition(":")[0]) is None
        )
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Runtime Endpoint is not an approved Runtime HTTP address")
    path = f"{parsed.path.rstrip('/')}/metrics"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _read_metrics(endpoint: str) -> bytes:
    request = urllib.request.Request(
        runtime_metrics_url(endpoint), headers={"Accept": "text/plain"}, method="GET"
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=RUNTIME_TIMEOUT_SECONDS) as response:
        declared_length = response.headers.get("Content-Length")
        if declared_length is not None and int(declared_length) > MAX_METRICS_BYTES:
            raise ValueError("metrics response exceeds 2 MiB")
        payload = bytes(response.read(MAX_METRICS_BYTES + 1))
    if len(payload) > MAX_METRICS_BYTES:
        raise ValueError("metrics response exceeds 2 MiB")
    return payload


async def read_runtime_metrics(endpoint: str) -> bytes:
    async with asyncio.timeout(RUNTIME_TIMEOUT_SECONDS + 0.25):
        return await asyncio.to_thread(_read_metrics, endpoint)


class PostgresHeartbeatGuard:
    """Reject a second fresh collector and publish this collector's lifecycle."""

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory,
        build_version: str,
        stale_after_seconds: float,
        instance_id: str = DEFAULT_COLLECTOR_INSTANCE_ID,
    ) -> None:
        if stale_after_seconds <= 0:
            raise ValueError("heartbeat freshness window must be positive")
        self._session_factory = session_factory
        self._build_version = build_version
        self._stale_after_seconds = stale_after_seconds
        self._instance_id = instance_id

    async def start(self) -> None:
        async with self._session_factory() as session, session.begin():
            repository = ComponentHeartbeatRepository(session)
            current = await repository.read(
                component=Component.TELEMETRY_WORKER,
                instance_id=self._instance_id,
            )
            if (
                current is not None
                and current.status is not ComponentStatus.STOPPING
                and current.age_seconds <= self._stale_after_seconds
            ):
                _LOGGER.critical(
                    "duplicate telemetry collector rejected",
                    extra={
                        "component": Component.TELEMETRY_WORKER.value,
                        "event_code": "duplicate_collector_rejected",
                    },
                )
                raise CollectorAlreadyRunning("a fresh telemetry collector heartbeat exists")
            await repository.write(
                component=Component.TELEMETRY_WORKER,
                instance_id=self._instance_id,
                version=self._build_version,
                status=ComponentStatus.HEALTHY,
            )

    async def heartbeat(self) -> None:
        await self._write(ComponentStatus.HEALTHY)

    async def stop(self) -> None:
        await self._write(ComponentStatus.STOPPING)

    async def _write(self, status: ComponentStatus) -> None:
        try:
            async with self._session_factory() as session, session.begin():
                await ComponentHeartbeatRepository(session).write(
                    component=Component.TELEMETRY_WORKER,
                    instance_id=self._instance_id,
                    version=self._build_version,
                    status=status,
                )
        except SQLAlchemyError:
            _LOGGER.error(
                "telemetry collector heartbeat unavailable",
                extra={
                    "component": Component.TELEMETRY_WORKER.value,
                    "event_code": "heartbeat_failed",
                },
            )


class TelemetryCollector:
    """Collect current Targets into bounded memory and minute-wide storage."""

    def __init__(
        self,
        *,
        gpus: GpuInventorySource,
        instances: ModelInstanceSource,
        ai_services: AiServiceSource,
        store: TelemetryStore | None = None,
        guard: CollectorGuard | None = None,
        metrics_reader: Callable[[str], Awaitable[bytes]] = read_runtime_metrics,
        metric_mapping_resolver: Callable[[str], RuntimeMetricMapping | None],
        session_factory: AsyncSessionFactory | None = None,
        interval_seconds: float = COLLECTION_INTERVAL_SECONDS,
        jitter_seconds: float = COLLECTION_JITTER_SECONDS,
    ) -> None:
        if interval_seconds <= 0 or jitter_seconds < 0 or jitter_seconds >= interval_seconds:
            raise ValueError("telemetry sampling interval is invalid")
        self._gpus = gpus
        self._instances = instances
        self._ai_services = ai_services
        self._store = store or TelemetryStore()
        self._guard = guard
        self._metrics_reader = metrics_reader
        self._metric_mapping_resolver = metric_mapping_resolver
        self._session_factory = session_factory
        self._interval_seconds = interval_seconds
        self._jitter_seconds = jitter_seconds
        self._task: asyncio.Task[None] | None = None
        self._guard_started = False
        self._minute_bucket_at: datetime | None = None
        self._pending_minute_bucket_at: datetime | None = None
        self._known_instance_ids: set[UUID] = set()
        self._known_service_ids: set[UUID] = set()
        self._minute_write_failures_total = 0

    @property
    def store(self) -> TelemetryStore:
        return self._store

    @property
    def minute_write_failures_total(self) -> int:
        return self._minute_write_failures_total

    async def start(self) -> None:
        if self._task is not None:
            return
        if self._guard is not None:
            await self._guard.start()
            self._guard_started = True
        self._task = asyncio.create_task(self._run(), name="telemetry-collector")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._guard is not None and self._guard_started:
            self._guard_started = False
            await self._guard.stop()

    async def collect_once(self) -> None:
        sampled_at = datetime.now(UTC)
        observed_monotonic = time.monotonic()
        instance_result, service_result, gpu_result, service_samples = await asyncio.gather(
            self._safe_instances(),
            self._safe_services(),
            self._safe_gpus(),
            self._safe_service_samples(),
        )
        await self._roll_completed_minute(
            sampled_at=sampled_at,
            observed_monotonic=observed_monotonic,
        )

        gpu_total: int | None = None
        if (
            gpu_result is not None
            and gpu_result.discovery_state == agent_pb2.GPU_DISCOVERY_STATE_READY
        ):
            devices = tuple(gpu_result.devices)
            gpu_total = len(devices)
            self._store.reconcile_gpus({device.uuid for device in devices})
            for device in devices:
                self._store.record_gpu(
                    device,
                    sampled_at=sampled_at,
                    observed_monotonic=observed_monotonic,
                )
        else:
            self._store.fail_gpus()

        if instance_result is None:
            self._store.fail_instances()
            self._store.fail_overview()
            return
        known_ids = {item.id for item in instance_result}
        self._known_instance_ids = known_ids
        running = tuple(
            item
            for item in instance_result
            if item.status is ModelInstanceStatus.RUNNING and item.internal_endpoint is not None
        )
        self._store.reconcile_instances(known_ids, {item.id for item in running})
        await asyncio.gather(
            *(
                self._collect_instance(
                    item.id,
                    item.internal_endpoint or "",
                    item.runtime_type.value,
                    sampled_at=sampled_at,
                    observed_monotonic=observed_monotonic,
                )
                for item in running
            )
        )

        if service_result is None:
            self._store.fail_services()
            self._store.fail_overview()
            return
        self._known_service_ids = {item.id for item in service_result}
        self._store.reconcile_services(self._known_service_ids)
        if service_samples is None:
            self._store.fail_services()
        else:
            for sample in service_samples:
                if sample.service_id in self._known_service_ids:
                    self._store.record_service(
                        sample, observed_monotonic=observed_monotonic, now=sampled_at
                    )
        instance_by_id = {item.id: item for item in instance_result}
        allocated_uuids = {
            uuid
            for item in instance_result
            if item.status
            in {
                ModelInstanceStatus.STARTING,
                ModelInstanceStatus.RUNNING,
                ModelInstanceStatus.STOPPING,
            }
            for uuid in item.expected_gpu_uuids
        }
        # 四态映射是服务状态的唯一定义源；
        # 不要退回按 ModelInstanceStatus 直接数的旧口径。
        service_statuses = tuple(
            derive_ai_service_runtime_status(
                enabled=item.enabled,
                primary_instance_status=(
                    instance.status
                    if (instance := instance_by_id.get(item.primary_instance_id)) is not None
                    else None
                ),
            )
            for item in service_result
        )
        services_available = service_statuses.count(AiServiceRuntimeStatus.AVAILABLE)
        services_starting = service_statuses.count(AiServiceRuntimeStatus.DEGRADED)
        instance_snapshots = [
            self._store.instance(item.id, now_monotonic=observed_monotonic) for item in running
        ]
        request_values_known = all(
            item.current.running is not None and item.current.waiting is not None
            for item in instance_snapshots
        )
        service_snapshots = [
            self._store.service(item.id, now_monotonic=observed_monotonic)
            for item in service_result
        ]
        fresh_service_snapshots = tuple(
            item
            for item in service_snapshots
            if item.telemetry_status is not TelemetryStatus.UNAVAILABLE
        )
        accepted = sum(item.current.accepted_requests or 0 for item in fresh_service_snapshots)
        errors = sum(item.current.error_requests or 0 for item in fresh_service_snapshots)
        requests_1h, error_rate_1h, window_started_at = (
            self._store.overview_request_window(sampled_at=sampled_at)
            if service_samples is not None
            else (None, None, None)
        )
        # Gateway stores only the latest sample per service and writes on change.
        # A successful read with no window points is known zero, even with old samples.
        # Reset-only windows have a start time and must remain unknown.
        if service_samples is not None and window_started_at is None:
            requests_1h, error_rate_1h, window_started_at = 0, None, None
        self._store.record_overview(
            OverviewValues(
                gpu_allocated=(len(allocated_uuids) if gpu_total is not None else None),
                gpu_available=(
                    max(0, gpu_total - len(allocated_uuids)) if gpu_total is not None else None
                ),
                gpu_total=gpu_total,
                requests_1h=requests_1h,
                error_rate_1h=error_rate_1h,
                window_started_at=window_started_at,
                requests_running=(
                    sum(item.current.inflight_last or 0 for item in fresh_service_snapshots)
                    if service_samples is not None
                    else None
                ),
                requests_waiting=(
                    sum(item.current.waiting or 0 for item in instance_snapshots)
                    if request_values_known
                    else None
                ),
                services_available=services_available,
                services_starting=services_starting,
                services_unavailable=service_statuses.count(AiServiceRuntimeStatus.UNAVAILABLE),
                services_total=len(service_result),
                ttft_p95_ms=self._store.platform_ttft_p95_ms(now_monotonic=observed_monotonic),
                error_rate=(
                    errors / accepted if service_samples is not None and accepted > 0 else None
                ),
            ),
            sampled_at=sampled_at,
            observed_monotonic=observed_monotonic,
        )

    async def _roll_completed_minute(
        self,
        *,
        sampled_at: datetime,
        observed_monotonic: float,
    ) -> None:
        if self._session_factory is None:
            return
        current_bucket = sampled_at.astimezone(UTC).replace(second=0, microsecond=0)
        if self._minute_bucket_at is None:
            self._minute_bucket_at = current_bucket
            return
        if current_bucket > self._minute_bucket_at:
            # Only the latest completed bucket is retained for retry. A prolonged
            # database outage must not create an unbounded in-process queue.
            self._pending_minute_bucket_at = current_bucket - timedelta(minutes=1)
            self._minute_bucket_at = current_bucket
        bucket_at = self._pending_minute_bucket_at
        if bucket_at is None:
            return
        try:
            instances = tuple(
                self._instance_minute_metric(
                    instance_id,
                    bucket_at=bucket_at,
                    observed_monotonic=observed_monotonic,
                )
                for instance_id in sorted(self._known_instance_ids, key=str)
            )
            services = tuple(
                self._service_minute_metric(
                    service_id, bucket_at=bucket_at, observed_monotonic=observed_monotonic
                )
                for service_id in sorted(self._known_service_ids, key=str)
            )
            gpus = tuple(
                self._gpu_minute_metric(snapshot, bucket_at=bucket_at)
                for snapshot in self._store.gpus(now_monotonic=observed_monotonic)
            )
            async with self._session_factory() as session, session.begin():
                await TelemetryMinuteRepository(session).upsert(
                    instances=instances,
                    services=services,
                    gpus=gpus,
                )
            self._pending_minute_bucket_at = None
        except Exception as error:
            self._minute_write_failures_total += 1
            _LOGGER.error(
                "minute telemetry write failed; sampling continues",
                extra={
                    "error_code": type(error).__name__[:128],
                    "minute_write_failures_total": self._minute_write_failures_total,
                },
            )

    def _service_minute_metric(
        self, service_id: UUID, *, bucket_at: datetime, observed_monotonic: float
    ) -> ServiceMinuteMetric:
        snapshot = self._store.service(service_id, now_monotonic=observed_monotonic)
        bucket_end = bucket_at + timedelta(minutes=1)
        points = tuple(
            point for point in snapshot.series if bucket_at <= point.sampled_at < bucket_end
        )
        deltas = tuple(point.delta for point in points if point.delta is not None)
        inflight = aggregate_gauge(tuple(point.values.inflight_last for point in points))
        elapsed = sum(item.elapsed_seconds for item in deltas)
        output = sum(item.output_tokens for item in deltas)
        accepted = sum(item.accepted for item in deltas)
        rejected = sum(item.rejected for item in deltas)
        disconnects = sum(item.disconnects for item in deltas)
        errors = sum(item.errors for item in deltas)

        def percentile(name: str, quantile: float) -> float | None:
            if not deltas:
                return None
            buckets = tuple(
                sum(getattr(item, name)[index] for item in deltas)
                for index in range(len(SERVICE_METRIC_BUCKET_BOUNDS))
            )
            return _seconds_to_ms(_service_percentile(buckets, quantile))

        return ServiceMinuteMetric(
            bucket_at=bucket_at,
            service_id=service_id,
            quality=minute_quality(len(points)).value,
            telemetry_status=snapshot.telemetry_status.value,
            ttft_p50_ms=percentile("ttft_buckets", 0.5),
            ttft_p95_ms=percentile("ttft_buckets", 0.95),
            ttft_p99_ms=percentile("ttft_buckets", 0.99),
            tpot_p50_ms=percentile("tpot_buckets", 0.5),
            tpot_p95_ms=percentile("tpot_buckets", 0.95),
            e2e_latency_p50_ms=percentile("e2e_buckets", 0.5),
            e2e_latency_p95_ms=percentile("e2e_buckets", 0.95),
            e2e_latency_p99_ms=percentile("e2e_buckets", 0.99),
            output_tokens_delta=output if deltas else None,
            output_tps_avg=output / elapsed if elapsed > 0 else None,
            output_tps_max=(
                max(item.output_tokens / item.elapsed_seconds for item in deltas)
                if deltas
                else None
            ),
            inflight_avg=inflight.average,
            inflight_max=inflight.maximum,
            inflight_last=inflight.last,
            request_rate_avg=accepted / elapsed if elapsed > 0 else None,
            request_rate_max=(
                max(item.accepted / item.elapsed_seconds for item in deltas) if deltas else None
            ),
            accepted_requests=accepted if deltas else None,
            rejected_requests=rejected if deltas else None,
            client_disconnects=disconnects if deltas else None,
            error_requests=errors if deltas else None,
            error_rate=errors / accepted if accepted > 0 else None,
        )

    def _instance_minute_metric(
        self,
        instance_id: UUID,
        *,
        bucket_at: datetime,
        observed_monotonic: float,
    ) -> InstanceMinuteMetric:
        snapshot = self._store.instance(instance_id, now_monotonic=observed_monotonic)
        bucket_end = bucket_at + timedelta(minutes=1)
        points = tuple(
            point for point in snapshot.series if bucket_at <= point.sampled_at < bucket_end
        )
        baseline = next(
            (point for point in reversed(snapshot.series) if point.sampled_at < bucket_at),
            None,
        )
        running = aggregate_gauge(tuple(point.values.running for point in points))
        waiting = aggregate_gauge(tuple(point.values.waiting for point in points))
        kv_cache = aggregate_gauge(tuple(point.values.kv_cache_usage_ratio for point in points))
        counter_points = ((baseline,) if baseline is not None else ()) + points
        output = aggregate_counter(
            tuple(
                (
                    point.sampled_at,
                    None if point.raw is None else point.raw.output_tokens_total,
                )
                for point in counter_points
            )
        )
        requests = aggregate_counter(
            tuple(
                (point.sampled_at, None if point.raw is None else point.raw.requests_total)
                for point in counter_points
            )
        )
        errors = aggregate_counter(
            tuple(
                (point.sampled_at, None if point.raw is None else point.raw.errors_total)
                for point in counter_points
            )
        )
        previous_raw = None if baseline is None else baseline.raw
        current_raw = None if not points else points[-1].raw

        def percentile(
            buckets_name: str,
            quantile: float,
        ) -> float | None:
            if previous_raw is None or current_raw is None:
                return None
            return histogram_percentile_delta(
                getattr(current_raw, buckets_name),
                getattr(previous_raw, buckets_name),
                quantile,
            )

        error_rate = None
        if requests.delta is not None and requests.delta > 0 and errors.delta is not None:
            error_rate = errors.delta / requests.delta
        return InstanceMinuteMetric(
            bucket_at=bucket_at,
            instance_id=instance_id,
            quality=minute_quality(len(points)).value,
            telemetry_status=snapshot.telemetry_status.value,
            ttft_p50_ms=_seconds_to_ms(percentile("ttft_buckets", 0.5)),
            ttft_p95_ms=_seconds_to_ms(percentile("ttft_buckets", 0.95)),
            ttft_p99_ms=_seconds_to_ms(percentile("ttft_buckets", 0.99)),
            output_tokens_delta=output.delta,
            output_tps_avg=output.rate_average,
            output_tps_max=output.rate_maximum,
            requests_delta=requests.delta,
            running_avg=running.average,
            running_max=running.maximum,
            running_last=running.last,
            waiting_avg=waiting.average,
            waiting_max=waiting.maximum,
            waiting_last=waiting.last,
            queue_time_p95_ms=_seconds_to_ms(percentile("queue_time_buckets", 0.95)),
            kv_cache_avg=kv_cache.average,
            kv_cache_max=kv_cache.maximum,
            kv_cache_last=kv_cache.last,
            errors_delta=errors.delta,
            error_rate=error_rate,
        )

    @staticmethod
    def _gpu_minute_metric(
        snapshot: GpuTelemetrySnapshot,
        *,
        bucket_at: datetime,
    ) -> GpuMinuteMetric:
        bucket_end = bucket_at + timedelta(minutes=1)
        points = tuple(
            point for point in snapshot.series if bucket_at <= point.sampled_at < bucket_end
        )
        utilization = aggregate_gauge(tuple(point.values.utilization_percent for point in points))
        memory = aggregate_gauge(tuple(point.values.memory_used_bytes for point in points))
        temperature = aggregate_gauge(tuple(point.values.temperature_celsius for point in points))
        power = aggregate_gauge(tuple(point.values.power_watts for point in points))
        health = next(
            (point.values.health for point in reversed(points) if point.values.health is not None),
            None,
        )
        return GpuMinuteMetric(
            bucket_at=bucket_at,
            gpu_id=snapshot.uuid,
            quality=minute_quality(len(points)).value,
            telemetry_status=snapshot.telemetry_status.value,
            utilization_avg=utilization.average,
            utilization_max=utilization.maximum,
            utilization_last=utilization.last,
            memory_used_bytes_avg=memory.average,
            memory_used_bytes_max=(None if memory.maximum is None else int(memory.maximum)),
            memory_used_bytes_last=None if memory.last is None else int(memory.last),
            temperature_celsius_avg=temperature.average,
            temperature_celsius_max=temperature.maximum,
            temperature_celsius_last=temperature.last,
            power_watts_avg=power.average,
            power_watts_max=power.maximum,
            power_watts_last=power.last,
            health_last=health,
        )

    async def _run(self) -> None:
        while True:
            cycle_started = time.monotonic()
            try:
                await self.collect_once()
            except Exception as error:
                self._store.fail_overview()
                self._store.fail_gpus()
                _LOGGER.error(
                    "telemetry collection cycle failed",
                    extra={"error_code": type(error).__name__[:128]},
                )
            if self._guard is not None:
                await self._guard.heartbeat()
            target_interval = self._interval_seconds + random.uniform(
                -self._jitter_seconds,
                self._jitter_seconds,
            )
            await asyncio.sleep(max(0.0, target_interval - (time.monotonic() - cycle_started)))

    async def _collect_instance(
        self,
        instance_id: UUID,
        endpoint: str,
        runtime_type: str,
        *,
        sampled_at: datetime,
        observed_monotonic: float,
    ) -> None:
        try:
            metric_mapping = self._metric_mapping_resolver(runtime_type)
            if metric_mapping is None:
                raise ValueError("Runtime metric mapping is unavailable")
            payload = await self._metrics_reader(endpoint)
            self._store.record_instance(
                instance_id,
                payload,
                sampled_at=sampled_at,
                observed_monotonic=observed_monotonic,
                metric_mapping=metric_mapping,
            )
        except Exception:
            self._store.fail_instance(instance_id)

    async def _safe_instances(self) -> tuple[ModelInstanceRecord, ...] | None:
        try:
            return await self._instances.list()
        except Exception:
            return None

    async def _safe_services(self) -> tuple[AiServiceRecord, ...] | None:
        try:
            return await self._ai_services.list()
        except Exception:
            return None

    async def _safe_gpus(self) -> agent_pb2.ListGPUsResponse | None:
        try:
            return await self._gpus.observe_gpus()
        except Exception:
            return None

    async def _safe_service_samples(self) -> tuple[GatewayServiceMetricSample, ...] | None:
        if self._session_factory is None:
            return ()
        try:
            async with asyncio.timeout(RUNTIME_TIMEOUT_SECONDS):
                async with self._session_factory() as session:
                    return await GatewayServiceMetricSampleRepository(session).list_all()
        except Exception:
            return None

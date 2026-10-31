"""Control API assembly and administrator queries for the telemetry collector."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from holm_contracts.agent.v1 import agent_pb2
from holm_persistence import (
    AsyncSessionFactory,
    GpuMinuteMetric,
    InstanceMinuteMetric,
    ServiceMinuteMetric,
    TelemetryMinuteRepository,
)
from holm_runtimes import RuntimeCompatibilityCatalog
from holm_telemetry import (
    COLLECTION_INTERVAL_SECONDS,
    COLLECTION_JITTER_SECONDS,
    LEGACY_VLLM_METRIC_MAPPING,
    CanonicalRuntimeMetric,
    CollectorGuard,
    DerivedRuntimeMetrics,
    GpuMetricValues,
    GpuTelemetryPoint,
    GpuTelemetrySnapshot,
    InstanceTelemetryPoint,
    InstanceTelemetrySnapshot,
    OverviewSnapshot,
    OverviewValues,
    PostgresHeartbeatGuard,
    RuntimeMetricMapping,
    RuntimeMetricSource,
    RuntimeMetricType,
    ServiceMetricValues,
    ServiceTelemetryPoint,
    ServiceTelemetrySnapshot,
    TelemetryCollector,
    TelemetryQuality,
    TelemetryStatus,
    TelemetryStore,
    read_runtime_metrics,
    runtime_metrics_url,
)

from holm_control_api.agent_client import AgentSnapshot, HostAgentClient
from holm_control_api.ai_service_service import AiServiceManagementService
from holm_control_api.model_instance_service import ModelInstanceService


class _ControlGpuSource:
    def __init__(self, agent: HostAgentClient) -> None:
        self._agent = agent

    async def observe_gpus(self) -> agent_pb2.ListGPUsResponse | None:
        observation = await self._agent.observe()
        return observation.gpu_inventory if isinstance(observation, AgentSnapshot) else None


class TelemetryRange(StrEnum):
    ONE_HOUR = "1h"
    SIX_HOURS = "6h"
    TWENTY_FOUR_HOURS = "24h"
    SEVEN_DAYS = "7d"

    @property
    def duration(self) -> timedelta:
        return {
            TelemetryRange.ONE_HOUR: timedelta(hours=1),
            TelemetryRange.SIX_HOURS: timedelta(hours=6),
            TelemetryRange.TWENTY_FOUR_HOURS: timedelta(hours=24),
            TelemetryRange.SEVEN_DAYS: timedelta(days=7),
        }[self]


def runtime_metric_mapping_resolver(
    catalog: RuntimeCompatibilityCatalog,
) -> Callable[[str], RuntimeMetricMapping | None]:
    """Adapt the validated catalog to the collector's narrow mapping contract."""

    mappings: dict[str, RuntimeMetricMapping | None] = {}
    for runtime_name, runtime in catalog.runtimes.items():
        if runtime.metrics is None:
            mappings[runtime_name] = None
            continue
        converted: dict[CanonicalRuntimeMetric, RuntimeMetricSource | None] = {}
        for canonical_name, source in runtime.metrics.items():
            canonical = CanonicalRuntimeMetric(canonical_name)
            converted[canonical] = (
                None
                if source is None
                else RuntimeMetricSource(
                    source=source.source,
                    metric_type=RuntimeMetricType(source.metric_type),
                    labels=source.labels,
                )
            )
        mappings[runtime_name] = MappingProxyType(converted)
    return MappingProxyType(mappings).get


def _legacy_metric_mapping_resolver(_: str) -> RuntimeMetricMapping:
    return LEGACY_VLLM_METRIC_MAPPING


class TelemetryService:
    """Compose the package collector and expose Control API query operations."""

    def __init__(
        self,
        *,
        agent: HostAgentClient,
        instances: ModelInstanceService,
        ai_services: AiServiceManagementService,
        store: TelemetryStore | None = None,
        session_factory: AsyncSessionFactory | None = None,
        build_version: str | None = None,
        heartbeat_stale_after_seconds: float = 30.0,
        guard: CollectorGuard | None = None,
        metrics_reader: Callable[[str], Awaitable[bytes]] = read_runtime_metrics,
        metric_mapping_resolver: Callable[[str], RuntimeMetricMapping | None] | None = None,
        interval_seconds: float = COLLECTION_INTERVAL_SECONDS,
        jitter_seconds: float = COLLECTION_JITTER_SECONDS,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if (session_factory is None) != (build_version is None):
            raise ValueError("telemetry heartbeat assembly requires session factory and version")
        if guard is not None and session_factory is not None:
            raise ValueError("telemetry heartbeat guard must have one assembly source")
        configured_guard = guard
        if session_factory is not None and build_version is not None:
            configured_guard = PostgresHeartbeatGuard(
                session_factory=session_factory,
                build_version=build_version,
                stale_after_seconds=heartbeat_stale_after_seconds,
            )
        self._instances = instances
        self._ai_services = ai_services
        self._session_factory = session_factory
        self._clock = clock
        configured_mapping_resolver = metric_mapping_resolver
        if configured_mapping_resolver is None:
            configured_mapping_resolver = _legacy_metric_mapping_resolver
        self._collector = TelemetryCollector(
            gpus=_ControlGpuSource(agent),
            instances=instances,
            ai_services=ai_services,
            store=store,
            guard=configured_guard,
            metrics_reader=metrics_reader,
            metric_mapping_resolver=configured_mapping_resolver,
            session_factory=session_factory,
            interval_seconds=interval_seconds,
            jitter_seconds=jitter_seconds,
        )

    async def start(self) -> None:
        await self._collector.start()

    @property
    def store(self) -> TelemetryStore:
        return self._collector.store

    async def stop(self) -> None:
        await self._collector.stop()

    async def collect_once(self) -> None:
        await self._collector.collect_once()

    async def instance(
        self,
        instance_id: UUID,
        query_range: TelemetryRange = TelemetryRange.ONE_HOUR,
    ) -> InstanceTelemetrySnapshot:
        await self._instances.get(instance_id)
        if query_range is TelemetryRange.ONE_HOUR:
            return self._collector.store.instance(instance_id, now_monotonic=time.monotonic())
        records = await self._instance_records(instance_id, query_range)
        points = tuple(self._instance_point(record) for record in records)
        if not points:
            return InstanceTelemetrySnapshot(
                sampled_at=None,
                telemetry_status=TelemetryStatus.UNAVAILABLE,
                current=DerivedRuntimeMetrics(None, None, None, None, None, None, None, None, None),
                series=(),
                quality=TelemetryQuality.PARTIAL,
            )
        latest = points[-1]
        return InstanceTelemetrySnapshot(
            sampled_at=latest.sampled_at,
            telemetry_status=latest.telemetry_status,
            current=latest.values,
            series=points,
            quality=latest.quality,
        )

    async def gpus(
        self,
        query_range: TelemetryRange = TelemetryRange.ONE_HOUR,
    ) -> tuple[GpuTelemetrySnapshot, ...]:
        if query_range is TelemetryRange.ONE_HOUR:
            return self._collector.store.gpus(now_monotonic=time.monotonic())
        records = await self._gpu_records(query_range)
        by_gpu: dict[str, list[GpuTelemetryPoint]] = {}
        for record in records:
            by_gpu.setdefault(record.gpu_id, []).append(self._gpu_point(record))
        return tuple(
            GpuTelemetrySnapshot(
                uuid=gpu_id,
                sampled_at=points[-1].sampled_at,
                telemetry_status=points[-1].telemetry_status,
                current=points[-1].values,
                series=tuple(points),
                quality=points[-1].quality,
            )
            for gpu_id, points in sorted(by_gpu.items())
        )

    async def service(
        self,
        service_id: UUID,
        query_range: TelemetryRange = TelemetryRange.ONE_HOUR,
    ) -> ServiceTelemetrySnapshot:
        await self._ai_services.get(service_id)
        if query_range is TelemetryRange.ONE_HOUR:
            return self._collector.store.service(service_id, now_monotonic=time.monotonic())
        records = await self._service_records(service_id, query_range)
        points = tuple(self._service_point(record) for record in records)
        if not points:
            return ServiceTelemetrySnapshot(
                service_id=service_id,
                sampled_at=None,
                telemetry_status=TelemetryStatus.UNAVAILABLE,
                current=ServiceMetricValues(),
                series=(),
            )
        latest = points[-1]
        return ServiceTelemetrySnapshot(
            service_id=service_id,
            sampled_at=latest.sampled_at,
            telemetry_status=latest.telemetry_status,
            current=latest.values,
            series=points,
            quality=latest.quality,
        )

    def overview(self) -> OverviewSnapshot:
        return self._collector.store.overview(now_monotonic=time.monotonic())

    def _require_session_factory(self) -> AsyncSessionFactory:
        if self._session_factory is None:
            raise RuntimeError("minute telemetry storage is unavailable")
        return self._session_factory

    async def _instance_records(
        self, instance_id: UUID, query_range: TelemetryRange
    ) -> tuple[InstanceMinuteMetric, ...]:
        async with self._require_session_factory()() as session:
            return await TelemetryMinuteRepository(session).list_instances(
                instance_id,
                since=self._clock() - query_range.duration,
            )

    async def _service_records(
        self, service_id: UUID, query_range: TelemetryRange
    ) -> tuple[ServiceMinuteMetric, ...]:
        async with self._require_session_factory()() as session:
            return await TelemetryMinuteRepository(session).list_services(
                service_id,
                since=self._clock() - query_range.duration,
            )

    async def _gpu_records(self, query_range: TelemetryRange) -> tuple[GpuMinuteMetric, ...]:
        async with self._require_session_factory()() as session:
            return await TelemetryMinuteRepository(session).list_gpus(
                since=self._clock() - query_range.duration
            )

    @staticmethod
    def _instance_point(record: InstanceMinuteMetric) -> InstanceTelemetryPoint:
        return InstanceTelemetryPoint(
            sampled_at=record.bucket_at,
            values=DerivedRuntimeMetrics(
                running=record.running_last,
                waiting=record.waiting_last,
                kv_cache_usage_ratio=record.kv_cache_last,
                output_tps=record.output_tps_avg,
                ttft_p50_seconds=_ms_to_seconds(record.ttft_p50_ms),
                ttft_p95_seconds=_ms_to_seconds(record.ttft_p95_ms),
                ttft_p99_seconds=_ms_to_seconds(record.ttft_p99_ms),
                queue_time_p50_seconds=None,
                queue_time_p95_seconds=_ms_to_seconds(record.queue_time_p95_ms),
            ),
            quality=TelemetryQuality(record.quality),
            telemetry_status=TelemetryStatus(record.telemetry_status),
        )

    @staticmethod
    def _gpu_point(record: GpuMinuteMetric) -> GpuTelemetryPoint:
        return GpuTelemetryPoint(
            sampled_at=record.bucket_at,
            values=GpuMetricValues(
                utilization_percent=record.utilization_avg,
                memory_used_bytes=(
                    None
                    if record.memory_used_bytes_avg is None
                    else int(record.memory_used_bytes_avg)
                ),
                temperature_celsius=record.temperature_celsius_avg,
                power_watts=record.power_watts_avg,
                health=record.health_last,
            ),
            quality=TelemetryQuality(record.quality),
            telemetry_status=TelemetryStatus(record.telemetry_status),
        )

    @staticmethod
    def _service_point(record: ServiceMinuteMetric) -> ServiceTelemetryPoint:
        return ServiceTelemetryPoint(
            sampled_at=record.bucket_at,
            values=ServiceMetricValues(
                **{name: getattr(record, name) for name in ServiceMetricValues.__dataclass_fields__}
            ),
            quality=TelemetryQuality(record.quality),
            telemetry_status=TelemetryStatus(record.telemetry_status),
        )


def _ms_to_seconds(value: float | None) -> float | None:
    return None if value is None else value / 1_000


_runtime_metrics_url = runtime_metrics_url

__all__ = [
    "COLLECTION_INTERVAL_SECONDS",
    "GpuMetricValues",
    "GpuTelemetryPoint",
    "GpuTelemetrySnapshot",
    "InstanceTelemetryPoint",
    "InstanceTelemetrySnapshot",
    "OverviewSnapshot",
    "OverviewValues",
    "TelemetryService",
    "TelemetryStore",
    "TelemetryRange",
    "runtime_metric_mapping_resolver",
]

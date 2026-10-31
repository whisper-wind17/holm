"""Administrator-only telemetry query API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Self
from uuid import UUID

from fastapi import APIRouter, Query
from holm_domain import Capability
from holm_telemetry import (
    CanonicalRuntimeMetric,
    DerivedRuntimeMetrics,
    GpuMetricValues,
    GpuTelemetryPoint,
    GpuTelemetrySnapshot,
    InstanceTelemetryPoint,
    InstanceTelemetrySnapshot,
    OverviewSnapshot,
    OverviewValues,
    ServiceMetricValues,
    ServiceTelemetryPoint,
    ServiceTelemetrySnapshot,
    TelemetryQuality,
    TelemetryStatus,
)
from holm_telemetry.collector import ServiceRequestWindow
from pydantic import BaseModel, ConfigDict

from holm_control_api.authorization import capability_route
from holm_control_api.telemetry_service import TelemetryRange, TelemetryService

TELEMETRY_PATH: Final = "/api/v1/telemetry"


class _ClosedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InstanceMetricValuesResponse(_ClosedResponse):
    running: float | None
    waiting: float | None
    kv_cache_usage_ratio: float | None
    output_tps: float | None
    ttft_p50_seconds: float | None
    ttft_p95_seconds: float | None
    ttft_p99_seconds: float | None
    queue_time_p50_seconds: float | None
    queue_time_p95_seconds: float | None
    unsupported: tuple[CanonicalRuntimeMetric, ...]

    @classmethod
    def from_value(cls, value: DerivedRuntimeMetrics) -> Self:
        return cls(
            running=value.running,
            waiting=value.waiting,
            kv_cache_usage_ratio=value.kv_cache_usage_ratio,
            output_tps=value.output_tps,
            ttft_p50_seconds=value.ttft_p50_seconds,
            ttft_p95_seconds=value.ttft_p95_seconds,
            ttft_p99_seconds=value.ttft_p99_seconds,
            queue_time_p50_seconds=value.queue_time_p50_seconds,
            queue_time_p95_seconds=value.queue_time_p95_seconds,
            unsupported=value.unsupported or (),
        )


class InstancePointResponse(InstanceMetricValuesResponse):
    sampled_at: datetime
    quality: TelemetryQuality
    telemetry_status: TelemetryStatus

    @classmethod
    def from_point(cls, point: InstanceTelemetryPoint) -> Self:
        return cls(
            sampled_at=point.sampled_at,
            quality=point.quality,
            telemetry_status=point.telemetry_status,
            running=point.values.running,
            waiting=point.values.waiting,
            kv_cache_usage_ratio=point.values.kv_cache_usage_ratio,
            output_tps=point.values.output_tps,
            ttft_p50_seconds=point.values.ttft_p50_seconds,
            ttft_p95_seconds=point.values.ttft_p95_seconds,
            ttft_p99_seconds=point.values.ttft_p99_seconds,
            queue_time_p50_seconds=point.values.queue_time_p50_seconds,
            queue_time_p95_seconds=point.values.queue_time_p95_seconds,
            unsupported=point.values.unsupported or (),
        )


class ServiceRequestWindowResponse(_ClosedResponse):
    requests: int | None
    error_rate: float | None
    ttft_p95_ms: float | None
    started_at: datetime | None

    @classmethod
    def from_value(cls, value: ServiceRequestWindow) -> Self:
        return cls(**{name: getattr(value, name) for name in cls.model_fields})


class InstanceTelemetryResponse(_ClosedResponse):
    instance_id: UUID
    sampled_at: datetime | None
    telemetry_status: TelemetryStatus
    quality: TelemetryQuality
    current: InstanceMetricValuesResponse
    series: tuple[InstancePointResponse, ...]
    window_1h: ServiceRequestWindowResponse

    @classmethod
    def from_snapshot(cls, instance_id: UUID, value: InstanceTelemetrySnapshot) -> Self:
        return cls(
            window_1h=ServiceRequestWindowResponse.from_value(value.window_1h),
            instance_id=instance_id,
            sampled_at=value.sampled_at,
            telemetry_status=value.telemetry_status,
            quality=value.quality,
            current=InstanceMetricValuesResponse.from_value(value.current),
            series=tuple(InstancePointResponse.from_point(point) for point in value.series),
        )


class GpuMetricValuesResponse(_ClosedResponse):
    utilization_percent: float | None
    memory_used_bytes: int | None
    temperature_celsius: float | None
    power_watts: float | None
    health: str | None

    @classmethod
    def from_value(cls, value: GpuMetricValues) -> Self:
        return cls(**{name: getattr(value, name) for name in cls.model_fields})


class GpuPointResponse(GpuMetricValuesResponse):
    sampled_at: datetime
    quality: TelemetryQuality
    telemetry_status: TelemetryStatus

    @classmethod
    def from_point(cls, point: GpuTelemetryPoint) -> Self:
        return cls(
            sampled_at=point.sampled_at,
            quality=point.quality,
            telemetry_status=point.telemetry_status,
            **{name: getattr(point.values, name) for name in GpuMetricValuesResponse.model_fields},
        )


class GpuTelemetryResponse(_ClosedResponse):
    uuid: str
    sampled_at: datetime | None
    telemetry_status: TelemetryStatus
    quality: TelemetryQuality
    current: GpuMetricValuesResponse
    series: tuple[GpuPointResponse, ...]

    @classmethod
    def from_snapshot(cls, value: GpuTelemetrySnapshot) -> Self:
        return cls(
            uuid=value.uuid,
            sampled_at=value.sampled_at,
            telemetry_status=value.telemetry_status,
            quality=value.quality,
            current=GpuMetricValuesResponse.from_value(value.current),
            series=tuple(GpuPointResponse.from_point(point) for point in value.series),
        )


class GpuTelemetryListResponse(_ClosedResponse):
    sampled_at: datetime | None
    telemetry_status: TelemetryStatus
    quality: TelemetryQuality
    items: tuple[GpuTelemetryResponse, ...]


class ServiceMetricValuesResponse(_ClosedResponse):
    ttft_p50_ms: float | None
    ttft_p95_ms: float | None
    ttft_p99_ms: float | None
    tpot_p50_ms: float | None
    tpot_p95_ms: float | None
    e2e_latency_p50_ms: float | None
    e2e_latency_p95_ms: float | None
    e2e_latency_p99_ms: float | None
    output_tps_avg: float | None
    output_tps_max: float | None
    inflight_avg: float | None
    inflight_max: float | None
    inflight_last: float | None
    request_rate_avg: float | None
    accepted_requests: int | None
    rejected_requests: int | None
    client_disconnects: int | None
    error_requests: int | None
    error_rate: float | None

    @classmethod
    def from_value(cls, value: ServiceMetricValues) -> Self:
        return cls(**{name: getattr(value, name) for name in cls.model_fields})


class ServicePointResponse(ServiceMetricValuesResponse):
    sampled_at: datetime
    quality: TelemetryQuality
    telemetry_status: TelemetryStatus

    @classmethod
    def from_point(cls, point: ServiceTelemetryPoint) -> Self:
        return cls(
            sampled_at=point.sampled_at,
            quality=point.quality,
            telemetry_status=point.telemetry_status,
            **{
                name: getattr(point.values, name)
                for name in ServiceMetricValuesResponse.model_fields
            },
        )


class ServiceTelemetryResponse(_ClosedResponse):
    service_id: UUID
    sampled_at: datetime | None
    telemetry_status: TelemetryStatus
    quality: TelemetryQuality
    current: ServiceMetricValuesResponse
    series: tuple[ServicePointResponse, ...]
    window_1h: ServiceRequestWindowResponse

    @classmethod
    def from_snapshot(cls, value: ServiceTelemetrySnapshot) -> Self:
        return cls(
            window_1h=ServiceRequestWindowResponse.from_value(value.window_1h),
            service_id=value.service_id,
            sampled_at=value.sampled_at,
            telemetry_status=value.telemetry_status,
            quality=value.quality,
            current=ServiceMetricValuesResponse.from_value(value.current),
            series=tuple(ServicePointResponse.from_point(point) for point in value.series),
        )


class OverviewValuesResponse(_ClosedResponse):
    gpu_allocated: int | None
    gpu_available: int | None
    gpu_total: int | None
    requests_running: float | None
    requests_waiting: float | None
    services_available: int | None
    services_starting: int | None
    services_unavailable: int | None
    services_total: int | None
    ttft_p95_ms: float | None
    error_rate: float | None
    requests_1h: int | None
    error_rate_1h: float | None
    window_started_at: datetime | None

    @classmethod
    def from_value(cls, value: OverviewValues) -> Self:
        return cls(**{name: getattr(value, name) for name in cls.model_fields})


class OverviewTelemetryResponse(_ClosedResponse):
    sampled_at: datetime | None
    telemetry_status: TelemetryStatus
    current: OverviewValuesResponse

    @classmethod
    def from_snapshot(cls, value: OverviewSnapshot) -> Self:
        return cls(
            sampled_at=value.sampled_at,
            telemetry_status=value.telemetry_status,
            current=OverviewValuesResponse.from_value(value.values),
        )


def create_telemetry_router(service: TelemetryService) -> APIRouter:
    router = APIRouter(prefix=TELEMETRY_PATH, tags=["telemetry"])

    @router.get("/instances/{instance_id}", response_model=InstanceTelemetryResponse)
    @capability_route(Capability.VIEW_INFRASTRUCTURE)
    async def read_instance(
        instance_id: UUID,
        query_range: Annotated[TelemetryRange, Query(alias="range")] = TelemetryRange.ONE_HOUR,
    ) -> InstanceTelemetryResponse:
        response = InstanceTelemetryResponse.from_snapshot(
            instance_id, await service.instance(instance_id, query_range)
        )
        if query_range is not TelemetryRange.ONE_HOUR:
            recent = await service.instance(instance_id, TelemetryRange.ONE_HOUR)
            response = response.model_copy(
                update={"window_1h": ServiceRequestWindowResponse.from_value(recent.window_1h)}
            )
        return response

    @router.get("/gpus", response_model=GpuTelemetryListResponse)
    @capability_route(Capability.VIEW_INFRASTRUCTURE)
    async def read_gpus(
        query_range: Annotated[TelemetryRange, Query(alias="range")] = TelemetryRange.ONE_HOUR,
    ) -> GpuTelemetryListResponse:
        snapshots = await service.gpus(query_range)
        items = tuple(GpuTelemetryResponse.from_snapshot(item) for item in snapshots)
        sampled = tuple(item.sampled_at for item in items if item.sampled_at is not None)
        statuses = {item.telemetry_status for item in items}
        status = (
            TelemetryStatus.UNAVAILABLE
            if not items or TelemetryStatus.UNAVAILABLE in statuses
            else TelemetryStatus.DELAYED
            if TelemetryStatus.DELAYED in statuses
            else TelemetryStatus.AVAILABLE
        )
        return GpuTelemetryListResponse(
            sampled_at=max(sampled) if sampled else None,
            telemetry_status=status,
            quality=(
                TelemetryQuality.PARTIAL
                if not items or TelemetryQuality.PARTIAL in {item.quality for item in items}
                else TelemetryQuality.FULL
            ),
            items=items,
        )

    @router.get("/services/{service_id}", response_model=ServiceTelemetryResponse)
    @capability_route(Capability.VIEW_INFRASTRUCTURE)
    async def read_service(
        service_id: UUID,
        query_range: Annotated[TelemetryRange, Query(alias="range")] = TelemetryRange.ONE_HOUR,
    ) -> ServiceTelemetryResponse:
        snapshot = await service.service(service_id, query_range)
        response = ServiceTelemetryResponse.from_snapshot(snapshot)
        if query_range is not TelemetryRange.ONE_HOUR:
            # The summary always uses the ring buffer, independent of chart range.
            recent = await service.service(service_id, TelemetryRange.ONE_HOUR)
            response = response.model_copy(
                update={"window_1h": ServiceRequestWindowResponse.from_value(recent.window_1h)}
            )
        return response

    @router.get("/overview", response_model=OverviewTelemetryResponse)
    @capability_route(Capability.VIEW_INFRASTRUCTURE)
    async def read_overview() -> OverviewTelemetryResponse:
        return OverviewTelemetryResponse.from_snapshot(service.overview())

    return router


__all__ = [
    "TELEMETRY_PATH",
    "GpuTelemetryListResponse",
    "InstanceTelemetryResponse",
    "OverviewTelemetryResponse",
    "ServiceTelemetryResponse",
    "create_telemetry_router",
]

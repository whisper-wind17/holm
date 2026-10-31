"""M6-07 / TC-TM-12: Fake NVML observations through the Collector and Store."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from uuid import UUID

from fixtures.fake_nvml import FakeNvml, FixtureId, scenario_for
from holm_contracts.agent.v1 import agent_pb2
from holm_domain import ModelInstanceStatus, RuntimeType
from holm_host_agent.gpu_discovery import (
    NvmlCallError,
    NvmlErrorKind,
    discover_gpus,
    inventory_to_proto,
)
from holm_persistence import AiServiceRecord, ModelInstanceRecord
from holm_telemetry import LEGACY_VLLM_METRIC_MAPPING, TelemetryCollector, TelemetryStatus

INSTANCE_ID = UUID("00000000-0000-7000-8000-000000006071")
ARTIFACT_ID = UUID("00000000-0000-7000-8000-000000006072")


class _GpuSource:
    def __init__(self, fake: FakeNvml) -> None:
        self.fake = fake

    async def observe_gpus(self) -> agent_pb2.ListGPUsResponse:
        return inventory_to_proto(discover_gpus(self.fake))


class _Instances:
    def __init__(self, records: tuple[ModelInstanceRecord, ...] = ()) -> None:
        self.records = records

    async def list(self) -> tuple[ModelInstanceRecord, ...]:
        return self.records


class _Services:
    async def list(self) -> tuple[AiServiceRecord, ...]:
        return ()


def _collector(fake: FakeNvml, instances: _Instances | None = None) -> TelemetryCollector:
    async def read_metrics(_endpoint: str) -> bytes:
        return b"vllm:num_requests_running 1\nvllm:num_requests_waiting 0\n"

    return TelemetryCollector(
        gpus=_GpuSource(fake),
        instances=instances or _Instances(),
        ai_services=_Services(),
        metrics_reader=read_metrics,
        metric_mapping_resolver=lambda _runtime_type: LEGACY_VLLM_METRIC_MAPPING,
        interval_seconds=30,
        jitter_seconds=0,
    )


def _running_instance(gpu_uuid: str) -> ModelInstanceRecord:
    now = datetime(2026, 9, 23, tzinfo=UTC)
    return ModelInstanceRecord(
        name="FX-M3-10B instance",
        id=INSTANCE_ID,
        artifact_id=ARTIFACT_ID,
        runtime_type=RuntimeType.VLLM,
        expected_gpu_uuids=(gpu_uuid,),
        tensor_parallel_size=1,
        context_length=4096,
        gpu_memory_utilization=0.9,
        internal_model_name="mi_m607",
        status=ModelInstanceStatus.RUNNING,
        error_code=None,
        internal_endpoint="127.0.0.1:8607",
        config_version=1,
        created_at=now,
        updated_at=now,
    )


def test_M6_07_TC_TM_12_FX_NV_06_disappeared_gpu_is_not_fresh_and_current_clears() -> None:
    scenario = scenario_for(FixtureId.FX_NV_06)
    fake = FakeNvml(scenario.snapshots)
    transition = scenario.instance_transition
    assert transition is not None
    missing_uuid = transition.allocation_gpu_uuids_before[0]
    instances = _Instances((_running_instance(missing_uuid),))
    collector = _collector(fake, instances)

    async def run() -> None:
        await collector.collect_once()
        before = {item.uuid: item for item in collector.store.gpus(now_monotonic=time.monotonic())}
        assert before[missing_uuid].telemetry_status is TelemetryStatus.AVAILABLE
        assert before[missing_uuid].current.utilization_percent is not None
        fake.advance()
        await collector.collect_once()

    asyncio.run(run())
    after = {item.uuid: item for item in collector.store.gpus(now_monotonic=time.monotonic())}
    assert after[missing_uuid].telemetry_status is not TelemetryStatus.AVAILABLE
    assert len(after[missing_uuid].series) == 1
    assert instances.records[0].status is ModelInstanceStatus.RUNNING
    if after[missing_uuid].telemetry_status is TelemetryStatus.UNAVAILABLE:
        assert after[missing_uuid].current.utilization_percent is None
        assert after[missing_uuid].current.memory_used_bytes is None
        assert after[missing_uuid].current.health is None


class _TemperatureFailureFake(FakeNvml):
    def device_get_temperature_celsius(self, handle: object) -> float:
        self.device_get_uuid(handle)
        raise NvmlCallError(NvmlErrorKind.OTHER)


def test_M6_07_TC_TM_12_nvml_health_passes_through_and_missing_reading_stays_none() -> None:
    scenario = scenario_for(FixtureId.FX_NV_07)
    fake = _TemperatureFailureFake(scenario.snapshots)
    collector = _collector(fake)
    asyncio.run(collector.collect_once())

    snapshots = {item.uuid: item for item in collector.store.gpus(now_monotonic=time.monotonic())}
    for device in fake.current_snapshot.devices:
        current = snapshots[device.uuid].current
        assert current.health == device.health.value.lower()
        assert current.temperature_celsius is None
        assert current.utilization_percent == device.utilization_percent


def test_M6_07_TC_TM_12_FX_NV_08_index_drift_keeps_one_series_per_uuid() -> None:
    scenario = scenario_for(FixtureId.FX_NV_08)
    fake = FakeNvml(scenario.snapshots)
    collector = _collector(fake)
    before_indices = {device.uuid: device.display_index for device in fake.current_snapshot.devices}

    async def run() -> None:
        await collector.collect_once()
        fake.advance()
        await collector.collect_once()

    asyncio.run(run())
    after_indices = {device.uuid: device.display_index for device in fake.current_snapshot.devices}
    snapshots = {item.uuid: item for item in collector.store.gpus(now_monotonic=time.monotonic())}
    assert before_indices.keys() == after_indices.keys() == snapshots.keys()
    assert all(before_indices[uuid] != after_indices[uuid] for uuid in snapshots)
    assert all(len(item.series) == 2 for item in snapshots.values())
    assert len(collector.store.gpus(now_monotonic=time.monotonic())) == len(before_indices)


def test_M6_07_TC_TM_12_gpu_and_runtime_points_share_sampled_at_without_health_change() -> None:
    scenario = scenario_for(FixtureId.FX_NV_02)
    fake = FakeNvml(scenario.snapshots)
    instance = _running_instance(fake.current_snapshot.devices[0].uuid)
    instances = _Instances((instance,))
    collector = _collector(fake, instances)

    async def run() -> None:
        await collector.collect_once()

    asyncio.run(run())
    gpu = collector.store.gpus(now_monotonic=time.monotonic())[0]
    runtime = collector.store.instance(INSTANCE_ID, now_monotonic=time.monotonic())
    assert gpu.series[0].sampled_at == runtime.series[0].sampled_at
    assert len(runtime.series) == 1
    assert instances.records[0].status is ModelInstanceStatus.RUNNING

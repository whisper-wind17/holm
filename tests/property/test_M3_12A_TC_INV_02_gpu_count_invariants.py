"""M3-12A / TC-INV-02 deterministic N=1..8 GPU-count properties."""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final
from uuid import UUID

import pytest
import sqlalchemy as sa
from fixtures.fake_nvml import FakeNvml, FixtureId, scenario_for
from holm_contracts.agent.v1 import agent_pb2
from holm_control_api.agent_client import AgentSnapshot
from holm_control_api.model_instance_service import (
    CreateModelInstance,
    ModelInstanceService,
)
from holm_domain import (
    ArtifactStorageType,
    GpuAllocationStatus,
    MetadataStatus,
    ModelArtifactStatus,
    ModelInstanceStatus,
    ModelOriginType,
    RuntimeType,
)
from holm_host_agent.runtime_process import RuntimeLaunchSpec
from holm_host_agent.runtime_process import RuntimeObservation as HostRuntimeObservation
from holm_host_agent.runtime_process import RuntimeStateValue as HostRuntimeState
from holm_host_agent.server import HostAgentService
from holm_instance_controller import InstanceController
from holm_instance_controller.store import PostgresControllerStore
from holm_persistence import (
    AsyncSessionFactory,
    create_database_engine,
    create_session_factory,
)
from holm_persistence.model_instances import GPU_ALLOCATIONS
from holm_runtimes import load_runtime_compatibility
from integration import conftest as integration_fixtures
from integration.migration_test_support import upgrade_head
from sqlalchemy.engine import URL, Engine

ephemeral_postgres = integration_fixtures.ephemeral_postgres

ROOT: Final[Path] = Path(__file__).resolve().parents[2]
MODEL_ID: Final[UUID] = UUID("41000000-0000-4000-8000-00000000312a")
ARTIFACT_ID: Final[UUID] = UUID("42000000-0000-4000-8000-00000000312a")
GPU_ORDER_SEED: Final[int] = 0x312A
ACTIVE_STATUSES: Final[tuple[ModelInstanceStatus, ...]] = (
    ModelInstanceStatus.STARTING,
    ModelInstanceStatus.RUNNING,
    ModelInstanceStatus.RESTARTING,
)


@dataclass(frozen=True, slots=True)
class _InvariantMeasurement:
    status: ModelInstanceStatus
    allocation_gpu_uuids: tuple[str, ...]
    launch_spec: RuntimeLaunchSpec
    active_allocations_per_gpu: tuple[int, ...]


class _NvmlBackedAgent:
    """Expose one FX-NV-04 inventory through the production Host Agent mapping."""

    def __init__(self, fake_nvml: FakeNvml) -> None:
        self._service = HostAgentService(nvml_factory=lambda: fake_nvml)

    async def observe(self) -> AgentSnapshot:
        inventory = await self._service.ListGPUs(agent_pb2.ListGPUsRequest(), object())
        return AgentSnapshot(
            host_info=agent_pb2.GetHostInfoResponse(),
            gpu_inventory=inventory,
        )


class _CapturingRuntimeManager:
    """Capture the production RuntimeLaunchSpec without starting a Runtime."""

    def __init__(self) -> None:
        self.specs: list[RuntimeLaunchSpec] = []

    async def ensure(self, spec: RuntimeLaunchSpec) -> HostRuntimeObservation:
        self.specs.append(spec)
        return HostRuntimeObservation(HostRuntimeState.RUNNING, endpoint="127.0.0.1:8312")


def _fake_nvml_for_count(gpu_count: int) -> tuple[FakeNvml, tuple[str, ...]]:
    source = scenario_for(FixtureId.FX_NV_04).snapshots[0]
    devices = list(source.devices)
    random.Random(GPU_ORDER_SEED + gpu_count).shuffle(devices)
    selected = tuple(devices[:gpu_count])
    selected_uuids = {device.uuid for device in selected}
    links = tuple(
        link
        for link in source.topology_links
        if link.gpu_a_uuid in selected_uuids and link.gpu_b_uuid in selected_uuids
    )
    snapshot = replace(source, devices=selected, topology_links=links)
    return FakeNvml((snapshot,)), tuple(device.uuid for device in selected)


def _async_url(fixture: integration_fixtures.EphemeralPostgres) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username=integration_fixtures.CLUSTER_USER,
        host=str(fixture.socket_directory),
        port=fixture.port,
        database="postgres",
    )


def _reset_and_seed_database(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(sa.text("TRUNCATE core.models CASCADE"))
        connection.execute(
            sa.text(
                "INSERT INTO core.models "
                "(id, source_type, source_key, display_name, external_path, metadata_status, "
                "created_at, updated_at) VALUES "
                "(:id, :source_type, :source_key, :display_name, :path, :metadata_status, "
                "clock_timestamp(), clock_timestamp())"
            ),
            {
                "id": MODEL_ID,
                "source_type": ModelOriginType.LOCAL_PATH.value,
                "source_key": "/models/m3-12a",
                "display_name": "M3-12A Model",
                "path": "/models/m3-12a",
                "metadata_status": MetadataStatus.CONFIRMED.value,
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO core.model_artifacts "
                "(id, model_id, source_type, repository_id, resolved_revision, storage_type, "
                "status, path, manifest_hash, size_bytes, created_at, updated_at) VALUES "
                "(:id, :model_id, :source_type, :repository_id, :revision, :storage_type, "
                ":status, :path, :manifest_hash, 1024, clock_timestamp(), clock_timestamp())"
            ),
            {
                "id": ARTIFACT_ID,
                "model_id": MODEL_ID,
                "source_type": ModelOriginType.LOCAL_PATH.value,
                "repository_id": "m3-12a-property",
                "revision": "a" * 40,
                "storage_type": ArtifactStorageType.EXTERNAL.value,
                "status": ModelArtifactStatus.READY.value,
                "path": "/models/m3-12a",
                "manifest_hash": "b" * 64,
            },
        )


async def _host_launch_spec(request: object) -> RuntimeLaunchSpec:
    capture = _CapturingRuntimeManager()
    service = HostAgentService(runtime_manager=capture)  # type: ignore[arg-type]
    response = await service.EnsureRuntime(
        agent_pb2.EnsureRuntimeRequest(
            instance_id=request.instance_id,
            runtime_name=request.runtime_name,
            model_path=request.model_path,
            gpu_uuids=request.gpu_uuids,
            tensor_parallel_size=request.tensor_parallel_size,
            context_length=request.context_length,
            gpu_memory_utilization=request.gpu_memory_utilization,
            served_model_name=request.served_model_name,
            config_version=request.config_version,
        ),
        object(),
    )
    assert response.status == agent_pb2.RUNTIME_PROCESS_STATE_RUNNING
    assert len(capture.specs) == 1
    return capture.specs[0]


async def _measure(
    session_factory: AsyncSessionFactory,
    controller: InstanceController,
    instance_id: UUID,
    status: ModelInstanceStatus,
) -> _InvariantMeasurement:
    item = await PostgresControllerStore(session_factory).claim(status)
    assert item is not None
    assert item.instance.id == instance_id
    request = controller._launch_request(item)
    assert request is not None
    launch_spec = await _host_launch_spec(request)

    async with session_factory() as session:
        allocation_gpu_uuids = tuple(
            (
                await session.execute(
                    sa.select(GPU_ALLOCATIONS.c.gpu_uuid)
                    .where(
                        GPU_ALLOCATIONS.c.instance_id == instance_id,
                        GPU_ALLOCATIONS.c.status != GpuAllocationStatus.RELEASED.value,
                    )
                    .order_by(GPU_ALLOCATIONS.c.gpu_uuid)
                )
            ).scalars()
        )
        active_allocations_per_gpu = tuple(
            int(count)
            for count in (
                await session.execute(
                    sa.select(sa.func.count())
                    .select_from(GPU_ALLOCATIONS)
                    .where(GPU_ALLOCATIONS.c.status != GpuAllocationStatus.RELEASED.value)
                    .group_by(GPU_ALLOCATIONS.c.gpu_uuid)
                    .order_by(GPU_ALLOCATIONS.c.gpu_uuid)
                )
            ).scalars()
        )
    return _InvariantMeasurement(
        status=status,
        allocation_gpu_uuids=allocation_gpu_uuids,
        launch_spec=launch_spec,
        active_allocations_per_gpu=active_allocations_per_gpu,
    )


async def _exercise_invariant(
    fixture: integration_fixtures.EphemeralPostgres,
    gpu_count: int,
) -> tuple[_InvariantMeasurement, ...]:
    fake_nvml, requested_gpu_uuids = _fake_nvml_for_count(gpu_count)
    engine = create_database_engine(_async_url(fixture), connect_timeout_seconds=5)
    try:
        session_factory = create_session_factory(engine)
        service = ModelInstanceService(
            session_factory=session_factory,
            agent=_NvmlBackedAgent(fake_nvml),
        )
        instance = await service.create(
            CreateModelInstance(
                artifact_id=ARTIFACT_ID,
                runtime_type=RuntimeType.VLLM,
                gpu_uuids=requested_gpu_uuids,
                tensor_parallel_size=gpu_count,
                context_length=4096,
                gpu_memory_utilization=0.9,
            )
        )
        runtime = load_runtime_compatibility(ROOT / "runtime-compat.yaml").runtime("vllm")
        assert runtime is not None
        store = PostgresControllerStore(session_factory)
        controller = InstanceController(
            store=store,
            agent=object(),  # type: ignore[arg-type]
            health=object(),  # type: ignore[arg-type]
            runtime=runtime,
        )

        measurements = [
            await _measure(
                session_factory,
                controller,
                instance.id,
                ModelInstanceStatus.STARTING,
            )
        ]
        assert await store.mark_running(instance.id)
        measurements.append(
            await _measure(
                session_factory,
                controller,
                instance.id,
                ModelInstanceStatus.RUNNING,
            )
        )
        assert await store.mark_unhealthy(
            instance.id,
            expected_status=ModelInstanceStatus.RUNNING,
            error_code="m3_12a_injected_unhealthy",
        )
        assert await store.begin_restart(instance.id, restart_window_seconds=600)
        measurements.append(
            await _measure(
                session_factory,
                controller,
                instance.id,
                ModelInstanceStatus.RESTARTING,
            )
        )
        return tuple(measurements)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("gpu_count", tuple(range(1, 9)), ids=lambda count: f"N-{count}")
def test_M3_12A_TC_INV_02_allocation_launch_spec_and_tp_match_for_N_1_through_8(
    ephemeral_postgres: integration_fixtures.EphemeralPostgres,
    gpu_count: int,
) -> None:
    upgrade_head(ephemeral_postgres.engine)
    _reset_and_seed_database(ephemeral_postgres.engine)
    try:
        measurements = asyncio.run(_exercise_invariant(ephemeral_postgres, gpu_count))
    finally:
        with ephemeral_postgres.engine.begin() as connection:
            connection.execute(sa.text("TRUNCATE core.models CASCADE"))

    assert tuple(measurement.status for measurement in measurements) == ACTIVE_STATUSES
    for measurement in measurements:
        allocation_count = len(measurement.allocation_gpu_uuids)
        launch_spec_gpu_count = len(measurement.launch_spec.gpu_uuids)
        launch_spec_tp = measurement.launch_spec.tensor_parallel_size

        assert allocation_count == launch_spec_gpu_count == launch_spec_tp == gpu_count
        assert set(measurement.allocation_gpu_uuids) == set(measurement.launch_spec.gpu_uuids)

    if gpu_count == 8:
        assert all(
            measurement.active_allocations_per_gpu == (1,) * 8 for measurement in measurements
        )

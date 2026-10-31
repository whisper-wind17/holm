"""PROTO-03: lifecycle release and wake-up behavior on real PostgreSQL."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from holm_common.config import StorageConfig
from holm_contracts.agent.v1 import agent_pb2
from holm_control_api.agent_client import AgentSnapshot
from holm_control_api.model_instance_service import (
    CreateModelInstance,
    ModelInstanceInvalidState,
    ModelInstanceService,
)
from holm_domain import (
    ArtifactManifestEntry,
    ArtifactStorageType,
    MetadataStatus,
    ModelArtifactStatus,
    ModelInstanceStatus,
    ModelOriginType,
    RuntimeType,
    build_artifact_manifest,
    serialize_artifact_manifest,
)
from holm_gateway.routing import PostgresRouteSource  # type: ignore[import-untyped]
from holm_instance_controller import (
    InstanceController,
    PostgresControllerStore,
    RuntimeLaunchRequest,
    RuntimeObservation,
    RuntimeProcessState,
    RuntimeSummary,
)
from holm_persistence import (
    AiServiceCreate,
    AiServiceRepository,
    AsyncSessionFactory,
    ModelInstanceRepository,
    create_database_engine,
    create_session_factory,
    database_utc_now,
)
from holm_runtimes import RuntimeCompatibilityCatalog, load_runtime_compatibility
from migration_test_support import upgrade_head
from sqlalchemy.engine import URL

ROOT = Path(__file__).resolve().parents[2]
MODEL_ID = UUID("51000000-0000-4000-8000-000000000001")
ARTIFACT_ID = UUID("52000000-0000-4000-8000-000000000001")
GPU_0 = "GPU-proto-03-integration-0000"


class _InventoryAgent:
    async def observe(self) -> AgentSnapshot:
        return AgentSnapshot(
            host_info=agent_pb2.GetHostInfoResponse(),
            gpu_inventory=agent_pb2.ListGPUsResponse(
                discovery_state=agent_pb2.GPU_DISCOVERY_STATE_READY,
                devices=[agent_pb2.GpuDevice(uuid=GPU_0)],
            ),
        )


class _RuntimeAgent:
    def __init__(
        self,
        ensure_state: RuntimeProcessState = RuntimeProcessState.RUNNING,
        *,
        inventory_gpu_uuids: frozenset[str] = frozenset({GPU_0}),
    ) -> None:
        self.ensure_state = ensure_state
        self.inventory_gpu_uuids = inventory_gpu_uuids
        self.launches: list[RuntimeLaunchRequest] = []
        self.stop_calls: list[str] = []

    async def remove(self, instance_id: str) -> None:
        return None

    async def ensure(self, request: RuntimeLaunchRequest) -> RuntimeObservation:
        self.launches.append(request)
        return RuntimeObservation(self.ensure_state, "127.0.0.1:8123")

    async def stop(self, instance_id: str, *, grace_seconds: int) -> RuntimeObservation:
        del grace_seconds
        self.stop_calls.append(instance_id)
        return RuntimeObservation(RuntimeProcessState.EXITED)

    async def inspect(self, instance_id: str) -> RuntimeObservation:
        del instance_id
        return RuntimeObservation(
            RuntimeProcessState.RUNNING,
            "127.0.0.1:8123",
            started_at=datetime.now(UTC) - timedelta(seconds=30),
        )

    async def list(self) -> tuple[RuntimeSummary, ...]:
        return ()

    async def list_gpus(self) -> frozenset[str]:
        return self.inventory_gpu_uuids


class _Health:
    async def ready(self, endpoint: str, served_model_name: str) -> bool:
        del endpoint, served_model_name
        return True

    async def warmup(self, endpoint: str, served_model_name: str) -> bool:
        del endpoint, served_model_name
        return True


def _async_url(fixture: EphemeralPostgres) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username="ubuntu",
        host=str(fixture.socket_directory),
        port=fixture.port,
        database="postgres",
    )


@pytest.fixture(autouse=True)
def _database(ephemeral_postgres: EphemeralPostgres) -> Iterator[None]:
    upgrade_head(ephemeral_postgres.engine)
    with ephemeral_postgres.engine.begin() as connection:
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
                "source_key": "/models/proto-03",
                "display_name": "PROTO-03 Model",
                "path": "/models/proto-03",
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
                "repository_id": "proto-03-model",
                "revision": "c" * 40,
                "storage_type": ArtifactStorageType.EXTERNAL.value,
                "status": ModelArtifactStatus.READY.value,
                "path": "/models/proto-03",
                "manifest_hash": "d" * 64,
            },
        )
    yield
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(sa.text("TRUNCATE core.models CASCADE"))


def _value(*, runtime_type: RuntimeType = RuntimeType.VLLM) -> CreateModelInstance:
    return CreateModelInstance(
        artifact_id=ARTIFACT_ID,
        runtime_type=runtime_type,
        gpu_uuids=(GPU_0,),
        tensor_parallel_size=1,
        context_length=4096,
        gpu_memory_utilization=0.8,
    )


def _controller(
    store: PostgresControllerStore,
    agent: _RuntimeAgent,
    *,
    runtime_catalog: RuntimeCompatibilityCatalog | None = None,
) -> InstanceController:
    catalog = runtime_catalog or load_runtime_compatibility(ROOT / "runtime-compat.yaml")
    return InstanceController(
        store=store,
        agent=agent,
        health=_Health(),
        runtime_catalog=catalog,
        start_timeout_seconds=5,
        start_poll_seconds=0.001,
        health_seconds=0.001,
        dispatch_seconds=0.001,
        stop_drain_seconds=0.001,
        stop_grace_seconds=1,
    )


async def _verify_synthetic_launch(factory: AsyncSessionFactory, root: Path) -> None:
    """FX-M3-07B: supply real tiny files for the existing lifecycle fixture."""
    from test_M2_10C_artifact_reverification import _worker

    path = root / "external"
    path.mkdir()
    contents = b"12345678"
    (path / "model.safetensors").write_bytes(contents)
    (path / "config.json").write_bytes(b"{}")
    manifest = serialize_artifact_manifest(
        build_artifact_manifest(
            (
                ArtifactManifestEntry(
                    "model.safetensors", len(contents), hashlib.sha256(contents).hexdigest()
                ),
                ArtifactManifestEntry("config.json", 2, hashlib.sha256(b"{}").hexdigest()),
            )
        )
    )
    (path / "manifest.json").write_bytes(manifest)
    async with factory() as session, session.begin():
        await session.execute(
            sa.text("UPDATE core.model_artifacts SET path=:path, manifest_hash=:hash WHERE id=:id"),
            {"path": str(path), "hash": hashlib.sha256(manifest).hexdigest(), "id": ARTIFACT_ID},
        )
    await _worker(
        factory,
        StorageConfig(
            model_root=root,
            tokenizer_root=root / "tokenizers",
            external_model_roots=(path,),
        ),
    ).run_once()


def test_PROTO_03_start_failure_releases_gpu_in_real_postgres(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    async def scenario() -> UUID:
        engine = create_database_engine(_async_url(ephemeral_postgres))
        try:
            factory = create_session_factory(engine)
            created = await ModelInstanceService(
                session_factory=factory,
                agent=_InventoryAgent(),
            ).create(_value())
            await _verify_synthetic_launch(factory, tmp_path)
            store = PostgresControllerStore(factory)
            item = await store.claim(ModelInstanceStatus.STARTING)
            assert item is not None
            await _controller(
                store,
                _RuntimeAgent(RuntimeProcessState.EXITED),
            ).process_starting(item)
            return created.id
        finally:
            await engine.dispose()

    instance_id = asyncio.run(scenario())
    with ephemeral_postgres.engine.connect() as connection:
        row = (
            connection.execute(
                sa.text(
                    "SELECT instance.status, instance.error_code, allocation.status AS allocation "
                    "FROM core.model_instances AS instance "
                    "JOIN core.gpu_allocations AS allocation "
                    "ON allocation.instance_id = instance.id WHERE instance.id = :id"
                ),
                {"id": instance_id},
            )
            .mappings()
            .one()
        )
    assert row == {
        "status": ModelInstanceStatus.FAILED.value,
        "error_code": "runtime_start_failed",
        "allocation": "Released",
    }


def test_FIX_05_sglang_runtime_selection_reaches_launch_request_in_real_postgres(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    runtime_catalog = load_runtime_compatibility(ROOT / "runtime-compat.yaml")

    async def scenario() -> tuple[UUID, _RuntimeAgent]:
        engine = create_database_engine(_async_url(ephemeral_postgres))
        try:
            factory = create_session_factory(engine)
            created = await ModelInstanceService(
                session_factory=factory,
                agent=_InventoryAgent(),
                runtime_catalog=runtime_catalog,
            ).create(_value(runtime_type=RuntimeType.SGLANG))
            await _verify_synthetic_launch(factory, tmp_path)
            store = PostgresControllerStore(factory)
            item = await store.claim(ModelInstanceStatus.STARTING)
            assert item is not None
            agent = _RuntimeAgent()
            await _controller(store, agent).process_starting(item)
            return created.id, agent
        finally:
            await engine.dispose()

    instance_id, agent = asyncio.run(scenario())
    with ephemeral_postgres.engine.connect() as connection:
        status = connection.execute(
            sa.text("SELECT status FROM core.model_instances WHERE id = :id"),
            {"id": instance_id},
        ).scalar_one()
    assert status == ModelInstanceStatus.RUNNING.value
    assert [launch.runtime_name for launch in agent.launches] == ["sglang"]


def test_FIX_05_uncertified_runtime_persists_stable_failure_without_launch(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    full_catalog = load_runtime_compatibility(ROOT / "runtime-compat.yaml")
    vllm = full_catalog.runtime("vllm")
    assert vllm is not None
    vllm_only_catalog = RuntimeCompatibilityCatalog(
        schema_version=full_catalog.schema_version,
        runtimes={"vllm": vllm},
    )

    async def scenario() -> tuple[UUID, _RuntimeAgent]:
        engine = create_database_engine(_async_url(ephemeral_postgres))
        try:
            factory = create_session_factory(engine)
            created = await ModelInstanceService(
                session_factory=factory,
                agent=_InventoryAgent(),
                runtime_catalog=full_catalog,
            ).create(_value(runtime_type=RuntimeType.SGLANG))
            store = PostgresControllerStore(factory)
            item = await store.claim(ModelInstanceStatus.STARTING)
            assert item is not None
            agent = _RuntimeAgent()
            await _controller(
                store,
                agent,
                runtime_catalog=vllm_only_catalog,
            ).process_starting(item)
            return created.id, agent
        finally:
            await engine.dispose()

    instance_id, agent = asyncio.run(scenario())
    with ephemeral_postgres.engine.connect() as connection:
        row = (
            connection.execute(
                sa.text(
                    "SELECT instance.status, instance.error_code, "
                    "allocation.status AS allocation "
                    "FROM core.model_instances AS instance "
                    "JOIN core.gpu_allocations AS allocation "
                    "ON allocation.instance_id = instance.id WHERE instance.id = :id"
                ),
                {"id": instance_id},
            )
            .mappings()
            .one()
        )
    assert row == {
        "status": ModelInstanceStatus.FAILED.value,
        "error_code": "runtime_not_certified",
        "allocation": "Released",
    }
    assert agent.launches == []


def test_PROTO_03_stop_releases_gpu_in_real_postgres(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> UUID:
        engine = create_database_engine(_async_url(ephemeral_postgres))
        try:
            factory = create_session_factory(engine)
            service = ModelInstanceService(session_factory=factory, agent=_InventoryAgent())
            created = await service.create(_value())
            requested = await service.stop(created.id)
            assert requested.status is ModelInstanceStatus.STOPPING
            store = PostgresControllerStore(factory)
            item = await store.claim(ModelInstanceStatus.STOPPING)
            assert item is not None
            await _controller(store, _RuntimeAgent()).process_stopping(item)
            return created.id
        finally:
            await engine.dispose()

    instance_id = asyncio.run(scenario())
    with ephemeral_postgres.engine.connect() as connection:
        statuses = (
            connection.execute(
                sa.text(
                    "SELECT instance.status, allocation.status AS allocation "
                    "FROM core.model_instances AS instance "
                    "JOIN core.gpu_allocations AS allocation "
                    "ON allocation.instance_id = instance.id WHERE instance.id = :id"
                ),
                {"id": instance_id},
            )
            .mappings()
            .one()
        )
    assert statuses == {"status": ModelInstanceStatus.STOPPED.value, "allocation": "Released"}


def test_PROTO_03_release_wakes_oldest_waiting_instance_in_real_postgres(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> tuple[UUID, UUID]:
        engine = create_database_engine(_async_url(ephemeral_postgres))
        try:
            factory = create_session_factory(engine)
            service = ModelInstanceService(session_factory=factory, agent=_InventoryAgent())
            first = await service.create(_value())
            second = await service.create(_value())
            assert second.status is ModelInstanceStatus.WAITING_FOR_RESOURCE
            await service.stop(first.id)
            store = PostgresControllerStore(factory)
            item = await store.claim(ModelInstanceStatus.STOPPING)
            assert item is not None
            await _controller(store, _RuntimeAgent()).process_stopping(item)
            return first.id, second.id
        finally:
            await engine.dispose()

    first_id, second_id = asyncio.run(scenario())
    with ephemeral_postgres.engine.connect() as connection:
        instances: dict[UUID, str] = dict(
            connection.execute(
                sa.text("SELECT id, status FROM core.model_instances ORDER BY created_at")
            )
            .tuples()
            .all()
        )
        active_owner = connection.execute(
            sa.text(
                "SELECT instance_id FROM core.gpu_allocations "
                "WHERE gpu_uuid = :gpu_uuid AND status <> 'Released'"
            ),
            {"gpu_uuid": GPU_0},
        ).scalar_one()
    assert instances[first_id] == ModelInstanceStatus.STOPPED.value
    assert instances[second_id] == ModelInstanceStatus.STARTING.value
    assert active_owner == second_id


def test_M3_09_TC_GPU_14_reconciliation_releases_terminal_dangling_allocation(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> UUID:
        engine = create_database_engine(_async_url(ephemeral_postgres))
        try:
            factory = create_session_factory(engine)
            created = await ModelInstanceService(
                session_factory=factory,
                agent=_InventoryAgent(),
            ).create(_value())
            async with factory() as session, session.begin():
                await session.execute(
                    sa.text(
                        "UPDATE core.model_instances SET status = 'Failed', "
                        "error_code = 'injected_terminal_drift' WHERE id = :id"
                    ),
                    {"id": created.id},
                )
            released = await PostgresControllerStore(factory).release_terminal_allocations()
            assert released == (created.id,)
            return created.id
        finally:
            await engine.dispose()

    instance_id = asyncio.run(scenario())
    with ephemeral_postgres.engine.connect() as connection:
        allocation = (
            connection.execute(
                sa.text(
                    "SELECT status, released_at IS NOT NULL AS released "
                    "FROM core.gpu_allocations WHERE instance_id = :id"
                ),
                {"id": instance_id},
            )
            .mappings()
            .one()
        )
    assert allocation == {"status": "Released", "released": True}


def test_M3_09_TC_GPU_09_restarting_retains_original_allocation(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> UUID:
        engine = create_database_engine(_async_url(ephemeral_postgres))
        try:
            factory = create_session_factory(engine)
            created = await ModelInstanceService(
                session_factory=factory,
                agent=_InventoryAgent(),
            ).create(_value())
            async with factory() as session, session.begin():
                assert await ModelInstanceRepository(session).mark_running(
                    created.id,
                    now=await database_utc_now(session),
                )
            store = PostgresControllerStore(factory)
            assert await store.mark_unhealthy(
                created.id,
                expected_status=ModelInstanceStatus.RUNNING,
                error_code="runtime_exited",
            )
            assert await store.begin_restart(created.id, restart_window_seconds=600)
            return created.id
        finally:
            await engine.dispose()

    instance_id = asyncio.run(scenario())
    with ephemeral_postgres.engine.connect() as connection:
        row = (
            connection.execute(
                sa.text(
                    "SELECT instance.status, allocation.gpu_uuid, allocation.status AS allocation "
                    "FROM core.model_instances AS instance JOIN core.gpu_allocations AS allocation "
                    "ON allocation.instance_id = instance.id WHERE instance.id = :id"
                ),
                {"id": instance_id},
            )
            .mappings()
            .one()
        )
    assert row == {
        "status": ModelInstanceStatus.RESTARTING.value,
        "gpu_uuid": GPU_0,
        "allocation": "Allocated",
    }


def test_FIX_02_TC_GPU_13_FX_NV_06_vanished_gpu_becomes_unhealthy_without_reallocation(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> tuple[UUID, _RuntimeAgent]:
        engine = create_database_engine(_async_url(ephemeral_postgres))
        try:
            factory = create_session_factory(engine)
            created = await ModelInstanceService(
                session_factory=factory,
                agent=_InventoryAgent(),
            ).create(_value())
            async with factory() as session, session.begin():
                assert await ModelInstanceRepository(session).mark_running(
                    created.id,
                    now=await database_utc_now(session),
                )
            agent = _RuntimeAgent(inventory_gpu_uuids=frozenset())
            await _controller(PostgresControllerStore(factory), agent).recover_once()
            return created.id, agent
        finally:
            await engine.dispose()

    instance_id, agent = asyncio.run(scenario())
    with ephemeral_postgres.engine.connect() as connection:
        row = (
            connection.execute(
                sa.text(
                    "SELECT instance.status, instance.error_code, "
                    "instance.expected_gpu_uuids, instance.internal_endpoint, "
                    "allocation.gpu_uuid, allocation.status AS allocation, "
                    "allocation.released_at "
                    "FROM core.model_instances AS instance "
                    "JOIN core.gpu_allocations AS allocation "
                    "ON allocation.instance_id = instance.id WHERE instance.id = :id"
                ),
                {"id": instance_id},
            )
            .mappings()
            .one()
        )
        events = tuple(
            connection.execute(
                sa.text(
                    "SELECT event, payload FROM core.domain_events "
                    "WHERE aggregate_id = :id ORDER BY version"
                ),
                {"id": instance_id},
            ).mappings()
        )
    assert row == {
        "status": ModelInstanceStatus.UNHEALTHY.value,
        "error_code": "gpu_device_missing",
        "expected_gpu_uuids": [GPU_0],
        "internal_endpoint": None,
        "gpu_uuid": GPU_0,
        "allocation": "Allocated",
        "released_at": None,
    }
    assert events
    requests = [event for event in events if event["event"] == "instance.verification_requested"]
    assert len(requests) == 1
    assert requests[0]["payload"] == {
        "request_id": requests[0]["payload"]["request_id"],
        "artifact_id": str(ARTIFACT_ID),
        "config_version": 1,
        "status": "Starting",
        "attempt": 0,
    }
    assert UUID(requests[0]["payload"]["request_id"])
    assert all(
        event == {"event": "instance.route_changed", "payload": {}}
        for event in events
        if event not in requests
    )
    assert agent.stop_calls == []


def test_M3_09_TC_GPU_16_manual_start_clears_automatic_retry_window(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> tuple[int, object]:
        engine = create_database_engine(_async_url(ephemeral_postgres))
        try:
            factory = create_session_factory(engine)
            service = ModelInstanceService(session_factory=factory, agent=_InventoryAgent())
            created = await service.create(_value())
            async with factory() as session, session.begin():
                repository = ModelInstanceRepository(session)
                now = await database_utc_now(session)
                assert await repository.release_and_transition(
                    created.id,
                    expected_statuses=(ModelInstanceStatus.STARTING,),
                    target_status=ModelInstanceStatus.FAILED,
                    error_code="runtime_restart_failed",
                    now=now,
                )
                await session.execute(
                    sa.text(
                        "UPDATE core.model_instances SET restart_count = 3, "
                        "restart_window_started_at = clock_timestamp() WHERE id = :id"
                    ),
                    {"id": created.id},
                )
            restarted = await service.start(created.id)
            return restarted.restart_count, restarted.restart_window_started_at
        finally:
            await engine.dispose()

    restart_count, window_started_at = asyncio.run(scenario())
    assert restart_count == 0
    assert window_started_at is None


def test_M3_09_reconciliation_mutex_blocks_only_affected_lifecycle_write(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> None:
        engine = create_database_engine(_async_url(ephemeral_postgres))
        try:
            factory = create_session_factory(engine)
            service = ModelInstanceService(session_factory=factory, agent=_InventoryAgent())
            affected = await service.create(_value())
            unrelated = await service.create(_value())
            async with factory() as session, session.begin():
                repository = ModelInstanceRepository(session)
                now = await database_utc_now(session)
                assert await repository.mark_running(affected.id, now=now)
                await AiServiceRepository(session).create(
                    AiServiceCreate(
                        public_model_name="m3-09-established-route",
                        description="",
                        enabled=True,
                        primary_instance_id=affected.id,
                    ),
                    now=now,
                )

            store = PostgresControllerStore(factory)
            async with store.lifecycle_lock(affected.id):
                try:
                    await service.stop(affected.id)
                except ModelInstanceInvalidState:
                    pass
                else:
                    raise AssertionError("affected lifecycle write was not rejected")

                other = await service.start(unrelated.id)
                assert other.status is ModelInstanceStatus.WAITING_FOR_RESOURCE
                routes = await PostgresRouteSource(factory).list_routes()
                assert len(routes) == 1
                assert routes[0].instance_id == affected.id
                assert routes[0].instance_status == ModelInstanceStatus.RUNNING.value
        finally:
            await engine.dispose()

    asyncio.run(scenario())

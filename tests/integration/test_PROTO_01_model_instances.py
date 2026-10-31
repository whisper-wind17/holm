"""PROTO-01: Model Instance API and real PostgreSQL GPU reservation races."""

from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http.cookies import SimpleCookie
from multiprocessing.connection import Connection as PipeConnection
from pathlib import Path
from typing import Any, Final, cast
from uuid import UUID

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from fixtures.asgi_client import (
    NO_CONFIGURED_CREDENTIAL_ROWS,
    AsgiResponse,
    AsgiTestClient,
    SessionCookies,
)
from holm_contracts import VersionPayload
from holm_contracts.agent.v1 import agent_pb2
from holm_control_api.agent_client import AgentSnapshot
from holm_control_api.application import create_app
from holm_control_api.authentication import AuthenticationService
from holm_control_api.http_constants import SESSION_COOKIE_NAME
from holm_control_api.model_catalog_service import ModelCatalogService
from holm_control_api.model_instance_service import CreateModelInstance, ModelInstanceService
from holm_control_api.readiness import DependencyCheck, ReadinessReason
from holm_domain import (
    ArtifactStorageType,
    DownloadTaskStatus,
    MetadataStatus,
    ModelArtifactStatus,
    ModelInstanceStatus,
    ModelOriginType,
    RuntimeType,
    UserRole,
)
from holm_domain.model_instances import ModelInstanceEventType
from holm_instance_controller import (
    PostgresControllerStore,
    RuntimeAgentError,
    RuntimeObservation,
    RuntimeProcessState,
)
from holm_persistence import (
    AsyncSessionFactory,
    ModelInstanceRepository,
    UserRepository,
    create_database_engine,
    create_session_factory,
    database_utc_now,
)
from holm_runtimes import RuntimeCompatibilityCatalog, load_runtime_compatibility
from holm_security import (
    OWASP_MINIMUM_MEMORY_COST_KIB,
    OWASP_MINIMUM_TIME_COST,
    Argon2idParameters,
    PasswordHasher,
)
from migration_test_support import upgrade_head
from sqlalchemy.engine import URL, Engine
from test_PROTO_03_instance_controller_postgres import _controller, _RuntimeAgent

ADMIN_USERNAME: Final[str] = "proto-01-admin"
MEMBER_USERNAME: Final[str] = "proto-01-member"
PASSWORD: Final[str] = "correct-password-for-proto-01-tests"
CHEAP: Final = Argon2idParameters(
    memory_cost_kib=OWASP_MINIMUM_MEMORY_COST_KIB,
    time_cost=OWASP_MINIMUM_TIME_COST,
    parallelism=1,
)
MODEL_ID: Final[UUID] = UUID("41000000-0000-4000-8000-000000000001")
READY_ARTIFACT_ID: Final[UUID] = UUID("42000000-0000-4000-8000-000000000001")
NOT_READY_ARTIFACT_ID: Final[UUID] = UUID("42000000-0000-4000-8000-000000000002")
DOWNLOAD_TASK_ID: Final[UUID] = UUID("42000000-0000-4000-8000-000000000003")
GPU_0: Final[str] = "GPU-proto-01-0000"
GPU_1: Final[str] = "GPU-proto-01-0001"


class _StaticAgent:
    def __init__(self, gpu_uuids: tuple[str, ...]) -> None:
        self._gpu_uuids = gpu_uuids

    async def observe(self) -> AgentSnapshot:
        return AgentSnapshot(
            host_info=agent_pb2.GetHostInfoResponse(),
            gpu_inventory=agent_pb2.ListGPUsResponse(
                discovery_state=agent_pb2.GPU_DISCOVERY_STATE_READY,
                devices=[agent_pb2.GpuDevice(uuid=value) for value in self._gpu_uuids],
            ),
        )


class _BarrierAgent:
    def __init__(self, barrier: Any, gpu_uuid: str) -> None:
        self._barrier = barrier
        self._gpu_uuid = gpu_uuid

    async def observe(self) -> AgentSnapshot:
        self._barrier.wait(timeout=30)
        return AgentSnapshot(
            host_info=agent_pb2.GetHostInfoResponse(),
            gpu_inventory=agent_pb2.ListGPUsResponse(
                discovery_state=agent_pb2.GPU_DISCOVERY_STATE_READY,
                devices=[agent_pb2.GpuDevice(uuid=self._gpu_uuid)],
            ),
        )


@dataclass(frozen=True, slots=True)
class _Environment:
    client: AsgiTestClient
    session_factory: AsyncSessionFactory


async def _readiness_ok() -> ReadinessReason:
    return ReadinessReason.OK


def _async_url(fixture: EphemeralPostgres) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username="ubuntu",
        host=str(fixture.socket_directory),
        port=fixture.port,
        database="postgres",
    )


@asynccontextmanager
async def _environment(
    fixture: EphemeralPostgres,
    *,
    runtime_catalog: RuntimeCompatibilityCatalog | None = None,
) -> AsyncIterator[_Environment]:
    engine = create_database_engine(_async_url(fixture), connect_timeout_seconds=5)
    try:
        session_factory = create_session_factory(engine)
        authentication = AuthenticationService(
            session_factory=session_factory,
            password_hasher=PasswordHasher(CHEAP),
            minimum_login_failure_seconds=0,
        )
        instances = ModelInstanceService(
            session_factory=session_factory,
            agent=_StaticAgent((GPU_0, GPU_1)),
            runtime_catalog=runtime_catalog,
        )
        app = create_app(
            version_payload=VersionPayload(
                build_version="0.1.0+proto01",
                commit="1" * 40,
                schema_version=1,
                runtime_compat_hash="2" * 64,
            ),
            readiness_checks=[DependencyCheck(name="database", probe=_readiness_ok)],
            credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
            authentication=authentication,
            model_catalog=ModelCatalogService(session_factory=session_factory),
            model_instances=instances,
        )
        yield _Environment(client=AsgiTestClient(app), session_factory=session_factory)
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def _migrated_instance_database(ephemeral_postgres: EphemeralPostgres) -> Iterator[None]:
    upgrade_head(ephemeral_postgres.engine)
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(sa.text("TRUNCATE core.models, core.users CASCADE"))
    _seed_model_and_artifacts(ephemeral_postgres.engine)
    yield
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(sa.text("TRUNCATE core.models, core.users CASCADE"))


def _seed_model_and_artifacts(engine: Engine) -> None:
    with engine.begin() as connection:
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
                "source_key": "/models/proto-01",
                "display_name": "PROTO-01 Model",
                "path": "/models/proto-01",
                "metadata_status": MetadataStatus.CONFIRMED.value,
            },
        )
        for artifact_id, status, revision in (
            (READY_ARTIFACT_ID, ModelArtifactStatus.READY, "a" * 40),
            (NOT_READY_ARTIFACT_ID, ModelArtifactStatus.CORRUPTED, "b" * 40),
        ):
            connection.execute(
                sa.text(
                    "INSERT INTO core.model_artifacts "
                    "(id, model_id, source_type, repository_id, resolved_revision, storage_type, "
                    "status, path, manifest_hash, size_bytes, created_at, updated_at) VALUES "
                    "(:id, :model_id, :source_type, :repository_id, :revision, :storage_type, "
                    ":status, :path, :manifest_hash, 1024, clock_timestamp(), clock_timestamp())"
                ),
                {
                    "id": artifact_id,
                    "model_id": MODEL_ID,
                    "source_type": ModelOriginType.LOCAL_PATH.value,
                    "repository_id": f"proto-01-{artifact_id.hex[-1]}",
                    "revision": revision,
                    "storage_type": ArtifactStorageType.EXTERNAL.value,
                    "status": status.value,
                    "path": "/models/proto-01",
                    "manifest_hash": revision,
                },
            )
        connection.execute(
            sa.text(
                "INSERT INTO core.download_tasks "
                "(id, model_id, artifact_id, source_type, repository_id, "
                "requested_revision_norm, resolved_revision, status, attempt, downloaded_bytes, "
                "total_bytes, lease_owner, lease_until, staging_path, error_code, created_at, "
                "updated_at, completed_at) VALUES "
                "(:id, :model_id, :artifact_id, :source_type, :repository_id, :revision, "
                ":revision, :status, 1, 1024, 1024, NULL, NULL, NULL, NULL, "
                "clock_timestamp(), clock_timestamp(), clock_timestamp())"
            ),
            {
                "id": DOWNLOAD_TASK_ID,
                "model_id": MODEL_ID,
                "artifact_id": READY_ARTIFACT_ID,
                "source_type": ModelOriginType.LOCAL_PATH.value,
                "repository_id": "proto-01-shared-download",
                "revision": "a" * 40,
                "status": DownloadTaskStatus.COMPLETED.value,
            },
        )


async def _create_accounts(environment: _Environment) -> None:
    async with environment.session_factory() as session:
        password_hash = await asyncio.to_thread(PasswordHasher(CHEAP).hash, PASSWORD)
        now = await database_utc_now(session)
        repository = UserRepository(session)
        for username, role in (
            (ADMIN_USERNAME, UserRole.ADMIN),
            (MEMBER_USERNAME, UserRole.USER),
        ):
            await repository.create(
                username=username,
                display_name=username,
                role=role,
                password_hash=password_hash,
                now=now,
                must_change_password=False,
            )
        await session.commit()


def _cookie(response: AsgiResponse) -> str:
    values: list[str] = []
    for key, raw in response.headers:
        if key.casefold() != "set-cookie":
            continue
        parsed = SimpleCookie()
        parsed.load(raw)
        if SESSION_COOKIE_NAME in parsed:
            values.append(parsed[SESSION_COOKIE_NAME].value)
    assert len(values) == 1
    return values[0]


async def _login(environment: _Environment, username: str) -> Mapping[str, str]:
    response = await environment.client.request(
        "POST",
        "/api/v1/auth/login",
        json_body={"username": username, "password": PASSWORD},
    )
    assert response.status == 200
    return SessionCookies(
        {SESSION_COOKIE_NAME: _cookie(response)},
        csrf_token=str(response.json()["csrf_token"]),
    )


def _create_body(
    artifact_id: UUID,
    gpu_uuids: list[str],
    tensor_parallel_size: int,
) -> dict[str, object]:
    return {
        "artifact_id": str(artifact_id),
        "runtime_type": RuntimeType.VLLM.value,
        "gpu_uuids": gpu_uuids,
        "tensor_parallel_size": tensor_parallel_size,
        "context_length": 4096,
        "gpu_memory_utilization": 0.9,
    }


@pytest.mark.parametrize("stop_result", ["exited", "running", "unavailable"])
def test_M3_07A_TC_GPU_09_TC_GPU_20_unhealthy_stop_closes_lifecycle(
    ephemeral_postgres: EphemeralPostgres,
    stop_result: str,
) -> None:
    """FX-M3-07A: reuse PROTO-01 PG/API and PROTO-03 controlled Runtime fixtures."""

    class StopAgent(_RuntimeAgent):
        result = stop_result

        async def stop(self, instance_id: str, *, grace_seconds: int) -> RuntimeObservation:
            if self.result == "unavailable":
                raise RuntimeAgentError()
            if self.result == "running":
                return RuntimeObservation(RuntimeProcessState.RUNNING)
            return await super().stop(instance_id, grace_seconds=grace_seconds)

    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as environment:
            await _create_accounts(environment)
            admin = await _login(environment, ADMIN_USERNAME)
            member = await _login(environment, MEMBER_USERNAME)
            created = await environment.client.request(
                "POST",
                "/api/v1/instances",
                cookies=admin,
                json_body=_create_body(READY_ARTIFACT_ID, [GPU_0], 1),
            )
            assert created.status == 201
            instance_id = UUID(created.json()["id"])
            store = PostgresControllerStore(environment.session_factory)
            assert await store.mark_running(instance_id)
            assert await store.mark_unhealthy(
                instance_id,
                expected_status=ModelInstanceStatus.RUNNING,
                error_code="runtime_exited",
            )

            async def allocation() -> dict[str, object]:
                async with environment.session_factory() as session:
                    return dict(
                        (
                            await session.execute(
                                sa.text(
                                    "SELECT id, gpu_uuid, status, released_at "
                                    "FROM core.gpu_allocations "
                                    "WHERE instance_id = :id ORDER BY created_at LIMIT 1"
                                ),
                                {"id": instance_id},
                            )
                        )
                        .mappings()
                        .one()
                    )

            original = await allocation()
            assert original["status"] == "Allocated"
            assert original["gpu_uuid"] == GPU_0
            assert original["released_at"] is None
            path = f"/api/v1/instances/{instance_id}:stop"
            unauthenticated = await environment.client.request("POST", path)
            forbidden = await environment.client.request("POST", path, cookies=member)
            assert unauthenticated.status == 401
            assert forbidden.status == 403
            assert await store.status(instance_id) is ModelInstanceStatus.UNHEALTHY
            assert await allocation() == original

            async def route_event_count() -> int:
                async with environment.session_factory() as session:
                    return int(
                        await session.scalar(
                            sa.text(
                                "SELECT count(*) FROM core.domain_events "
                                "WHERE aggregate_id = :id AND event = 'instance.route_changed'"
                            ),
                            {"id": instance_id},
                        )
                    )

            before_stop = await route_event_count()
            stopping = await environment.client.request("POST", path, cookies=admin)
            assert stopping.status == 200
            assert stopping.json()["status"] == ModelInstanceStatus.STOPPING.value
            assert stopping.json()["internal_endpoint"] is None
            assert await allocation() == original
            assert await route_event_count() == before_stop + 1

            agent = StopAgent()
            controller = _controller(store, agent)
            item = await store.claim(ModelInstanceStatus.STOPPING)
            assert item is not None
            assert item.instance.id == instance_id
            assert item.instance.internal_endpoint is None
            async with store.lifecycle_lock(instance_id):
                await controller.process_stopping(item)
            if stop_result != "exited":
                assert await store.status(instance_id) is ModelInstanceStatus.STOPPING
                assert await allocation() == original
                agent.result = "exited"
                item = await store.claim(ModelInstanceStatus.STOPPING)
                assert item is not None
                async with store.lifecycle_lock(instance_id):
                    await controller.process_stopping(item)

            assert agent.stop_calls == [str(instance_id)]
            assert await store.status(instance_id) is ModelInstanceStatus.STOPPED
            released = await allocation()
            assert released["id"] == original["id"]
            assert released["gpu_uuid"] == original["gpu_uuid"]
            assert released["status"] == "Released"
            assert released["released_at"] is not None
            events = await environment.client.get(
                f"/api/v1/instances/{instance_id}/events?page_size=100",
                cookies=admin,
            )
            assert events.status == 200
            transitions = {
                (event["from_status"], event["to_status"], event["actor"])
                for event in events.json()["items"]
            }
            assert ("Unhealthy", "Stopping", "admin") in transitions
            assert ("Stopping", "Stopped", "controller") in transitions
            invalid = await environment.client.request("POST", path, cookies=admin)
            assert invalid.status == 409
            restarted = await environment.client.request(
                "POST",
                f"/api/v1/instances/{instance_id}:start",
                cookies=admin,
            )
            assert restarted.status == 200
            assert restarted.json()["status"] == ModelInstanceStatus.STARTING.value
            async with environment.session_factory() as session:
                reserved = (
                    (
                        await session.execute(
                            sa.text(
                                "SELECT gpu_uuid, status FROM core.gpu_allocations "
                                "WHERE instance_id = :id AND released_at IS NULL"
                            ),
                            {"id": instance_id},
                        )
                    )
                    .mappings()
                    .one()
                )
            assert dict(reserved) == {"gpu_uuid": GPU_0, "status": "Reserved"}

    asyncio.run(scenario())


def test_PROTO_01_api_reserves_waits_releases_restarts_and_guards(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> tuple[AsgiResponse, ...]:
        async with _environment(ephemeral_postgres) as environment:
            await _create_accounts(environment)
            admin = await _login(environment, ADMIN_USERNAME)
            member = await _login(environment, MEMBER_USERNAME)
            first = await environment.client.request(
                "POST",
                "/api/v1/instances",
                cookies=admin,
                json_body=_create_body(READY_ARTIFACT_ID, [GPU_1, GPU_0], 2),
            )
            second = await environment.client.request(
                "POST",
                "/api/v1/instances",
                cookies=admin,
                json_body=_create_body(READY_ARTIFACT_ID, [GPU_1], 1),
            )
            mismatch = await environment.client.request(
                "POST",
                "/api/v1/instances",
                cookies=admin,
                json_body=_create_body(READY_ARTIFACT_ID, [GPU_0], 2),
            )
            not_ready = await environment.client.request(
                "POST",
                "/api/v1/instances",
                cookies=admin,
                json_body=_create_body(NOT_READY_ARTIFACT_ID, [GPU_0], 1),
            )
            forbidden = await environment.client.request(
                "POST",
                "/api/v1/instances",
                cookies=member,
                json_body=_create_body(READY_ARTIFACT_ID, [GPU_0], 1),
            )
            listed = await environment.client.get("/api/v1/instances", cookies=admin)
            fetched = await environment.client.get(
                f"/api/v1/instances/{first.json()['id']}", cookies=admin
            )
            stopped = await environment.client.request(
                "POST", f"/api/v1/instances/{first.json()['id']}:stop", cookies=admin
            )
            async with environment.session_factory() as session, session.begin():
                repository = ModelInstanceRepository(session)
                now = await database_utc_now(session)
                assert await repository.release_and_transition(
                    UUID(first.json()["id"]),
                    expected_statuses=(ModelInstanceStatus.STOPPING,),
                    target_status=ModelInstanceStatus.STOPPED,
                    error_code=None,
                    now=now,
                )
            with ephemeral_postgres.engine.connect() as connection:
                released_before_delete = connection.execute(
                    sa.text(
                        "SELECT count(*) FROM core.gpu_allocations "
                        "WHERE instance_id = :instance_id AND status = 'Released' "
                        "AND released_at IS NOT NULL"
                    ),
                    {"instance_id": UUID(first.json()["id"])},
                ).scalar_one()
            assert released_before_delete == 2
            restarted = await environment.client.request(
                "POST", f"/api/v1/instances/{second.json()['id']}:start", cookies=admin
            )
            deleted = await environment.client.request(
                "DELETE", f"/api/v1/instances/{first.json()['id']}", cookies=admin
            )
            delete_running = await environment.client.request(
                "DELETE", f"/api/v1/instances/{second.json()['id']}", cookies=admin
            )
            catalog = await environment.client.get("/api/v1/models", cookies=admin)
            return (
                first,
                second,
                mismatch,
                not_ready,
                forbidden,
                listed,
                fetched,
                stopped,
                restarted,
                deleted,
                delete_running,
                catalog,
            )

    (
        first,
        second,
        mismatch,
        not_ready,
        forbidden,
        listed,
        fetched,
        stopped,
        restarted,
        deleted,
        delete_running,
        catalog,
    ) = asyncio.run(scenario())
    assert first.status == second.status == 201
    assert first.json()["status"] == ModelInstanceStatus.STARTING.value
    assert first.json()["gpu_uuids"] == [GPU_0, GPU_1]
    assert second.json()["status"] == ModelInstanceStatus.WAITING_FOR_RESOURCE.value
    assert mismatch.status == 400
    assert mismatch.json()["error"]["code"] == "invalid_request"
    assert not_ready.status == 409
    assert not_ready.json()["error"]["code"] == "artifact_not_ready"
    assert forbidden.status == 403
    assert len(listed.json()["items"]) == 2
    assert fetched.json()["id"] == first.json()["id"]
    assert stopped.json()["status"] == ModelInstanceStatus.STOPPING.value
    assert stopped.json()["internal_endpoint"] is None
    assert restarted.json()["status"] == ModelInstanceStatus.STARTING.value
    assert deleted.status == 204
    assert delete_running.status == 409
    assert catalog.json()["items"][0]["references"]["model_instances"] == 1

    with ephemeral_postgres.engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                "SELECT gpu_uuid, status, released_at IS NOT NULL AS released "
                "FROM core.gpu_allocations ORDER BY created_at, gpu_uuid"
            )
        ).mappings()
        allocations = tuple(rows)
    assert sum(row["status"] == "Reserved" for row in allocations) == 1
    assert sum(row["status"] == "Released" and row["released"] for row in allocations) == 0


def test_M3_03A_TC_GPU_03_partition_rejection_precedes_instance_and_allocation_creation(
    ephemeral_postgres: EphemeralPostgres,
    caplog: pytest.LogCaptureFixture,
) -> None:
    catalog = load_runtime_compatibility(Path("runtime-compat.yaml"))
    caplog.set_level(logging.INFO, logger="holm.control_api.model_instances")

    async def scenario() -> tuple[AsgiResponse, AsgiResponse, AsgiResponse, AsgiResponse]:
        async with _environment(ephemeral_postgres, runtime_catalog=catalog) as environment:
            await _create_accounts(environment)
            admin = await _login(environment, ADMIN_USERNAME)
            with ephemeral_postgres.engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "UPDATE core.model_artifacts SET attention_heads = 7, kv_heads = 4 "
                        "WHERE id = :id"
                    ),
                    {"id": READY_ARTIFACT_ID},
                )
            attention_rejected = await environment.client.request(
                "POST",
                "/api/v1/instances",
                cookies=admin,
                json_body=_create_body(READY_ARTIFACT_ID, [GPU_0, GPU_1], 2),
            )
            with ephemeral_postgres.engine.connect() as connection:
                assert (
                    connection.execute(
                        sa.text("SELECT count(*) FROM core.model_instances")
                    ).scalar_one()
                    == 0
                )
                assert (
                    connection.execute(
                        sa.text("SELECT count(*) FROM core.gpu_allocations")
                    ).scalar_one()
                    == 0
                )

            with ephemeral_postgres.engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "UPDATE core.model_artifacts SET attention_heads = 8, kv_heads = 3 "
                        "WHERE id = :id"
                    ),
                    {"id": READY_ARTIFACT_ID},
                )
            kv_rejected = await environment.client.request(
                "POST",
                "/api/v1/instances",
                cookies=admin,
                json_body=_create_body(READY_ARTIFACT_ID, [GPU_0, GPU_1], 2),
            )
            with ephemeral_postgres.engine.connect() as connection:
                assert (
                    connection.execute(
                        sa.text("SELECT count(*) FROM core.model_instances")
                    ).scalar_one()
                    == 0
                )
                assert (
                    connection.execute(
                        sa.text("SELECT count(*) FROM core.gpu_allocations")
                    ).scalar_one()
                    == 0
                )

            with ephemeral_postgres.engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "UPDATE core.model_artifacts SET attention_heads = 8, kv_heads = 4 "
                        "WHERE id = :id"
                    ),
                    {"id": READY_ARTIFACT_ID},
                )
            accepted = await environment.client.request(
                "POST",
                "/api/v1/instances",
                cookies=admin,
                json_body=_create_body(READY_ARTIFACT_ID, [GPU_0, GPU_1], 2),
            )

            with ephemeral_postgres.engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "UPDATE core.model_artifacts SET attention_heads = NULL, kv_heads = NULL "
                        "WHERE id = :id"
                    ),
                    {"id": READY_ARTIFACT_ID},
                )
            uncollected = await environment.client.request(
                "POST",
                "/api/v1/instances",
                cookies=admin,
                json_body=_create_body(READY_ARTIFACT_ID, [GPU_0], 1),
            )
            return attention_rejected, kv_rejected, accepted, uncollected

    attention_rejected, kv_rejected, accepted, uncollected = asyncio.run(scenario())
    for response in (attention_rejected, kv_rejected):
        assert response.status == 422
        assert response.json()["error"]["code"] == "invalid_tensor_parallel_size"
    assert accepted.status == 201
    assert accepted.json()["status"] == ModelInstanceStatus.STARTING.value
    assert uncollected.status == 201
    assert uncollected.json()["status"] == ModelInstanceStatus.WAITING_FOR_RESOURCE.value
    unavailable = [
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "model_instance_tp_partition_check_unavailable"
    ]
    assert len(unavailable) == 1
    assert getattr(unavailable[0], "attention_heads_collected", None) is False
    assert getattr(unavailable[0], "kv_heads_collected", None) is False
    assert "/models/" not in caplog.text


def _reservation_worker(
    socket_directory: str,
    port: int,
    barrier: Any,
    sender: PipeConnection,
) -> None:
    async def run() -> None:
        url = URL.create(
            "postgresql+psycopg",
            username="ubuntu",
            host=socket_directory,
            port=port,
            database="postgres",
        )
        engine = create_database_engine(url, connect_timeout_seconds=5)
        try:
            service = ModelInstanceService(
                session_factory=create_session_factory(engine),
                agent=_BarrierAgent(barrier, GPU_0),
            )
            record = await service.create(
                CreateModelInstance(
                    artifact_id=READY_ARTIFACT_ID,
                    runtime_type=RuntimeType.VLLM,
                    gpu_uuids=(GPU_0,),
                    tensor_parallel_size=1,
                    context_length=4096,
                    gpu_memory_utilization=0.9,
                )
            )
            sender.send((str(record.id), record.status.value))
        finally:
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        sender.close()


def test_PROTO_01_two_process_transactions_only_one_reserves_same_gpu(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    processes = []
    receivers: list[PipeConnection] = []
    try:
        for _ in range(2):
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(
                target=_reservation_worker,
                args=(
                    str(ephemeral_postgres.socket_directory),
                    ephemeral_postgres.port,
                    barrier,
                    sender,
                ),
            )
            process.start()
            sender.close()
            receivers.append(receiver)
            processes.append(process)

        results = []
        for receiver in receivers:
            assert receiver.poll(30), "PROTO-01 reservation worker timed out"
            results.append(receiver.recv())
        for process in processes:
            process.join(timeout=30)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        for receiver in receivers:
            receiver.close()

    assert sorted(status for _, status in results) == [
        ModelInstanceStatus.STARTING.value,
        ModelInstanceStatus.WAITING_FOR_RESOURCE.value,
    ]
    with ephemeral_postgres.engine.connect() as connection:
        active_count = connection.execute(
            sa.text(
                "SELECT count(*) FROM core.gpu_allocations "
                "WHERE gpu_uuid = :gpu_uuid AND status <> 'Released'"
            ),
            {"gpu_uuid": GPU_0},
        ).scalar_one()
        loser_allocation_count = connection.execute(
            sa.text(
                "SELECT count(*) FROM core.gpu_allocations AS allocation "
                "JOIN core.model_instances AS instance ON instance.id = allocation.instance_id "
                "WHERE instance.status = 'WaitingForResource'"
            )
        ).scalar_one()
    assert active_count == 1
    assert loser_allocation_count == 0


def test_M3_01_TC_GPU_08_TC_INV_07_lifecycle_commands_and_events(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    missing_id = UUID("4f000000-0000-4000-8000-000000000001")

    async def scenario() -> dict[str, object]:
        async with _environment(ephemeral_postgres) as environment:
            await _create_accounts(environment)
            admin = await _login(environment, ADMIN_USERNAME)
            member = await _login(environment, MEMBER_USERNAME)

            first = await environment.client.request(
                "POST",
                "/api/v1/instances",
                cookies=admin,
                json_body=_create_body(READY_ARTIFACT_ID, [GPU_0], 1),
            )
            first_id = UUID(first.json()["id"])
            async with environment.session_factory() as session, session.begin():
                assert await ModelInstanceRepository(session).mark_running(
                    first_id,
                    now=await database_utc_now(session),
                )

            waiting = await environment.client.request(
                "POST",
                "/api/v1/instances",
                cookies=admin,
                json_body=_create_body(READY_ARTIFACT_ID, [GPU_0], 1),
            )
            waiting_id = UUID(waiting.json()["id"])
            waited_once = await environment.client.request(
                "POST",
                f"/api/v1/instances/{waiting_id}:start",
                cookies=admin,
            )
            waited_twice = await environment.client.request(
                "POST",
                f"/api/v1/instances/{waiting_id}:start",
                cookies=admin,
            )
            selected = await environment.client.request(
                "POST",
                f"/api/v1/instances/{waiting_id}:select-gpus",
                cookies=admin,
                json_body={"gpu_uuids": [GPU_0]},
            )
            invalid_waiting_restart = await environment.client.request(
                "POST",
                f"/api/v1/instances/{waiting_id}:restart",
                cookies=admin,
            )

            forbidden: list[AsgiResponse] = []
            for suffix, body in (
                ("restart", None),
                ("retry", None),
                ("cancel", None),
                ("select-gpus", {"gpu_uuids": [GPU_0]}),
            ):
                forbidden.append(
                    await environment.client.request(
                        "POST",
                        f"/api/v1/instances/{waiting_id}:{suffix}",
                        cookies=member,
                        json_body=body,
                    )
                )

            missing: list[AsgiResponse] = []
            for suffix, body in (
                ("restart", None),
                ("retry", None),
                ("cancel", None),
                ("select-gpus", {"gpu_uuids": [GPU_0]}),
            ):
                missing.append(
                    await environment.client.request(
                        "POST",
                        f"/api/v1/instances/{missing_id}:{suffix}",
                        cookies=admin,
                        json_body=body,
                    )
                )

            invalid_running_cancel = await environment.client.request(
                "POST",
                f"/api/v1/instances/{first_id}:cancel",
                cookies=admin,
            )
            cancelled = await environment.client.request(
                "POST",
                f"/api/v1/instances/{waiting_id}:cancel",
                cookies=admin,
            )
            cancelled_events = await environment.client.get(
                f"/api/v1/instances/{waiting_id}/events",
                cookies=admin,
            )

            with ephemeral_postgres.engine.connect() as connection:
                before_restart = tuple(
                    connection.execute(
                        sa.text(
                            "SELECT gpu_uuid FROM core.gpu_allocations "
                            "WHERE instance_id = :id AND status <> 'Released' ORDER BY gpu_uuid"
                        ),
                        {"id": first_id},
                    ).scalars()
                )
            restarted = await environment.client.request(
                "POST",
                f"/api/v1/instances/{first_id}:restart",
                cookies=admin,
            )
            with ephemeral_postgres.engine.connect() as connection:
                after_restart = tuple(
                    connection.execute(
                        sa.text(
                            "SELECT gpu_uuid FROM core.gpu_allocations "
                            "WHERE instance_id = :id AND status <> 'Released' ORDER BY gpu_uuid"
                        ),
                        {"id": first_id},
                    ).scalars()
                )

            first_events_page = await environment.client.get(
                f"/api/v1/instances/{first_id}/events?page_size=1",
                cookies=admin,
            )
            next_cursor = first_events_page.json()["next_cursor"]
            second_events_page = await environment.client.get(
                f"/api/v1/instances/{first_id}/events?page_size=1&cursor={next_cursor}",
                cookies=admin,
            )
            all_first_events = await environment.client.get(
                f"/api/v1/instances/{first_id}/events?page_size=100",
                cookies=admin,
            )

            async with environment.session_factory() as session, session.begin():
                repository = ModelInstanceRepository(session)
                assert await repository.mark_restarted_running(
                    first_id,
                    endpoint="127.0.0.1:8123",
                    now=await database_utc_now(session),
                )
            await environment.client.request(
                "POST",
                f"/api/v1/instances/{first_id}:stop",
                cookies=admin,
            )
            async with environment.session_factory() as session, session.begin():
                repository = ModelInstanceRepository(session)
                assert await repository.release_and_transition(
                    first_id,
                    expected_statuses=(ModelInstanceStatus.STOPPING,),
                    target_status=ModelInstanceStatus.STOPPED,
                    error_code=None,
                    now=await database_utc_now(session),
                )
            invalid_stopped_stop = await environment.client.request(
                "POST",
                f"/api/v1/instances/{first_id}:stop",
                cookies=admin,
            )

            failed = await environment.client.request(
                "POST",
                "/api/v1/instances",
                cookies=admin,
                json_body=_create_body(READY_ARTIFACT_ID, [GPU_1], 1),
            )
            failed_id = UUID(failed.json()["id"])
            async with environment.session_factory() as session, session.begin():
                repository = ModelInstanceRepository(session)
                assert await repository.release_and_transition(
                    failed_id,
                    expected_statuses=(ModelInstanceStatus.STARTING,),
                    target_status=ModelInstanceStatus.FAILED,
                    error_code="runtime_start_failed",
                    now=await database_utc_now(session),
                )
            retried = await environment.client.request(
                "POST",
                f"/api/v1/instances/{failed_id}:retry",
                cookies=admin,
            )

            return {
                "waiting_id": waiting_id,
                "waited_once": waited_once,
                "waited_twice": waited_twice,
                "selected": selected,
                "invalid_waiting_restart": invalid_waiting_restart,
                "forbidden": forbidden,
                "missing": missing,
                "invalid_running_cancel": invalid_running_cancel,
                "cancelled": cancelled,
                "cancelled_events": cancelled_events,
                "before_restart": before_restart,
                "after_restart": after_restart,
                "restarted": restarted,
                "first_events_page": first_events_page,
                "second_events_page": second_events_page,
                "all_first_events": all_first_events,
                "invalid_stopped_stop": invalid_stopped_stop,
                "retried": retried,
            }

    result = asyncio.run(scenario())
    for key in ("waited_once", "waited_twice", "selected"):
        response = result[key]
        assert isinstance(response, AsgiResponse)
        assert response.status == 200
        assert response.json()["status"] == ModelInstanceStatus.WAITING_FOR_RESOURCE.value
        assert response.json()["gpu_uuids"] == [GPU_0]
        assert response.json()["tensor_parallel_size"] == 1
    selected = cast(AsgiResponse, result["selected"])
    assert selected.json()["config_version"] == 2
    for key in ("invalid_waiting_restart", "invalid_running_cancel", "invalid_stopped_stop"):
        response = result[key]
        assert isinstance(response, AsgiResponse)
        assert response.status == 409
        assert response.json()["error"]["code"] == "invalid_instance_state"
    forbidden = cast(list[AsgiResponse], result["forbidden"])
    missing = cast(list[AsgiResponse], result["missing"])
    assert all(response.status == 403 for response in forbidden)
    assert all(
        response.status == 404 and response.json()["error"]["code"] == "instance_not_found"
        for response in missing
    )
    cancelled = cast(AsgiResponse, result["cancelled"])
    restarted = cast(AsgiResponse, result["restarted"])
    retried = cast(AsgiResponse, result["retried"])
    first_events_page = cast(AsgiResponse, result["first_events_page"])
    second_events_page = cast(AsgiResponse, result["second_events_page"])
    assert cancelled.status == 204
    assert result["before_restart"] == result["after_restart"] == (GPU_0,)
    assert restarted.json()["status"] == ModelInstanceStatus.RESTARTING.value
    assert retried.json()["status"] == ModelInstanceStatus.STARTING.value
    assert first_events_page.status == 200
    assert first_events_page.json()["next_cursor"]
    assert len(second_events_page.json()["items"]) == 1

    cancelled_events = result["cancelled_events"]
    assert isinstance(cancelled_events, AsgiResponse)
    assert cancelled_events.status == 200
    assert ModelInstanceEventType.DEPLOYMENT_CANCELLED.value in {
        item["event_type"] for item in cancelled_events.json()["items"]
    }
    for item in cancelled_events.json()["items"]:
        assert set(item) == {
            "id",
            "event_type",
            "from_status",
            "to_status",
            "occurred_at",
            "actor",
            "detail",
        }
        assert item["detail"] == {}
    assert "M3-01-PROMPT-SENTINEL" not in json.dumps(cancelled_events.json())

    all_first_events = result["all_first_events"]
    assert isinstance(all_first_events, AsgiResponse)
    assert {
        ModelInstanceEventType.RESTART_REQUESTED.value,
        ModelInstanceEventType.STATUS_CHANGED.value,
    }.issubset({item["event_type"] for item in all_first_events.json()["items"]})

    with ephemeral_postgres.engine.connect() as connection:
        remaining = (
            connection.execute(
                sa.text(
                    "SELECT "
                    "(SELECT count(*) FROM core.model_instances "
                    "WHERE id = :instance_id) AS instances, "
                    "(SELECT count(*) FROM core.model_artifacts "
                    "WHERE id = :artifact_id) AS artifacts, "
                    "(SELECT count(*) FROM core.download_tasks WHERE id = :task_id) AS downloads, "
                    "(SELECT count(*) FROM core.audit_events WHERE object_id = :instance_id "
                    "AND action = 'model_instance.lifecycle') AS audits"
                ),
                {
                    "instance_id": result["waiting_id"],
                    "artifact_id": READY_ARTIFACT_ID,
                    "task_id": DOWNLOAD_TASK_ID,
                },
            )
            .mappings()
            .one()
        )
    assert remaining == {"instances": 0, "artifacts": 1, "downloads": 1, "audits": 1}


def test_M3_01_instance_event_detail_guard_rejects_body_sentinel(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    with pytest.raises(sa.exc.IntegrityError), ephemeral_postgres.engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO core.model_instance_events "
                "(id, instance_id, event_type, from_status, to_status, occurred_at, actor, "
                "detail) VALUES (:id, :instance_id, 'StatusChanged', NULL, "
                "'WaitingForResource', clock_timestamp(), 'system', "
                "CAST(:detail AS jsonb))"
            ),
            {
                "id": UUID("4f000000-0000-4000-8000-000000000002"),
                "instance_id": UUID("4f000000-0000-4000-8000-000000000003"),
                "detail": '{"prompt":"M3-01-PROMPT-SENTINEL"}',
            },
        )

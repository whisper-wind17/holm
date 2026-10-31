"""M4-04 integration coverage for service-layer invalidation and Gateway behavior."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Final, cast
from uuid import UUID

import httpx
import sqlalchemy as sa
from conftest import EphemeralPostgres
from holm_contracts.agent.v1 import agent_pb2
from holm_control_api.agent_client import AgentSnapshot
from holm_control_api.ai_service_service import (
    AiServiceManagementService,
    CreateAiService,
    UpdateAiService,
)
from holm_control_api.api_key_service import (
    ApiKeyManagementService,
    CreatePersonalApiKey,
)
from holm_control_api.model_instance_service import ModelInstanceService
from holm_control_api.user_management_service import UserManagementService
from holm_domain import (
    ArtifactStorageType,
    MetadataStatus,
    ModelArtifactStatus,
    ModelInstanceStatus,
    ModelOriginType,
    RuntimeType,
    UserRole,
)
from holm_gateway.application import create_app  # type: ignore[import-untyped]
from holm_gateway.authentication import ApiKeyAuthenticator  # type: ignore[import-untyped]
from holm_gateway.route_snapshot import (  # type: ignore[import-untyped]
    PostgresRouteSnapshotSource,
    RouteSnapshotManager,
    SnapshotApiKeyValidationLookup,
)
from holm_instance_controller import PostgresControllerStore
from holm_persistence import (
    AsyncSessionFactory,
    PsycopgNotificationListener,
    create_database_engine,
    create_session_factory,
)
from holm_runtimes import RuntimeRequestCapabilities
from holm_security import ApiKeyPepperKeyring
from migration_test_support import upgrade_head
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.types import ASGIApp

MODEL_ID: Final = UUID("75000000-0000-4000-8000-000000000001")
ARTIFACT_ID: Final = UUID("75000000-0000-4000-8000-000000000002")
INSTANCE_A: Final = UUID("75000000-0000-4000-8000-000000000003")
INSTANCE_B: Final = UUID("75000000-0000-4000-8000-000000000004")
ADMIN_ID: Final = UUID("75000000-0000-4000-8000-000000000005")
MEMBER_ID: Final = UUID("75000000-0000-4000-8000-000000000006")
GPU_A: Final = "GPU-M4-04-A"
GPU_B: Final = "GPU-M4-04-B"
ENDPOINT_A: Final = "runtime-a.internal:9001"
ENDPOINT_B: Final = "runtime-b.internal:9002"


class _StaticAgent:
    async def observe(self) -> AgentSnapshot:
        return AgentSnapshot(
            host_info=agent_pb2.GetHostInfoResponse(),
            gpu_inventory=agent_pb2.ListGPUsResponse(
                discovery_state=agent_pb2.GPU_DISCOVERY_STATE_READY,
                devices=[agent_pb2.GpuDevice(uuid=GPU_A), agent_pb2.GpuDevice(uuid=GPU_B)],
            ),
        )


@dataclass(slots=True)
class _Environment:
    factory: AsyncSessionFactory
    engine: AsyncEngine
    manager: RouteSnapshotManager
    snapshot_source: PostgresRouteSnapshotSource
    user_service: UserManagementService
    api_key_service: ApiKeyManagementService
    ai_service: AiServiceManagementService
    instance_service: ModelInstanceService
    controller: PostgresControllerStore
    service_id: UUID
    key_id: UUID
    plaintext_key: str
    gateway: ASGIApp
    upstream: httpx.AsyncClient
    upstream_hosts: list[str]


def _async_url(fixture: EphemeralPostgres) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username="ubuntu",
        host=str(fixture.socket_directory),
        port=fixture.port,
        database="postgres",
    )


def _seed(fixture: EphemeralPostgres) -> None:
    upgrade_head(fixture.engine)
    with fixture.engine.begin() as connection:
        connection.execute(
            sa.text(
                "TRUNCATE core.ai_services, core.users, core.models, "
                "core.domain_events, core.audit_events CASCADE"
            )
        )
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
                "source_key": "/models/m4-04",
                "display_name": "M4-04 Model",
                "path": "/models/m4-04",
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
                "repository_id": "m4-04",
                "revision": "4" * 40,
                "storage_type": ArtifactStorageType.EXTERNAL.value,
                "status": ModelArtifactStatus.READY.value,
                "path": "/models/m4-04",
                "manifest_hash": "5" * 40,
            },
        )
        for instance_id, gpu_uuid, endpoint, internal_name in (
            (INSTANCE_A, GPU_A, ENDPOINT_A, "mi_aaaaaaaaaaaa"),
            (INSTANCE_B, GPU_B, ENDPOINT_B, "mi_bbbbbbbbbbbb"),
        ):
            connection.execute(
                sa.text(
                    "INSERT INTO core.model_instances "
                    "(id, artifact_id, runtime_type, expected_gpu_uuids, tensor_parallel_size, "
                    "context_length, gpu_memory_utilization, name, internal_model_name, status, "
                    "error_code, internal_endpoint, config_version, created_at, updated_at) "
                    "VALUES (:id, :artifact_id, :runtime_type, CAST(:gpus AS jsonb), 1, 4096, "
                    "0.9, :display_name, :internal_name, :status, NULL, :endpoint, 1, "
                    "clock_timestamp(), clock_timestamp())"
                ),
                {
                    "id": instance_id,
                    "artifact_id": ARTIFACT_ID,
                    "runtime_type": RuntimeType.VLLM.value,
                    "gpus": json.dumps([gpu_uuid]),
                    "display_name": f"FX instance {instance_id}",
                    "internal_name": internal_name,
                    "status": ModelInstanceStatus.RUNNING.value,
                    "endpoint": endpoint,
                },
            )
            connection.execute(
                sa.text(
                    "INSERT INTO core.gpu_allocations "
                    "(id, instance_id, gpu_uuid, status, created_at, updated_at, released_at) "
                    "VALUES (:id, :instance_id, :gpu_uuid, 'Allocated', "
                    "clock_timestamp(), clock_timestamp(), NULL)"
                ),
                {
                    "id": UUID(int=instance_id.int + 100),
                    "instance_id": instance_id,
                    "gpu_uuid": gpu_uuid,
                },
            )


@asynccontextmanager
async def _environment(
    fixture: EphemeralPostgres,
    *,
    listen: bool = True,
) -> AsyncIterator[_Environment]:
    url = _async_url(fixture)
    engine = create_database_engine(url, connect_timeout_seconds=2)
    factory = create_session_factory(engine)
    keyring = ApiKeyPepperKeyring(current_key_id="m4-04", keys={"m4-04": b"K" * 32})
    users = UserManagementService(session_factory=factory)
    keys = ApiKeyManagementService(session_factory=factory, keyring=keyring)
    services = AiServiceManagementService(session_factory=factory)
    instances = ModelInstanceService(session_factory=factory, agent=_StaticAgent())
    controller = PostgresControllerStore(factory)
    manager: RouteSnapshotManager | None = None
    upstream_hosts: list[str] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        upstream_hosts.append(request.url.host or "")
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            content=b"data: "
            + json.dumps(
                {
                    "id": "chatcmpl-m4-04",
                    "object": "chat.completion.chunk",
                    "model": payload["model"],
                    "choices": [{"delta": {"role": "assistant", "content": "ok"}}],
                }
            ).encode()
            + b"\n\ndata: [DONE]\n\n",
        )

    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream), timeout=None)
    try:
        async with factory() as session, session.begin():
            for user_id, username, role in (
                (ADMIN_ID, "m4-04-admin", UserRole.ADMIN),
                (MEMBER_ID, "m4-04-member", UserRole.USER),
            ):
                # Fixed IDs make the service-layer assertions readable while the
                # repository path still emits the same route invalidation event.
                await session.execute(
                    sa.text(
                        "INSERT INTO core.users "
                        "(id, username_norm, display_name, role, status, password_hash, "
                        "must_change_password, created_at, updated_at, deleted_at) VALUES "
                        "(:id, :username, :username, :role, 'active', :hash, false, "
                        "clock_timestamp(), clock_timestamp(), NULL)"
                    ),
                    {
                        "id": user_id,
                        "username": username,
                        "role": role.value,
                        "hash": "$argon2id$m4-04-fixture",
                    },
                )
        service = await services.create(
            CreateAiService("m4-04-model", "M4-04", True, INSTANCE_A),
            actor_id=ADMIN_ID,
            request_id=UUID("75000000-0000-4000-8000-000000000007"),
        )
        await users.replace_user_permissions(
            MEMBER_ID,
            (service.id,),
            actor_id=ADMIN_ID,
            request_id=UUID("75000000-0000-4000-8000-000000000008"),
        )
        created_key = await keys.create(
            CreatePersonalApiKey("M4-04", None, (service.id,)),
            user_id=MEMBER_ID,
            user_role=UserRole.USER,
            request_id=UUID("75000000-0000-4000-8000-000000000009"),
        )
        snapshot_source = PostgresRouteSnapshotSource(factory)
        manager = RouteSnapshotManager(
            snapshot_source,
            listener=PsycopgNotificationListener(url) if listen else None,
            poll_seconds=0.1,
            max_age_seconds=60.0,
        )
        await manager.start()
        gateway = create_app(
            authenticator=ApiKeyAuthenticator(
                lookup=SnapshotApiKeyValidationLookup(manager),
                keyring=keyring,
                availability_check=manager.ensure_available,
            ),
            routes=manager,
            runtime_capabilities={
                "vllm": RuntimeRequestCapabilities(
                    openai_chat_completions=frozenset({"model", "messages"})
                )
            },
            upstream_client=upstream_client,
        )
        yield _Environment(
            factory=factory,
            engine=engine,
            manager=manager,
            snapshot_source=snapshot_source,
            user_service=users,
            api_key_service=keys,
            ai_service=services,
            instance_service=instances,
            controller=controller,
            service_id=service.id,
            key_id=created_key.record.id,
            plaintext_key=created_key.plaintext,
            gateway=gateway,
            upstream=upstream_client,
            upstream_hosts=upstream_hosts,
        )
    finally:
        if manager is not None:
            await manager.stop()
        await upstream_client.aclose()
        await engine.dispose()


async def _call(environment: _Environment) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=environment.gateway),
        base_url="http://gateway",
    ) as client:
        return await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {environment.plaintext_key}"},
            json={"model": "m4-04-model", "messages": []},
        )


async def _wait_for(
    environment: _Environment,
    *,
    status: int,
    code: str | None,
) -> float:
    started = time.monotonic()
    while True:
        response = await _call(environment)
        actual_code = None if response.status_code == 200 else response.json()["error"]["code"]
        if response.status_code == status and actual_code == code:
            elapsed = time.monotonic() - started
            assert elapsed <= 2.0
            return elapsed
        if time.monotonic() - started > 2.0:
            raise AssertionError(
                f"Gateway did not reach {status}/{code}; last={response.status_code}/{actual_code}"
            )
        await asyncio.sleep(0.02)


def test_M4_04_key_revocation_changes_gateway_within_two_seconds(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _seed(ephemeral_postgres)

    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as environment:
            assert (await _call(environment)).status_code == 200
            await environment.api_key_service.revoke(
                environment.key_id,
                user_id=MEMBER_ID,
                request_id=UUID("75000000-0000-4000-8000-000000000010"),
            )
            await _wait_for(environment, status=401, code="invalid_api_key")

    asyncio.run(scenario())


def test_M4_04_user_disable_and_enable_change_gateway_within_two_seconds(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _seed(ephemeral_postgres)

    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as environment:
            await environment.user_service.disable_user(
                MEMBER_ID,
                actor_id=ADMIN_ID,
                request_id=UUID("75000000-0000-4000-8000-000000000011"),
            )
            await _wait_for(environment, status=401, code="invalid_api_key")
            await environment.user_service.enable_user(
                MEMBER_ID,
                actor_id=ADMIN_ID,
                request_id=UUID("75000000-0000-4000-8000-000000000012"),
            )
            await _wait_for(environment, status=200, code=None)

    asyncio.run(scenario())


def test_M4_04_permission_revoke_and_restore_poll_without_listen_within_two_seconds(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _seed(ephemeral_postgres)

    async def scenario() -> None:
        async with _environment(ephemeral_postgres, listen=False) as environment:
            await environment.user_service.replace_user_permissions(
                MEMBER_ID,
                (),
                actor_id=ADMIN_ID,
                request_id=UUID("75000000-0000-4000-8000-000000000013"),
            )
            await _wait_for(environment, status=403, code="service_not_authorized")
            await environment.user_service.replace_service_permissions(
                environment.service_id,
                (MEMBER_ID,),
                actor_id=ADMIN_ID,
                request_id=UUID("75000000-0000-4000-8000-000000000014"),
            )
            await _wait_for(environment, status=200, code=None)

    asyncio.run(scenario())


def test_M4_04_service_disable_and_enable_change_gateway_within_two_seconds(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _seed(ephemeral_postgres)

    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as environment:
            await environment.ai_service.update(
                environment.service_id,
                UpdateAiService(enabled=False),
                actor_id=ADMIN_ID,
                request_id=UUID("75000000-0000-4000-8000-000000000015"),
            )
            await _wait_for(environment, status=503, code="service_disabled")
            await environment.ai_service.update(
                environment.service_id,
                UpdateAiService(enabled=True),
                actor_id=ADMIN_ID,
                request_id=UUID("75000000-0000-4000-8000-000000000016"),
            )
            await _wait_for(environment, status=200, code=None)

    asyncio.run(scenario())


def test_M4_04_instance_stop_and_start_change_gateway_within_two_seconds(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _seed(ephemeral_postgres)

    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as environment:
            await environment.instance_service.stop(INSTANCE_A)
            await _wait_for(environment, status=503, code="backend_unavailable")
            assert await environment.controller.mark_stopped(INSTANCE_A)
            await environment.instance_service.start(INSTANCE_A)
            assert await environment.controller.set_endpoint(
                INSTANCE_A,
                expected_status=ModelInstanceStatus.STARTING,
                endpoint=ENDPOINT_A,
            )
            assert await environment.controller.mark_running(INSTANCE_A)
            await _wait_for(environment, status=200, code=None)

    asyncio.run(scenario())


def test_M4_04_rebind_sends_new_requests_to_new_backend_within_two_seconds(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _seed(ephemeral_postgres)

    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as environment:
            assert (await _call(environment)).status_code == 200
            assert environment.upstream_hosts[-1] == "runtime-a.internal"
            await environment.ai_service.update(
                environment.service_id,
                UpdateAiService(primary_instance_id=INSTANCE_B),
                actor_id=ADMIN_ID,
                request_id=UUID("75000000-0000-4000-8000-000000000017"),
            )
            await _wait_for(environment, status=200, code=None)
            deadline = time.monotonic() + 2.0
            while environment.upstream_hosts[-1] != "runtime-b.internal":
                assert time.monotonic() < deadline
                await asyncio.sleep(0.02)
                await _call(environment)
            assert environment.manager.snapshot.routes_by_model["m4-04-model"].route_version == 2

    asyncio.run(scenario())


def test_M4_04_hot_authentication_and_routing_do_not_open_database_sessions(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _seed(ephemeral_postgres)

    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as environment:

            def explode() -> object:
                raise AssertionError("Gateway hot path opened a PostgreSQL session")

            environment.snapshot_source._session_factory = cast(AsyncSessionFactory, explode)

            response = await _call(environment)
            assert response.status_code == 200
            assert environment.upstream_hosts[-1] == "runtime-a.internal"

    asyncio.run(scenario())

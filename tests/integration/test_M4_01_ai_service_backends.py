"""M4-01: AI Service Backend history, atomic rebinds, and active-only readers."""

from __future__ import annotations

import asyncio
from typing import Final
from uuid import UUID

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from conftest import EphemeralPostgres
from holm_contracts.agent.v1 import agent_pb2
from holm_control_api.agent_client import AgentSnapshot
from holm_control_api.ai_service_service import (
    AiServiceManagementService,
    CreateAiService,
    UpdateAiService,
)
from holm_control_api.model_instance_service import (
    ModelInstanceInvalidState,
    ModelInstanceReferenced,
    ModelInstanceService,
)
from holm_control_api.telemetry_service import TelemetryService
from holm_domain import (
    ArtifactStorageType,
    MetadataStatus,
    ModelArtifactStatus,
    ModelInstanceStatus,
    ModelOriginType,
    RuntimeType,
    UserRole,
)
from holm_gateway.routing import PostgresRouteSource  # type: ignore[import-untyped]
from holm_persistence import (
    AsyncSessionFactory,
    UserManagementRepository,
    create_database_engine,
    create_session_factory,
)
from migration_test_support import ALEMBIC_CONFIG_PATH, upgrade_head
from sqlalchemy.engine import URL, Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

MODEL_ID: Final = UUID("61000000-0000-4000-8000-000000000001")
ARTIFACT_ID: Final = UUID("62000000-0000-4000-8000-000000000001")
INSTANCE_A: Final = UUID("63000000-0000-4000-8000-000000000001")
INSTANCE_B: Final = UUID("63000000-0000-4000-8000-000000000002")
INSTANCE_C: Final = UUID("63000000-0000-4000-8000-000000000003")
ACTOR_ID: Final = UUID("64000000-0000-4000-8000-000000000001")


class _Agent:
    async def observe(self) -> AgentSnapshot:
        return AgentSnapshot(
            host_info=agent_pb2.GetHostInfoResponse(),
            gpu_inventory=agent_pb2.ListGPUsResponse(
                discovery_state=agent_pb2.GPU_DISCOVERY_STATE_READY,
                devices=[],
            ),
        )


def _async_url(fixture: EphemeralPostgres) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username="ubuntu",
        host=str(fixture.socket_directory),
        port=fixture.port,
        database="postgres",
    )


def _alembic_config(connection: Connection) -> Config:
    return Config(ALEMBIC_CONFIG_PATH, attributes={"connection": connection})


def _seed_model_instances(fixture: EphemeralPostgres, *, legacy: bool = False) -> None:
    with fixture.engine.begin() as connection:
        connection.execute(
            sa.text("TRUNCATE core.ai_services, core.models, core.audit_events CASCADE")
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
                "source_key": "/models/m4-01",
                "display_name": "M4-01 Model",
                "path": "/models/m4-01",
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
                "repository_id": "m4-01",
                "revision": "1" * 40,
                "storage_type": ArtifactStorageType.EXTERNAL.value,
                "status": ModelArtifactStatus.READY.value,
                "path": "/models/m4-01",
                "manifest_hash": "2" * 40,
            },
        )
        for number, instance_id, status in (
            (1, INSTANCE_A, ModelInstanceStatus.RUNNING),
            (2, INSTANCE_B, ModelInstanceStatus.RUNNING),
            (3, INSTANCE_C, ModelInstanceStatus.STOPPED),
        ):
            connection.execute(
                sa.text(
                    "INSERT INTO core.model_instances "
                    "(id, artifact_id, runtime_type, expected_gpu_uuids, tensor_parallel_size, "
                    "context_length, gpu_memory_utilization, "
                    + ("" if legacy else "name, ")
                    + "internal_model_name, status, error_code, internal_endpoint, config_version, "
                    "created_at, updated_at) VALUES "
                    "(:id, :artifact_id, :runtime_type, CAST(:gpus AS jsonb), 1, 4096, 0.9, "
                    + ("" if legacy else ":display_name, ")
                    + ":internal_name, :status, NULL, :endpoint, 1, "
                    "clock_timestamp(), clock_timestamp())"
                ),
                {
                    "id": instance_id,
                    "artifact_id": ARTIFACT_ID,
                    "runtime_type": RuntimeType.VLLM.value,
                    "gpus": f'["GPU-M4-01-{number}"]',
                    "display_name": f"FX instance {number}",
                    "internal_name": f"mi_{number:012x}",
                    "status": status.value,
                    "endpoint": None
                    if status is ModelInstanceStatus.STOPPED
                    else f"127.0.0.1:91{number:02d}",
                },
            )


def _prepare(fixture: EphemeralPostgres) -> None:
    upgrade_head(fixture.engine)
    _seed_model_instances(fixture)


async def _factory(fixture: EphemeralPostgres) -> tuple[AsyncSessionFactory, AsyncEngine]:
    engine = create_database_engine(_async_url(fixture), connect_timeout_seconds=5)
    return create_session_factory(engine), engine


async def _create_service(
    factory: AsyncSessionFactory,
    *,
    name: str = "m4-01-service",
    instance_id: UUID = INSTANCE_A,
) -> tuple[AiServiceManagementService, UUID]:
    service = AiServiceManagementService(session_factory=factory)
    record = await service.create(
        CreateAiService(
            public_model_name=name,
            description="M4-01",
            enabled=True,
            primary_instance_id=instance_id,
        ),
        actor_id=ACTOR_ID,
        request_id=UUID("65000000-0000-4000-8000-000000000001"),
    )
    return service, record.id


async def _set_instance_status(
    factory: AsyncSessionFactory,
    instance_id: UUID,
    status: ModelInstanceStatus,
    *,
    endpoint: str | None = None,
) -> None:
    async with factory() as session, session.begin():
        await session.execute(
            sa.text(
                "UPDATE core.model_instances SET status = :status, internal_endpoint = :endpoint, "
                "updated_at = clock_timestamp() WHERE id = :id"
            ),
            {"id": instance_id, "status": status.value, "endpoint": endpoint},
        )


def test_M4_01_create_rejects_non_running_target_without_writes(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _prepare(ephemeral_postgres)

    async def scenario() -> None:
        factory, engine = await _factory(ephemeral_postgres)
        try:
            service = AiServiceManagementService(session_factory=factory)
            with pytest.raises(ModelInstanceInvalidState):
                await service.create(
                    CreateAiService("not-running", "", True, INSTANCE_C),
                    actor_id=ACTOR_ID,
                    request_id=UUID("65000000-0000-4000-8000-000000000002"),
                )
            async with factory() as session:
                assert await session.scalar(sa.text("SELECT count(*) FROM core.ai_services")) == 0
                assert (
                    await session.scalar(sa.text("SELECT count(*) FROM core.ai_service_backends"))
                    == 0
                )
                assert await session.scalar(sa.text("SELECT count(*) FROM core.audit_events")) == 0
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_M4_01_non_running_rebind_preserves_backend_and_route_version(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _prepare(ephemeral_postgres)

    async def scenario() -> None:
        factory, engine = await _factory(ephemeral_postgres)
        try:
            service, service_id = await _create_service(factory)
            with pytest.raises(ModelInstanceInvalidState):
                await service.update(
                    service_id,
                    UpdateAiService(primary_instance_id=INSTANCE_C),
                    actor_id=ACTOR_ID,
                    request_id=UUID("65000000-0000-4000-8000-000000000003"),
                )
            async with factory() as session:
                row = (
                    (
                        await session.execute(
                            sa.text(
                                "SELECT service.route_version, backend.instance_id, "
                                "backend.retired_at "
                                "FROM core.ai_services AS service "
                                "JOIN core.ai_service_backends AS backend "
                                "ON backend.service_id = service.id "
                                "WHERE service.id = :id"
                            ),
                            {"id": service_id},
                        )
                    )
                    .mappings()
                    .one()
                )
                assert row["route_version"] == 1
                assert row["instance_id"] == INSTANCE_A
                assert row["retired_at"] is None
                assert await session.scalar(sa.text("SELECT count(*) FROM core.audit_events")) == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_M4_01_same_binding_resubmitted_with_stopped_backend_still_saves_description(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    """The service settings form always resubmits the current binding (service-pages.tsx)."""

    _prepare(ephemeral_postgres)

    async def scenario() -> None:
        factory, engine = await _factory(ephemeral_postgres)
        try:
            service, service_id = await _create_service(factory)
            await _set_instance_status(factory, INSTANCE_A, ModelInstanceStatus.STOPPED)
            updated = await service.update(
                service_id,
                UpdateAiService(
                    description="edited while stopped",
                    primary_instance_id=INSTANCE_A,
                ),
                actor_id=ACTOR_ID,
                request_id=UUID("65000000-0000-4000-8000-000000000009"),
            )
            assert updated.description == "edited while stopped"
            assert updated.primary_instance_id == INSTANCE_A
            assert updated.route_version == 1
            async with factory() as session:
                backend_rows = await session.scalar(
                    sa.text("SELECT count(*) FROM core.ai_service_backends WHERE service_id = :id"),
                    {"id": service_id},
                )
                assert backend_rows == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_M4_01_rebind_retires_history_noop_keeps_version_and_audit_is_allowlisted(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _prepare(ephemeral_postgres)

    async def scenario() -> None:
        factory, engine = await _factory(ephemeral_postgres)
        try:
            service, service_id = await _create_service(factory)
            rebound = await service.update(
                service_id,
                UpdateAiService(primary_instance_id=INSTANCE_B),
                actor_id=ACTOR_ID,
                request_id=UUID("65000000-0000-4000-8000-000000000004"),
            )
            noop = await service.update(
                service_id,
                UpdateAiService(description="changed", primary_instance_id=INSTANCE_B),
                actor_id=ACTOR_ID,
                request_id=UUID("65000000-0000-4000-8000-000000000005"),
            )
            assert rebound.route_version == noop.route_version == 2
            async with factory() as session:
                rows = (
                    (
                        await session.execute(
                            sa.text(
                                "SELECT instance_id, retired_at FROM core.ai_service_backends "
                                "WHERE service_id = :id ORDER BY joined_at, id"
                            ),
                            {"id": service_id},
                        )
                    )
                    .mappings()
                    .all()
                )
                assert len(rows) == 2
                assert rows[0]["instance_id"] == INSTANCE_A and rows[0]["retired_at"] is not None
                assert rows[1]["instance_id"] == INSTANCE_B and rows[1]["retired_at"] is None
                details = tuple(
                    (
                        await session.execute(
                            sa.text(
                                "SELECT details::text FROM core.audit_events "
                                "WHERE action = 'ai_service.change' ORDER BY created_at"
                            )
                        )
                    ).scalars()
                )
                assert details == ("{}", "{}", "{}")
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_M4_01_database_rejects_second_active_primary(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _prepare(ephemeral_postgres)

    async def create() -> UUID:
        factory, engine = await _factory(ephemeral_postgres)
        try:
            _, service_id = await _create_service(factory)
            return service_id
        finally:
            await engine.dispose()

    service_id = asyncio.run(create())
    with pytest.raises(IntegrityError) as failure, ephemeral_postgres.engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO core.ai_service_backends "
                "(id, service_id, instance_id, instance_name_snapshot, runtime_snapshot, "
                "backend_key, weight, enabled, health_gate_passed, joined_at, retired_at) VALUES "
                "(:id, :service_id, :instance_id, 'mi_000000000002', 'vllm', 'primary', 100, "
                "true, true, clock_timestamp(), NULL)"
            ),
            {
                "id": UUID("66000000-0000-4000-8000-000000000001"),
                "service_id": service_id,
                "instance_id": INSTANCE_B,
            },
        )
    diagnostic = getattr(failure.value.orig, "diag", None)
    assert getattr(diagnostic, "constraint_name", None) == "uq_active_service_backend_key"


def test_M4_01_gateway_routes_only_the_new_active_backend_after_rebind(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _prepare(ephemeral_postgres)

    async def scenario() -> None:
        factory, engine = await _factory(ephemeral_postgres)
        try:
            service, service_id = await _create_service(factory)
            await service.update(
                service_id,
                UpdateAiService(primary_instance_id=INSTANCE_B),
                actor_id=ACTOR_ID,
                request_id=UUID("65000000-0000-4000-8000-000000000006"),
            )
            routes = await PostgresRouteSource(factory).list_routes()
            assert len(routes) == 1
            assert routes[0].service_id == service_id
            assert routes[0].instance_id == INSTANCE_B
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_UI_45_M4_01_authorized_service_list_only_uses_the_new_active_backend(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _prepare(ephemeral_postgres)

    async def scenario() -> None:
        factory, engine = await _factory(ephemeral_postgres)
        try:
            from test_PROTO_05_services_api_keys import (
                ADMIN_USERNAME,
                _create_accounts,
                _environment,
            )

            async with _environment(ephemeral_postgres) as env:
                user_id = (await _create_accounts(env))[ADMIN_USERNAME]
            service, _ = await _create_service(factory)
            record = (await service.list())[0]
            await service.update(
                record.id,
                UpdateAiService(primary_instance_id=INSTANCE_B),
                actor_id=ACTOR_ID,
                request_id=UUID("65000000-0000-4000-8000-000000000007"),
            )
            await _set_instance_status(factory, INSTANCE_A, ModelInstanceStatus.STOPPED)
            async with factory() as session, session.begin():
                await session.execute(
                    sa.text(
                        "INSERT INTO core.user_service_permissions "
                        "(user_id, service_id, created_at, created_by) "
                        "VALUES (:user_id, :service_id, now(), :user_id)"
                    ),
                    {"user_id": user_id, "service_id": record.id},
                )
                items = await UserManagementRepository(session).list_personal_services(
                    user_id=user_id,
                    role=UserRole.ADMIN,
                )
            assert len(items) == 1
            assert items[0].public_model_name == "m4-01-service"
            assert items[0].available is True
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_M4_01_telemetry_availability_only_uses_the_new_active_backend(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _prepare(ephemeral_postgres)

    async def scenario() -> None:
        factory, engine = await _factory(ephemeral_postgres)
        try:
            services, service_id = await _create_service(factory)
            await services.update(
                service_id,
                UpdateAiService(primary_instance_id=INSTANCE_B),
                actor_id=ACTOR_ID,
                request_id=UUID("65000000-0000-4000-8000-000000000008"),
            )
            await _set_instance_status(factory, INSTANCE_A, ModelInstanceStatus.STOPPED)
            await _set_instance_status(factory, INSTANCE_B, ModelInstanceStatus.RUNNING)
            telemetry = TelemetryService(
                agent=_Agent(),
                instances=ModelInstanceService(session_factory=factory, agent=_Agent()),
                ai_services=services,
                interval_seconds=30,
                jitter_seconds=0,
            )
            await telemetry.collect_once()
            overview = telemetry.overview()
            assert overview.values.services_total == 1
            assert overview.values.services_available == 1
            assert overview.values.services_unavailable == 0
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_M4_01_instance_delete_guard_ignores_history_and_preserves_null_snapshot(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _prepare(ephemeral_postgres)

    async def scenario() -> None:
        factory, engine = await _factory(ephemeral_postgres)
        try:
            services, service_id = await _create_service(factory)
            await services.update(
                service_id,
                UpdateAiService(primary_instance_id=INSTANCE_B),
                actor_id=ACTOR_ID,
                request_id=UUID("65000000-0000-4000-8000-000000000009"),
            )
            await _set_instance_status(factory, INSTANCE_A, ModelInstanceStatus.STOPPED)
            await _set_instance_status(factory, INSTANCE_B, ModelInstanceStatus.STOPPED)
            instances = ModelInstanceService(session_factory=factory, agent=_Agent())
            await instances.delete(INSTANCE_A)
            with pytest.raises(ModelInstanceReferenced):
                await instances.delete(INSTANCE_B)
            async with factory() as session:
                rows = (
                    (
                        await session.execute(
                            sa.text(
                                "SELECT instance_id, instance_name_snapshot, retired_at "
                                "FROM core.ai_service_backends WHERE service_id = :id "
                                "ORDER BY joined_at, id"
                            ),
                            {"id": service_id},
                        )
                    )
                    .mappings()
                    .all()
                )
                assert rows[0]["instance_id"] is None
                assert rows[0]["instance_name_snapshot"] == "FX instance 1"
                assert rows[0]["retired_at"] is not None
                assert rows[1]["instance_id"] == INSTANCE_B
                assert rows[1]["retired_at"] is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_M4_01_migration_backfills_and_round_trips_existing_service(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    with ephemeral_postgres.engine.connect() as connection:
        command.downgrade(_alembic_config(connection), "20260916_0016")
    _seed_model_instances(ephemeral_postgres, legacy=True)
    service_id = UUID("67000000-0000-4000-8000-000000000001")
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO core.ai_services "
                "(id, public_model_name, description, enabled, primary_instance_id, route_version, "
                "created_at, updated_at, deleted_at) VALUES "
                "(:id, 'm4-01-existing', '', true, :instance_id, 1, "
                "clock_timestamp(), clock_timestamp(), NULL)"
            ),
            {"id": service_id, "instance_id": INSTANCE_A},
        )

    with ephemeral_postgres.engine.connect() as connection:
        command.upgrade(_alembic_config(connection), "20260917_0017")
    with ephemeral_postgres.engine.connect() as connection:
        first = (
            connection.execute(
                sa.text(
                    "SELECT instance_id, backend_key, weight, enabled, retired_at "
                    "FROM core.ai_service_backends WHERE service_id = :id"
                ),
                {"id": service_id},
            )
            .mappings()
            .one()
        )
        assert first["instance_id"] == INSTANCE_A
        assert (first["backend_key"], first["weight"], first["enabled"]) == (
            "primary",
            100,
            True,
        )
        assert first["retired_at"] is None
        index_names = frozenset(
            connection.execute(
                sa.text(
                    "SELECT indexname FROM pg_indexes WHERE schemaname = 'core' "
                    "AND tablename = 'ai_service_backends'"
                )
            ).scalars()
        )
        assert {
            "uq_active_service_backend_key",
            "uq_active_service_backend_instance",
        } <= index_names
        assert (
            connection.scalar(
                sa.text(
                    "SELECT count(*) FROM pg_constraint WHERE conname = "
                    "'ck_active_backend_has_instance'"
                )
            )
            == 1
        )
        assert (
            connection.scalar(
                sa.text(
                    "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'core' "
                    "AND table_name = 'ai_services' AND column_name = 'primary_instance_id'"
                )
            )
            == 0
        )

    with ephemeral_postgres.engine.connect() as connection:
        command.downgrade(_alembic_config(connection), "20260916_0016")
        assert (
            connection.scalar(
                sa.text("SELECT primary_instance_id FROM core.ai_services WHERE id = :id"),
                {"id": service_id},
            )
            == INSTANCE_A
        )
        assert connection.scalar(sa.text("SELECT to_regclass('core.ai_service_backends')")) is None

    with ephemeral_postgres.engine.connect() as connection:
        command.upgrade(_alembic_config(connection), "head")
        assert (
            connection.scalar(
                sa.text(
                    "SELECT instance_id FROM core.ai_service_backends "
                    "WHERE service_id = :id AND retired_at IS NULL"
                ),
                {"id": service_id},
            )
            == INSTANCE_A
        )


def test_M4_01_concurrent_rebinds_leave_exactly_one_active_primary(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _prepare(ephemeral_postgres)

    async def scenario() -> None:
        factory, engine = await _factory(ephemeral_postgres)
        try:
            services, service_id = await _create_service(factory)

            async def rebind(instance_id: UUID, request_id: UUID) -> None:
                await services.update(
                    service_id,
                    UpdateAiService(primary_instance_id=instance_id),
                    actor_id=ACTOR_ID,
                    request_id=request_id,
                )

            await _set_instance_status(
                factory,
                INSTANCE_C,
                ModelInstanceStatus.RUNNING,
                endpoint="127.0.0.1:9103",
            )
            await asyncio.gather(
                rebind(INSTANCE_B, UUID("65000000-0000-4000-8000-000000000010")),
                rebind(INSTANCE_C, UUID("65000000-0000-4000-8000-000000000011")),
            )
            async with factory() as session:
                route_version = await session.scalar(
                    sa.text("SELECT route_version FROM core.ai_services WHERE id = :id"),
                    {"id": service_id},
                )
                active = tuple(
                    (
                        await session.execute(
                            sa.text(
                                "SELECT instance_id FROM core.ai_service_backends "
                                "WHERE service_id = :id AND retired_at IS NULL"
                            ),
                            {"id": service_id},
                        )
                    ).scalars()
                )
                total = await session.scalar(
                    sa.text("SELECT count(*) FROM core.ai_service_backends WHERE service_id = :id"),
                    {"id": service_id},
                )
                assert route_version == 3
                assert len(active) == 1 and active[0] in {INSTANCE_B, INSTANCE_C}
                assert total == 3
        finally:
            await engine.dispose()

    asyncio.run(scenario())

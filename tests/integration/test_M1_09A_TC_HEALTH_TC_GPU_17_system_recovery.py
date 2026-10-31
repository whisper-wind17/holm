"""M1-09A real PostgreSQL, real Agent UDS, and real Control API recovery."""

from __future__ import annotations

import asyncio
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, cast
from uuid import UUID

import psycopg
import pytest
from fastapi import FastAPI
from fixtures.asgi_client import NO_CONFIGURED_CREDENTIAL_ROWS, AsgiTestClient
from fixtures.fake_nvml import FixtureId, scenario_for
from holm_contracts import VersionPayload
from holm_control_api import (
    READY_PATH,
    SYSTEM_COMPONENTS_PATH,
    SYSTEM_SERVER_PATH,
    AuthenticatedSession,
    AuthenticationService,
    GrpcHostAgentClient,
    SystemStatusService,
    create_app,
    database_schema_check,
)
from holm_domain import Component, ComponentStatus, UserRole, UserStatus
from holm_host_agent import server as server_module
from holm_host_agent.authorization import IdentityCatalog
from holm_host_agent.server import HostAgentServer
from holm_persistence import (
    AuditEventDraft,
    AuditEventRecord,
    ComponentHeartbeatRepository,
    SessionRecord,
    SystemStatusRepository,
    UserRecord,
    create_database_engine,
    create_session_factory,
)
from migration_test_support import head_revision, upgrade_head
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

POSTGRESQL_BIN: Final[Path] = Path("/usr/lib/postgresql/16/bin")
RUNUSER: Final[Path] = Path("/usr/sbin/runuser")
CLUSTER_USER: Final[str] = "ubuntu"
CONTROL_API_UID: Final[int] = 21_004
NOW: Final[datetime] = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


class PostgresFixture:
    engine: Engine
    data_directory: Path
    socket_directory: Path
    port: int


class _DatabaseReader:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def read(self):  # type: ignore[no-untyped-def]
        async with self._factory() as session, session.begin():
            return await SystemStatusRepository(session).read()


class _AuditWriter:
    async def add(self, session: AsyncSession, event: AuditEventDraft) -> AuditEventRecord:
        raise AssertionError("integration authenticator has no business transaction")

    async def write(self, event: AuditEventDraft) -> AuditEventRecord:
        return cast(AuditEventRecord, object())


class _Authenticator:
    def __init__(self) -> None:
        user = UserRecord(
            id=UUID("00000000-0000-7000-8000-000000000911"),
            username_norm="m1-09a-admin",
            display_name="M1-09A Admin",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            must_change_password=False,
            created_at=NOW,
            updated_at=NOW,
            deleted_at=None,
            password_hash="$argon2id$not-returned",
        )
        self.authenticated = AuthenticatedSession(
            user=user,
            session=SessionRecord(
                id=UUID("00000000-0000-7000-8000-000000000912"),
                user_id=user.id,
                session_hash="a" * 64,
                csrf_hash="b" * 64,
                created_at=NOW,
                last_seen_at=NOW,
                expires_at=NOW + timedelta(hours=1),
                revoked_at=None,
            ),
        )
        self.audit_writer = _AuditWriter()

    async def authenticate_session(
        self,
        *,
        session_token: str | None,
        csrf_token: str | None = None,
        require_csrf: bool = False,
    ) -> AuthenticatedSession:
        return self.authenticated


def _conninfo(postgres: PostgresFixture) -> str:
    return psycopg.conninfo.make_conninfo(
        host=str(postgres.socket_directory),
        port=postgres.port,
        user=CLUSTER_USER,
        dbname="postgres",
        connect_timeout=2,
    )


def _schema_reader(conninfo: str):  # type: ignore[no-untyped-def]
    async def read_revision() -> str | None:
        connection = await psycopg.AsyncConnection.connect(conninfo, autocommit=True)
        async with connection, connection.cursor() as cursor:
            await cursor.execute("SELECT version_num FROM public.alembic_version")
            row = await cursor.fetchone()
            return None if row is None else str(row[0])

    return read_revision


def _pg_ctl(postgres: PostgresFixture, action: str) -> None:
    command = [
        str(RUNUSER),
        "--user",
        CLUSTER_USER,
        "--",
        str(POSTGRESQL_BIN / "pg_ctl"),
        "-D",
        str(postgres.data_directory),
    ]
    if action == "start":
        server_options = " ".join(
            (
                "-F",
                f"-p {postgres.port}",
                f"-k {postgres.socket_directory}",
                "-c listen_addresses=''",
                "-c max_connections=10",
            )
        )
        command.extend(
            [
                "-l",
                str(postgres.data_directory.parent / "postgres.log"),
                "-o",
                server_options,
            ]
        )
    elif action == "stop":
        command.extend(["-m", "fast"])
    else:
        raise ValueError(f"unsupported pg_ctl action: {action}")
    command.extend(["-w", action])
    subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)


def _version() -> VersionPayload:
    return VersionPayload(
        build_version="1.0.0+gm109a000002",
        commit="7" * 40,
        schema_version=1,
        runtime_compat_hash="6" * 64,
    )


def _component(payload: dict[str, object], component_id: str) -> dict[str, object]:
    components = cast(list[dict[str, object]], payload["components"])
    return next(component for component in components if component["id"] == component_id)


def test_M1_09A_TC_HEALTH_16_real_database_heartbeat_and_restart_recover_without_api_restart(
    ephemeral_postgres: PostgresFixture,
    tmp_path: Path,
) -> None:
    upgrade_head(ephemeral_postgres.engine)
    engine = create_database_engine(ephemeral_postgres.engine.url, connect_timeout_seconds=2)
    factory = create_session_factory(engine)

    async def scenario() -> None:
        service = SystemStatusService(
            agent=GrpcHostAgentClient(tmp_path / "absent-agent.sock", timeout_seconds=0.1),
            database=_DatabaseReader(factory),
            version=_version(),
        )
        app = create_app(
            version_payload=_version(),
            credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
            readiness_checks=[
                database_schema_check(
                    _schema_reader(_conninfo(ephemeral_postgres)),
                    compatible_revisions=(head_revision(),),
                )
            ],
            readiness_timeout_seconds=3,
            authentication=cast(AuthenticationService, _Authenticator()),
            system_status=service,
        )
        client = AsgiTestClient(app)
        before_start = await client.get(READY_PATH)
        assert before_start.status == 503
        assert before_start.json()["status"] == "not_ready"
        async with client.lifespan():
            ready = await client.get(READY_PATH)
            components = await client.get(SYSTEM_COMPONENTS_PATH)
            assert ready.status == 200
            assert _component(components.json(), "Database")["status"] == "ready"

            async with factory() as session, session.begin():
                await ComponentHeartbeatRepository(session).write(
                    component=Component.TELEMETRY_WORKER,
                    instance_id="telemetry-1",
                    version="1.0.0",
                    status=ComponentStatus.HEALTHY,
                )
                await session.execute(
                    text(
                        "UPDATE core.component_heartbeats "
                        "SET last_heartbeat_at = clock_timestamp() - interval '31 seconds' "
                        "WHERE component = 'telemetry-worker'"
                    )
                )
            stale = await client.get(SYSTEM_COMPONENTS_PATH)
            assert _component(stale.json(), "Telemetry Collector")["status"] == "degraded"
            assert _component(stale.json(), "Telemetry Collector")["reason"] == "heartbeat_stale"

            async with factory() as session, session.begin():
                await ComponentHeartbeatRepository(session).write(
                    component=Component.TELEMETRY_WORKER,
                    instance_id="telemetry-1",
                    version="1.0.1",
                    status=ComponentStatus.HEALTHY,
                )
            fresh = await client.get(SYSTEM_COMPONENTS_PATH)
            assert _component(fresh.json(), "Telemetry Collector")["status"] == "ready"

            await asyncio.to_thread(_pg_ctl, ephemeral_postgres, "stop")
            database_down = await client.get(READY_PATH)
            components_down = await client.get(SYSTEM_COMPONENTS_PATH)
            assert database_down.status == 503
            assert database_down.json()["dependencies"] == [
                {
                    "name": "database",
                    "status": "fail",
                    "reason": "dependency_unreachable",
                }
            ]
            assert _component(components_down.json(), "Database")["status"] == "unavailable"

            await asyncio.to_thread(_pg_ctl, ephemeral_postgres, "start")
            recovered = await client.get(READY_PATH)
            components_recovered = await client.get(SYSTEM_COMPONENTS_PATH)
            assert recovered.status == 200
            assert _component(components_recovered.json(), "Database")["status"] == "ready"
        after_stop = await client.get(READY_PATH)
        assert after_stop.status == 503
        assert after_stop.json()["status"] == "not_ready"

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(engine.dispose())


def test_M1_09A_TC_HEALTH_17_TC_GPU_17_real_agent_uds_stop_and_restart_recovers_without_api_restart(
    ephemeral_postgres: PostgresFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upgrade_head(ephemeral_postgres.engine)
    engine = create_database_engine(ephemeral_postgres.engine.url)
    factory = create_session_factory(engine)
    socket_path = tmp_path / "agent.sock"
    monkeypatch.setattr(server_module, "_read_peer_uid", lambda _socket: CONTROL_API_UID)

    def new_agent() -> HostAgentServer:
        return HostAgentServer(
            socket_path,
            identity_catalog=IdentityCatalog.from_pairs(((CONTROL_API_UID, "control-api"),)),
            nvml_factory=lambda: scenario_for(FixtureId.FX_NV_02).open(),
        )

    async def scenario() -> None:
        agent = new_agent()
        await agent.start()
        service = SystemStatusService(
            agent=GrpcHostAgentClient(socket_path, timeout_seconds=1),
            database=_DatabaseReader(factory),
            version=_version(),
        )
        app: FastAPI = create_app(
            version_payload=_version(),
            credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
            readiness_checks=[
                database_schema_check(
                    _schema_reader(_conninfo(ephemeral_postgres)),
                    compatible_revisions=(head_revision(),),
                )
            ],
            authentication=cast(AuthenticationService, _Authenticator()),
            system_status=service,
        )
        client = AsgiTestClient(app)
        try:
            async with client.lifespan():
                before = await client.get(SYSTEM_SERVER_PATH)
                assert before.status == 200
                assert before.json()["agent"]["status"] == "available"
                first_snapshot = before.json()["agent"]["gpus"]["snapshot_id"]

                await agent.stop(grace=0)
                while_down = await client.get(SYSTEM_SERVER_PATH)
                ready_while_down = await client.get(READY_PATH)
                assert while_down.json()["agent"]["status"] == "unavailable"
                assert ready_while_down.status == 200
                assert all(
                    dependency["name"] != "agent"
                    for dependency in ready_while_down.json()["dependencies"]
                )

                agent = new_agent()
                await agent.start()
                recovered = await client.get(SYSTEM_SERVER_PATH)
                assert recovered.json()["agent"]["status"] == "available"
                assert recovered.json()["agent"]["gpus"]["snapshot_id"] != first_snapshot
        finally:
            await agent.stop(grace=0)

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(engine.dispose())

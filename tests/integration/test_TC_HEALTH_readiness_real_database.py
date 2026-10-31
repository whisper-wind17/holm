"""TC-HEALTH-11: readiness against a real PostgreSQL cluster and real Alembic state.

The contract cases in ``tests/contract/test_TC_HEALTH_internal_endpoints.py``
drive the readiness logic with an injected reader. This case binds that port to
a real async psycopg connection and walks the three states an operator actually
sees: cluster up but not migrated, cluster up and migrated to head, and cluster
unreachable. The integration-test policy does not accept a Mock as the evidence for
"the dependency check is real", which is why this file exists.

It also proves the liveness rule against a genuinely dead database rather than
against a raising fake: ``/internal/health/live`` answers 200 in every state.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Final, Protocol

import psycopg
from fastapi import FastAPI
from fixtures.asgi_client import (
    NO_CONFIGURED_CREDENTIAL_ROWS,
    AsgiResponse,
    AsgiTestClient,
)
from holm_contracts import VersionPayload
from holm_control_api import (
    LIVE_PATH,
    READY_PATH,
    SchemaRevisionReader,
    create_app,
    database_schema_check,
)
from migration_test_support import head_revision, upgrade_head
from sqlalchemy.engine import Engine

CLUSTER_USER: Final[str] = "ubuntu"
CLUSTER_DATABASE: Final[str] = "postgres"
CONNECT_TIMEOUT_SECONDS: Final[int] = 2
SCHEMA_REVISION_QUERY: Final[str] = "SELECT version_num FROM public.alembic_version"
VERSION_TABLE_QUERY: Final[str] = "SELECT to_regclass('public.alembic_version')"


class PostgresSocketFixture(Protocol):
    """The part of the FX-DB-M0-05 fixture this case needs."""

    @property
    def engine(self) -> Engine: ...

    @property
    def socket_directory(self) -> Path: ...

    @property
    def port(self) -> int: ...


def _conninfo(socket_directory: Path, port: int) -> str:
    return psycopg.conninfo.make_conninfo(
        host=str(socket_directory),
        port=port,
        user=CLUSTER_USER,
        dbname=CLUSTER_DATABASE,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
    )


def _schema_revision_reader(conninfo: str) -> SchemaRevisionReader:
    """Return the real async reader bound behind the readiness port."""

    async def read_applied_schema_revision() -> str | None:
        connection = await psycopg.AsyncConnection.connect(conninfo, autocommit=True)
        async with connection, connection.cursor() as cursor:
            await cursor.execute(VERSION_TABLE_QUERY)
            table = await cursor.fetchone()
            if table is None or table[0] is None:
                return None
            await cursor.execute(SCHEMA_REVISION_QUERY)
            row = await cursor.fetchone()
            if row is None or row[0] is None:
                return None
            revision = row[0]
            if not isinstance(revision, str):
                raise RuntimeError("alembic_version.version_num must be text")
            return revision

    return read_applied_schema_revision


def _build_app(conninfo: str, compatible_revision: str) -> FastAPI:
    reader = _schema_revision_reader(conninfo)
    return create_app(
        version_payload=VersionPayload(
            build_version="1.4.0+gm1010a2222",
            commit="c" * 40,
            schema_version=1,
            runtime_compat_hash="d" * 64,
        ),
        credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
        readiness_checks=[
            database_schema_check(reader, compatible_revisions=(compatible_revision,))
        ],
        readiness_timeout_seconds=10.0,
    )


def _get_all(app: FastAPI, paths: Sequence[str]) -> tuple[AsgiResponse, ...]:
    async def scenario() -> tuple[AsgiResponse, ...]:
        client = AsgiTestClient(app)
        responses: list[AsgiResponse] = []
        async with client.lifespan():
            for path in paths:
                responses.append(await client.get(path))
        return tuple(responses)

    return asyncio.run(scenario())


def test_TC_HEALTH_11_readiness_reflects_real_database_and_migration_state(
    ephemeral_postgres: PostgresSocketFixture,
    tmp_path: Path,
) -> None:
    head = head_revision()
    conninfo = _conninfo(ephemeral_postgres.socket_directory, ephemeral_postgres.port)
    app = _build_app(conninfo, head)

    # 1. The cluster is reachable but nothing has been migrated yet.
    ready, live = _get_all(app, (READY_PATH, LIVE_PATH))
    assert ready.status == 503
    assert ready.json()["dependencies"] == [
        {"name": "database", "status": "fail", "reason": "schema_revision_missing"}
    ]
    assert live.status == 200

    # 2. After a real Alembic upgrade the applied revision matches head.
    upgrade_head(ephemeral_postgres.engine)
    ready, live = _get_all(app, (READY_PATH, LIVE_PATH))
    assert ready.status == 200
    assert ready.json() == {
        "status": "ready",
        "dependencies": [{"name": "database", "status": "pass", "reason": "ok"}],
    }
    assert live.status == 200

    # 3. A socket directory with no PostgreSQL behind it: a real connection
    #    failure, not a raised fake. Liveness must still answer 200.
    unreachable = _build_app(_conninfo(tmp_path, ephemeral_postgres.port), head)
    ready, live = _get_all(unreachable, (READY_PATH, LIVE_PATH))
    assert ready.status == 503
    assert ready.json()["dependencies"] == [
        {"name": "database", "status": "fail", "reason": "dependency_unreachable"}
    ]
    assert str(tmp_path) not in ready.text
    assert live.status == 200
    assert live.json() == {"status": "live"}

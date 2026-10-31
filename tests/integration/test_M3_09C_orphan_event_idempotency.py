"""M3-09C Orphan Domain Event versioning on real PostgreSQL."""

from __future__ import annotations

import asyncio
from uuid import UUID

import sqlalchemy as sa
from conftest import EphemeralPostgres
from holm_domain import PRODUCTION_EVENT_REGISTRY, DomainEventType, DownloadTaskStatus
from holm_instance_controller import PostgresControllerStore
from holm_persistence import (
    DomainEventRepository,
    create_database_engine,
    create_session_factory,
)
from migration_test_support import upgrade_head
from sqlalchemy.engine import URL

INSTANCE_ID = UUID("43000000-0000-4000-8000-00000000090c")


def _async_url(fixture: EphemeralPostgres) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username="ubuntu",
        host=str(fixture.socket_directory),
        port=fixture.port,
        database="postgres",
    )


def test_M3_09C_orphan_event_is_once_only_and_uses_next_aggregate_version(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    upgrade_head(ephemeral_postgres.engine)
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(sa.text("TRUNCATE core.domain_events"))

    async def scenario() -> tuple[bool, bool, list[tuple[str, int]]]:
        engine = create_database_engine(_async_url(ephemeral_postgres))
        sessions = create_session_factory(engine)
        try:
            async with sessions() as session, session.begin():
                await DomainEventRepository(session).append(
                    PRODUCTION_EVENT_REGISTRY.build_draft(
                        DomainEventType.DOWNLOAD_RESOLUTION_STARTED.value,
                        aggregate_id=INSTANCE_ID,
                        version=7,
                        candidates={"status": DownloadTaskStatus.RESOLVING.value},
                    )
                )
            store = PostgresControllerStore(sessions)
            first = await store.record_orphan_isolated(INSTANCE_ID)
            second = await store.record_orphan_isolated(INSTANCE_ID)
            async with sessions() as session:
                rows = (
                    await session.execute(
                        sa.text(
                            "SELECT event, version FROM core.domain_events "
                            "WHERE aggregate_id = :aggregate_id ORDER BY version"
                        ),
                        {"aggregate_id": INSTANCE_ID},
                    )
                ).tuples()
                return first, second, [(str(event), int(version)) for event, version in rows]
        finally:
            await engine.dispose()

    first, second, rows = asyncio.run(scenario())

    assert first
    assert not second
    assert rows == [
        (DomainEventType.DOWNLOAD_RESOLUTION_STARTED.value, 7),
        (DomainEventType.INSTANCE_RUNTIME_ORPHAN_ISOLATED.value, 8),
    ]

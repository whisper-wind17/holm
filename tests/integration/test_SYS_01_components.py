"""FX-SYS-01: real PostgreSQL heartbeats and real SQLite backlog observations."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from conftest import EphemeralPostgres
from holm_contracts import VersionPayload
from holm_control_api import SystemStatusService
from holm_control_api.agent_client import AgentUnavailable, AgentUnavailableReason
from holm_domain import Component, ComponentStatus
from holm_domain.ledger_health import LedgerBacklogDetails, decode_ledger_backlog_details
from holm_instance_controller.controller import InstanceController
from holm_instance_controller.heartbeat import ControllerHeartbeat
from holm_ledger import (
    LedgerHealthMonitor,
    ReceivedEvent,
    RequestOperation,
    RequestProtocol,
    open_ledger,
)
from holm_ledger_writer import LedgerWriter
from holm_persistence import (
    AsyncSessionFactory,
    SystemStatusRepository,
    SystemStatusSnapshot,
    create_database_engine,
    create_session_factory,
)
from holm_runtimes import load_runtime_compatibility
from migration_test_support import head_revision, upgrade_head


class _Agent:
    async def observe(self) -> AgentUnavailable:
        return AgentUnavailable(AgentUnavailableReason.CONNECTION_FAILED)


class _Database:
    def __init__(self, factory: AsyncSessionFactory) -> None:
        self.factory = factory

    async def read(self) -> SystemStatusSnapshot:
        async with self.factory() as session, session.begin():
            return await SystemStatusRepository(session).read()


def _service(
    database: _Database, monitor: LedgerHealthMonitor | None = None
) -> SystemStatusService:
    return SystemStatusService(
        agent=_Agent(),
        database=database,
        version=VersionPayload(
            build_version="1.0.0", commit="1" * 40, schema_version=1, runtime_compat_hash="2" * 64
        ),
        ledger_details=None
        if monitor is None
        else lambda: None if monitor.last_sample is None else monitor.last_sample.details,
    )


def test_SYS_01_TC_HEALTH_14_16_controller_heartbeat_and_stopping(
    ephemeral_postgres: EphemeralPostgres,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upgrade_head(ephemeral_postgres.engine)
    engine = create_database_engine(ephemeral_postgres.engine.url)
    factory = create_session_factory(engine)

    async def control_loop(self: InstanceController, stop: asyncio.Event) -> None:
        await stop.wait()

    monkeypatch.setattr(InstanceController, "_run_control_loop", control_loop)

    async def scenario() -> None:
        database = _Database(factory)
        heartbeat = ControllerHeartbeat(
            factory,
            instance_id="FX-SYS-01-controller",
            build_version="1.0.0",
            interval_seconds=0.05,
        )
        controller = InstanceController(
            store=cast(Any, object()),
            agent=cast(Any, object()),
            health=cast(Any, object()),
            heartbeat=heartbeat,
            runtime_catalog=load_runtime_compatibility(Path("runtime-compat.yaml")),
        )
        stop = asyncio.Event()
        task = asyncio.create_task(controller.run(stop))
        try:
            async with asyncio.timeout(5):
                while True:
                    snapshot = await database.read()
                    row = snapshot.heartbeats.get(Component.INSTANCE_CONTROLLER)
                    if (
                        row is not None
                        and row.instance_id == "FX-SYS-01-controller"
                        and row.status is ComponentStatus.HEALTHY
                    ):
                        break
                    await asyncio.sleep(0.01)
            response = await _service(database).read_components()
            scheduler = next(item for item in response.components if item.id == "Scheduler")
            assert scheduler.status == "ready"
            assert snapshot.schema_revision == head_revision()
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=6)
        snapshot = await database.read()
        assert snapshot.heartbeats[Component.INSTANCE_CONTROLLER].status is ComponentStatus.STOPPING
        response = await _service(database).read_components()
        scheduler = next(item for item in response.components if item.id == "Scheduler")
        assert (scheduler.status, scheduler.reason) == ("degraded", "heartbeat_stopping")

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(engine.dispose())


def test_SYS_01_TC_HEALTH_15_real_backlog_local_and_heartbeat_after_ack(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    upgrade_head(ephemeral_postgres.engine)
    engine = create_database_engine(ephemeral_postgres.engine.url)
    factory = create_session_factory(engine)
    spool = tmp_path / "spool"
    spool.mkdir(mode=0o700)
    path = spool / "request-ledger.sqlite3"
    now = datetime.now(UTC)
    events = tuple(
        ReceivedEvent(
            request_id=UUID(int=index + 1),
            event_seq=1,
            created_at=now + timedelta(seconds=index),
            received_at=now + timedelta(seconds=index),
            protocol=RequestProtocol.OPENAI,
            operation=RequestOperation.CHAT_COMPLETIONS,
            endpoint="/v1/chat/completions",
            stream=False,
            input_bytes=1,
        )
        for index in range(2)
    )

    async def scenario() -> None:
        with open_ledger(path) as ledger:
            ledger.append_batch(events)
            monitor = LedgerHealthMonitor(path)
            writer = LedgerWriter(
                ledger=ledger,
                session_factory=factory,
                engine=engine,
                instance_id="FX-SYS-01-writer",
            )
            database = _Database(factory)
            try:
                for count in (2, 1, 0):
                    expected = LedgerBacklogDetails(
                        count, events[2 - count].created_at if count else None
                    )
                    assert ledger.backlog_details() == expected
                    sample = await monitor.sample_once()
                    assert sample is not None and sample.details == expected
                    await writer.refresh_backlog_health()
                    await writer._write_heartbeat(ComponentStatus.HEALTHY)
                    snapshot = await database.read()
                    assert (
                        decode_ledger_backlog_details(
                            snapshot.heartbeats[Component.LEDGER_WRITER].reason
                        )
                        == expected
                    )
                    for service in (_service(database, monitor), _service(database)):
                        response = await service.read_components()
                        component = next(
                            item
                            for item in response.components
                            if item.id == "Request Ledger Writer"
                        )
                        details = component.details.model_dump(mode="json")
                        assert details["pending_event_count"] == {"state": "known", "value": count}
                        if count:
                            assert details["oldest_event_at"]["state"] == "known"
                            assert (
                                datetime.fromisoformat(details["oldest_event_at"]["value"])
                                == expected.oldest_event_at
                            )
                        else:
                            assert details["oldest_event_at"] == {
                                "state": "unknown",
                                "reason": "empty_value",
                            }
                    if count:
                        assert ledger.mark_acked(
                            events[2 - count].request_id, 1, acked_at=now + timedelta(seconds=3)
                        )
            finally:
                await monitor.aclose()

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(engine.dispose())

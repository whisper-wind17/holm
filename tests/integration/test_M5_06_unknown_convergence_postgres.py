"""M5-06 Unknown convergence against a real SQLite Ledger and PostgreSQL (TC-LG-05)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final
from uuid import UUID

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from holm_control_api.attention_service import (
    AttentionItem,
    AttentionKind,
    AttentionService,
)
from holm_domain import RequestFinishReason
from holm_ledger import (
    CompletedEvent,
    EventType,
    InProgressEvent,
    LedgerEvent,
    ReceivedEvent,
    RequestEventSummary,
    RequestOperation,
    RequestProtocol,
    SQLiteLedger,
    open_ledger,
)
from holm_ledger_writer import LedgerWriter
from holm_persistence import (
    create_database_engine,
    create_session_factory,
    summarize_unknown_request_records,
)
from migration_test_support import upgrade_head
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine

NOW: Final = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
LATER: Final = NOW + timedelta(hours=2)
USER_ID: Final = UUID("7c000000-0000-4000-8000-000000000001")
KEY_ID: Final = UUID("7c000000-0000-4000-8000-000000000002")
SERVICE_ID: Final = UUID("7c000000-0000-4000-8000-000000000003")
INSTANCE_ID: Final = UUID("7c000000-0000-4000-8000-000000000004")


def _uuid(value: int) -> UUID:
    return UUID(f"7d000000-0000-4000-8000-{value:012d}")


def _async_url(fixture: EphemeralPostgres) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username="ubuntu",
        host=str(fixture.socket_directory),
        port=fixture.port,
        database="postgres",
    )


@pytest.fixture(scope="module", autouse=True)
def _migrated_database(ephemeral_postgres: EphemeralPostgres) -> Iterator[None]:
    upgrade_head(ephemeral_postgres.engine)
    yield


@pytest.fixture(autouse=True)
def _empty_tables(
    ephemeral_postgres: EphemeralPostgres,
    _migrated_database: None,
) -> Iterator[None]:
    del _migrated_database
    statement = sa.text(
        "TRUNCATE usage.request_event_quarantine, usage.request_records, "
        "usage.request_id_registry, core.component_heartbeats"
    )
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(statement)
    yield
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(statement)


def _ledger(tmp_path: Path) -> SQLiteLedger:
    directory = tmp_path / "spool"
    directory.mkdir(mode=0o700, exist_ok=True)
    return open_ledger(directory / "request-ledger.sqlite3")


def _received(request_id: UUID, received_at: datetime = NOW) -> ReceivedEvent:
    return ReceivedEvent(
        request_id=request_id,
        event_seq=1,
        created_at=received_at,
        received_at=received_at,
        protocol=RequestProtocol.OPENAI,
        operation=RequestOperation.CHAT_COMPLETIONS,
        endpoint="/v1/chat/completions",
        stream=True,
        input_bytes=64,
    )


def _in_progress(request_id: UUID, received_at: datetime = NOW) -> InProgressEvent:
    return InProgressEvent(
        request_id=request_id,
        event_seq=2,
        created_at=received_at + timedelta(seconds=1),
        received_at=received_at,
        user_id=USER_ID,
        api_key_id=KEY_ID,
        service_id=SERVICE_ID,
        instance_id=INSTANCE_ID,
        requested_max_tokens=4096,
    )


def _completed(request_id: UUID, received_at: datetime = NOW) -> CompletedEvent:
    return CompletedEvent(
        request_id=request_id,
        event_seq=3,
        created_at=received_at + timedelta(seconds=2),
        received_at=received_at,
        input_tokens=3,
        output_tokens=5,
        ttft_ms=10,
        duration_ms=20,
        finish_reason=RequestFinishReason.STOP,
        http_status=200,
    )


def _writer(
    fixture: EphemeralPostgres,
    ledger: SQLiteLedger,
    *,
    clock: Callable[[], datetime] = lambda: LATER,
    unknown_scan_seconds: float = 300.0,
) -> tuple[LedgerWriter, AsyncEngine]:
    engine = create_database_engine(_async_url(fixture), connect_timeout_seconds=5)
    writer = LedgerWriter(
        ledger=ledger,
        session_factory=create_session_factory(engine),
        engine=engine,
        poll_seconds=0.2,
        heartbeat_seconds=0.1,
        instance_id="m5-06-integration",
        build_version="m5-06-test",
        unknown_after_seconds=3900.0,
        unknown_scan_seconds=unknown_scan_seconds,
        clock=clock,
    )
    return writer, engine


async def _replay(ledger: SQLiteLedger, writer: LedgerWriter) -> None:
    while ledger.list_unacked(limit=500):
        await writer.replay_once()


def _record(fixture: EphemeralPostgres, request_id: UUID) -> dict[str, object]:
    with fixture.engine.connect() as connection:
        row = (
            connection.execute(
                sa.text("SELECT * FROM usage.request_records WHERE request_id = :request_id"),
                {"request_id": request_id},
            )
            .mappings()
            .one()
        )
    return dict(row)


def _event_types(ledger: SQLiteLedger, request_id: UUID) -> list[str]:
    return [
        str(row[0])
        for row in ledger.connection.execute(
            "SELECT event_type FROM request_events WHERE request_id = ? ORDER BY event_seq",
            (str(request_id),),
        )
    ]


def test_M5_06_TC_LG_05_stale_in_progress_converges_to_unknown(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    request_id = _uuid(1)

    async def scenario() -> int:
        ledger = _ledger(tmp_path)
        writer, engine = _writer(ephemeral_postgres, ledger)
        try:
            ledger.append_batch((_received(request_id), _in_progress(request_id)))
            await _replay(ledger, writer)
            written = await writer.converge_unknown_once()
            await _replay(ledger, writer)
            return written
        finally:
            ledger.close()
            await engine.dispose()

    assert asyncio.run(scenario()) == 1
    record = _record(ephemeral_postgres, request_id)
    assert record["record_state"] == "unknown"
    assert record["event_seq"] == 3
    assert record["last_confirmed_event_type"] == EventType.IN_PROGRESS.value
    assert record["last_confirmed_event_seq"] == 2
    assert record["user_id"] == USER_ID
    assert record["api_key_id"] == KEY_ID
    assert record["service_id"] == SERVICE_ID
    assert record["instance_id"] == INSTANCE_ID
    assert record["requested_max_tokens"] == 4096
    assert record["finish_reason"] is None
    assert record["input_tokens"] is None
    assert record["output_tokens"] is None
    assert record["error_code"] is None
    assert record["terminal_at"] == LATER
    assert record["received_at"] == NOW


def test_M5_06_received_only_converges_with_received_as_last_confirmed(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    request_id = _uuid(2)

    async def scenario() -> list[str]:
        ledger = _ledger(tmp_path)
        writer, engine = _writer(ephemeral_postgres, ledger)
        try:
            ledger.append_batch((_received(request_id),))
            await _replay(ledger, writer)
            assert await writer.converge_unknown_once() == 1
            await _replay(ledger, writer)
            return _event_types(ledger, request_id)
        finally:
            ledger.close()
            await engine.dispose()

    assert asyncio.run(scenario()) == ["Received", "Unknown"]
    record = _record(ephemeral_postgres, request_id)
    assert record["record_state"] == "unknown"
    assert record["last_confirmed_event_type"] == EventType.RECEIVED.value
    assert record["last_confirmed_event_seq"] == 1
    assert record["user_id"] is None
    assert record["api_key_id"] is None


def test_M5_06_fresh_and_terminal_records_are_left_alone(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    fresh = _uuid(3)
    completed = _uuid(4)
    recent = LATER - timedelta(minutes=30)

    async def scenario() -> int:
        ledger = _ledger(tmp_path)
        writer, engine = _writer(ephemeral_postgres, ledger)
        try:
            ledger.append_batch(
                (
                    _received(fresh, recent),
                    _in_progress(fresh, recent),
                    _received(completed),
                    _in_progress(completed),
                    _completed(completed),
                )
            )
            await _replay(ledger, writer)
            return await writer.converge_unknown_once()
        finally:
            ledger.close()
            await engine.dispose()

    assert asyncio.run(scenario()) == 0
    assert _record(ephemeral_postgres, fresh)["record_state"] == "in_progress"
    assert _record(ephemeral_postgres, completed)["record_state"] == "completed"


def test_M5_06_unacked_events_defer_convergence_until_replayed(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    pending_terminal = _uuid(5)
    pending_progress = _uuid(6)

    async def scenario() -> tuple[int, int]:
        ledger = _ledger(tmp_path)
        writer, engine = _writer(ephemeral_postgres, ledger)
        try:
            ledger.append_batch(
                (
                    _received(pending_terminal),
                    _in_progress(pending_terminal),
                    _received(pending_progress),
                )
            )
            await _replay(ledger, writer)
            # Both records are open in PostgreSQL but have unreplayed local events.
            ledger.append_batch((_completed(pending_terminal), _in_progress(pending_progress)))
            first = await writer.converge_unknown_once()
            await _replay(ledger, writer)
            second = await writer.converge_unknown_once()
            await _replay(ledger, writer)
            return first, second
        finally:
            ledger.close()
            await engine.dispose()

    assert asyncio.run(scenario()) == (0, 1)
    assert _record(ephemeral_postgres, pending_terminal)["record_state"] == "completed"
    progressed = _record(ephemeral_postgres, pending_progress)
    assert progressed["record_state"] == "unknown"
    assert progressed["last_confirmed_event_type"] == EventType.IN_PROGRESS.value


def test_M5_06_terminal_written_after_scan_wins_without_error(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_id = _uuid(7)

    async def scenario() -> tuple[int, list[str]]:
        ledger = _ledger(tmp_path)
        writer, engine = _writer(ephemeral_postgres, ledger)
        try:
            ledger.append_batch((_received(request_id), _in_progress(request_id)))
            await _replay(ledger, writer)

            # The Gateway lands its terminal event between the summary read and the append.
            def summarize_then_gateway_finishes(
                self: SQLiteLedger,
                request_ids: object,
            ) -> dict[UUID, RequestEventSummary]:
                self.append_batch((_completed(request_id),))
                return {request_id: RequestEventSummary(max_event_seq=2, unacked_count=0)}

            monkeypatch.setattr(SQLiteLedger, "summarize_requests", summarize_then_gateway_finishes)
            written = await writer.converge_unknown_once()
            monkeypatch.undo()
            await _replay(ledger, writer)
            return written, _event_types(ledger, request_id)
        finally:
            ledger.close()
            await engine.dispose()

    assert asyncio.run(scenario()) == (0, ["Received", "InProgress", "Completed"])
    assert _record(ephemeral_postgres, request_id)["record_state"] == "completed"


def test_M5_06_repeated_scans_and_writer_restart_append_one_unknown(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    request_id = _uuid(8)

    async def scenario() -> list[int]:
        ledger = _ledger(tmp_path)
        writer, engine = _writer(ephemeral_postgres, ledger)
        counts: list[int] = []
        try:
            ledger.append_batch((_received(request_id), _in_progress(request_id)))
            await _replay(ledger, writer)
            counts.append(await writer.converge_unknown_once())
            counts.append(await writer.converge_unknown_once())
        finally:
            ledger.close()
            await engine.dispose()

        # Restart before the Unknown was replayed, then again after replay.
        ledger = _ledger(tmp_path)
        restarted, engine = _writer(ephemeral_postgres, ledger)
        try:
            counts.append(await restarted.converge_unknown_once())
            await _replay(ledger, restarted)
            before = _record(ephemeral_postgres, request_id)
            counts.append(await restarted.converge_unknown_once())
            await restarted.replay_once()
            assert _record(ephemeral_postgres, request_id) == before
            counts.append(_event_types(ledger, request_id).count("Unknown"))
        finally:
            ledger.close()
            await engine.dispose()
        return counts

    assert asyncio.run(scenario()) == [1, 0, 0, 0, 1]
    assert _record(ephemeral_postgres, request_id)["record_state"] == "unknown"


def test_M5_06_run_loop_converges_at_startup(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    request_id = _uuid(9)

    async def scenario() -> None:
        ledger = _ledger(tmp_path)
        # Seed the projection with a stale InProgress record through a first writer.
        seeder, engine = _writer(ephemeral_postgres, ledger)
        try:
            ledger.append_batch((_received(request_id), _in_progress(request_id)))
            await _replay(ledger, seeder)
        finally:
            await engine.dispose()

        writer, engine = _writer(ephemeral_postgres, ledger, unknown_scan_seconds=0.1)
        stop = asyncio.Event()
        task = asyncio.create_task(writer.run(stop))
        try:
            for _ in range(100):
                if _record(ephemeral_postgres, request_id)["record_state"] == "unknown":
                    break
                await asyncio.sleep(0.05)
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=10)
            ledger.close()
            await engine.dispose()

    asyncio.run(scenario())
    assert _record(ephemeral_postgres, request_id)["record_state"] == "unknown"


def test_M5_06_attention_summary_counts_recent_unknown_only(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    real_now = datetime.now(UTC).replace(microsecond=0)
    recent_old = real_now - timedelta(hours=3)
    recent_new = real_now - timedelta(hours=2)
    too_old = real_now - timedelta(hours=30)
    stale_ids = {_uuid(10): recent_old, _uuid(11): recent_new, _uuid(12): too_old}
    completed_id = _uuid(13)

    async def scenario() -> tuple[
        int, datetime | None, tuple[AttentionItem, ...], tuple[AttentionItem, ...]
    ]:
        ledger = _ledger(tmp_path)
        writer, engine = _writer(ephemeral_postgres, ledger, clock=lambda: real_now)
        service = AttentionService(session_factory=create_session_factory(engine))
        try:
            empty = await service.items()
            events: list[LedgerEvent] = []
            for request_id, received_at in stale_ids.items():
                events.extend(
                    (_received(request_id, received_at), _in_progress(request_id, received_at))
                )
            events.extend(
                (
                    _received(completed_id, recent_new),
                    _in_progress(completed_id, recent_new),
                    _completed(completed_id, recent_new),
                )
            )
            ledger.append_batch(events)
            await _replay(ledger, writer)
            assert await writer.converge_unknown_once() == 3
            await _replay(ledger, writer)
            async with create_session_factory(engine)() as session:
                summary = await summarize_unknown_request_records(
                    session, since=real_now - timedelta(hours=24)
                )
            return summary.count, summary.latest_received_at, empty, await service.items()
        finally:
            ledger.close()
            await engine.dispose()

    count, latest, empty, items = asyncio.run(scenario())
    assert (count, latest) == (2, recent_new)
    assert empty == ()
    assert items == (
        AttentionItem(kind=AttentionKind.UNKNOWN_REQUESTS, count=2, latest_at=recent_new),
    )

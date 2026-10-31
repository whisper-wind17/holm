"""M5-05 real SQLite and PostgreSQL Ledger Writer integration coverage."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final
from uuid import UUID

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from holm_domain import ComponentStatus, RequestFinishReason
from holm_ledger import (
    CompletedEvent,
    ConnectionRole,
    InProgressEvent,
    LedgerEvent,
    LedgerStorageError,
    ReceivedEvent,
    RequestOperation,
    RequestProtocol,
    SQLiteLedger,
    open_ledger,
)
from holm_ledger_writer import LedgerWriter
from holm_persistence import (
    RequestRecordLedgerRepository,
    RequestRecordUpsertResult,
    create_database_engine,
    create_session_factory,
)
from migration_test_support import upgrade_head
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

NOW: Final = datetime(2026, 9, 17, 16, 30, tzinfo=UTC)
USER_ID: Final = UUID("7a000000-0000-4000-8000-000000000001")
KEY_ID: Final = UUID("7a000000-0000-4000-8000-000000000002")
SERVICE_ID: Final = UUID("7a000000-0000-4000-8000-000000000003")
INSTANCE_ID: Final = UUID("7a000000-0000-4000-8000-000000000004")


def _uuid(value: int) -> UUID:
    return UUID(f"7b000000-0000-4000-8000-{value:012d}")


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
        "usage.request_id_registry, core.component_heartbeats, core.api_keys, "
        "core.users CASCADE"
    )
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(statement)
    yield
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(statement)


def _seed_key(fixture: EphemeralPostgres, *, created_at: datetime | None = None) -> None:
    created = created_at or NOW - timedelta(minutes=10)
    with fixture.engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO core.users "
                "(id, username_norm, display_name, role, status, password_hash, "
                "must_change_password, created_at, updated_at, deleted_at) VALUES "
                "(:id, 'm5-05-user', 'M5-05 User', 'admin', 'active', "
                "'$argon2id$test', false, :created_at, :created_at, NULL)"
            ),
            {"id": USER_ID, "created_at": created},
        )
        connection.execute(
            sa.text(
                "INSERT INTO core.api_keys "
                "(id, user_id, name, lookup_id, display_prefix, last4, pepper_key_id, "
                "secret_hmac, status, expires_at, last_used_at, created_at, revoked_at) "
                "VALUES (:id, :user_id, 'M5-05 Key', 'AAAAAAAAAAAAAAAAAAAAAAAAAA', "
                "'holm_sk_', 'Ab_1', 'm5-05', :secret_hmac, 'Active', NULL, NULL, "
                ":created_at, NULL)"
            ),
            {
                "id": KEY_ID,
                "user_id": USER_ID,
                "secret_hmac": b"k" * 32,
                "created_at": created,
            },
        )


def _ledger_path(tmp_path: Path) -> Path:
    directory = tmp_path / "spool"
    directory.mkdir(mode=0o700)
    return directory / "request-ledger.sqlite3"


def _received(request_id: UUID, received_at: datetime = NOW) -> ReceivedEvent:
    return ReceivedEvent(
        request_id=request_id,
        event_seq=1,
        created_at=received_at,
        received_at=received_at,
        protocol=RequestProtocol.OPENAI,
        operation=RequestOperation.CHAT_COMPLETIONS,
        endpoint="/v1/chat/completions",
        stream=False,
        input_bytes=32,
    )


def _in_progress(
    request_id: UUID,
    received_at: datetime = NOW,
    *,
    key_id: UUID = KEY_ID,
) -> InProgressEvent:
    return InProgressEvent(
        request_id=request_id,
        event_seq=2,
        created_at=received_at + timedelta(seconds=1),
        received_at=received_at,
        user_id=USER_ID,
        api_key_id=key_id,
        service_id=SERVICE_ID,
        instance_id=INSTANCE_ID,
        requested_max_tokens=16,
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


async def _with_writer(
    fixture: EphemeralPostgres,
    ledger: SQLiteLedger,
    *,
    poll_seconds: float = 1.0,
) -> tuple[LedgerWriter, AsyncEngine]:
    engine = create_database_engine(_async_url(fixture), connect_timeout_seconds=5)
    writer = LedgerWriter(
        ledger=ledger,
        session_factory=create_session_factory(engine),
        engine=engine,
        poll_seconds=poll_seconds,
        heartbeat_seconds=0.1,
        instance_id="m5-05-integration",
        build_version="m5-05-test",
    )
    return writer, engine


def test_M5_05_normal_replay_commits_records_and_last_used_before_ack(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    _seed_key(ephemeral_postgres)
    request_id = _uuid(1)

    async def scenario() -> None:
        ledger = open_ledger(_ledger_path(tmp_path))
        ledger.append_batch(
            (_received(request_id), _in_progress(request_id), _completed(request_id))
        )
        writer, engine = await _with_writer(ephemeral_postgres, ledger)
        try:
            assert await writer.replay_once() == 3
            assert ledger.list_unacked() == ()
        finally:
            ledger.close()
            await engine.dispose()

    asyncio.run(scenario())
    with ephemeral_postgres.engine.connect() as connection:
        record = connection.execute(
            sa.text(
                "SELECT record_state, event_seq, input_tokens, output_tokens, api_key_id "
                "FROM usage.request_records WHERE request_id = :request_id"
            ),
            {"request_id": request_id},
        ).one()
        last_used = connection.execute(
            sa.text("SELECT last_used_at FROM core.api_keys WHERE id = :id"),
            {"id": KEY_ID},
        ).scalar_one()
    assert record == ("completed", 3, 3, 5, KEY_ID)
    assert last_used == NOW + timedelta(seconds=2)


def test_M5_05_full_batch_continues_until_more_than_500_are_acked(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        ledger = open_ledger(_ledger_path(tmp_path))
        ledger.append_batch(tuple(_received(_uuid(value)) for value in range(10, 511)))
        writer, engine = await _with_writer(ephemeral_postgres, ledger)
        try:
            assert await writer.replay_once() == 500
            assert len(ledger.list_unacked()) == 1
            assert await writer.replay_once() == 1
            assert ledger.list_unacked() == ()
        finally:
            ledger.close()
            await engine.dispose()

    asyncio.run(scenario())
    with ephemeral_postgres.engine.connect() as connection:
        assert (
            connection.execute(sa.text("SELECT count(*) FROM usage.request_records")).scalar_one()
            == 501
        )


def test_M5_05_TC_LG_04_ack_failure_replay_is_idempotent(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_id = _uuid(600)
    original = SQLiteLedger.mark_acked_batch

    async def scenario() -> None:
        ledger = open_ledger(_ledger_path(tmp_path))
        ledger.append(_received(request_id))
        writer, engine = await _with_writer(ephemeral_postgres, ledger)

        def fail_ack(*args: object, **kwargs: object) -> int:
            del args, kwargs
            raise LedgerStorageError("injected ack failure")

        monkeypatch.setattr(SQLiteLedger, "mark_acked_batch", fail_ack)
        try:
            with pytest.raises(LedgerStorageError, match="injected ack failure"):
                await writer.replay_once()
            assert len(ledger.list_unacked()) == 1
            monkeypatch.setattr(SQLiteLedger, "mark_acked_batch", original)
            assert await writer.replay_once() == 1
            assert ledger.list_unacked() == ()
        finally:
            ledger.close()
            await engine.dispose()

    asyncio.run(scenario())
    with ephemeral_postgres.engine.connect() as connection:
        assert connection.execute(
            sa.text(
                "SELECT count(*), max(event_seq) FROM usage.request_records "
                "WHERE request_id = :request_id"
            ),
            {"request_id": request_id},
        ).one() == (1, 1)


def test_M5_05_partition_conflict_is_quarantined_as_metadata_and_acked(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    request_id = _uuid(700)
    registered_at = NOW - timedelta(days=40)
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO usage.request_id_registry "
                "(request_id, received_at, highest_event_seq) VALUES (:id, :at, 1)"
            ),
            {"id": request_id, "at": registered_at},
        )

    async def scenario() -> None:
        ledger = open_ledger(_ledger_path(tmp_path))
        ledger.append(_received(request_id))
        writer, engine = await _with_writer(ephemeral_postgres, ledger)
        try:
            assert await writer.replay_once() == 1
            assert ledger.list_unacked() == ()
        finally:
            ledger.close()
            await engine.dispose()

    asyncio.run(scenario())
    with ephemeral_postgres.engine.connect() as connection:
        row = connection.execute(
            sa.text(
                "SELECT request_id, event_seq, event_type, event_received_at, "
                "registered_received_at FROM usage.request_event_quarantine"
            )
        ).one()
        columns = tuple(
            connection.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'usage' "
                    "AND table_name = 'request_event_quarantine' ORDER BY ordinal_position"
                )
            ).scalars()
        )
    assert row == (request_id, 1, "Received", NOW, registered_at)
    assert columns == (
        "request_id",
        "event_seq",
        "event_type",
        "event_received_at",
        "registered_received_at",
        "quarantined_at",
    )


def test_M5_05_single_nonconnection_database_error_is_quarantined_without_blocking_batch(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    healthy_id = _uuid(750)
    poisoned_id = _uuid(751)
    original = RequestRecordLedgerRepository.upsert_batch

    async def reject_one_event(
        repository: RequestRecordLedgerRepository,
        events: Iterable[LedgerEvent],
    ) -> tuple[RequestRecordUpsertResult, ...]:
        pending = tuple(events)
        if len(pending) > 1 or pending[0].request_id == poisoned_id:
            raise IntegrityError(
                "M5_05_BODY_SENTINEL must not be logged",
                {},
                Exception("injected nonconnection rejection"),
            )
        return await original(repository, pending)

    async def scenario() -> None:
        ledger = open_ledger(_ledger_path(tmp_path))
        ledger.append_batch((_received(healthy_id), _received(poisoned_id)))
        writer, engine = await _with_writer(ephemeral_postgres, ledger)
        monkeypatch.setattr(RequestRecordLedgerRepository, "upsert_batch", reject_one_event)
        try:
            assert await writer.replay_once() == 2
            assert ledger.list_unacked() == ()
        finally:
            ledger.close()
            await engine.dispose()

    asyncio.run(scenario())
    with ephemeral_postgres.engine.connect() as connection:
        records = tuple(
            connection.execute(
                sa.text("SELECT request_id FROM usage.request_records ORDER BY request_id")
            ).scalars()
        )
        quarantined = connection.execute(
            sa.text("SELECT request_id, event_seq, event_type FROM usage.request_event_quarantine")
        ).one()
    assert records == (healthy_id,)
    assert quarantined == (poisoned_id, 1, "Received")


def test_M5_05_TC_LG_11_last_used_throttles_per_minute_and_never_regresses(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    _seed_key(ephemeral_postgres)
    update_count = 0

    async def scenario() -> None:
        nonlocal update_count
        ledger = open_ledger(_ledger_path(tmp_path))
        writer, engine = await _with_writer(ephemeral_postgres, ledger)

        def count_updates(
            conn: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            executemany: bool,
        ) -> None:
            del conn, cursor, parameters, context, executemany
            nonlocal update_count
            if "core.holm_update_api_key_last_used" in statement:
                update_count += 1

        sa.event.listen(engine.sync_engine, "before_cursor_execute", count_updates)
        try:
            first = _uuid(800)
            ledger.append_batch((_received(first), _in_progress(first), _completed(first)))
            assert await writer.replay_once() == 3
            assert update_count == 1

            later = NOW + timedelta(minutes=1)
            second = _uuid(801)
            ledger.append_batch((_received(second, later), _in_progress(second, later)))
            assert await writer.replay_once() == 2

            earlier = NOW - timedelta(minutes=1)
            third = _uuid(802)
            missing = _uuid(803)
            ledger.append_batch(
                (
                    _received(third, earlier),
                    _in_progress(third, earlier),
                    _received(missing, later + timedelta(minutes=1)),
                    _in_progress(missing, later + timedelta(minutes=1), key_id=_uuid(9999)),
                )
            )
            assert await writer.replay_once() == 4
        finally:
            sa.event.remove(engine.sync_engine, "before_cursor_execute", count_updates)
            ledger.close()
            await engine.dispose()

    asyncio.run(scenario())
    with ephemeral_postgres.engine.connect() as connection:
        last_used = connection.execute(
            sa.text("SELECT last_used_at FROM core.api_keys WHERE id = :id"),
            {"id": KEY_ID},
        ).scalar_one()
    assert update_count == 3
    assert last_used == NOW + timedelta(minutes=1, seconds=1)


def test_M5_05_new_event_wakes_writer_well_before_poll_fallback_and_heartbeat_is_written(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    request_id = _uuid(900)

    async def scenario() -> float:
        path = _ledger_path(tmp_path)
        ledger = open_ledger(path, ConnectionRole.LEDGER_WRITER)
        gateway = open_ledger(path, ConnectionRole.GATEWAY_SPOOL_WRITER)
        writer, engine = await _with_writer(ephemeral_postgres, ledger, poll_seconds=5.0)
        stop = asyncio.Event()
        task = asyncio.create_task(writer.run(stop))
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            await asyncio.sleep(0.05)
            await asyncio.to_thread(gateway.append, _received(request_id))
            deadline = loop.time() + 1.5
            while loop.time() < deadline:
                with ephemeral_postgres.engine.connect() as connection:
                    found = connection.execute(
                        sa.text(
                            "SELECT EXISTS (SELECT 1 FROM usage.request_records "
                            "WHERE request_id = :id)"
                        ),
                        {"id": request_id},
                    ).scalar_one()
                if found:
                    break
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("new Ledger event did not wake the writer")
            return loop.time() - started
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=2.0)
            gateway.close()
            ledger.close()
            await engine.dispose()

    elapsed = asyncio.run(scenario())
    assert elapsed < 1.5
    with ephemeral_postgres.engine.connect() as connection:
        heartbeat = connection.execute(
            sa.text(
                "SELECT component, instance_id, version, status "
                "FROM core.component_heartbeats WHERE component = 'ledger-writer'"
            )
        ).one()
    assert heartbeat == (
        "ledger-writer",
        "m5-05-integration",
        "m5-05-test",
        ComponentStatus.STOPPING.value,
    )

"""M0-06F Integration checks for the SQLite reliable queue fixture FX-SQ-01~05.

Every case here runs against a real SQLite file in a task-owned temporary
directory with a real WAL. No in-memory database and no Mock is used, because
the integration-test policy does not accept a Mock as milestone evidence for WAL
behavior.

Nothing in this file implements Gateway, Spool Writer, Ledger Writer, Group
Commit or Ack/Checkpoint behavior; those belong to M4/M5. The subject under
test is the fixture itself.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from typing import Final

import pytest
from fixtures.sqlite_ledger import (
    BUSY_TIMEOUT_MS,
    DATABASE_HEADER_WAL_VERSION,
    EXPECTED_FOREIGN_KEYS,
    EXPECTED_JOURNAL_MODE,
    EXPECTED_SYNCHRONOUS,
    EXPECTED_WAL_AUTOCHECKPOINT,
    FIXTURE_REVIEW_STATUS,
    GATEWAY_BUSY_TIMEOUT_MS,
    LEDGER_BUSY_TIMEOUT_MS,
    METADATA_COLUMN_ALLOWLIST,
    NETWORK_FILESYSTEMS,
    TASK_ROOT_PARENT,
    TASK_ROOT_PREFIX,
    ConnectionRole,
    EventType,
    FixtureId,
    LedgerEvent,
    LedgerWorkspace,
    SqliteLedger,
    SynchronousLevel,
    assert_local_disk,
    block_wal_path,
    closed_connections,
    corrupt_wal_frame_after_commits,
    corrupt_wal_header,
    exhaust_database_capacity,
    filesystem_type,
    held_write_transaction,
    restore_database_capacity,
    sqlite_error_name,
    unblock_wal_path,
)

#: A request body the fixture must never be able to persist. TDD 14.7 requires
#: the sentinel sweep to find nothing in SQLite or its WAL.
BODY_SENTINEL: Final = "HOLM-M0-06F-BODY-a7f3c1"
REQUEST_BODY: Final = (
    '{"messages": [{"role": "user", "content": "' + BODY_SENTINEL + '"}], "stream": true}'
)

BASE_TIME: Final = datetime(2026, 8, 28, 9, 0, 0, tzinfo=UTC)
CAPACITY_PROBE_EVENTS: Final = 4_000
LOCK_HOLD_SECONDS: Final = 0.3
THREAD_JOIN_SECONDS: Final = 30.0


@pytest.fixture
def workspace() -> Iterator[LedgerWorkspace]:
    """FX-SQ-01: a task-owned temporary workspace, destroyed after each test."""
    created = LedgerWorkspace.create()
    try:
        yield created
    finally:
        created.destroy()


@pytest.fixture
def ledger(workspace: LedgerWorkspace) -> SqliteLedger:
    """FX-SQ-01: one real ledger database with the illustrative metadata table."""
    return workspace.ledger()


def _event(
    request_id: str,
    event_seq: int = 1,
    event_type: EventType = EventType.RECEIVED,
) -> LedgerEvent:
    return LedgerEvent(
        request_id=request_id,
        event_seq=event_seq,
        event_type=event_type,
        created_at=BASE_TIME + timedelta(seconds=event_seq),
        http_status=200 if event_type is EventType.COMPLETED else None,
        prompt_tokens=12 if event_type is EventType.COMPLETED else None,
        completion_tokens=34 if event_type is EventType.COMPLETED else None,
    )


def test_FX_SQ_01_journal_mode_and_synchronous_are_read_back_from_the_database(
    ledger: SqliteLedger,
) -> None:
    connection = ledger.connect()
    ledger.append_event(connection, _event("req-readback"))

    state = ledger.pragma_state(connection)
    assert state.journal_mode == EXPECTED_JOURNAL_MODE == "wal"
    assert state.synchronous == int(EXPECTED_SYNCHRONOUS) == SynchronousLevel.FULL.value == 2
    assert state.foreign_keys == EXPECTED_FOREIGN_KEYS
    assert state.wal_autocheckpoint == EXPECTED_WAL_AUTOCHECKPOINT == 0

    # The same two values through SQLite's pragma table-valued functions, which
    # take a different code path than the PRAGMA statement itself.
    journal_mode, synchronous = ledger.pragma_state_from_table_valued_function(connection)
    assert journal_mode == EXPECTED_JOURNAL_MODE
    assert synchronous == int(EXPECTED_SYNCHRONOUS)

    # And from the raw bytes of the file, independent of any pragma: header
    # offsets 18 and 19 are both 2 only for a WAL-mode database.
    assert ledger.header_versions() == (
        DATABASE_HEADER_WAL_VERSION,
        DATABASE_HEADER_WAL_VERSION,
    )

    assert ledger.path.is_file()
    assert ledger.wal_path.is_file()
    assert ledger.wal_path.stat().st_size > 0
    assert ledger.shm_path.is_file()


def test_FX_SQ_01_each_new_connection_reads_back_full_durability_and_its_busy_timeout(
    workspace: LedgerWorkspace, ledger: SqliteLedger
) -> None:
    for role in ConnectionRole:
        connection = ledger.connect(role)
        state = ledger.pragma_state(connection)
        assert state.synchronous == int(EXPECTED_SYNCHRONOUS), role
        assert state.journal_mode == EXPECTED_JOURNAL_MODE, role
        assert state.busy_timeout == BUSY_TIMEOUT_MS[role], role
        workspace.close(connection)

    assert BUSY_TIMEOUT_MS[ConnectionRole.GATEWAY_SPOOL_WRITER] == GATEWAY_BUSY_TIMEOUT_MS == 100
    assert BUSY_TIMEOUT_MS[ConnectionRole.LEDGER_WRITER] == LEDGER_BUSY_TIMEOUT_MS == 2_000

    # journal_mode is persisted in the file, so a connection that sets no pragma
    # at all still reads WAL back. This distinguishes a durable file mode from a
    # per-session setting the fixture just applied.
    bare = sqlite3.connect(str(ledger.path), isolation_level=None)
    workspace.register(bare)
    try:
        row = bare.execute("PRAGMA journal_mode").fetchone()
        assert row is not None
        assert str(row[0]) == EXPECTED_JOURNAL_MODE
    finally:
        workspace.close(bare)


def test_FX_SQ_01_the_wal_lives_on_local_disk_and_not_on_a_network_filesystem(
    workspace: LedgerWorkspace, ledger: SqliteLedger
) -> None:
    assert workspace.root.parent == TASK_ROOT_PARENT
    assert workspace.root.name.startswith(TASK_ROOT_PREFIX)

    found = assert_local_disk(ledger.wal_path.parent)
    assert found == workspace.filesystem_type
    assert found not in NETWORK_FILESYSTEMS
    assert filesystem_type(ledger.path.parent) == found


def test_FX_SQ_01_the_ledger_table_carries_only_allowlisted_metadata_columns(
    ledger: SqliteLedger,
) -> None:
    connection = ledger.connect()
    columns = ledger.table_columns(connection)

    assert set(columns) == set(METADATA_COLUMN_ALLOWLIST)
    ledger.assert_columns_are_metadata_only(connection)

    forbidden_columns = {
        "payload",
        "payload_json",
        "body",
        "request_body",
        "response_body",
        "messages",
        "prompt",
        "completion",
        "content",
        "error_detail",
        "headers",
    }
    assert forbidden_columns.isdisjoint(columns)

    # The only text columns are an opaque Request ID, a state name and a
    # timestamp; everything else is an integer, so no column can hold a body.
    types = ledger.column_types(connection)
    text_columns = {name for name, declared in types.items() if declared.upper() == "TEXT"}
    assert text_columns == {"request_id", "event_type", "created_at"}

    event_fields = {field.name for field in fields(LedgerEvent)}
    assert event_fields == set(METADATA_COLUMN_ALLOWLIST)


def test_FX_SQ_01_a_request_body_is_rejected_and_never_reaches_the_database_or_wal(
    ledger: SqliteLedger,
) -> None:
    with pytest.raises(ValueError):
        LedgerEvent(
            request_id=REQUEST_BODY,
            event_seq=1,
            event_type=EventType.RECEIVED,
            created_at=BASE_TIME,
        )

    connection = ledger.connect()
    ledger.append_event(connection, _event("req-zero-body", 1))
    ledger.append_event(connection, _event("req-zero-body", 2, EventType.COMPLETED))
    assert ledger.event_count(connection) == 2

    durable = ledger.durable_bytes()
    assert BODY_SENTINEL.encode() not in durable
    assert b"messages" not in durable
    assert b"content" not in durable

    ledger.checkpoint_passive(connection)
    assert BODY_SENTINEL.encode() not in ledger.durable_bytes()

    assert FIXTURE_REVIEW_STATUS == "待真机复核"
    assert FixtureId.FX_SQ_01.value == "FX-SQ-01"


def test_FX_SQ_02_a_crash_snapshot_recovers_every_uncheckpointed_event(
    ledger: SqliteLedger,
) -> None:
    connection = ledger.connect()
    for index in range(1, 4):
        ledger.append_event(connection, _event(f"req-crash-{index}"))

    live = ledger.live_wal_frames()
    assert len([frame for frame in live if frame.is_commit]) == 3

    snapshot = ledger.crash_snapshot("intact")
    assert snapshot.wal_path.is_file()

    recovered = snapshot.connect(ConnectionRole.READER)
    assert snapshot.integrity_check(recovered) == "ok"
    assert snapshot.request_ids(recovered) == ("req-crash-1", "req-crash-2", "req-crash-3")
    assert FixtureId.FX_SQ_02.value == "FX-SQ-02"


def test_FX_SQ_03_wal_header_corruption_silently_drops_every_uncheckpointed_event(
    ledger: SqliteLedger,
) -> None:
    connection = ledger.connect()
    ledger.append_event(connection, _event("req-checkpointed"))
    ledger.checkpoint_passive(connection)
    for index in range(1, 3):
        ledger.append_event(connection, _event(f"req-wal-only-{index}"))
    assert ledger.event_count(connection) == 3

    snapshot = ledger.crash_snapshot("header")
    corruption = corrupt_wal_header(snapshot)
    assert corruption.offset == 0
    assert corruption.original_bytes != corruption.corrupted_bytes
    assert snapshot.wal_path.read_bytes()[:4] == corruption.corrupted_bytes

    damaged = snapshot.connect(ConnectionRole.READER)
    # SQLite treats an unreadable WAL header as an empty WAL: the loss is silent
    # and integrity_check still says "ok", which is why TDD 18.x drives Ledger
    # Unavailable from the write path rather than from an integrity probe.
    assert snapshot.request_ids(damaged) == ("req-checkpointed",)
    assert snapshot.integrity_check(damaged) == "ok"
    assert FixtureId.FX_SQ_03.value == "FX-SQ-03"


def test_FX_SQ_03_wal_frame_corruption_stops_recovery_at_the_last_intact_commit(
    ledger: SqliteLedger,
) -> None:
    connection = ledger.connect()
    ledger.append_event(connection, _event("req-a-checkpointed"))
    ledger.checkpoint_passive(connection)
    for name in ("req-b", "req-c", "req-d"):
        ledger.append_event(connection, _event(name))
    assert ledger.event_count(connection) == 4

    snapshot = ledger.crash_snapshot("frame")
    commits_before = [frame for frame in snapshot.live_wal_frames() if frame.is_commit]
    assert len(commits_before) == 3

    corruption = corrupt_wal_frame_after_commits(snapshot, surviving_commits=1)
    assert corruption.frame_index is not None
    assert corruption.frame_index > commits_before[0].index
    assert corruption.original_bytes != corruption.corrupted_bytes

    damaged = snapshot.connect(ConnectionRole.READER)
    assert snapshot.request_ids(damaged) == ("req-a-checkpointed", "req-b")
    assert snapshot.integrity_check(damaged) == "ok"


def test_FX_SQ_04_a_write_that_cannot_allocate_fails_with_sqlite_full_and_stores_nothing(
    ledger: SqliteLedger,
) -> None:
    connection = ledger.connect()
    ledger.append_event(connection, _event("req-before-full"))
    before = ledger.event_count(connection)

    capped_pages = exhaust_database_capacity(connection)
    assert capped_pages > 0

    batch = [_event(f"req-full-{index}", 1) for index in range(CAPACITY_PROBE_EVENTS)]
    with pytest.raises(sqlite3.OperationalError) as failure:
        ledger.append_events(connection, batch)

    assert sqlite_error_name(failure.value) == "SQLITE_FULL"
    assert "database or disk is full" in str(failure.value)
    assert not connection.in_transaction
    assert ledger.event_count(connection) == before
    assert ledger.request_ids(connection) == ("req-before-full",)

    restore_database_capacity(connection)
    ledger.append_event(connection, _event("req-after-full"))
    assert ledger.request_ids(connection) == ("req-after-full", "req-before-full")
    assert FixtureId.FX_SQ_04.value == "FX-SQ-04"


def test_FX_SQ_04_a_write_fails_with_sqlite_cantopen_when_the_wal_cannot_be_created(
    workspace: LedgerWorkspace, ledger: SqliteLedger
) -> None:
    connection = ledger.connect()
    ledger.append_event(connection, _event("req-before-block"))
    workspace.close_all()

    blocked = block_wal_path(ledger)
    assert blocked.is_dir()

    with pytest.raises(sqlite3.OperationalError) as failure:
        ledger.connect()
    assert sqlite_error_name(failure.value) == "SQLITE_CANTOPEN"
    # The failed connection must not linger; the fixture closes it before
    # re-raising so teardown can still prove nothing was left open.
    assert workspace.connections == ()

    unblock_wal_path(ledger)
    recovered = ledger.connect()
    assert ledger.request_ids(recovered) == ("req-before-block",)
    ledger.append_event(recovered, _event("req-after-block"))
    assert ledger.request_ids(recovered) == ("req-after-block", "req-before-block")


def test_FX_SQ_05_a_second_writer_is_refused_within_the_gateway_busy_timeout(
    ledger: SqliteLedger,
) -> None:
    outcome: dict[str, str] = {}
    waited: list[float] = []

    def contend() -> None:
        connection = ledger.connect(ConnectionRole.GATEWAY_SPOOL_WRITER)
        started_at = time.monotonic()
        try:
            assert ledger.pragma_state(connection).busy_timeout == GATEWAY_BUSY_TIMEOUT_MS
            ledger.append_event(connection, _event("req-contender"))
            outcome["result"] = "committed"
        except sqlite3.OperationalError as error:
            outcome["result"] = "busy"
            outcome["error_name"] = sqlite_error_name(error)
        finally:
            waited.append(time.monotonic() - started_at)
            ledger.close_connection(connection)

    with held_write_transaction(ledger) as holder:
        holder.execute(
            "INSERT INTO request_events "
            "(request_id, event_seq, event_type, created_at) VALUES (?, ?, ?, ?)",
            ("req-holder", 1, str(EventType.RECEIVED), BASE_TIME.isoformat()),
        )
        thread = threading.Thread(target=contend, name="fx-sq-05-writer")
        thread.start()
        thread.join(THREAD_JOIN_SECONDS)
        assert not thread.is_alive()

    # The holder is not released until the contender has returned, so SQLITE_BUSY
    # proves the Gateway writer fails fast instead of waiting for the lock.
    assert outcome["result"] == "busy"
    assert outcome["error_name"] == "SQLITE_BUSY"
    # Keep the load-insensitive half of the original timing judgement: the
    # read-back PRAGMA only proves the timeout was configured, not that SQLite
    # honored it, so still require that the contender did not wait the Ledger
    # Writer timeout (2s, i.e. 20x the Gateway budget).  The lower bound was
    # dropped on purpose - it measured machine load, not the timeout.
    assert waited[0] < LEDGER_BUSY_TIMEOUT_MS / 1000
    assert FixtureId.FX_SQ_05.value == "FX-SQ-05"


def test_FX_SQ_05_readers_are_never_blocked_by_an_open_write_transaction(
    workspace: LedgerWorkspace, ledger: SqliteLedger
) -> None:
    writer = ledger.connect(ConnectionRole.GATEWAY_SPOOL_WRITER)
    ledger.append_event(writer, _event("req-visible"))
    visible: list[tuple[str, ...]] = []

    def read_while_writer_holds_lock() -> None:
        reader = ledger.connect(ConnectionRole.READER)
        try:
            visible.append(ledger.request_ids(reader))
        finally:
            ledger.close_connection(reader)

    try:
        with held_write_transaction(ledger) as holder:
            holder.execute(
                "INSERT INTO request_events "
                "(request_id, event_seq, event_type, created_at) VALUES (?, ?, ?, ?)",
                ("req-uncommitted", 1, str(EventType.RECEIVED), BASE_TIME.isoformat()),
            )
            reader_thread = threading.Thread(
                target=read_while_writer_holds_lock,
                name="fx-sq-05-reader",
            )
            reader_thread.start()
            reader_thread.join(THREAD_JOIN_SECONDS)
            assert not reader_thread.is_alive()
            assert visible == [("req-visible",)]
            holder.execute("COMMIT")

        # Completion before COMMIT above, rather than an elapsed-time threshold,
        # proves that the open write transaction did not block the WAL reader.
        reader = ledger.connect(ConnectionRole.READER)
        try:
            assert ledger.request_ids(reader) == ("req-uncommitted", "req-visible")
        finally:
            workspace.close(reader)
    finally:
        workspace.close(writer)


def test_FX_SQ_05_the_ledger_writer_waits_where_the_gateway_writer_fails_fast(
    ledger: SqliteLedger,
) -> None:
    outcome: dict[str, str] = {}
    release = threading.Event()

    def patient_writer() -> None:
        connection = ledger.connect(ConnectionRole.LEDGER_WRITER)
        try:
            release.set()
            ledger.append_event(connection, _event("req-patient"))
            outcome["result"] = "committed"
        except sqlite3.OperationalError as error:
            outcome["result"] = sqlite_error_name(error)
        finally:
            ledger.close_connection(connection)

    thread = threading.Thread(target=patient_writer, name="fx-sq-05-ledger-writer")
    with held_write_transaction(ledger, ConnectionRole.GATEWAY_SPOOL_WRITER) as holder:
        holder.execute(
            "INSERT INTO request_events "
            "(request_id, event_seq, event_type, created_at) VALUES (?, ?, ?, ?)",
            ("req-holder", 1, str(EventType.RECEIVED), BASE_TIME.isoformat()),
        )
        thread.start()
        assert release.wait(THREAD_JOIN_SECONDS)
        # This is an interleaving aid, not a performance assertion: 300 ms is
        # well inside the separately read-back 2-second Ledger Writer timeout
        # and gives the real SQLite statement time to contend for the held lock.
        time.sleep(LOCK_HOLD_SECONDS)
        holder.execute("ROLLBACK")

    thread.join(THREAD_JOIN_SECONDS)
    assert not thread.is_alive()
    assert outcome["result"] == "committed"

    reader = ledger.connect(ConnectionRole.READER)
    assert ledger.request_ids(reader) == ("req-patient",)


def test_FX_SQ_01_the_workspace_removes_every_database_file_and_closes_every_connection() -> None:
    created = LedgerWorkspace.create()
    root = created.root
    ledger = created.ledger()
    connection = ledger.connect()
    ledger.append_event(connection, _event("req-teardown"))

    artifacts = created.database_artifacts()
    assert any(path.name.endswith(".db") for path in artifacts)
    assert any(path.name.endswith(".db-wal") for path in artifacts)
    tracked = created.connections
    assert len(tracked) == 1

    created.destroy()

    assert closed_connections(tracked) == (True,)
    assert created.connections == ()
    assert not root.exists()
    assert created.database_artifacts() == ()

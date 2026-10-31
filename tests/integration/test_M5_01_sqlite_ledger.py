"""M5-01 real-file SQLite Ledger integration checks."""

from __future__ import annotations

import os
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from holm_domain import RequestFinishReason
from holm_ledger import (
    BUSY_TIMEOUT_MS,
    REQUEST_EVENT_COLUMNS,
    SCHEMA_VERSION,
    CompletedEvent,
    ConnectionRole,
    InProgressEvent,
    LedgerConfigurationError,
    LedgerPermissionError,
    LedgerSchemaError,
    LedgerSequenceError,
    ReceivedEvent,
    RequestOperation,
    RequestProtocol,
    SequenceAction,
    open_ledger,
    validate_event,
)
from pydantic import ValidationError

REQUEST_ID = UUID("10000000-0000-4000-8000-000000000001")
USER_ID = UUID("10000000-0000-4000-8000-000000000002")
KEY_ID = UUID("10000000-0000-4000-8000-000000000003")
SERVICE_ID = UUID("10000000-0000-4000-8000-000000000004")
INSTANCE_ID = UUID("10000000-0000-4000-8000-000000000005")
NOW = datetime(2026, 9, 17, 11, 0, tzinfo=UTC)


def _private_database(tmp_path: Path) -> Path:
    directory = tmp_path / "ledger"
    directory.mkdir(mode=0o700)
    return directory / "request-ledger.sqlite3"


def _received(*, seq: int = 1) -> ReceivedEvent:
    return ReceivedEvent(
        request_id=REQUEST_ID,
        event_seq=seq,
        created_at=NOW,
        received_at=NOW,
        protocol=RequestProtocol.OPENAI,
        operation=RequestOperation.CHAT_COMPLETIONS,
        endpoint="/v1/chat/completions",
        stream=False,
        input_bytes=64,
    )


def _in_progress(*, seq: int = 2) -> InProgressEvent:
    return InProgressEvent(
        request_id=REQUEST_ID,
        event_seq=seq,
        created_at=NOW + timedelta(milliseconds=1),
        received_at=NOW,
        user_id=USER_ID,
        api_key_id=KEY_ID,
        service_id=SERVICE_ID,
        instance_id=INSTANCE_ID,
        requested_max_tokens=32,
    )


def _completed(*, seq: int = 3) -> CompletedEvent:
    return CompletedEvent(
        request_id=REQUEST_ID,
        event_seq=seq,
        created_at=NOW + timedelta(milliseconds=2),
        received_at=NOW,
        input_tokens=3,
        output_tokens=5,
        ttft_ms=1,
        duration_ms=2,
        finish_reason=RequestFinishReason.STOP,
        http_status=200,
    )


@pytest.mark.parametrize("role", tuple(ConnectionRole))
def test_M5_01_pragmas_and_busy_timeout_are_read_back(tmp_path: Path, role: ConnectionRole) -> None:
    path = _private_database(tmp_path)
    with open_ledger(path, role) as ledger:
        connection = ledger.connection
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 0
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS[role]


def test_M5_01_schema_version_columns_and_partial_index(tmp_path: Path) -> None:
    with open_ledger(_private_database(tmp_path)) as ledger:
        connection = ledger.connection
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert (
            tuple(row[1] for row in connection.execute("PRAGMA table_info(request_events)"))
            == REQUEST_EVENT_COLUMNS
        )
        index_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='ix_request_events_unacked'"
        ).fetchone()[0]
        assert "WHERE acked_at IS NULL" in index_sql


def test_M5_01_database_directory_and_sidecars_are_private(tmp_path: Path) -> None:
    path = _private_database(tmp_path)
    with open_ledger(path):
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        for target in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
            assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_M5_01_existing_overbroad_database_fails_loudly(tmp_path: Path) -> None:
    path = _private_database(tmp_path)
    path.touch(mode=0o600)
    path.chmod(0o644)
    with pytest.raises(LedgerPermissionError, match="0600"):
        open_ledger(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_M5_01_missing_or_invalid_path_has_typed_failure(tmp_path: Path) -> None:
    with pytest.raises(LedgerConfigurationError):
        open_ledger("")
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("x", encoding="utf-8")
    with pytest.raises(LedgerConfigurationError):
        open_ledger(parent_file / "ledger.sqlite3")


def test_M5_01_unknown_schema_version_fails_loudly(tmp_path: Path) -> None:
    path = _private_database(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version=99")
    connection.close()
    path.chmod(0o600)
    with pytest.raises(LedgerSchemaError, match="99"):
        open_ledger(path)


def test_M5_01_append_query_and_ack_normal_lifecycle(tmp_path: Path) -> None:
    with open_ledger(_private_database(tmp_path)) as ledger:
        assert ledger.append(_received()) is SequenceAction.WRITE
        assert ledger.append(_in_progress()) is SequenceAction.WRITE
        assert ledger.append(_completed()) is SequenceAction.WRITE
        events = ledger.list_unacked()
        assert tuple(item.event for item in events) == (_received(), _in_progress(), _completed())
        assert ledger.mark_acked(REQUEST_ID, 1, acked_at=NOW + timedelta(seconds=1))
        assert not ledger.mark_acked(REQUEST_ID, 1, acked_at=NOW + timedelta(seconds=2))
        assert tuple(item.event.event_seq for item in ledger.list_unacked()) == (2, 3)


def test_M5_01_TC_LG_06_application_sequence_rules(tmp_path: Path) -> None:
    with open_ledger(_private_database(tmp_path)) as ledger:
        with pytest.raises(LedgerSequenceError, match="first_event"):
            ledger.append(_in_progress())
        ledger.append(_received())
        ledger.append(_in_progress())
        ledger.append(_completed())
        assert ledger.append(_in_progress(seq=3)) is SequenceAction.IGNORE
        with pytest.raises(LedgerSequenceError, match="terminal"):
            ledger.append(_in_progress(seq=4))
        with pytest.raises(LedgerSequenceError, match="final"):
            ledger.append(_completed(seq=4))


def test_M5_01_trigger_rejects_direct_non_increasing_insert(tmp_path: Path) -> None:
    with open_ledger(_private_database(tmp_path)) as ledger:
        ledger.append(_received())
        with pytest.raises(sqlite3.IntegrityError, match="event_seq_not_increasing"):
            ledger.connection.execute(
                "INSERT INTO request_events VALUES (?, ?, ?, ?, ?, NULL)",
                (str(REQUEST_ID), 1, "Received", "{}", NOW.isoformat()),
            )


def test_M5_01_trigger_rejects_direct_in_progress_after_terminal(tmp_path: Path) -> None:
    with open_ledger(_private_database(tmp_path)) as ledger:
        ledger.append(_received())
        ledger.append(_in_progress())
        ledger.append(_completed())
        with pytest.raises(sqlite3.IntegrityError, match="terminal_cannot_return"):
            ledger.connection.execute(
                "INSERT INTO request_events VALUES (?, ?, ?, ?, ?, NULL)",
                (str(REQUEST_ID), 4, "InProgress", "{}", NOW.isoformat()),
            )


def test_M5_01_body_sentinel_never_reaches_database_or_wal(tmp_path: Path) -> None:
    sentinel = "M5_01_BODY_SENTINEL_7af6d0d9"
    path = _private_database(tmp_path)
    with open_ledger(path) as ledger:
        body_event = _received().model_dump(mode="json")
        body_event["messages"] = sentinel
        with pytest.raises(ValidationError):
            validate_event(body_event)
        ledger.connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
        assert sentinel.encode() not in path.read_bytes()
        wal_path = Path(f"{path}-wal")
        assert wal_path.exists()
        assert sentinel.encode() not in wal_path.read_bytes()


def test_M5_01_unacked_query_uses_partial_index(tmp_path: Path) -> None:
    with open_ledger(_private_database(tmp_path)) as ledger:
        plan = ledger.connection.execute(
            "EXPLAIN QUERY PLAN SELECT payload_json, acked_at FROM request_events "
            "WHERE acked_at IS NULL ORDER BY created_at, request_id, event_seq LIMIT 500"
        ).fetchall()
        assert any("ix_request_events_unacked" in str(row[3]) for row in plan)


def test_M5_01_existing_overbroad_directory_fails_without_repair(tmp_path: Path) -> None:
    directory = tmp_path / "ledger"
    directory.mkdir(mode=0o700)
    directory.chmod(0o755)
    with pytest.raises(LedgerPermissionError, match="0700"):
        open_ledger(directory / "ledger.sqlite3")
    assert stat.S_IMODE(directory.stat().st_mode) == 0o755
    assert not os.path.exists(directory / "ledger.sqlite3")

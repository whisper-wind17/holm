"""Standard-library SQLite storage for the metadata-only Request Ledger."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import stat
import threading
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final
from uuid import UUID

from holm_domain.ledger_health import LedgerBacklogDetails

from holm_ledger.errors import (
    LedgerConfigurationError,
    LedgerPermissionError,
    LedgerSchemaError,
    LedgerSequenceError,
    LedgerStorageError,
)
from holm_ledger.models import LedgerEvent, deserialize_event, serialize_event
from holm_ledger.sequence import SequenceAction, SequenceDecision, decide_event_sequence

SCHEMA_VERSION: Final = 1
DATABASE_MODE: Final = 0o600
DIRECTORY_MODE: Final = 0o700
REQUEST_EVENT_COLUMNS: Final = (
    "request_id",
    "event_seq",
    "event_type",
    "payload_json",
    "created_at",
    "acked_at",
)
_TERMINAL_EVENT_VALUES: Final = "'Rejected', 'Completed', 'Failed', 'Aborted', 'Unknown'"
_EVENT_TYPE_VALUES: Final = (
    "'Received', 'Rejected', 'InProgress', 'Completed', 'Failed', 'Aborted', 'Unknown'"
)
_UMASK_LOCK = threading.Lock()


class ConnectionRole(StrEnum):
    GATEWAY_SPOOL_WRITER = "gateway-spool-writer"
    LEDGER_WRITER = "ledger-writer"
    READER = "reader"


BUSY_TIMEOUT_MS: Final[dict[ConnectionRole, int]] = {
    ConnectionRole.GATEWAY_SPOOL_WRITER: 100,
    ConnectionRole.LEDGER_WRITER: 2_000,
    ConnectionRole.READER: 100,
}


@dataclass(frozen=True, slots=True)
class StoredEvent:
    event: LedgerEvent
    acked_at: datetime | None


@dataclass(frozen=True, slots=True)
class RequestEventSummary:
    """Per-request Ledger facts needed to decide Unknown convergence."""

    max_event_seq: int
    unacked_count: int


@dataclass(frozen=True, slots=True)
class PassiveCheckpointResult:
    """One SQLite ``PASSIVE`` checkpoint outcome.

    SQLite reports lock contention separately from a checkpoint that made only
    partial progress because a reader still needs older WAL frames.  Callers
    must keep that distinction so neither outcome is silently treated as a
    complete checkpoint.
    """

    busy: bool
    wal_frames: int
    checkpointed_frames: int

    @property
    def complete(self) -> bool:
        return not self.busy and self.checkpointed_frames == self.wal_frames


@contextlib.contextmanager
def _private_creation_umask() -> Iterator[None]:
    # umask is process-global. Serialize the short initialization section and
    # only make it more restrictive, never more permissive.
    with _UMASK_LOCK:
        previous = os.umask(0o077)
        try:
            yield
        finally:
            os.umask(previous)


def _require_private_path(path: Path, *, expected_mode: int, kind: str) -> None:
    try:
        details = path.lstat()
    except OSError as error:
        raise LedgerStorageError(f"cannot inspect Ledger {kind}") from error
    if stat.S_ISLNK(details.st_mode):
        raise LedgerPermissionError(f"Ledger {kind} must not be a symbolic link")
    expected_type = stat.S_ISDIR if kind == "directory" else stat.S_ISREG
    if not expected_type(details.st_mode):
        raise LedgerConfigurationError(f"Ledger {kind} has the wrong file type")
    if stat.S_IMODE(details.st_mode) != expected_mode:
        raise LedgerPermissionError(
            f"Ledger {kind} permissions must be {expected_mode:04o}, "
            f"found {stat.S_IMODE(details.st_mode):04o}"
        )


def _prepare_path(path_value: str | os.PathLike[str]) -> tuple[Path, bool]:
    if isinstance(path_value, str) and not path_value.strip():
        raise LedgerConfigurationError("Ledger database path is required")
    path = Path(path_value)
    if path.name in {"", ".", "..", ":memory:"}:
        raise LedgerConfigurationError("Ledger database path must name an on-disk file")
    path = path.absolute()
    directory = path.parent

    try:
        if directory.exists():
            _require_private_path(directory, expected_mode=DIRECTORY_MODE, kind="directory")
        else:
            directory.mkdir(mode=DIRECTORY_MODE, parents=True)
            _require_private_path(directory, expected_mode=DIRECTORY_MODE, kind="directory")
    except (LedgerConfigurationError, LedgerPermissionError, LedgerStorageError):
        raise
    except OSError as error:
        raise LedgerStorageError("cannot create Ledger directory") from error

    existed = path.exists() or path.is_symlink()
    if existed:
        _require_private_path(path, expected_mode=DATABASE_MODE, kind="database")
    else:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, DATABASE_MODE)
        except OSError as error:
            raise LedgerStorageError("cannot create Ledger database") from error
        else:
            os.close(descriptor)
        _require_private_path(path, expected_mode=DATABASE_MODE, kind="database")

    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.exists() or sidecar.is_symlink():
            _require_private_path(sidecar, expected_mode=DATABASE_MODE, kind=suffix[1:])
    return path, existed


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("Ledger timestamps must use UTC")
    # Fixed-width fractional seconds preserve chronological order under SQLite's
    # TEXT collation; optional fractions would sort ``.001`` before the prior
    # whole second's trailing ``Z``.
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise LedgerSchemaError("persisted Ledger timestamp is not UTC")
    return parsed


_CREATE_TABLE_SQL: Final = f"""
CREATE TABLE IF NOT EXISTS request_events (
  request_id TEXT NOT NULL,
  event_seq INTEGER NOT NULL CHECK (event_seq >= 1),
  event_type TEXT NOT NULL CHECK (event_type IN ({_EVENT_TYPE_VALUES})),
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  acked_at TEXT,
  PRIMARY KEY (request_id, event_seq)
)
"""
_CREATE_INDEX_SQL: Final = """
CREATE INDEX IF NOT EXISTS ix_request_events_unacked
ON request_events (created_at)
WHERE acked_at IS NULL
"""
_CREATE_SEQUENCE_TRIGGER_SQL: Final = """
CREATE TRIGGER IF NOT EXISTS trg_request_events_seq_increasing
BEFORE INSERT ON request_events
WHEN EXISTS (
  SELECT 1 FROM request_events
  WHERE request_id = NEW.request_id AND event_seq >= NEW.event_seq
)
BEGIN
  SELECT RAISE(ABORT, 'ledger_event_seq_not_increasing');
END
"""
_CREATE_TERMINAL_TRIGGER_SQL: Final = f"""
CREATE TRIGGER IF NOT EXISTS trg_request_events_terminal_no_in_progress
BEFORE INSERT ON request_events
WHEN NEW.event_type = 'InProgress' AND EXISTS (
  SELECT 1 FROM request_events
  WHERE request_id = NEW.request_id AND event_type IN ({_TERMINAL_EVENT_VALUES})
)
BEGIN
  SELECT RAISE(ABORT, 'ledger_terminal_cannot_return_to_in_progress');
END
"""


class SQLiteLedger:
    """One role-configured SQLite connection with short explicit transactions."""

    __slots__ = ("_connection", "_path", "_role")

    def __init__(self, path: Path, role: ConnectionRole, connection: sqlite3.Connection) -> None:
        self._path = path
        self._role = role
        self._connection = connection

    @classmethod
    def open(
        cls,
        path: str | os.PathLike[str],
        role: ConnectionRole = ConnectionRole.LEDGER_WRITER,
    ) -> SQLiteLedger:
        if not isinstance(role, ConnectionRole):
            raise LedgerConfigurationError("Ledger connection role is invalid")
        with _private_creation_umask():
            database_path, existed = _prepare_path(path)
            connection: sqlite3.Connection | None = None
            try:
                # A Ledger connection is process-owned and all operations are serialized by
                # its caller.  Disabling sqlite's thread affinity lets async processes move
                # these short blocking calls onto a worker thread without sharing operations.
                connection = sqlite3.connect(
                    database_path,
                    isolation_level=None,
                    check_same_thread=False,
                )
                connection.row_factory = sqlite3.Row
                cls._preflight_schema(connection, existed=existed)
                cls._configure(connection, role)
                cls._initialize_schema(connection)
                cls._validate_schema(connection)
                cls._validate_sidecars(database_path)
            except (LedgerPermissionError, LedgerSchemaError, LedgerStorageError):
                if connection is not None:
                    connection.close()
                raise
            except (OSError, sqlite3.Error) as error:
                if connection is not None:
                    connection.close()
                raise LedgerStorageError("cannot open or initialize Ledger database") from error
        if connection is None:  # pragma: no cover - defensive type narrowing
            raise LedgerStorageError("Ledger connection was not initialized")
        return cls(database_path, role, connection)

    @staticmethod
    def _preflight_schema(connection: sqlite3.Connection, *, existed: bool) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in {0, SCHEMA_VERSION}:
            raise LedgerSchemaError(f"unsupported Ledger schema version {version}")
        if not existed or version != 0:
            return
        table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'request_events'"
        ).fetchone()
        if table_exists is not None:
            raise LedgerSchemaError("unversioned existing Ledger schema is not supported")

    @staticmethod
    def _configure(connection: sqlite3.Connection, role: ConnectionRole) -> None:
        journal_mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
        if journal_mode != "wal":
            raise LedgerStorageError("Ledger database did not enter WAL mode")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS[role]}")

    @staticmethod
    def _initialize_schema(connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in {0, SCHEMA_VERSION}:
            raise LedgerSchemaError(f"unsupported Ledger schema version {version}")

        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(_CREATE_TABLE_SQL)
            connection.execute(_CREATE_INDEX_SQL)
            connection.execute(_CREATE_SEQUENCE_TRIGGER_SQL)
            connection.execute(_CREATE_TERMINAL_TRIGGER_SQL)
            if version == 0:
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != SCHEMA_VERSION:
            raise LedgerSchemaError(f"unsupported Ledger schema version {version}")
        columns = tuple(
            str(row[1]) for row in connection.execute("PRAGMA table_info(request_events)")
        )
        if columns != REQUEST_EVENT_COLUMNS:
            raise LedgerSchemaError("Ledger request_events columns do not match version 1")

    @staticmethod
    def _validate_sidecars(path: Path) -> None:
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{path}{suffix}")
            if not sidecar.exists():
                raise LedgerStorageError(f"Ledger {suffix[1:]} file was not created")
            _require_private_path(sidecar, expected_mode=DATABASE_MODE, kind=suffix[1:])

    @property
    def connection(self) -> sqlite3.Connection:
        """Expose SQLite for diagnostics and the required database-level guard tests."""

        return self._connection

    @property
    def path(self) -> Path:
        return self._path

    @property
    def role(self) -> ConnectionRole:
        return self._role

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SQLiteLedger:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def append(self, event: LedgerEvent) -> SequenceAction:
        """Apply the pure sequence decision and insert within one write transaction."""

        decision = self.append_batch((event,))[0]
        if decision.action is SequenceAction.REJECT:
            raise LedgerSequenceError(decision.reason)
        return decision.action

    def append_batch(self, events: Iterable[LedgerEvent]) -> tuple[SequenceDecision, ...]:
        """Apply events in input order and commit all accepted writes once.

        Sequence rejections are per-event results: they do not roll back accepted
        siblings. Events accepted earlier in this batch are visible to later
        sequence decisions before the transaction is committed.
        """

        pending = tuple(events)
        if not pending:
            return ()

        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing_by_request: dict[UUID, list[LedgerEvent]] = {}
            decisions: list[SequenceDecision] = []
            for event in pending:
                existing = existing_by_request.get(event.request_id)
                if existing is None:
                    rows = connection.execute(
                        "SELECT payload_json FROM request_events "
                        "WHERE request_id = ? ORDER BY event_seq",
                        (str(event.request_id),),
                    ).fetchall()
                    existing = [deserialize_event(str(row[0])) for row in rows]
                    existing_by_request[event.request_id] = existing

                decision = decide_event_sequence(existing, event)
                decisions.append(decision)
                if decision.action is not SequenceAction.WRITE:
                    continue
                connection.execute(
                    "INSERT INTO request_events "
                    "(request_id, event_seq, event_type, payload_json, created_at, acked_at) "
                    "VALUES (?, ?, ?, ?, ?, NULL)",
                    (
                        str(event.request_id),
                        event.event_seq,
                        event.event_type.value,
                        serialize_event(event),
                        _utc_text(event.created_at),
                    ),
                )
                existing.append(event)
            connection.execute("COMMIT")
            return tuple(decisions)
        except BaseException as error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            if isinstance(error, sqlite3.Error):
                raise LedgerStorageError("cannot commit Ledger event batch") from error
            raise

    def list_unacked(self, *, limit: int = 500) -> tuple[StoredEvent, ...]:
        if not 1 <= limit <= 500:
            raise ValueError("unacked event limit must be between 1 and 500")
        try:
            rows = self._connection.execute(
                "SELECT payload_json, acked_at FROM request_events "
                "WHERE acked_at IS NULL ORDER BY created_at, request_id, event_seq LIMIT ?",
                (limit,),
            ).fetchall()
        except sqlite3.Error as error:
            raise LedgerStorageError("cannot read unacknowledged Ledger events") from error
        return tuple(
            StoredEvent(
                event=deserialize_event(str(row[0])),
                acked_at=None if row[1] is None else _parse_utc(str(row[1])),
            )
            for row in rows
        )

    def backlog_details(self) -> LedgerBacklogDetails:
        """Read unacknowledged count and oldest timestamp in one query."""
        try:
            row = self._connection.execute(
                "SELECT COUNT(*), MIN(created_at) FROM request_events WHERE acked_at IS NULL"
            ).fetchone()
        except sqlite3.Error as error:
            raise LedgerStorageError("cannot read Ledger backlog details") from error
        assert row is not None
        return LedgerBacklogDetails(
            pending_event_count=int(row[0]),
            oldest_event_at=None if row[1] is None else _parse_utc(str(row[1])),
        )

    def oldest_unacked_created_at(self) -> datetime | None:
        """Return the creation time of the oldest unacknowledged event, if any.

        Backlog protection decides the state from this one number. The partial
        ``ix_request_events_unacked`` index answers it without scanning the
        acknowledged history, which makes it cheap enough for a periodic health
        loop - and still far too expensive for a request hot path.
        """

        try:
            row = self._connection.execute(
                "SELECT MIN(created_at) FROM request_events WHERE acked_at IS NULL"
            ).fetchone()
        except sqlite3.Error as error:
            raise LedgerStorageError(
                "cannot read the oldest unacknowledged Ledger event"
            ) from error
        if row is None or row[0] is None:
            return None
        return _parse_utc(str(row[0]))

    def has_unacked_events(self) -> bool:
        """Return whether any Gateway event still needs replay or Ack."""

        try:
            row = self._connection.execute(
                "SELECT 1 FROM request_events WHERE acked_at IS NULL LIMIT 1"
            ).fetchone()
        except sqlite3.Error as error:
            raise LedgerStorageError("cannot inspect unacknowledged Ledger events") from error
        return row is not None

    def summarize_requests(self, request_ids: Iterable[UUID]) -> dict[UUID, RequestEventSummary]:
        """Return the highest event sequence and unacknowledged count per known request.

        Read-only. Requests with no local events are absent from the result.
        """

        pending = tuple(dict.fromkeys(request_ids))
        if not pending:
            return {}
        if len(pending) > 500:
            raise ValueError("request summary batch cannot exceed 500 requests")
        placeholders = ", ".join("?" for _ in pending)
        try:
            rows = self._connection.execute(
                "SELECT request_id, MAX(event_seq), "
                "SUM(CASE WHEN acked_at IS NULL THEN 1 ELSE 0 END) "
                f"FROM request_events WHERE request_id IN ({placeholders}) "
                "GROUP BY request_id",
                tuple(str(request_id) for request_id in pending),
            ).fetchall()
        except sqlite3.Error as error:
            raise LedgerStorageError("cannot summarize Ledger requests") from error
        return {
            UUID(str(row[0])): RequestEventSummary(
                max_event_seq=int(row[1]),
                unacked_count=int(row[2]),
            )
            for row in rows
        }

    def mark_acked(self, request_id: UUID, event_seq: int, *, acked_at: datetime) -> bool:
        if event_seq < 1:
            raise ValueError("event_seq must be positive")
        try:
            cursor = self._connection.execute(
                "UPDATE request_events SET acked_at = ? "
                "WHERE request_id = ? AND event_seq = ? AND acked_at IS NULL",
                (_utc_text(acked_at), str(request_id), event_seq),
            )
        except sqlite3.Error as error:
            raise LedgerStorageError("cannot acknowledge Ledger event") from error
        return cursor.rowcount == 1

    def mark_acked_batch(
        self,
        event_keys: Iterable[tuple[UUID, int]],
        *,
        acked_at: datetime,
    ) -> int:
        """Acknowledge one replay batch with one short SQLite UPDATE transaction."""

        pending = tuple(event_keys)
        if not pending:
            return 0
        if len(pending) > 500:
            raise ValueError("acknowledgement batch cannot exceed 500 events")
        if len(set(pending)) != len(pending):
            raise ValueError("acknowledgement batch contains duplicate event keys")
        if any(event_seq < 1 for _, event_seq in pending):
            raise ValueError("event_seq must be positive")

        placeholders = ", ".join("(?, ?)" for _ in pending)
        parameters: list[object] = [_utc_text(acked_at)]
        for request_id, event_seq in pending:
            parameters.extend((str(request_id), event_seq))
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE request_events SET acked_at = ? "
                "WHERE acked_at IS NULL AND (request_id, event_seq) "
                f"IN (VALUES {placeholders})",
                parameters,
            )
            connection.execute("COMMIT")
        except BaseException as error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            if isinstance(error, sqlite3.Error):
                raise LedgerStorageError("cannot acknowledge Ledger event batch") from error
            raise
        return cursor.rowcount

    def delete_acked_before(self, cutoff: datetime, *, limit: int = 500) -> int:
        """Delete one bounded batch whose Ack retention has elapsed.

        The repeated ``acked_at IS NOT NULL`` guard is intentional defense in
        depth: an unacknowledged event is never eligible for an
        automatic cleanup path, even if this query is later refactored.
        """

        if not 1 <= limit <= 1_000:
            raise ValueError("acknowledged cleanup limit must be between 1 and 1000")
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM request_events "
                "WHERE acked_at IS NOT NULL AND rowid IN ("
                "SELECT rowid FROM request_events "
                "WHERE acked_at IS NOT NULL AND acked_at < ? "
                "ORDER BY acked_at, request_id, event_seq LIMIT ?"
                ")",
                (_utc_text(cutoff), limit),
            )
            connection.execute("COMMIT")
        except BaseException as error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            if isinstance(error, sqlite3.Error):
                raise LedgerStorageError("cannot clean acknowledged Ledger events") from error
            raise
        return cursor.rowcount

    def checkpoint_passive(self) -> PassiveCheckpointResult:
        """Run one non-truncating checkpoint and preserve SQLite's full result."""

        if self._connection.in_transaction:
            raise LedgerStorageError("cannot checkpoint inside a Ledger transaction")
        try:
            row = self._connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        except sqlite3.Error as error:
            raise LedgerStorageError("cannot checkpoint the Ledger WAL") from error
        if row is None or len(row) != 3:
            raise LedgerStorageError("Ledger checkpoint returned an invalid result")
        try:
            busy_value, wal_frames, checkpointed_frames = (int(value) for value in row)
        except (TypeError, ValueError) as error:
            raise LedgerStorageError("Ledger checkpoint returned an invalid result") from error
        if busy_value not in {0, 1} or wal_frames < 0 or checkpointed_frames < 0:
            raise LedgerStorageError("Ledger checkpoint returned an invalid result")
        return PassiveCheckpointResult(
            busy=bool(busy_value),
            wal_frames=wal_frames,
            checkpointed_frames=checkpointed_frames,
        )

    def data_version(self) -> int:
        """Return SQLite's connection-local change detector without writing the Ledger."""

        try:
            return int(self._connection.execute("PRAGMA data_version").fetchone()[0])
        except sqlite3.Error as error:
            raise LedgerStorageError("cannot inspect Ledger data version") from error


def open_ledger(
    path: str | os.PathLike[str],
    role: ConnectionRole = ConnectionRole.LEDGER_WRITER,
) -> SQLiteLedger:
    return SQLiteLedger.open(path, role)


__all__ = [
    "BUSY_TIMEOUT_MS",
    "ConnectionRole",
    "DATABASE_MODE",
    "DIRECTORY_MODE",
    "REQUEST_EVENT_COLUMNS",
    "SCHEMA_VERSION",
    "PassiveCheckpointResult",
    "RequestEventSummary",
    "SQLiteLedger",
    "StoredEvent",
    "open_ledger",
]

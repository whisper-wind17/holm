"""Task-owned SQLite Ledger workspace backed by real files and a real WAL.

Everything here operates on an actual database file inside a temporary
directory the fixture creates and destroys itself. There is no in-memory
database and no Mock: the integration-test policy requires WAL behavior to be
proven by a real Integration Test, and TDD section 45/98 require the WAL to
live on local disk.

Scope: this module only builds the environment. Spool Writer, Ledger Writer,
Group Commit scheduling and Ack/Checkpoint policy are M4/M5 product work and
are intentionally absent. ``checkpoint_passive`` is a one-shot test primitive
used to place rows behind the WAL boundary, not a checkpoint scheduler.
"""

from __future__ import annotations

import shutil
import sqlite3
import struct
import tempfile
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Final

from .models import (
    BUSY_TIMEOUT_MS,
    DATABASE_HEADER_READ_VERSION_OFFSET,
    DATABASE_HEADER_WRITE_VERSION_OFFSET,
    EXPECTED_JOURNAL_MODE,
    EXPECTED_SYNCHRONOUS,
    METADATA_COLUMN_ALLOWLIST,
    ConnectionRole,
    LedgerEvent,
    LedgerFixtureError,
    PragmaState,
    WalFrame,
)

TASK_ROOT_PARENT: Final[Path] = Path("/tmp")
TASK_ROOT_PREFIX: Final = "holm-m0-06f-"
DATABASE_FILENAME: Final = "ledger.db"
LEDGER_TABLE: Final = "request_events"

WAL_HEADER_SIZE: Final = 32
WAL_FRAME_HEADER_SIZE: Final = 24
WAL_MAGIC: Final = (0x377F0682, 0x377F0683)
WAL_SALT_OFFSET: Final = 16
WAL_FRAME_CHECKSUM_OFFSET: Final = 16
WAL_FRAME_CHECKSUM_LENGTH: Final = 8

#: Mount types that TDD section 98 forbids for the SQLite WAL.
NETWORK_FILESYSTEMS: Final = frozenset(
    {
        "9p",
        "afs",
        "ceph",
        "cifs",
        "davfs",
        "fuse.sshfs",
        "glusterfs",
        "lustre",
        "ncpfs",
        "nfs",
        "nfs4",
        "smb2",
        "smb3",
        "smbfs",
    }
)

#: Minimal illustrative table. This is NOT the M5-01 Ledger Schema: there is no
#: ``payload_json`` column, so a request body has nowhere to land even if a
#: future caller tried to write one.
_CREATE_LEDGER_TABLE: Final = f"""
CREATE TABLE {LEDGER_TABLE} (
  request_id TEXT NOT NULL,
  event_seq INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  created_at TEXT NOT NULL,
  http_status INTEGER,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  PRIMARY KEY (request_id, event_seq)
)
"""

_INSERT_EVENT: Final = f"""
INSERT INTO {LEDGER_TABLE} (
  request_id, event_seq, event_type, created_at,
  http_status, prompt_tokens, completion_tokens
) VALUES (?, ?, ?, ?, ?, ?, ?)
"""


def filesystem_type(path: Path) -> str:
    """Return the filesystem type of the mount that holds ``path``."""
    target = str(path.resolve())
    best_mount = ""
    best_type = ""
    with open("/proc/self/mountinfo", encoding="utf-8") as handle:
        for line in handle:
            fields = line.split(" ")
            try:
                separator = fields.index("-")
            except ValueError:  # pragma: no cover - malformed kernel line
                continue
            mount_point = fields[4]
            if target != mount_point and not target.startswith(mount_point.rstrip("/") + "/"):
                continue
            if len(mount_point) >= len(best_mount):
                best_mount = mount_point
                best_type = fields[separator + 1]
    if not best_type:  # pragma: no cover - / is always mounted
        raise LedgerFixtureError(f"no mount entry covers {target}")
    return best_type


def assert_local_disk(path: Path) -> str:
    """Fail closed when the WAL would live on a network filesystem."""
    found = filesystem_type(path)
    if found in NETWORK_FILESYSTEMS:
        raise LedgerFixtureError(
            f"SQLite WAL must stay on local disk; {path} is on {found} (TDD section 98)"
        )
    return found


def _scalar_int(connection: sqlite3.Connection, statement: str) -> int:
    row = connection.execute(statement).fetchone()
    if row is None:
        raise LedgerFixtureError(f"{statement} returned no row")
    return int(row[0])


def _scalar_text(connection: sqlite3.Connection, statement: str) -> str:
    row = connection.execute(statement).fetchone()
    if row is None:
        raise LedgerFixtureError(f"{statement} returned no row")
    return str(row[0])


class SqliteLedger:
    """One real SQLite ledger database file inside the task-owned workspace."""

    def __init__(self, workspace: LedgerWorkspace, path: Path) -> None:
        self._workspace = workspace
        self._path = path

    @property
    def path(self) -> Path:
        """The ``.db`` file itself."""
        return self._path

    @property
    def wal_path(self) -> Path:
        """The ``-wal`` sidecar SQLite creates in WAL mode."""
        return Path(f"{self._path}-wal")

    @property
    def shm_path(self) -> Path:
        """The ``-shm`` shared-memory index sidecar."""
        return Path(f"{self._path}-shm")

    def connect(
        self, role: ConnectionRole = ConnectionRole.GATEWAY_SPOOL_WRITER
    ) -> sqlite3.Connection:
        """Open a real connection with the TDD 14.1 pragma block applied.

        The connection is registered with the workspace so teardown can prove
        that nothing was left open.
        """
        busy_timeout_ms = BUSY_TIMEOUT_MS[role]
        connection = sqlite3.connect(
            str(self._path),
            isolation_level=None,
            timeout=busy_timeout_ms / 1000,
        )
        self._workspace.register(connection)
        try:
            connection.execute("PRAGMA journal_mode=WAL").fetchall()
            connection.execute(f"PRAGMA synchronous={EXPECTED_SYNCHRONOUS.name}")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
            journal_mode = _scalar_text(connection, "PRAGMA journal_mode")
            if journal_mode != EXPECTED_JOURNAL_MODE:
                raise LedgerFixtureError(
                    f"journal_mode read back as {journal_mode!r}, expected "
                    f"{EXPECTED_JOURNAL_MODE!r}"
                )
        except BaseException:
            self._workspace.forget(connection)
            raise
        return connection

    def close_connection(self, connection: sqlite3.Connection) -> None:
        """Close one connection this ledger opened and drop it from the registry."""
        self._workspace.close(connection)

    def create_schema(self) -> None:
        """Create the illustrative body-free table on the real file."""
        connection = self.connect(ConnectionRole.LEDGER_WRITER)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(_CREATE_LEDGER_TABLE)
            connection.execute("COMMIT")
        finally:
            self._workspace.close(connection)

    def pragma_state(self, connection: sqlite3.Connection) -> PragmaState:
        """Read every durability pragma back out of the live connection."""
        return PragmaState(
            journal_mode=_scalar_text(connection, "PRAGMA journal_mode"),
            synchronous=_scalar_int(connection, "PRAGMA synchronous"),
            foreign_keys=_scalar_int(connection, "PRAGMA foreign_keys"),
            wal_autocheckpoint=_scalar_int(connection, "PRAGMA wal_autocheckpoint"),
            busy_timeout=_scalar_int(connection, "PRAGMA busy_timeout"),
            page_size=_scalar_int(connection, "PRAGMA page_size"),
        )

    def pragma_state_from_table_valued_function(
        self, connection: sqlite3.Connection
    ) -> tuple[str, int]:
        """Read the two durability pragmas back through ``SELECT``.

        ``PRAGMA x`` and ``SELECT * FROM pragma_x`` take different code paths
        inside SQLite, so agreeing on both makes the read-back independent of
        how the value was set.
        """
        journal_mode = _scalar_text(connection, "SELECT journal_mode FROM pragma_journal_mode")
        synchronous = _scalar_int(connection, "SELECT synchronous FROM pragma_synchronous")
        return journal_mode, synchronous

    def header_versions(self) -> tuple[int, int]:
        """Return the write/read format versions from the database header bytes."""
        header = self._path.read_bytes()[:100]
        return (
            header[DATABASE_HEADER_WRITE_VERSION_OFFSET],
            header[DATABASE_HEADER_READ_VERSION_OFFSET],
        )

    def table_columns(self, connection: sqlite3.Connection) -> tuple[str, ...]:
        """Read the real column names back out of the database."""
        rows = connection.execute(f"PRAGMA table_info({LEDGER_TABLE})").fetchall()
        return tuple(str(row[1]) for row in rows)

    def column_types(self, connection: sqlite3.Connection) -> dict[str, str]:
        """Read the declared column types back out of the database."""
        rows = connection.execute(f"PRAGMA table_info({LEDGER_TABLE})").fetchall()
        return {str(row[1]): str(row[2]) for row in rows}

    def append_event(self, connection: sqlite3.Connection, event: LedgerEvent) -> None:
        """Commit one event in its own transaction."""
        self.append_events(connection, (event,))

    def append_events(self, connection: sqlite3.Connection, events: Sequence[LedgerEvent]) -> None:
        """Commit a batch of events in one transaction.

        Batching here is only a test convenience. It is not the TDD 14.1 Group
        Commit scheduler, which M5 owns.
        """
        connection.execute("BEGIN IMMEDIATE")
        try:
            for event in events:
                connection.execute(_INSERT_EVENT, event.as_row())
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    def event_count(self, connection: sqlite3.Connection) -> int:
        """Count the durable rows currently visible to this connection."""
        return _scalar_int(connection, f"SELECT count(*) FROM {LEDGER_TABLE}")

    def request_ids(self, connection: sqlite3.Connection) -> tuple[str, ...]:
        """Return every stored Request ID in a stable order."""
        rows = connection.execute(
            f"SELECT request_id FROM {LEDGER_TABLE} ORDER BY request_id, event_seq"
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def integrity_check(self, connection: sqlite3.Connection) -> str:
        """Run SQLite's own integrity check and return its verdict."""
        return _scalar_text(connection, "PRAGMA integrity_check")

    def checkpoint_passive(self, connection: sqlite3.Connection) -> tuple[int, int, int]:
        """Run one PASSIVE checkpoint as a test setup primitive."""
        row = connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        if row is None:
            raise LedgerFixtureError("wal_checkpoint returned no row")
        return (int(row[0]), int(row[1]), int(row[2]))

    def wal_frames(self) -> tuple[WalFrame, ...]:
        """Parse the real ``-wal`` file into frames.

        Frames whose salt differs from the current WAL header belong to an
        earlier run that a checkpoint already reset, so they are reported but
        marked as not belonging to the current run.
        """
        if not self.wal_path.exists():
            return ()
        raw = self.wal_path.read_bytes()
        if len(raw) < WAL_HEADER_SIZE:
            return ()
        magic, _version, page_size = struct.unpack(">III", raw[:12])
        if magic not in WAL_MAGIC:
            return ()
        header_salt = struct.unpack(">II", raw[WAL_SALT_OFFSET : WAL_SALT_OFFSET + 8])
        stride = WAL_FRAME_HEADER_SIZE + page_size
        frames: list[WalFrame] = []
        offset = WAL_HEADER_SIZE
        index = 0
        while offset + stride <= len(raw):
            page_number, commit_size, salt_1, salt_2 = struct.unpack(
                ">IIII", raw[offset : offset + WAL_FRAME_CHECKSUM_OFFSET]
            )
            frames.append(
                WalFrame(
                    index=index,
                    offset=offset,
                    page_number=page_number,
                    database_size_after_commit=commit_size,
                    salt=(salt_1, salt_2),
                    belongs_to_current_run=(salt_1, salt_2) == header_salt,
                )
            )
            offset += stride
            index += 1
        return tuple(frames)

    def live_wal_frames(self) -> tuple[WalFrame, ...]:
        """Frames the next recovery would actually replay."""
        return tuple(frame for frame in self.wal_frames() if frame.belongs_to_current_run)

    def crash_snapshot(self, name: str) -> SqliteLedger:
        """Copy the database and its uncheckpointed WAL, as a crash would leave them.

        The ``-shm`` index is deliberately not copied: SQLite rebuilds it by
        recovering the WAL, which is exactly the path under test.
        """
        if not self.wal_path.exists():
            raise LedgerFixtureError("crash snapshot requires a live WAL on disk")
        target_directory = self._workspace.root / "snapshots" / name
        target_directory.mkdir(parents=True)
        target = target_directory / DATABASE_FILENAME
        shutil.copy2(self._path, target)
        shutil.copy2(self.wal_path, Path(f"{target}-wal"))
        return SqliteLedger(self._workspace, target)

    def durable_bytes(self) -> bytes:
        """Every byte SQLite has written for this ledger, database plus WAL."""
        chunks = [self._path.read_bytes()]
        if self.wal_path.exists():
            chunks.append(self.wal_path.read_bytes())
        return b"".join(chunks)

    def assert_columns_are_metadata_only(self, connection: sqlite3.Connection) -> None:
        """Fail closed when a column outside the metadata allowlist appears."""
        unexpected = set(self.table_columns(connection)) - set(METADATA_COLUMN_ALLOWLIST)
        if unexpected:
            raise LedgerFixtureError(
                "ledger table carries non-allowlisted columns: " + ", ".join(sorted(unexpected))
            )


class LedgerWorkspace:
    """Owns the temporary directory, every connection, and their destruction."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._connections: list[sqlite3.Connection] = []
        self._filesystem_type = assert_local_disk(root)

    @classmethod
    def create(cls) -> LedgerWorkspace:
        """Create a task-owned temporary directory on local disk."""
        root = Path(tempfile.mkdtemp(prefix=TASK_ROOT_PREFIX, dir=TASK_ROOT_PARENT))
        try:
            root.chmod(0o700)
            return cls(root)
        except BaseException:
            shutil.rmtree(root, ignore_errors=True)
            raise

    @property
    def root(self) -> Path:
        """The temporary directory this workspace owns exclusively."""
        return self._root

    @property
    def filesystem_type(self) -> str:
        """The filesystem holding the workspace, proven not to be a network mount."""
        return self._filesystem_type

    @property
    def connections(self) -> tuple[sqlite3.Connection, ...]:
        """Every connection the workspace opened and has not closed."""
        return tuple(self._connections)

    def ledger(self, name: str = "primary") -> SqliteLedger:
        """Create a new real ledger database with the illustrative table."""
        directory = self._root / name
        directory.mkdir(parents=True)
        ledger = SqliteLedger(self, directory / DATABASE_FILENAME)
        ledger.create_schema()
        return ledger

    def register(self, connection: sqlite3.Connection) -> None:
        """Track a connection so teardown can prove it was released."""
        self._connections.append(connection)

    def forget(self, connection: sqlite3.Connection) -> None:
        """Close and untrack a connection that failed during setup."""
        self.close(connection)

    def close(self, connection: sqlite3.Connection) -> None:
        """Close one tracked connection."""
        try:
            connection.close()
        finally:
            if connection in self._connections:
                self._connections.remove(connection)

    def close_all(self) -> None:
        """Close every connection this workspace opened."""
        errors: list[BaseException] = []
        for connection in tuple(self._connections):
            try:
                self.close(connection)
            except Exception as error:  # teardown must continue past one failure
                errors.append(error)
        if errors:
            raise LedgerFixtureError(f"{len(errors)} connections failed to close") from errors[0]

    def database_artifacts(self) -> tuple[Path, ...]:
        """Every database, WAL and SHM file still present under the workspace."""
        if not self._root.exists():
            return ()
        suffixes = (".db", ".db-wal", ".db-shm")
        return tuple(
            sorted(
                path
                for path in self._root.rglob("*")
                if path.is_file() and path.name.endswith(suffixes)
            )
        )

    def destroy(self) -> None:
        """Close everything and remove the temporary directory."""
        self.close_all()
        resolved = self._root.resolve()
        if resolved.parent != TASK_ROOT_PARENT.resolve() or not resolved.name.startswith(
            TASK_ROOT_PREFIX
        ):
            raise LedgerFixtureError(f"refusing to remove unsafe SQLite test path: {resolved}")
        shutil.rmtree(resolved, ignore_errors=False)


def closed_connections(connections: Iterable[sqlite3.Connection]) -> tuple[bool, ...]:
    """Probe each connection and report whether it is really closed."""
    results: list[bool] = []
    for connection in connections:
        try:
            connection.execute("SELECT 1").fetchone()
        except sqlite3.ProgrammingError:
            results.append(True)
        else:
            results.append(False)
    return tuple(results)

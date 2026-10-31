"""Fault injection for the SQLite Ledger WAL fixture (M0-06F).

Two independent failure classes are covered, each asserted separately by the
Integration tests:

* **WAL corruption** — real bytes are rewritten inside a real ``-wal`` file,
  either in the WAL header or in one frame header. Nothing is stubbed; SQLite
  performs its own recovery over the damaged file.
* **Write failure** — a write is forced to fail. ``exhaust_database_capacity``
  produces ``SQLITE_FULL`` ("database or disk is full"), and ``block_wal_path``
  makes the WAL impossible to open, producing ``SQLITE_CANTOPEN``. Both work
  for uid 0, which matters because the execution wrapper executes as root inside the
  development container and root ignores permission bits.

Known limitation (registered under M0-06F in the development backlog):
a genuine ``fsync(2)`` ``EIO`` cannot be injected from the standard library.
It needs an error-injecting filesystem or a syscall interposer, neither of
which is available to this fixture. The reachable durability failure classes
above are used instead and are marked ``待真机复核``.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from .ledger import (
    WAL_FRAME_CHECKSUM_LENGTH,
    WAL_FRAME_CHECKSUM_OFFSET,
    SqliteLedger,
)
from .models import ConnectionRole, LedgerFixtureError, WalCorruption

#: SQLite's own ceiling for ``max_page_count``.
DEFAULT_MAX_PAGE_COUNT: Final = 1_073_741_823

_INVALID_WAL_MAGIC: Final = b"\x00\x00\x00\x00"


def _rewrite(path: Path, offset: int, replacement: bytes) -> WalCorruption:
    raw = bytearray(path.read_bytes())
    end = offset + len(replacement)
    if end > len(raw):
        raise LedgerFixtureError(f"cannot rewrite {offset}..{end} of a {len(raw)} byte file")
    original = bytes(raw[offset:end])
    raw[offset:end] = replacement
    path.write_bytes(bytes(raw))
    return WalCorruption(
        path=str(path),
        offset=offset,
        original_bytes=original,
        corrupted_bytes=replacement,
        frame_index=None,
    )


def corrupt_wal_header(ledger: SqliteLedger) -> WalCorruption:
    """Invalidate the WAL magic so recovery discards every frame.

    SQLite treats a WAL whose header does not validate as empty. Everything
    committed since the last checkpoint is therefore lost silently, which is
    precisely why TDD 18.x classifies WAL corruption as Ledger Unavailable
    instead of relying on ``PRAGMA integrity_check``.
    """
    if not ledger.wal_path.exists():
        raise LedgerFixtureError("no WAL file to corrupt")
    original_magic = ledger.wal_path.read_bytes()[:4]
    if original_magic == _INVALID_WAL_MAGIC:
        raise LedgerFixtureError("WAL header is already invalid")
    return _rewrite(ledger.wal_path, 0, _INVALID_WAL_MAGIC)


def corrupt_wal_frame_after_commits(ledger: SqliteLedger, surviving_commits: int) -> WalCorruption:
    """Break the checksum of the frame that follows the Nth surviving commit.

    WAL recovery stops at the first frame whose checksum does not validate, so
    every transaction after ``surviving_commits`` is dropped while the earlier
    ones stay readable.
    """
    if surviving_commits < 0:
        raise LedgerFixtureError("surviving_commits must not be negative")
    frames = ledger.live_wal_frames()
    if not frames:
        raise LedgerFixtureError("the WAL holds no frame from the current run")
    commits = [frame for frame in frames if frame.is_commit]
    if len(commits) <= surviving_commits:
        raise LedgerFixtureError(
            f"the WAL holds {len(commits)} commit frames; cannot keep {surviving_commits}"
        )
    if surviving_commits == 0:
        target = frames[0]
    else:
        last_survivor = commits[surviving_commits - 1]
        following = [frame for frame in frames if frame.index > last_survivor.index]
        if not following:
            raise LedgerFixtureError("no frame follows the last surviving commit")
        target = following[0]

    offset = target.offset + WAL_FRAME_CHECKSUM_OFFSET
    current = ledger.wal_path.read_bytes()[offset : offset + WAL_FRAME_CHECKSUM_LENGTH]
    flipped = bytes(byte ^ 0xFF for byte in current)
    corruption = _rewrite(ledger.wal_path, offset, flipped)
    return WalCorruption(
        path=corruption.path,
        offset=corruption.offset,
        original_bytes=corruption.original_bytes,
        corrupted_bytes=corruption.corrupted_bytes,
        frame_index=target.index,
    )


def block_wal_path(ledger: SqliteLedger) -> Path:
    """Make the WAL impossible to open by putting a directory in its place.

    A read-only parent directory would not work here: the container executes as
    root, and root ignores the permission bits. Replacing the path with a
    directory fails for every uid.
    """
    for sidecar in (ledger.wal_path, ledger.shm_path):
        if sidecar.is_dir():
            raise LedgerFixtureError(f"{sidecar} is already blocked")
        if sidecar.exists():
            sidecar.unlink()
    ledger.wal_path.mkdir()
    return ledger.wal_path


def unblock_wal_path(ledger: SqliteLedger) -> None:
    """Remove the blocking directory so the ledger can recover."""
    if not ledger.wal_path.is_dir():
        raise LedgerFixtureError("the WAL path is not blocked")
    shutil.rmtree(ledger.wal_path)


def exhaust_database_capacity(connection: sqlite3.Connection) -> int:
    """Cap the database at its current size so the next write cannot allocate.

    SQLite reports the capped write as ``SQLITE_FULL`` with the message
    ``database or disk is full`` — the same error class TDD 18.x lists as
    "SQLite IO Error/Full".
    """
    row = connection.execute("PRAGMA page_count").fetchone()
    if row is None:
        raise LedgerFixtureError("PRAGMA page_count returned no row")
    page_count = int(row[0])
    applied = connection.execute(f"PRAGMA max_page_count={page_count}").fetchone()
    if applied is None or int(applied[0]) != page_count:
        raise LedgerFixtureError("max_page_count was not applied")
    return page_count


def restore_database_capacity(connection: sqlite3.Connection) -> None:
    """Lift the artificial capacity cap so recovery can be observed."""
    connection.execute(f"PRAGMA max_page_count={DEFAULT_MAX_PAGE_COUNT}").fetchall()


def sqlite_error_name(error: sqlite3.Error) -> str:
    """Return SQLite's symbolic error name for precise assertions."""
    name = getattr(error, "sqlite_errorname", None)
    if not isinstance(name, str):  # pragma: no cover - available since Python 3.11
        raise LedgerFixtureError("sqlite3.Error is missing sqlite_errorname")
    return name


@contextmanager
def held_write_transaction(
    ledger: SqliteLedger, role: ConnectionRole = ConnectionRole.LEDGER_WRITER
) -> Iterator[sqlite3.Connection]:
    """Hold the single SQLite write lock for the duration of the block."""
    connection = ledger.connect(role)
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    finally:
        try:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
        finally:
            ledger.close_connection(connection)

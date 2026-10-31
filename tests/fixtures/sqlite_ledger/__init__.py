"""Standard-library SQLite reliable-queue (Ledger WAL) fixture for M0-06F.

This closes the last gap in development plan M0-06, whose task text names
"PostgreSQL、SQLite、Fake Runtime、Fake Model Source、Fake Host Agent". The
PostgreSQL fixture ships with M0-05A/B; FX-RT, FX-NV, FX-HA and FX-MS ship with
M0-06A~E.

Source and version: Python standard-library ``sqlite3`` only — no dependency is
added. The behavior it exercises is that of the SQLite library bundled with the
container's CPython 3.12 build.

Review status: every value and expectation here is ``待真机复核`` and must be
reconciled against certified hardware and the real Gateway during G1
(development plan 16.2). In particular the busy-timeout budgets and the
consequences of WAL corruption are configuration-level facts of this fixture,
not measured production behavior.

Scope: environment only. No Gateway, Spool Writer, Ledger Writer, Group Commit
scheduler or Ack/Checkpoint policy is implemented here — those are M4/M5 — and
``request_events`` is a minimal illustrative table, not the M5-01 Ledger
Schema.

Fixture IDs: TST section 3 reserves the ``FX-`` prefix but does not yet
enumerate SQLite fixtures, so ``FX-SQ-01``~``FX-SQ-05`` are introduced by task
card M0-06F and require TST reconciliation during G1.
"""

from .faults import (
    DEFAULT_MAX_PAGE_COUNT,
    block_wal_path,
    corrupt_wal_frame_after_commits,
    corrupt_wal_header,
    exhaust_database_capacity,
    held_write_transaction,
    restore_database_capacity,
    sqlite_error_name,
    unblock_wal_path,
)
from .ledger import (
    DATABASE_FILENAME,
    LEDGER_TABLE,
    NETWORK_FILESYSTEMS,
    TASK_ROOT_PARENT,
    TASK_ROOT_PREFIX,
    WAL_FRAME_HEADER_SIZE,
    WAL_HEADER_SIZE,
    LedgerWorkspace,
    SqliteLedger,
    assert_local_disk,
    closed_connections,
    filesystem_type,
)
from .models import (
    BUSY_TIMEOUT_MS,
    DATABASE_HEADER_READ_VERSION_OFFSET,
    DATABASE_HEADER_WAL_VERSION,
    DATABASE_HEADER_WRITE_VERSION_OFFSET,
    EXPECTED_FOREIGN_KEYS,
    EXPECTED_JOURNAL_MODE,
    EXPECTED_SYNCHRONOUS,
    EXPECTED_WAL_AUTOCHECKPOINT,
    FIXTURE_REVIEW_STATUS,
    GATEWAY_BUSY_TIMEOUT_MS,
    LEDGER_BUSY_TIMEOUT_MS,
    METADATA_COLUMN_ALLOWLIST,
    READER_BUSY_TIMEOUT_MS,
    ConnectionRole,
    EventType,
    FixtureId,
    LedgerEvent,
    LedgerFixtureError,
    PragmaState,
    SynchronousLevel,
    WalCorruption,
    WalFrame,
)

__all__ = [
    "BUSY_TIMEOUT_MS",
    "DATABASE_FILENAME",
    "DATABASE_HEADER_READ_VERSION_OFFSET",
    "DATABASE_HEADER_WAL_VERSION",
    "DATABASE_HEADER_WRITE_VERSION_OFFSET",
    "DEFAULT_MAX_PAGE_COUNT",
    "EXPECTED_FOREIGN_KEYS",
    "EXPECTED_JOURNAL_MODE",
    "EXPECTED_SYNCHRONOUS",
    "EXPECTED_WAL_AUTOCHECKPOINT",
    "FIXTURE_REVIEW_STATUS",
    "GATEWAY_BUSY_TIMEOUT_MS",
    "LEDGER_BUSY_TIMEOUT_MS",
    "LEDGER_TABLE",
    "METADATA_COLUMN_ALLOWLIST",
    "NETWORK_FILESYSTEMS",
    "READER_BUSY_TIMEOUT_MS",
    "TASK_ROOT_PARENT",
    "TASK_ROOT_PREFIX",
    "WAL_FRAME_HEADER_SIZE",
    "WAL_HEADER_SIZE",
    "ConnectionRole",
    "EventType",
    "FixtureId",
    "LedgerEvent",
    "LedgerFixtureError",
    "LedgerWorkspace",
    "PragmaState",
    "SqliteLedger",
    "SynchronousLevel",
    "WalCorruption",
    "WalFrame",
    "assert_local_disk",
    "block_wal_path",
    "closed_connections",
    "corrupt_wal_frame_after_commits",
    "corrupt_wal_header",
    "exhaust_database_capacity",
    "filesystem_type",
    "held_write_transaction",
    "restore_database_capacity",
    "sqlite_error_name",
    "unblock_wal_path",
]

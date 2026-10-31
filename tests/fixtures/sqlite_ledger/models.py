"""Typed models for the standard-library SQLite Ledger WAL fixture (M0-06F).

The event model deliberately has no free-text field. TDD 14.7 and the privacy policy
forbid request/response bodies, Messages, Prompts and Secrets from
reaching SQLite or its WAL, so the fixture makes a body structurally
unrepresentable instead of filtering it after the fact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import IntEnum, StrEnum
from typing import Final

FIXTURE_REVIEW_STATUS: Final = "待真机复核"


#: TST section 3 reserves the ``FX-`` prefix for test doubles but does not yet
#: enumerate SQLite fixtures. These M0-06F identifiers cover SQLite WAL tests
#: for M0-06 and must be reconciled with TST during G1.
class FixtureId(StrEnum):
    """SQLite Ledger WAL fixture identifiers introduced by M0-06F."""

    FX_SQ_01 = "FX-SQ-01"
    """Real on-disk database with WAL journalling and read-back pragmas."""

    FX_SQ_02 = "FX-SQ-02"
    """Crash snapshot: database plus uncheckpointed WAL copied mid-flight."""

    FX_SQ_03 = "FX-SQ-03"
    """WAL corruption: invalid WAL header and invalid frame checksum."""

    FX_SQ_04 = "FX-SQ-04"
    """Write failure: the database cannot grow, and the WAL cannot be opened."""

    FX_SQ_05 = "FX-SQ-05"
    """Concurrent connections: writer contention and non-blocking readers."""


class EventType(StrEnum):
    """Ledger event types carried as metadata only (TDD 12.3, 14.1)."""

    RECEIVED = "Received"
    REJECTED = "Rejected"
    IN_PROGRESS = "InProgress"
    COMPLETED = "Completed"


class SynchronousLevel(IntEnum):
    """Values ``PRAGMA synchronous`` reads back, per the SQLite pragma contract."""

    OFF = 0
    NORMAL = 1
    FULL = 2
    EXTRA = 3


class ConnectionRole(StrEnum):
    """Busy-timeout profiles from TDD 14.1.

    The role only selects ``busy_timeout``. No Spool Writer, Ledger Writer,
    Group Commit or Ack behavior is implemented here; those belong to M4/M5.
    """

    GATEWAY_SPOOL_WRITER = "gateway-spool-writer"
    LEDGER_WRITER = "ledger-writer"
    READER = "reader"


#: TDD 14.1: the Gateway writer must fail fast so lock waits never enter TTFT.
GATEWAY_BUSY_TIMEOUT_MS: Final = 100
#: TDD 14.1: the Ledger Writer/Ack connection may wait longer.
LEDGER_BUSY_TIMEOUT_MS: Final = 2_000
#: Readers never take the write lock in WAL mode; the value only bounds misuse.
READER_BUSY_TIMEOUT_MS: Final = 100

BUSY_TIMEOUT_MS: Final[dict[ConnectionRole, int]] = {
    ConnectionRole.GATEWAY_SPOOL_WRITER: GATEWAY_BUSY_TIMEOUT_MS,
    ConnectionRole.LEDGER_WRITER: LEDGER_BUSY_TIMEOUT_MS,
    ConnectionRole.READER: READER_BUSY_TIMEOUT_MS,
}

#: TDD 14.1 pragma block. ``wal_autocheckpoint=0`` disables the commit-thread
#: checkpoint so a user request never pays the checkpoint tail latency.
EXPECTED_JOURNAL_MODE: Final = "wal"
EXPECTED_SYNCHRONOUS: Final = SynchronousLevel.FULL
EXPECTED_FOREIGN_KEYS: Final = 1
EXPECTED_WAL_AUTOCHECKPOINT: Final = 0

#: SQLite database header bytes 18 and 19 hold the file format write/read
#: version. Both are 2 exactly when the file is in WAL mode, which proves WAL
#: from the bytes on disk rather than from a pragma the fixture just set.
DATABASE_HEADER_WAL_VERSION: Final = 2
DATABASE_HEADER_WRITE_VERSION_OFFSET: Final = 18
DATABASE_HEADER_READ_VERSION_OFFSET: Final = 19

#: The only columns the illustrative ledger table may carry. This is not the
#: M5-01 Ledger Schema; it is the minimum needed to prove fixture capability
#: while keeping every column body-free.
METADATA_COLUMN_ALLOWLIST: Final = frozenset(
    {
        "request_id",
        "event_seq",
        "event_type",
        "created_at",
        "http_status",
        "prompt_tokens",
        "completion_tokens",
    }
)

_REQUEST_ID_PATTERN: Final = re.compile(r"\A[0-9A-Za-z_-]{1,64}\Z")
_MAX_TOKEN_COUNT: Final = 10_000_000


class LedgerFixtureError(RuntimeError):
    """Raised when the fixture is driven outside its supported contract."""


@dataclass(frozen=True, slots=True)
class LedgerEvent:
    """One body-free ledger event.

    Every field is metadata: an opaque Request ID, an ordinal, a state, a UTC
    timestamp, an HTTP status and Token counts. There is no payload, message,
    prompt, header or error-text field, so no request body can be expressed.
    """

    request_id: str
    event_seq: int
    event_type: EventType
    created_at: datetime
    http_status: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    def __post_init__(self) -> None:
        if not _REQUEST_ID_PATTERN.match(self.request_id):
            raise ValueError(
                "Request ID must be an opaque token of at most 64 URL-safe "
                "characters; free text is rejected so no body can be smuggled in"
            )
        if self.event_seq < 1:
            raise ValueError("event_seq must start at 1")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() != timedelta(0):
            raise ValueError("created_at must be an aware UTC timestamp")
        if self.http_status is not None and not 100 <= self.http_status <= 599:
            raise ValueError("http_status must be a valid HTTP status code")
        for name, value in (
            ("prompt_tokens", self.prompt_tokens),
            ("completion_tokens", self.completion_tokens),
        ):
            if value is None:
                continue
            if value < 0 or value > _MAX_TOKEN_COUNT:
                raise ValueError(f"{name} must be a non-negative bounded Token count")

    def as_row(self) -> tuple[str, int, str, str, int | None, int | None, int | None]:
        """Return the row in ``METADATA_COLUMN_ALLOWLIST`` order."""
        return (
            self.request_id,
            self.event_seq,
            str(self.event_type),
            self.created_at.isoformat().replace("+00:00", "Z"),
            self.http_status,
            self.prompt_tokens,
            self.completion_tokens,
        )


@dataclass(frozen=True, slots=True)
class PragmaState:
    """Pragma values read back out of a live connection, never assumed."""

    journal_mode: str
    synchronous: int
    foreign_keys: int
    wal_autocheckpoint: int
    busy_timeout: int
    page_size: int


@dataclass(frozen=True, slots=True)
class WalFrame:
    """One frame parsed out of the real ``-wal`` file."""

    index: int
    offset: int
    page_number: int
    database_size_after_commit: int
    salt: tuple[int, int]
    belongs_to_current_run: bool

    @property
    def is_commit(self) -> bool:
        """A frame with a non-zero database size is the commit record."""
        return self.database_size_after_commit != 0


@dataclass(frozen=True, slots=True)
class WalCorruption:
    """What a WAL corruption injection actually changed on disk."""

    path: str
    offset: int
    original_bytes: bytes
    corrupted_bytes: bytes
    frame_index: int | None

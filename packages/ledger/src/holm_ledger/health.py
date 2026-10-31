"""Ledger backlog protection: periodic verdict, hot-path-free reads.

The Gateway must reject new requests *before* forwarding them once the local
Ledger can no longer be trusted, but it must never pay for that decision on the
request path. ``LedgerHealthMonitor`` therefore samples the three backlog
inputs on its own interval - the oldest unacknowledged event's age, the Spool
Writer's own state, and the Ledger filesystem - and publishes a single
in-memory verdict that the hot path reads with pure arithmetic.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from holm_domain import (
    LEDGER_DELAYED_AFTER_SECONDS,
    LEDGER_DISK_CRITICAL_FREE_BYTES,
    LEDGER_DISK_CRITICAL_FREE_RATIO,
    LEDGER_UNAVAILABLE_AFTER_SECONDS,
    LedgerHealth,
    classify_backlog_age,
    is_disk_critical,
    worst_ledger_health,
)
from holm_domain.ledger_health import LedgerBacklogDetails

from holm_ledger.errors import LedgerStorageError
from holm_ledger.spool import SpoolWriterState
from holm_ledger.store import ConnectionRole, _parse_utc, open_ledger

_LOGGER: Final[logging.Logger] = logging.getLogger("holm.ledger.health")
DEFAULT_LEDGER_HEALTH_INTERVAL_SECONDS: Final[float] = 5.0

type MonotonicClock = Callable[[], float]
type WallClock = Callable[[], datetime]
type DiskUsage = Callable[[str | os.PathLike[str]], tuple[int, int, int]]


@dataclass(frozen=True, slots=True)
class LedgerDiskStatus:
    """One filesystem observation for the directory that holds the Ledger."""

    total_bytes: int
    free_bytes: int
    critical: bool


@dataclass(frozen=True, slots=True)
class LedgerBacklogSample:
    """One completed observation of the three backlog inputs."""

    observed_at: datetime
    oldest_unacked_at: datetime | None
    oldest_unacked_age_seconds: float | None
    disk: LedgerDiskStatus
    details: LedgerBacklogDetails | None = None


class LedgerBacklogSource(Protocol):
    """The read side the monitor needs; any Ledger connection satisfies it."""

    def oldest_unacked_created_at(self) -> datetime | None: ...

    def close(self) -> None: ...


@runtime_checkable
class LedgerBacklogDetailsSource(Protocol):
    def backlog_details(self) -> LedgerBacklogDetails: ...


class SpoolWriterStateSource(Protocol):
    @property
    def state(self) -> SpoolWriterState: ...


class ReadOnlyLedgerBacklogSource:
    """A query-only SQLite handle suitable for a least-privilege status reader.

    The handle can be opened while a launcher still has access to the Ledger's
    0700 directory and retained after privileges are dropped.  Unlike
    ``open_ledger``, it never initializes schema or changes journal settings.
    """

    __slots__ = ("_connection",)

    def __init__(self, path: str | os.PathLike[str]) -> None:
        uri = f"{Path(path).absolute().as_uri()}?mode=ro"
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                uri,
                uri=True,
                isolation_level=None,
                check_same_thread=False,
            )
            connection.execute("PRAGMA query_only=ON")
            # Force SQLite to open and validate the WAL read path before a
            # caller drops privileges; no application row is read here.
            connection.execute("SELECT 1 FROM request_events LIMIT 1").fetchone()
        except (OSError, sqlite3.Error) as error:
            if connection is not None:
                connection.close()
            raise LedgerStorageError("cannot open Ledger backlog reader") from error
        assert connection is not None
        self._connection = connection

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

    def close(self) -> None:
        self._connection.close()


def probe_ledger_disk(
    path: str | os.PathLike[str],
    *,
    disk_usage: DiskUsage = shutil.disk_usage,
    minimum_free_ratio: float = LEDGER_DISK_CRITICAL_FREE_RATIO,
    minimum_free_bytes: int = LEDGER_DISK_CRITICAL_FREE_BYTES,
) -> LedgerDiskStatus:
    """Measure the filesystem that holds the Ledger, never the file itself."""

    directory = Path(path).absolute().parent
    total, _used, free = disk_usage(directory)
    return LedgerDiskStatus(
        total_bytes=int(total),
        free_bytes=int(free),
        critical=is_disk_critical(
            total_bytes=int(total),
            free_bytes=int(free),
            minimum_free_ratio=minimum_free_ratio,
            minimum_free_bytes=minimum_free_bytes,
        ),
    )


def backlog_age_seconds(oldest_unacked_at: datetime | None, observed_at: datetime) -> float | None:
    """Difference two UTC Ledger timestamps; never returns a negative age."""

    if oldest_unacked_at is None:
        return None
    return max(0.0, (observed_at - oldest_unacked_at).total_seconds())


class LedgerHealthMonitor:
    """Publish one backlog verdict for the Gateway's admission decision.

    The verdict is recomputed from the last successful sample on every read, so
    a stalled or failing sampler ages into ``unavailable`` instead of silently
    reporting stale good news. Reads touch neither SQLite nor PostgreSQL.
    """

    __slots__ = (
        "_clock",
        "_delayed_after_seconds",
        "_disk_critical",
        "_disk_usage",
        "_interval_seconds",
        "_latched",
        "_minimum_free_bytes",
        "_minimum_free_ratio",
        "_oldest_age_seconds",
        "_path",
        "_sample",
        "_sampled_at",
        "_source",
        "_source_factory",
        "_task",
        "_unavailable_after_seconds",
        "_utc_now",
        "_writer",
    )

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        writer: SpoolWriterStateSource | None = None,
        delayed_after_seconds: float = LEDGER_DELAYED_AFTER_SECONDS,
        unavailable_after_seconds: float = LEDGER_UNAVAILABLE_AFTER_SECONDS,
        interval_seconds: float = DEFAULT_LEDGER_HEALTH_INTERVAL_SECONDS,
        minimum_free_ratio: float = LEDGER_DISK_CRITICAL_FREE_RATIO,
        minimum_free_bytes: int = LEDGER_DISK_CRITICAL_FREE_BYTES,
        clock: MonotonicClock = time.monotonic,
        utc_now: WallClock = lambda: datetime.now(UTC),
        disk_usage: DiskUsage = shutil.disk_usage,
        source_factory: Callable[[], LedgerBacklogSource] | None = None,
    ) -> None:
        if delayed_after_seconds <= 0 or unavailable_after_seconds <= 0:
            raise ValueError("Ledger health thresholds must be positive")
        if delayed_after_seconds > unavailable_after_seconds:
            raise ValueError("delayed threshold must not exceed the unavailable threshold")
        if interval_seconds <= 0:
            raise ValueError("Ledger health interval must be positive")
        self._path = Path(path)
        self._writer = writer
        self._delayed_after_seconds = float(delayed_after_seconds)
        self._unavailable_after_seconds = float(unavailable_after_seconds)
        self._interval_seconds = float(interval_seconds)
        self._minimum_free_ratio = minimum_free_ratio
        self._minimum_free_bytes = minimum_free_bytes
        self._clock = clock
        self._utc_now = utc_now
        self._disk_usage = disk_usage
        self._source_factory = source_factory or self._open_reader
        self._source: LedgerBacklogSource | None = None
        self._task: asyncio.Task[None] | None = None
        self._latched = False
        self._sample: LedgerBacklogSample | None = None
        self._oldest_age_seconds: float | None = None
        self._disk_critical = False
        # Grant the configured grace window before an unsampled monitor is
        # treated as unverifiable; otherwise startup would reject every request.
        self._sampled_at = clock()

    def _open_reader(self) -> LedgerBacklogSource:
        return open_ledger(self._path, ConnectionRole.READER)

    @property
    def last_sample(self) -> LedgerBacklogSample | None:
        return self._sample

    @property
    def health(self) -> LedgerHealth:
        """Return the current backlog state; pure in-memory arithmetic."""

        if self._latched:
            return LedgerHealth.UNAVAILABLE
        elapsed = max(0.0, self._clock() - self._sampled_at)
        if elapsed > self._unavailable_after_seconds:
            # The Ledger has not been observed for longer than the Unavailable
            # threshold itself; its state can no longer be asserted.
            return LedgerHealth.UNAVAILABLE
        age = self._oldest_age_seconds
        backlog = classify_backlog_age(
            None if age is None else age + elapsed,
            delayed_after=self._delayed_after_seconds,
            unavailable_after=self._unavailable_after_seconds,
        )
        writer_state = LedgerHealth.AVAILABLE
        if self._writer is not None and self._writer.state is SpoolWriterState.UNAVAILABLE:
            writer_state = LedgerHealth.UNAVAILABLE
        disk_state = LedgerHealth.UNAVAILABLE if self._disk_critical else LedgerHealth.AVAILABLE
        return worst_ledger_health(backlog, writer_state, disk_state)

    def latch_unavailable(self, reason: str) -> None:
        """Stop admitting new requests after a terminal event could not be written.

        Once even a terminal event cannot be
        persisted the Gateway stops accepting connections and emits a
        highest-severity system event that carries no request body.
        """

        if self._latched:
            return
        self._latched = True
        _LOGGER.critical(
            "request Ledger cannot persist terminal events; refusing new requests",
            extra={"component": "gateway", "error_code": reason},
        )

    async def sample_once(self) -> LedgerBacklogSample | None:
        """Take one observation off the event loop; failures keep the last one."""

        try:
            oldest, disk, details = await asyncio.to_thread(self._observe)
        except Exception:
            # The staleness rule in ``health`` turns a persistently failing
            # probe into Unavailable; a single transient lock must not.
            _LOGGER.warning(
                "request Ledger backlog probe failed",
                extra={"component": "gateway", "error_code": "ledger_backlog_probe_failed"},
            )
            return None
        observed_at = self._utc_now()
        sample = LedgerBacklogSample(
            observed_at=observed_at,
            oldest_unacked_at=oldest,
            oldest_unacked_age_seconds=backlog_age_seconds(oldest, observed_at),
            disk=disk,
            details=details,
        )
        self._sample = sample
        self._oldest_age_seconds = sample.oldest_unacked_age_seconds
        self._disk_critical = disk.critical
        self._sampled_at = self._clock()
        return sample

    def _observe(self) -> tuple[datetime | None, LedgerDiskStatus, LedgerBacklogDetails | None]:
        disk = probe_ledger_disk(
            self._path,
            disk_usage=self._disk_usage,
            minimum_free_ratio=self._minimum_free_ratio,
            minimum_free_bytes=self._minimum_free_bytes,
        )
        if self._source is None:
            self._source = self._source_factory()
        if isinstance(self._source, LedgerBacklogDetailsSource):
            details = self._source.backlog_details()
            return details.oldest_event_at, disk, details
        return self._source.oldest_unacked_created_at(), disk, None

    async def start(self) -> None:
        if self._task is not None:
            return
        await self.sample_once()
        self._task = asyncio.create_task(self._run(), name="holm-ledger-health")

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval_seconds)
            await self.sample_once()

    async def aclose(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        source = self._source
        self._source = None
        if source is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(source.close)


__all__ = [
    "DEFAULT_LEDGER_HEALTH_INTERVAL_SECONDS",
    "LedgerBacklogSample",
    "LedgerBacklogSource",
    "LedgerDiskStatus",
    "LedgerHealthMonitor",
    "ReadOnlyLedgerBacklogSource",
    "backlog_age_seconds",
    "probe_ledger_disk",
]

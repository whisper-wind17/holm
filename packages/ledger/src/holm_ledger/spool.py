"""Dedicated-thread, bounded-window Group Commit for Gateway Ledger events."""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from holm_ledger.errors import (
    LedgerSequenceError,
    LedgerStorageError,
    SpoolWriterClosedError,
    SpoolWriterUnavailableError,
)
from holm_ledger.models import LedgerEvent
from holm_ledger.sequence import SequenceAction, SequenceDecision
from holm_ledger.store import ConnectionRole, open_ledger

DEFAULT_BATCH_WINDOW_SECONDS = 0.002
DEFAULT_MAX_BATCH_REQUESTS = 64
MIN_BATCH_WINDOW_SECONDS = 0.000_001
MAX_BATCH_WINDOW_SECONDS = DEFAULT_BATCH_WINDOW_SECONDS
MIN_BATCH_REQUESTS = 1
MAX_BATCH_REQUESTS = DEFAULT_MAX_BATCH_REQUESTS
_START_TIMEOUT_SECONDS = 5.0


class SpoolWriterState(StrEnum):
    NEW = "new"
    STARTING = "starting"
    AVAILABLE = "available"
    CLOSING = "closing"
    UNAVAILABLE = "unavailable"
    CLOSED = "closed"


class _BatchLedger(Protocol):
    def append_batch(self, events: Sequence[LedgerEvent]) -> tuple[SequenceDecision, ...]: ...

    def close(self) -> None: ...


type LedgerFactory = Callable[[str | os.PathLike[str], ConnectionRole], _BatchLedger]
type MonotonicClock = Callable[[], float]
type BeforeCommitHook = Callable[[tuple[LedgerEvent, ...]], None]


@dataclass(slots=True)
class _Submission:
    event: LedgerEvent
    enqueued_at: float
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[SequenceAction]


def _set_result_if_pending(future: asyncio.Future[SequenceAction], result: SequenceAction) -> None:
    if not future.done():
        future.set_result(result)


def _set_exception_if_pending(future: asyncio.Future[SequenceAction], error: BaseException) -> None:
    if not future.done():
        future.set_exception(error)


class SpoolWriter:
    """Serialize Gateway event inserts on one connection owned by one thread.

    ``submit`` completes only after the transaction containing that event has
    committed. The request-count boundary counts distinct ``request_id`` values;
    logical events are always kept as separate, ordered inserts.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        batch_window_seconds: float = DEFAULT_BATCH_WINDOW_SECONDS,
        max_batch_requests: int = DEFAULT_MAX_BATCH_REQUESTS,
        clock: MonotonicClock = time.monotonic,
        ledger_factory: LedgerFactory = open_ledger,
        before_commit: BeforeCommitHook | None = None,
    ) -> None:
        if isinstance(batch_window_seconds, bool) or not isinstance(
            batch_window_seconds, (int, float)
        ):
            raise ValueError("batch window must be a number of seconds")
        if not MIN_BATCH_WINDOW_SECONDS <= batch_window_seconds <= MAX_BATCH_WINDOW_SECONDS:
            raise ValueError("batch window must be between 1 microsecond and 2 milliseconds")
        if isinstance(max_batch_requests, bool) or not isinstance(max_batch_requests, int):
            raise ValueError("maximum batch requests must be an integer")
        if not MIN_BATCH_REQUESTS <= max_batch_requests <= MAX_BATCH_REQUESTS:
            raise ValueError("maximum batch requests must be between 1 and 64")

        self._path = Path(path)
        self._batch_window_seconds = float(batch_window_seconds)
        self._max_batch_requests = max_batch_requests
        self._clock = clock
        self._ledger_factory = ledger_factory
        self._before_commit = before_commit
        self._condition = threading.Condition()
        self._queue: deque[_Submission] = deque()
        self._state = SpoolWriterState.NEW
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._pending_count = 0
        self._batch_count = 0
        self._max_batch_request_count = 0

    @property
    def state(self) -> SpoolWriterState:
        with self._condition:
            return self._state

    @property
    def is_available(self) -> bool:
        return self.state is SpoolWriterState.AVAILABLE

    @property
    def batch_count(self) -> int:
        with self._condition:
            return self._batch_count

    @property
    def max_batch_request_count(self) -> int:
        with self._condition:
            return self._max_batch_request_count

    @property
    def thread_alive(self) -> bool:
        with self._condition:
            thread = self._thread
        return thread is not None and thread.is_alive()

    def has_pending(self) -> bool:
        """Return whether an accepted event still lacks its durable outcome."""

        with self._condition:
            return self._pending_count > 0

    def start(self) -> None:
        """Start the owner thread and open its Gateway-role connection there."""

        with self._condition:
            if self._state is SpoolWriterState.AVAILABLE:
                return
            if self._state is SpoolWriterState.UNAVAILABLE:
                raise SpoolWriterUnavailableError("Spool Writer is unavailable")
            if self._state is not SpoolWriterState.NEW:
                raise SpoolWriterClosedError("Spool Writer cannot be started")
            self._state = SpoolWriterState.STARTING
            self._thread = threading.Thread(
                target=self._run,
                name="holm-ledger-spool-writer",
                daemon=False,
            )
            self._thread.start()

        if not self._started.wait(_START_TIMEOUT_SECONDS):
            raise SpoolWriterUnavailableError("Spool Writer did not start")
        with self._condition:
            state = self._state
        if state is SpoolWriterState.UNAVAILABLE:
            raise SpoolWriterUnavailableError("Spool Writer is unavailable")
        if state is not SpoolWriterState.AVAILABLE:
            raise SpoolWriterClosedError("Spool Writer did not become available")

    async def submit(self, event: LedgerEvent) -> SequenceAction:
        """Queue one event without performing SQLite I/O on the caller's loop."""

        loop = asyncio.get_running_loop()
        future: asyncio.Future[SequenceAction] = loop.create_future()
        with self._condition:
            if self._state is SpoolWriterState.UNAVAILABLE:
                raise SpoolWriterUnavailableError("Spool Writer is unavailable")
            if self._state is not SpoolWriterState.AVAILABLE:
                raise SpoolWriterClosedError("Spool Writer is not accepting events")
            self._queue.append(
                _Submission(
                    event=event,
                    enqueued_at=self._clock(),
                    loop=loop,
                    future=future,
                )
            )
            self._pending_count += 1
            self._condition.notify()
        return await future

    async def close(self) -> None:
        """Stop accepting events, durably drain accepted work, and join the thread."""

        with self._condition:
            if self._state is SpoolWriterState.NEW:
                self._state = SpoolWriterState.CLOSED
                return
            if self._state is SpoolWriterState.CLOSED:
                return
            if self._state in {SpoolWriterState.AVAILABLE, SpoolWriterState.STARTING}:
                self._state = SpoolWriterState.CLOSING
            self._condition.notify_all()
            thread = self._thread
        if thread is not None:
            await asyncio.to_thread(thread.join)

    def _run(self) -> None:
        ledger: _BatchLedger | None = None
        try:
            ledger = self._ledger_factory(self._path, ConnectionRole.GATEWAY_SPOOL_WRITER)
        except Exception:
            with self._condition:
                self._state = SpoolWriterState.UNAVAILABLE
                self._started.set()
                self._condition.notify_all()
            return

        with self._condition:
            if self._state is SpoolWriterState.STARTING:
                self._state = SpoolWriterState.AVAILABLE
            self._started.set()
            self._condition.notify_all()

        try:
            while True:
                batch = self._take_batch()
                if batch is None:
                    break
                events = tuple(item.event for item in batch)
                try:
                    if self._before_commit is not None:
                        self._before_commit(events)
                    decisions = ledger.append_batch(events)
                    if len(decisions) != len(batch):
                        raise LedgerStorageError("Ledger returned an incomplete batch result")
                except Exception:
                    self._enter_storage_protection(batch)
                    break
                self._complete_batch(batch, decisions)
        finally:
            try:
                if ledger is not None:
                    ledger.close()
            finally:
                with self._condition:
                    if self._state is SpoolWriterState.CLOSING:
                        self._state = SpoolWriterState.CLOSED
                    self._condition.notify_all()

    def _take_batch(self) -> tuple[_Submission, ...] | None:
        with self._condition:
            while not self._queue:
                if self._state in {
                    SpoolWriterState.CLOSING,
                    SpoolWriterState.UNAVAILABLE,
                    SpoolWriterState.CLOSED,
                }:
                    return None
                self._condition.wait()

            first = self._queue.popleft()
            batch = [first]
            request_ids = {first.event.request_id}
            deadline = first.enqueued_at + self._batch_window_seconds

            while len(request_ids) < self._max_batch_requests:
                if self._queue:
                    candidate = self._queue[0]
                    if (
                        self._state is not SpoolWriterState.CLOSING
                        and candidate.enqueued_at > deadline
                    ):
                        break
                    candidate = self._queue.popleft()
                    batch.append(candidate)
                    request_ids.add(candidate.event.request_id)
                    continue
                if self._state is SpoolWriterState.CLOSING:
                    break
                remaining = deadline - self._clock()
                if remaining <= 0:
                    break
                self._condition.wait(timeout=remaining)

            return tuple(batch)

    def _complete_batch(
        self,
        batch: tuple[_Submission, ...],
        decisions: tuple[SequenceDecision, ...],
    ) -> None:
        request_count = len({item.event.request_id for item in batch})
        with self._condition:
            self._batch_count += 1
            self._max_batch_request_count = max(self._max_batch_request_count, request_count)
            self._pending_count -= len(batch)
            self._condition.notify_all()

        for item, decision in zip(batch, decisions, strict=True):
            if decision.action is SequenceAction.REJECT:
                self._schedule_exception(item, LedgerSequenceError(decision.reason))
            else:
                self._schedule_result(item, decision.action)

    def _enter_storage_protection(self, batch: tuple[_Submission, ...]) -> None:
        with self._condition:
            queued = tuple(self._queue)
            self._queue.clear()
            self._pending_count = 0
            self._state = SpoolWriterState.UNAVAILABLE
            self._condition.notify_all()

        for item in (*batch, *queued):
            self._schedule_exception(
                item, SpoolWriterUnavailableError("Spool Writer commit failed")
            )

    @staticmethod
    def _schedule_result(item: _Submission, result: SequenceAction) -> None:
        # The caller may cancel and close its loop after the durable write.
        with contextlib.suppress(RuntimeError):
            item.loop.call_soon_threadsafe(_set_result_if_pending, item.future, result)

    @staticmethod
    def _schedule_exception(item: _Submission, error: BaseException) -> None:
        with contextlib.suppress(RuntimeError):
            item.loop.call_soon_threadsafe(_set_exception_if_pending, item.future, error)


__all__ = [
    "DEFAULT_BATCH_WINDOW_SECONDS",
    "DEFAULT_MAX_BATCH_REQUESTS",
    "MAX_BATCH_REQUESTS",
    "MAX_BATCH_WINDOW_SECONDS",
    "MIN_BATCH_REQUESTS",
    "MIN_BATCH_WINDOW_SECONDS",
    "SpoolWriter",
    "SpoolWriterState",
]

"""Atomic admission; only user × service concurrency may wait."""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from uuid import UUID

from holm_domain import ErrorCode
from holm_domain.call_limits import (
    CALL_LIMIT_QUEUE_SIZE,
    CALL_LIMIT_WAIT_SECONDS,
    CALL_LIMIT_WINDOW_SECONDS,
    MAX_REQUESTS_PER_MINUTE,
)

from holm_protocols.models import InferenceFailure, PlatformError

type GrantKey = tuple[UUID, UUID]
type Limits = tuple[int | None, int | None]


@dataclass(eq=False, slots=True)
class _Reservation:
    created_at: float
    started: bool = False


@dataclass(eq=False, slots=True)
class _Waiter:
    wake: asyncio.Event = field(default_factory=asyncio.Event)


class AdmissionPermit:
    def __init__(
        self,
        controller: AdmissionController,
        instance_id: UUID | None,
        key: GrantKey | None = None,
        reservation: _Reservation | None = None,
    ) -> None:
        self._controller = controller
        self._instance_id = instance_id
        self._key = key
        self._reservation = reservation
        self._released = False
        # Compatibility callers of try_acquire have no minute reservation.
        self._started = reservation is None

    async def mark_started(self) -> None:
        """The durable InProgress boundary commits the minute reservation."""
        async with self._controller._lock:
            self._started = True
            if self._reservation is not None and self._key is not None:
                self._reservation.started = True
                window = self._controller._windows.get(self._key, ())
                committed = [entry for entry in window if entry.started]
                # Pending entries must never evict committed history: a rejected
                # request rolls back and must not silently grant extra minute slots.
                for entry in committed[:-MAX_REQUESTS_PER_MINUTE]:
                    self._controller._rollback(self._key, entry)

    async def release(self) -> None:
        await self._controller._release(self)


class AdmissionController:
    """One lock owns minute reservations, FIFO waiters and all active counters."""

    def __init__(
        self,
        limit: int,
        instance_limit: int | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1 or (instance_limit is not None and instance_limit < 1):
            raise ValueError("admission limit must be positive")
        self._limit = limit
        self._instance_limit = limit if instance_limit is None else min(limit, instance_limit)
        self._active = 0
        self._active_by_instance: dict[UUID, int] = {}
        self._active_by_grant: dict[GrantKey, int] = {}
        self._windows: dict[GrantKey, deque[_Reservation]] = {}
        self._queues: dict[GrantKey, deque[_Waiter]] = {}
        self._lock = asyncio.Lock()
        self._clock = clock
        self._last_prune = clock()

    def _platform_acquire(self, instance_id: UUID | None) -> bool:
        if self._active >= self._limit:
            return False
        if instance_id is not None:
            active = self._active_by_instance.get(instance_id, 0)
            if active >= self._instance_limit:
                return False
            self._active_by_instance[instance_id] = active + 1
        self._active += 1
        return True

    async def try_acquire(self, instance_id: UUID | None = None) -> AdmissionPermit | None:
        async with self._lock:
            if not self._platform_acquire(instance_id):
                return None
            return AdmissionPermit(self, instance_id)

    @staticmethod
    def _limited(
        kind: str, limit: int, retry: int = 1, *, timed_out: bool = False
    ) -> InferenceFailure:
        return InferenceFailure(
            PlatformError(
                ErrorCode.RATE_LIMIT_EXCEEDED,
                limit_type=kind,
                limit=limit,
                retry_after=retry,
                queue_timed_out=timed_out,
            )
        )

    def _prune(self, key: GrantKey, now: float) -> None:
        window = self._windows.get(key)
        if window is not None:
            while window and now - window[0].created_at >= CALL_LIMIT_WINDOW_SECONDS:
                window.popleft()
            if not window:
                del self._windows[key]

    def _rollback(self, key: GrantKey, reservation: _Reservation | None) -> None:
        window = self._windows.get(key)
        if window is not None and reservation is not None and reservation in window:
            window.remove(reservation)
            if not window:
                del self._windows[key]

    def _remove_waiter(self, key: GrantKey, waiter: _Waiter | None) -> None:
        queue = self._queues.get(key)
        if queue is not None and waiter is not None and waiter in queue:
            queue.remove(waiter)
            if not queue:
                del self._queues[key]
        self._wake_head(key)

    def _wake_head(self, key: GrantKey) -> None:
        queue = self._queues.get(key)
        if queue:
            queue[0].wake.set()

    async def acquire(
        self,
        instance_id: UUID,
        *,
        user_id: UUID,
        service_id: UUID,
        limits: Callable[[], Limits],
        disconnected: Callable[[], Awaitable[bool]] | None = None,
    ) -> AdmissionPermit | None:
        key = (user_id, service_id)
        reservation: _Reservation | None = None
        waiter: _Waiter | None = None
        queued_at = self._clock()
        try:
            async with self._lock:
                now = self._clock()
                # Bound retained state by the sliding window, including inactive grants.
                if now - self._last_prune >= CALL_LIMIT_WINDOW_SECONDS:
                    for old_key in tuple(self._windows):
                        self._prune(old_key, now)
                    self._last_prune = now
                self._prune(key, now)
                concurrent, per_minute = limits()
                window = self._windows.get(key)
                if per_minute is not None and window is not None and len(window) >= per_minute:
                    raise self._limited(
                        "per_minute",
                        per_minute,
                        max(1, math.ceil(CALL_LIMIT_WINDOW_SECONDS - (now - window[0].created_at))),
                    )
                reservation = _Reservation(now)
                # Keeping the latest 600 entries also supports lowering an unlimited
                # grant; no supported minute limit needs older entries than these.
                self._windows.setdefault(key, deque()).append(reservation)
                if self._queues.get(key) or (
                    concurrent is not None and self._active_by_grant.get(key, 0) >= concurrent
                ):
                    queue = self._queues.setdefault(key, deque())
                    if len(queue) >= CALL_LIMIT_QUEUE_SIZE:
                        raise self._limited(
                            "concurrent", concurrent or self._active_by_grant.get(key, 0)
                        )
                    waiter = _Waiter()
                    queue.append(waiter)
                else:
                    if not self._platform_acquire(instance_id):
                        self._rollback(key, reservation)
                        return None
                    self._active_by_grant[key] = self._active_by_grant.get(key, 0) + 1
                    return AdmissionPermit(self, instance_id, key, reservation)
            while True:
                if disconnected is not None and await disconnected():
                    raise asyncio.CancelledError
                async with self._lock:
                    concurrent, per_minute = limits()
                    now = self._clock()
                    if waiter is not None and now - queued_at >= CALL_LIMIT_WAIT_SECONDS:
                        raise self._limited(
                            "concurrent",
                            concurrent or self._active_by_grant.get(key, 0),
                            timed_out=True,
                        )
                    self._prune(key, now)
                    window = self._windows.get(key)
                    # A lower live minute limit preserves earlier reservations and
                    # running calls; excess queued requests are still pre-InProgress.
                    if (
                        per_minute is not None
                        and window is not None
                        and reservation in window
                        and window.index(reservation) >= per_minute
                    ):
                        raise self._limited(
                            "per_minute",
                            per_minute,
                            max(
                                1,
                                math.ceil(CALL_LIMIT_WINDOW_SECONDS - (now - window[0].created_at)),
                            ),
                        )
                    is_head = waiter is None or self._queues[key][0] is waiter
                    if is_head and (
                        concurrent is None or self._active_by_grant.get(key, 0) < concurrent
                    ):
                        self._remove_waiter(key, waiter)
                        waiter = None
                        if not self._platform_acquire(instance_id):
                            self._rollback(key, reservation)
                            return None
                        self._active_by_grant[key] = self._active_by_grant.get(key, 0) + 1
                        return AdmissionPermit(self, instance_id, key, reservation)
                    assert waiter is not None
                    waiter.wake.clear()
                # Poll the in-memory policy so a snapshot change can release a queue
                # even when no active request completes. No database hot-path I/O.
                with suppress(TimeoutError):
                    await asyncio.wait_for(waiter.wake.wait(), timeout=0.1)
        except BaseException:
            async with self._lock:
                self._remove_waiter(key, waiter)
                self._rollback(key, reservation)
            raise

    async def active_counts(self) -> tuple[int, dict[UUID, int]]:
        async with self._lock:
            return self._active, dict(self._active_by_instance)

    async def grant_counts(
        self,
    ) -> tuple[dict[GrantKey, int], dict[GrantKey, int], dict[GrantKey, int]]:
        """Atomic diagnostics: active, queued, and unexpired minute entries."""
        async with self._lock:
            for key in tuple(self._windows):
                self._prune(key, self._clock())
            return (
                dict(self._active_by_grant),
                {key: len(value) for key, value in self._queues.items()},
                {key: len(value) for key, value in self._windows.items()},
            )

    async def _release(self, permit: AdmissionPermit) -> None:
        async with self._lock:
            if permit._released:
                return
            permit._released = True
            self._active -= 1
            instance_id = permit._instance_id
            if instance_id is not None:
                self._active_by_instance[instance_id] -= 1
                if not self._active_by_instance[instance_id]:
                    del self._active_by_instance[instance_id]
            key = permit._key
            if key is not None:
                self._active_by_grant[key] -= 1
                if not self._active_by_grant[key]:
                    del self._active_by_grant[key]
                if not permit._started:
                    self._rollback(key, permit._reservation)
                self._wake_head(key)

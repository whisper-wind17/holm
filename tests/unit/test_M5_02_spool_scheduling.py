"""M5-02 dedicated-thread scheduling and storage-protection checks."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from holm_ledger import (
    ConnectionRole,
    InProgressEvent,
    LedgerEvent,
    ReceivedEvent,
    RequestOperation,
    RequestProtocol,
    SequenceAction,
    SequenceDecision,
    SpoolWriter,
    SpoolWriterClosedError,
    SpoolWriterState,
    SpoolWriterUnavailableError,
    decide_event_sequence,
)

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


def _uuid(value: int) -> UUID:
    return UUID(int=value)


def _received(request_number: int) -> ReceivedEvent:
    return ReceivedEvent(
        request_id=_uuid(request_number),
        event_seq=1,
        created_at=NOW,
        received_at=NOW,
        protocol=RequestProtocol.OPENAI,
        operation=RequestOperation.CHAT_COMPLETIONS,
        endpoint="/v1/chat/completions",
        stream=False,
        input_bytes=32,
    )


def _in_progress(request_number: int) -> InProgressEvent:
    return InProgressEvent(
        request_id=_uuid(request_number),
        event_seq=2,
        created_at=NOW,
        received_at=NOW,
        user_id=_uuid(10_001),
        api_key_id=_uuid(10_002),
        service_id=_uuid(10_003),
        instance_id=_uuid(10_004),
        requested_max_tokens=16,
    )


class _FakeClock:
    def __init__(self) -> None:
        self._value = 100.0
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._value

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._value += seconds


class _FakeLedger:
    def __init__(self) -> None:
        self.batches: list[tuple[LedgerEvent, ...]] = []
        self.events: dict[UUID, list[LedgerEvent]] = {}
        self.closed = False

    def append_batch(self, events: tuple[LedgerEvent, ...]) -> tuple[SequenceDecision, ...]:
        self.batches.append(events)
        decisions: list[SequenceDecision] = []
        for event in events:
            existing = self.events.setdefault(event.request_id, [])
            decision = decide_event_sequence(existing, event)
            decisions.append(decision)
            if decision.action is SequenceAction.WRITE:
                existing.append(event)
        return tuple(decisions)

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("window", "requests"),
    [(0.0, 1), (0.003, 1), (0.001, 0), (0.001, 65)],
)
def test_M5_02_rejects_batch_configuration_outside_tdd_bounds(
    tmp_path: Path, window: float, requests: int
) -> None:
    with pytest.raises(ValueError):
        SpoolWriter(
            tmp_path / "ledger.sqlite3",
            batch_window_seconds=window,
            max_batch_requests=requests,
        )


def test_M5_02_connection_factory_runs_on_owner_thread_with_gateway_role(
    tmp_path: Path,
) -> None:
    caller_thread = threading.get_ident()
    opened: list[tuple[int, ConnectionRole]] = []
    ledger = _FakeLedger()

    def factory(path: str | Path, role: ConnectionRole) -> _FakeLedger:
        del path
        opened.append((threading.get_ident(), role))
        return ledger

    async def scenario() -> None:
        writer = SpoolWriter(tmp_path / "ledger.sqlite3", ledger_factory=factory)
        writer.start()
        assert writer.is_available
        await writer.close()
        assert writer.state is SpoolWriterState.CLOSED
        assert not writer.thread_alive

    asyncio.run(scenario())
    assert opened == [(opened[0][0], ConnectionRole.GATEWAY_SPOOL_WRITER)]
    assert opened[0][0] != caller_thread
    assert ledger.closed


def test_M5_02_TC_LG_03_distinct_request_limit_splits_batches_without_reordering(
    tmp_path: Path,
) -> None:
    clock = _FakeClock()
    ledger = _FakeLedger()

    async def scenario() -> None:
        writer = SpoolWriter(
            tmp_path / "ledger.sqlite3",
            max_batch_requests=2,
            clock=clock,
            ledger_factory=lambda path, role: ledger,
        )
        writer.start()
        first = asyncio.create_task(writer.submit(_received(1)))
        await asyncio.sleep(0)
        second = asyncio.create_task(writer.submit(_received(2)))
        results = await asyncio.gather(first, second)
        assert tuple(results) == (SequenceAction.WRITE,) * 2

        third = asyncio.create_task(writer.submit(_received(3)))
        await asyncio.sleep(0)
        clock.advance(0.003)
        fourth = asyncio.create_task(writer.submit(_received(4)))
        assert await third is SequenceAction.WRITE
        await writer.close()
        assert await fourth is SequenceAction.WRITE

        assert writer.batch_count == 3
        assert writer.max_batch_request_count == 2

    asyncio.run(scenario())
    assert [[event.request_id for event in batch] for batch in ledger.batches] == [
        [_uuid(1), _uuid(2)],
        [_uuid(3)],
        [_uuid(4)],
    ]


def test_M5_02_TC_LG_02_window_keeps_or_splits_logical_events_by_enqueue_time(
    tmp_path: Path,
) -> None:
    async def within_window() -> list[tuple[LedgerEvent, ...]]:
        clock = _FakeClock()
        ledger = _FakeLedger()
        writer = SpoolWriter(
            tmp_path / "within.sqlite3",
            clock=clock,
            ledger_factory=lambda path, role: ledger,
        )
        writer.start()
        received = asyncio.create_task(writer.submit(_received(1)))
        await asyncio.sleep(0)
        in_progress = asyncio.create_task(writer.submit(_in_progress(1)))
        await asyncio.sleep(0)
        clock.advance(0.003)
        boundary = asyncio.create_task(writer.submit(_received(2)))
        results = await asyncio.gather(received, in_progress)
        assert tuple(results) == (SequenceAction.WRITE,) * 2
        await writer.close()
        assert await boundary is SequenceAction.WRITE
        return ledger.batches

    async def outside_window() -> list[tuple[LedgerEvent, ...]]:
        clock = _FakeClock()
        ledger = _FakeLedger()
        writer = SpoolWriter(
            tmp_path / "outside.sqlite3",
            clock=clock,
            ledger_factory=lambda path, role: ledger,
        )
        writer.start()
        received = asyncio.create_task(writer.submit(_received(3)))
        await asyncio.sleep(0)
        clock.advance(0.003)
        in_progress = asyncio.create_task(writer.submit(_in_progress(3)))
        assert await received is SequenceAction.WRITE
        await writer.close()
        assert await in_progress is SequenceAction.WRITE
        return ledger.batches

    within_batches = asyncio.run(within_window())
    outside_batches = asyncio.run(outside_window())
    assert [len(batch) for batch in within_batches] == [2, 1]
    assert [len(batch) for batch in outside_batches] == [1, 1]


def test_M5_02_commit_in_progress_routes_new_event_to_next_batch_and_loop_runs(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    ledger = _FakeLedger()

    def before_commit(events: tuple[LedgerEvent, ...]) -> None:
        if events[0].request_id == _uuid(1):
            entered.set()
            assert release.wait(timeout=2)

    async def scenario() -> None:
        writer = SpoolWriter(
            tmp_path / "ledger.sqlite3",
            max_batch_requests=1,
            ledger_factory=lambda path, role: ledger,
            before_commit=before_commit,
        )
        writer.start()
        first = asyncio.create_task(writer.submit(_received(1)))
        assert await asyncio.to_thread(entered.wait, 1)
        second = asyncio.create_task(writer.submit(_received(2)))
        await asyncio.sleep(0)
        assert writer.has_pending()

        loop_progressed = False

        async def marker() -> None:
            nonlocal loop_progressed
            await asyncio.sleep(0)
            loop_progressed = True

        await marker()
        assert loop_progressed
        release.set()
        results = await asyncio.gather(first, second)
        await writer.close()
        assert tuple(results) == (SequenceAction.WRITE,) * 2

    asyncio.run(scenario())
    assert [len(batch) for batch in ledger.batches] == [1, 1]


def test_M5_02_commit_failure_fails_whole_batch_and_future_submits_fast(
    tmp_path: Path,
) -> None:
    attempts = 0

    def fail_commit(events: tuple[LedgerEvent, ...]) -> None:
        nonlocal attempts
        del events
        attempts += 1
        raise sqlite3.OperationalError("M5_02 injected commit failure")

    async def scenario() -> None:
        writer = SpoolWriter(
            tmp_path / "ledger.sqlite3",
            max_batch_requests=2,
            ledger_factory=lambda path, role: _FakeLedger(),
            before_commit=fail_commit,
        )
        writer.start()
        first = asyncio.create_task(writer.submit(_received(1)))
        second = asyncio.create_task(writer.submit(_received(2)))
        outcomes = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(item, SpoolWriterUnavailableError) for item in outcomes)
        assert writer.state is SpoolWriterState.UNAVAILABLE
        assert not writer.has_pending()
        with pytest.raises(SpoolWriterUnavailableError):
            await writer.submit(_received(3))
        await writer.close()
        assert not writer.thread_alive

    asyncio.run(scenario())
    assert attempts == 1


def test_M5_02_close_drains_queue_then_rejects_submissions_and_is_idempotent(
    tmp_path: Path,
) -> None:
    clock = _FakeClock()
    ledger = _FakeLedger()

    async def scenario() -> None:
        writer = SpoolWriter(
            tmp_path / "ledger.sqlite3",
            clock=clock,
            ledger_factory=lambda path, role: ledger,
        )
        writer.start()
        pending = asyncio.create_task(writer.submit(_received(1)))
        await asyncio.sleep(0)
        assert writer.has_pending()
        await writer.close()
        assert await pending is SequenceAction.WRITE
        assert not writer.has_pending()
        assert writer.state is SpoolWriterState.CLOSED
        assert not writer.thread_alive
        with pytest.raises(SpoolWriterClosedError):
            await writer.submit(_received(2))
        await writer.close()

    asyncio.run(scenario())


def test_M5_02_open_failure_enters_unavailable_without_thread_leak(tmp_path: Path) -> None:
    def factory(path: str | Path, role: ConnectionRole) -> _FakeLedger:
        del path, role
        raise sqlite3.OperationalError("M5_02 injected open failure")

    writer = SpoolWriter(tmp_path / "ledger.sqlite3", ledger_factory=factory)
    with pytest.raises(SpoolWriterUnavailableError):
        writer.start()
    assert writer.state is SpoolWriterState.UNAVAILABLE
    asyncio.run(writer.close())
    assert not writer.thread_alive

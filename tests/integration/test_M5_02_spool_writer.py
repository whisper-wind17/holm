"""M5-02 real-file Group Commit and durable-ack integration checks."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from holm_domain import RequestFinishReason
from holm_ledger import (
    CompletedEvent,
    ConnectionRole,
    InProgressEvent,
    LedgerSequenceError,
    ReceivedEvent,
    RequestOperation,
    RequestProtocol,
    SequenceAction,
    SpoolWriter,
    SpoolWriterState,
    SpoolWriterUnavailableError,
    open_ledger,
)

NOW = datetime(2026, 9, 17, 13, 0, tzinfo=UTC)


def _uuid(value: int) -> UUID:
    return UUID(int=value)


def _private_database(tmp_path: Path, name: str = "request-ledger.sqlite3") -> Path:
    directory = tmp_path / "ledger"
    directory.mkdir(mode=0o700, exist_ok=True)
    return directory / name


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
        input_bytes=64,
    )


def _in_progress(request_number: int, *, seq: int = 2) -> InProgressEvent:
    return InProgressEvent(
        request_id=_uuid(request_number),
        event_seq=seq,
        created_at=NOW + timedelta(milliseconds=1),
        received_at=NOW,
        user_id=_uuid(20_001),
        api_key_id=_uuid(20_002),
        service_id=_uuid(20_003),
        instance_id=_uuid(20_004),
        requested_max_tokens=32,
    )


def _completed(request_number: int, *, seq: int = 3) -> CompletedEvent:
    return CompletedEvent(
        request_id=_uuid(request_number),
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


def test_M5_02_batch_primitive_uses_one_transaction_and_sees_prior_batch_events(
    tmp_path: Path,
) -> None:
    path = _private_database(tmp_path)
    with open_ledger(path, ConnectionRole.GATEWAY_SPOOL_WRITER) as ledger:
        statements: list[str] = []
        ledger.connection.set_trace_callback(statements.append)
        decisions = ledger.append_batch(
            (_received(1), _in_progress(1), _completed(1), _received(2))
        )
        ledger.connection.set_trace_callback(None)

        assert tuple(item.action for item in decisions) == (SequenceAction.WRITE,) * 4
        assert sum(item == "BEGIN IMMEDIATE" for item in statements) == 1
        assert sum(item == "COMMIT" for item in statements) == 1
        rows = ledger.connection.execute(
            "SELECT request_id, event_seq FROM request_events ORDER BY rowid"
        ).fetchall()
        assert [(str(row[0]), int(row[1])) for row in rows] == [
            (str(_uuid(1)), 1),
            (str(_uuid(1)), 2),
            (str(_uuid(1)), 3),
            (str(_uuid(2)), 1),
        ]


def test_M5_02_sequence_rejection_isolated_inside_real_batch(tmp_path: Path) -> None:
    path = _private_database(tmp_path)
    with open_ledger(path, ConnectionRole.GATEWAY_SPOOL_WRITER) as ledger:
        decisions = ledger.append_batch(
            (_received(1), _completed(1, seq=2), _received(2), _in_progress(2))
        )
        assert tuple(item.action for item in decisions) == (
            SequenceAction.WRITE,
            SequenceAction.REJECT,
            SequenceAction.WRITE,
            SequenceAction.WRITE,
        )
        assert [
            (item.event.request_id, item.event.event_seq) for item in ledger.list_unacked()
        ] == [
            (_uuid(1), 1),
            (_uuid(2), 1),
            (_uuid(2), 2),
        ]


def test_M5_02_TC_LG_02_await_means_independent_reader_observes_durable_lifecycle(
    tmp_path: Path,
) -> None:
    path = _private_database(tmp_path)

    async def scenario() -> None:
        writer = SpoolWriter(path)
        writer.start()
        try:
            results = await asyncio.gather(
                writer.submit(_received(1)),
                writer.submit(_in_progress(1)),
                writer.submit(_completed(1)),
            )
            assert tuple(results) == (SequenceAction.WRITE,) * 3

            with open_ledger(path, ConnectionRole.READER) as reader:
                assert [item.event for item in reader.list_unacked()] == [
                    _received(1),
                    _in_progress(1),
                    _completed(1),
                ]
        finally:
            await writer.close()

    asyncio.run(scenario())


def test_M5_02_bad_event_future_fails_without_blocking_valid_sibling(tmp_path: Path) -> None:
    path = _private_database(tmp_path)

    async def scenario() -> None:
        writer = SpoolWriter(path, max_batch_requests=2)
        writer.start()
        try:
            invalid = asyncio.create_task(writer.submit(_in_progress(1)))
            valid = asyncio.create_task(writer.submit(_received(2)))
            outcomes = await asyncio.gather(invalid, valid, return_exceptions=True)
            assert isinstance(outcomes[0], LedgerSequenceError)
            assert outcomes[1] is SequenceAction.WRITE

            with open_ledger(path, ConnectionRole.READER) as reader:
                events = reader.list_unacked()
                assert len(events) == 1
                assert events[0].event == _received(2)
        finally:
            await writer.close()

    asyncio.run(scenario())


def test_M5_02_TC_LG_03_sixty_five_requests_split_at_count_boundary(
    tmp_path: Path,
) -> None:
    path = _private_database(tmp_path)

    async def scenario() -> None:
        # 冻结单调时钟：所有 enqueued_at 相同，2ms 批窗口不会先于计数边界到期，于是
        # 「64 个 request_id 的计数边界」成为第一批唯一可能的分批原因。真实时钟下这条
        # 该断言的结果随机器负载变化，不能用来判断分批规则。
        # 代价：窗口永不到期，第 65 条只能由 close() 的排空提交，所以分两段提交。
        writer = SpoolWriter(path, clock=lambda: 0.0)
        writer.start()
        overflow: asyncio.Task[SequenceAction] | None = None
        try:
            first_batch = await asyncio.gather(
                *(writer.submit(_received(index)) for index in range(1, 65))
            )
            assert first_batch == [SequenceAction.WRITE] * 64
            assert writer.batch_count == 1
            assert writer.max_batch_request_count == 64

            # submit 在 await future 之前就同步入队，让出一次调度即可保证第 65 条已排队。
            overflow = asyncio.create_task(writer.submit(_received(65)))
            await asyncio.sleep(0)
        finally:
            # 断言失败也必须关掉 Writer：它的线程不是守护线程，泄漏一次就让 pytest
            # 打完总结也无法退出。
            await writer.close()

        assert overflow is not None
        assert await overflow == SequenceAction.WRITE
        assert writer.batch_count == 2
        assert writer.max_batch_request_count == 64

        with open_ledger(path, ConnectionRole.READER) as reader:
            assert len(reader.list_unacked()) == 65

    asyncio.run(scenario())


def test_M5_02_gateway_busy_timeout_enters_storage_protection(tmp_path: Path) -> None:
    path = _private_database(tmp_path)

    async def scenario() -> None:
        writer = SpoolWriter(path, max_batch_requests=1)
        writer.start()
        blocker = sqlite3.connect(path, isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            with pytest.raises(SpoolWriterUnavailableError):
                await writer.submit(_received(1))
            assert writer.state is SpoolWriterState.UNAVAILABLE
            assert not writer.has_pending()
            with pytest.raises(SpoolWriterUnavailableError):
                await writer.submit(_received(2))
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()
            await writer.close()
        assert not writer.thread_alive

    asyncio.run(scenario())

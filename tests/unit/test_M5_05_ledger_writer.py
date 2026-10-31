"""M5-05 Ledger Writer wake, batching, retry, and CLI contracts."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from holm_ledger import (
    ConnectionRole,
    ReceivedEvent,
    RequestOperation,
    RequestProtocol,
    open_ledger,
)
from holm_ledger_writer import writer as writer_module
from holm_ledger_writer.__main__ import _parse_args
from holm_ledger_writer.writer import (
    DATABASE_RETRY_BACKOFF_SECONDS,
    MAX_REPLAY_BATCH_SIZE,
    WAKE_CHECK_SECONDS,
    LedgerWriter,
)
from sqlalchemy.exc import OperationalError

NOW = datetime(2026, 9, 17, 16, 0, tzinfo=UTC)


def _path(tmp_path: Path) -> Path:
    directory = tmp_path / "spool"
    directory.mkdir(mode=0o700)
    return directory / "request-ledger.sqlite3"


def _received(value: int) -> ReceivedEvent:
    return ReceivedEvent(
        request_id=UUID(f"79000000-0000-4000-8000-{value:012d}"),
        event_seq=1,
        created_at=NOW,
        received_at=NOW,
        protocol=RequestProtocol.OPENAI,
        operation=RequestOperation.CHAT_COMPLETIONS,
        endpoint="/v1/chat/completions",
        stream=False,
        input_bytes=1,
    )


def _writer(ledger: Any, *, poll_seconds: float = 1.0) -> LedgerWriter:
    return LedgerWriter(
        ledger=ledger,
        session_factory=cast(Any, object()),
        engine=cast(Any, object()),
        poll_seconds=poll_seconds,
    )


def test_M5_05_cli_contract_has_required_paths_and_one_second_poll_default() -> None:
    args = _parse_args(
        [
            "--database-url-file",
            "/run/secrets/database-url",
            "--request-ledger-path",
            "/var/lib/holm/request-ledger.sqlite3",
        ]
    )
    assert args.database_url_file == Path("/run/secrets/database-url")
    assert args.request_ledger_path == Path("/var/lib/holm/request-ledger.sqlite3")
    assert args.poll_seconds == 1.0


def test_M5_05_batch_and_backoff_limits_match_the_card() -> None:
    assert MAX_REPLAY_BATCH_SIZE == 500
    assert WAKE_CHECK_SECONDS <= 0.1
    assert DATABASE_RETRY_BACKOFF_SECONDS[-1] <= 30
    assert tuple(sorted(DATABASE_RETRY_BACKOFF_SECONDS)) == DATABASE_RETRY_BACKOFF_SECONDS


def test_M5_05_writer_rejects_invalid_timing_and_identity(tmp_path: Path) -> None:
    with open_ledger(_path(tmp_path)) as ledger:
        with pytest.raises(ValueError, match="poll interval"):
            _writer(ledger, poll_seconds=0)
        with pytest.raises(ValueError, match="instance_id"):
            LedgerWriter(
                ledger=ledger,
                session_factory=cast(Any, object()),
                engine=cast(Any, object()),
                instance_id=" ledger-writer",
            )


def test_M5_05_batch_ack_is_one_transaction_and_is_idempotent(tmp_path: Path) -> None:
    path = _path(tmp_path)
    with open_ledger(path) as ledger:
        events = tuple(_received(value) for value in range(1, 4))
        ledger.append_batch(events)
        keys = tuple((event.request_id, event.event_seq) for event in events)
        assert ledger.mark_acked_batch(keys, acked_at=NOW) == 3
        assert ledger.mark_acked_batch(keys, acked_at=NOW) == 0
        assert ledger.list_unacked() == ()


def test_M5_05_batch_ack_rejects_duplicates_and_more_than_500(tmp_path: Path) -> None:
    with open_ledger(_path(tmp_path)) as ledger:
        key = (_received(1).request_id, 1)
        with pytest.raises(ValueError, match="duplicate"):
            ledger.mark_acked_batch((key, key), acked_at=NOW)
        oversized = tuple((_received(value).request_id, 1) for value in range(1, 502))
        with pytest.raises(ValueError, match="500"):
            ledger.mark_acked_batch(oversized, acked_at=NOW)


def test_M5_05_data_version_wakes_for_another_connection_without_writing(tmp_path: Path) -> None:
    async def scenario() -> float:
        path = _path(tmp_path)
        reader = open_ledger(path, ConnectionRole.LEDGER_WRITER)
        gateway = open_ledger(path, ConnectionRole.GATEWAY_SPOOL_WRITER)
        try:
            worker = _writer(reader, poll_seconds=5.0)
            baseline = reader.data_version()
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            started = loop.time()
            waiter = asyncio.create_task(worker._wait_for_change(stop, baseline))
            await asyncio.sleep(0.02)
            await asyncio.to_thread(gateway.append, _received(1))
            changed = await asyncio.wait_for(waiter, timeout=1.0)
            assert changed != baseline
            return loop.time() - started
        finally:
            reader.close()
            gateway.close()

    assert asyncio.run(scenario()) < 1.0


def test_M5_05_database_outage_retries_without_exit_or_error_detail_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> int:
        path = _path(tmp_path)
        ledger = open_ledger(path)
        worker = _writer(ledger)
        stop = asyncio.Event()
        attempts = 0

        async def replay_once(self: LedgerWriter) -> int:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OperationalError("SELECT M5_05_BODY_SENTINEL", {}, Exception("secret"))
            stop.set()
            return 0

        monkeypatch.setattr(LedgerWriter, "replay_once", replay_once)
        monkeypatch.setattr(writer_module, "DATABASE_RETRY_BACKOFF_SECONDS", (0.001,))
        try:
            await asyncio.wait_for(worker._replay_loop(stop), timeout=1.0)
        finally:
            ledger.close()
        return attempts

    caplog.set_level(logging.ERROR, logger="holm.ledger_writer")
    assert asyncio.run(scenario()) == 2
    assert "M5_05_BODY_SENTINEL" not in caplog.text
    assert "secret" not in caplog.text
    assert "replay unavailable" in caplog.text

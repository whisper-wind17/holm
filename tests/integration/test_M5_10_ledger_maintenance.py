"""M5-10 / TC-LG-12 Ack retention, maintenance yield, and checkpoint isolation."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from holm_domain import (
    Component,
    ComponentStatus,
    LedgerHealth,
    decode_ledger_backlog_reason,
)
from holm_ledger import (
    ConnectionRole,
    ReceivedEvent,
    RequestOperation,
    RequestProtocol,
    open_ledger,
)
from holm_ledger_writer import writer as writer_module
from holm_ledger_writer.writer import LedgerWriter

NOW = datetime(2026, 9, 20, 8, 0, tzinfo=UTC)
SENTINEL = "M5_10_BODY_SENTINEL_0c247e"


class _RecordingHeartbeats:
    written: list[dict[str, Any]] = []

    def __init__(self, session: Any) -> None:
        del session

    async def write(self, **fields: Any) -> None:
        self.written.append(fields)


class _FakeSession:
    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    def begin(self) -> _FakeSession:
        return self


def _path(tmp_path: Path) -> Path:
    directory = tmp_path / "spool"
    directory.mkdir(mode=0o700)
    return directory / "request-ledger.sqlite3"


def _event(value: int, *, created_at: datetime) -> ReceivedEvent:
    return ReceivedEvent(
        request_id=UUID(f"5a100000-0000-4000-8000-{value:012d}"),
        event_seq=1,
        created_at=created_at,
        received_at=created_at,
        protocol=RequestProtocol.OPENAI,
        operation=RequestOperation.CHAT_COMPLETIONS,
        endpoint="/v1/chat/completions",
        stream=False,
        input_bytes=1,
    )


def _stored_ids(ledger: Any) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in ledger.connection.execute(
            "SELECT request_id FROM request_events ORDER BY request_id"
        ).fetchall()
    )


def _writer(ledger: Any, *, idle_seconds: float = 0.001) -> LedgerWriter:
    return LedgerWriter(
        ledger=ledger,
        session_factory=cast(Any, object()),
        engine=cast(Any, object()),
        gateway_idle_confirm_seconds=idle_seconds,
        clock=lambda: NOW,
    )


def test_M5_10_TC_LG_12_cleanup_positive_control_never_deletes_old_unacked(
    tmp_path: Path,
) -> None:
    path = _path(tmp_path)
    old_acked = _event(1, created_at=NOW - timedelta(days=7))
    recent_acked = _event(2, created_at=NOW - timedelta(days=7))
    old_unacked = _event(3, created_at=NOW - timedelta(days=7))

    with open_ledger(path) as ledger:
        # All three enter in one Gateway batch; only Ack state may distinguish
        # cleanup eligibility.
        ledger.append_batch((old_acked, recent_acked, old_unacked))
        assert ledger.mark_acked(
            old_acked.request_id,
            1,
            acked_at=NOW - timedelta(hours=49),
        )
        assert ledger.mark_acked(
            recent_acked.request_id,
            1,
            acked_at=NOW - timedelta(hours=47),
        )

        assert ledger.delete_acked_before(NOW - timedelta(hours=48), limit=500) == 1
        assert _stored_ids(ledger) == (
            str(recent_acked.request_id),
            str(old_unacked.request_id),
        )
        assert ledger.oldest_unacked_created_at() == old_unacked.created_at


def test_M5_10_TC_LG_12_external_gateway_commit_during_idle_window_yields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_partitions(*args: object, **kwargs: object) -> tuple[str, ...]:
        del args, kwargs
        return ()

    async def scenario() -> None:
        path = _path(tmp_path)
        ledger = open_ledger(path, ConnectionRole.LEDGER_WRITER)
        gateway = open_ledger(path, ConnectionRole.GATEWAY_SPOOL_WRITER)
        try:
            old = _event(10, created_at=NOW - timedelta(days=7))
            ledger.append(old)
            ledger.mark_acked(old.request_id, 1, acked_at=NOW - timedelta(hours=49))
            worker = _writer(ledger, idle_seconds=0.05)
            maintenance = asyncio.create_task(worker.maintain_once())
            await asyncio.sleep(0.01)
            pending = _event(11, created_at=NOW)
            await asyncio.to_thread(gateway.append, pending)
            result = await maintenance

            assert result.yielded_to_gateway is True
            assert result.deleted_acked_events == 0
            assert result.checkpoint is None
            assert _stored_ids(ledger) == (str(old.request_id), str(pending.request_id))
        finally:
            gateway.close()
            ledger.close()

    monkeypatch.setattr(writer_module, "drop_expired_request_record_partitions", no_partitions)
    monkeypatch.setattr(writer_module, "drop_expired_telemetry_partitions", no_partitions)
    asyncio.run(scenario())


def test_M5_10_TC_LG_12_incomplete_passive_checkpoint_keeps_wal_and_alerts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def no_partitions(*args: object, **kwargs: object) -> tuple[str, ...]:
        del args, kwargs
        return ()

    async def scenario() -> tuple[int, int, bool]:
        path = _path(tmp_path)
        ledger = open_ledger(path, ConnectionRole.LEDGER_WRITER)
        gateway = open_ledger(path, ConnectionRole.GATEWAY_SPOOL_WRITER)
        reader = sqlite3.connect(path, isolation_level=None)
        try:
            first = _event(20, created_at=NOW)
            gateway.append(first)
            ledger.mark_acked(first.request_id, 1, acked_at=NOW)
            reader.execute("BEGIN")
            reader.execute("SELECT COUNT(*) FROM request_events").fetchone()

            second = _event(21, created_at=NOW)
            gateway.append(second)
            ledger.mark_acked(second.request_id, 1, acked_at=NOW)
            wal_path = Path(f"{path}-wal")
            before = wal_path.stat().st_size

            worker = LedgerWriter(
                ledger=ledger,
                session_factory=cast(Any, _FakeSession),
                engine=cast(Any, object()),
                gateway_idle_confirm_seconds=0.001,
                clock=lambda: NOW,
            )
            result = await worker.maintain_once()
            assert result.checkpoint is not None
            assert result.checkpoint.busy is False
            assert result.checkpoint.complete is False
            assert result.checkpoint.checkpointed_frames < result.checkpoint.wal_frames
            await worker._write_heartbeat(ComponentStatus.HEALTHY)
            return (
                before,
                wal_path.stat().st_size,
                cast(Any, worker)._maintenance_failed,
            )
        finally:
            with contextlib.suppress(sqlite3.OperationalError):
                reader.execute("ROLLBACK")
            reader.close()
            gateway.close()
            ledger.close()

    monkeypatch.setattr(writer_module, "drop_expired_request_record_partitions", no_partitions)
    monkeypatch.setattr(writer_module, "drop_expired_telemetry_partitions", no_partitions)
    _RecordingHeartbeats.written = []
    monkeypatch.setattr(writer_module, "ComponentHeartbeatRepository", _RecordingHeartbeats)
    caplog.set_level(logging.WARNING, logger="holm.ledger_writer")
    before, after, maintenance_failed = asyncio.run(scenario())
    assert before > 0
    assert after > 0
    assert maintenance_failed is True
    assert len(_RecordingHeartbeats.written) == 1
    heartbeat = _RecordingHeartbeats.written[0]
    assert heartbeat["component"] is Component.LEDGER_WRITER
    assert heartbeat["status"] is ComponentStatus.DEGRADED
    assert decode_ledger_backlog_reason(heartbeat["reason"]) is LedgerHealth.UNAVAILABLE
    assert "ledger_checkpoint_incomplete" in tuple(
        getattr(record, "error_code", "") for record in caplog.records
    )
    assert SENTINEL not in caplog.text

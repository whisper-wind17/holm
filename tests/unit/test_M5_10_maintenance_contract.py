"""M5-10 maintenance configuration and ordering contracts."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from holm_ledger import LedgerStorageError
from holm_ledger.store import PassiveCheckpointResult
from holm_ledger_writer import writer as writer_module
from holm_ledger_writer.__main__ import (
    _load_request_records_retention_days,
    _parse_args,
)
from holm_ledger_writer.writer import (
    DEFAULT_ACK_RETENTION_SECONDS,
    LedgerWriter,
)
from sqlalchemy.exc import SQLAlchemyError


class _OrderedLedger:
    path = Path("/not-read")

    def __init__(self) -> None:
        self.calls: list[str] = []

    def data_version(self) -> int:
        self.calls.append("data_version")
        return 1

    def has_unacked_events(self) -> bool:
        self.calls.append("has_unacked")
        return False

    def delete_acked_before(self, cutoff: datetime, *, limit: int) -> int:
        del cutoff, limit
        self.calls.append("delete_acked")
        return 0

    def checkpoint_passive(self) -> PassiveCheckpointResult:
        self.calls.append("checkpoint_passive")
        return PassiveCheckpointResult(False, 3, 3)


class _CheckpointOutcomeLedger(_OrderedLedger):
    def __init__(self, outcome: PassiveCheckpointResult | BaseException) -> None:
        super().__init__()
        self.outcome = outcome

    def checkpoint_passive(self) -> PassiveCheckpointResult:
        self.calls.append("checkpoint_passive")
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


async def _no_partitions(*args: object, **kwargs: object) -> tuple[str, ...]:
    del args, kwargs
    return ()


def _maintenance_worker(ledger: _OrderedLedger) -> LedgerWriter:
    return LedgerWriter(
        ledger=cast(Any, ledger),
        session_factory=cast(Any, object()),
        engine=cast(Any, object()),
        gateway_idle_confirm_seconds=0.000_001,
        clock=lambda: datetime(2026, 9, 20, tzinfo=UTC),
    )


def test_M5_10_maintenance_orders_idle_probe_cleanup_partition_then_passive_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _OrderedLedger()

    async def drop_partitions(*args: object, **kwargs: object) -> tuple[str, ...]:
        del args, kwargs
        ledger.calls.append("drop_partitions")
        return ()

    async def drop_telemetry(*args: object, **kwargs: object) -> tuple[str, ...]:
        del args, kwargs
        ledger.calls.append("drop_telemetry")
        return ()

    monkeypatch.setattr(writer_module, "drop_expired_request_record_partitions", drop_partitions)
    monkeypatch.setattr(writer_module, "drop_expired_telemetry_partitions", drop_telemetry)
    worker = _maintenance_worker(ledger)
    result = asyncio.run(worker.maintain_once())

    assert result.checkpoint is not None and result.checkpoint.complete
    assert ledger.calls.index("has_unacked") < ledger.calls.index("delete_acked")
    assert ledger.calls.index("delete_acked") < ledger.calls.index("drop_partitions")
    assert ledger.calls.index("drop_partitions") < ledger.calls.index("drop_telemetry")
    assert ledger.calls.index("drop_telemetry") < ledger.calls.index("checkpoint_passive")


def test_M5_10_busy_checkpoint_is_distinct_from_incomplete_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ledger = _CheckpointOutcomeLedger(PassiveCheckpointResult(True, 5, 2))
    monkeypatch.setattr(writer_module, "drop_expired_request_record_partitions", _no_partitions)
    monkeypatch.setattr(writer_module, "drop_expired_telemetry_partitions", _no_partitions)
    caplog.set_level(logging.WARNING, logger="holm.ledger_writer")

    result = asyncio.run(_maintenance_worker(ledger).maintain_once())

    assert result.checkpoint == PassiveCheckpointResult(True, 5, 2)
    assert [getattr(record, "error_code", "") for record in caplog.records] == [
        "ledger_checkpoint_busy"
    ]


def test_M5_10_checkpoint_exception_alert_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "M5_10_PRIVATE_BODY_SENTINEL_89b7"
    ledger = _CheckpointOutcomeLedger(LedgerStorageError(sentinel))
    monkeypatch.setattr(writer_module, "drop_expired_request_record_partitions", _no_partitions)
    monkeypatch.setattr(writer_module, "drop_expired_telemetry_partitions", _no_partitions)
    caplog.set_level(logging.WARNING, logger="holm.ledger_writer")

    result = asyncio.run(_maintenance_worker(ledger).maintain_once())

    assert result.checkpoint is None
    assert [getattr(record, "error_code", "") for record in caplog.records] == [
        "ledger_checkpoint_failed"
    ]
    assert sentinel not in caplog.text


def test_M6_09_TC_TM_09_telemetry_retention_failure_is_loud_and_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "M6_09_PRIVATE_BODY_SENTINEL_3f8c"

    async def fail_retention(*args: object, **kwargs: object) -> tuple[str, ...]:
        del args, kwargs
        raise SQLAlchemyError(sentinel)

    ledger = _OrderedLedger()
    monkeypatch.setattr(writer_module, "drop_expired_request_record_partitions", _no_partitions)
    monkeypatch.setattr(writer_module, "drop_expired_telemetry_partitions", fail_retention)
    caplog.set_level(logging.ERROR, logger="holm.ledger_writer")

    with pytest.raises(SQLAlchemyError):
        asyncio.run(_maintenance_worker(ledger).maintain_once())

    assert "partition_retention_failed" in tuple(
        getattr(record, "error_code", "") for record in caplog.records
    )
    assert sentinel not in caplog.text


def test_M5_10_cli_defaults_to_48_hours_and_reads_manifest_retention(tmp_path: Path) -> None:
    manifest = tmp_path / "release-manifest.json"
    manifest.write_text(
        json.dumps({"retention": {"request_records_days": 37}}),
        encoding="utf-8",
    )
    args = _parse_args(
        [
            "--database-url-file",
            "/run/secrets/database-url",
            "--request-ledger-path",
            "/var/lib/holm/request-ledger.sqlite3",
            "--release-manifest",
            str(manifest),
        ]
    )
    assert args.ack_retention_seconds == DEFAULT_ACK_RETENTION_SECONDS == 48 * 60 * 60
    assert _load_request_records_retention_days(args.release_manifest) == 37


def test_M5_10_manifest_retention_rejects_boolean(tmp_path: Path) -> None:
    manifest = tmp_path / "release-manifest.json"
    manifest.write_text(
        json.dumps({"retention": {"request_records_days": True}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="retention is invalid"):
        _load_request_records_retention_days(manifest)

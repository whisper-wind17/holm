"""M7-08B / TC-DP-12: UTC retention and fail-closed filesystem boundaries."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

import pytest
from fixtures.backup_retention import write_backup
from holm_maintenance_worker.worker import MaintenanceWorker
from holm_persistence.backup_format import (
    BACKUP_MEMBERS,
    BackupError,
    validate_backup_manifest,
)
from holm_persistence.backup_retention import BackupRetentionResult, retain_backups

NOW = datetime(2026, 1, 12, 12, tzinfo=UTC)


def test_M7_08B_TC_DP_12_seven_days_four_completed_weeks_union_and_idempotence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "backups"
    root.mkdir(mode=0o700)
    dates = {
        "recent-a": NOW - timedelta(hours=1),
        "recent-b": NOW - timedelta(days=2),
        "seven-days-exact": NOW - timedelta(days=7),
        "week1": datetime(2026, 1, 11, 23, tzinfo=UTC),
        "week2-old": datetime(2025, 12, 29, tzinfo=UTC),
        "week2-a": datetime(2026, 1, 4, 23, tzinfo=UTC),
        "week2-z": datetime(2026, 1, 4, 23, tzinfo=UTC),
        "week3": datetime(2025, 12, 28, 23, tzinfo=UTC),
        "week4": datetime(2025, 12, 21, 23, tzinfo=UTC),
        "expired": datetime(2025, 12, 14, tzinfo=UTC),
    }
    for name, created in dates.items():
        path = write_backup(root, name, created)
        os.utime(path, (0, 0))
    expected = {"recent-a", "recent-b", "seven-days-exact", "week1", "week2-z", "week3", "week4"}
    assert retain_backups(root, now=NOW) == BackupRetentionResult(10, 7, 3)
    assert {item.name for item in root.iterdir()} == expected
    assert retain_backups(root, now=NOW) == BackupRetentionResult(7, 7, 0)


def test_M7_08B_TC_DP_12_empty_and_latest_old_backup_are_kept(tmp_path: Path) -> None:
    root = tmp_path / "backups"
    root.mkdir(mode=0o700)
    assert retain_backups(root, now=NOW) == BackupRetentionResult(0, 0, 0)
    write_backup(root, "older", NOW - timedelta(days=100))
    write_backup(root, "newest", NOW - timedelta(days=90))
    assert retain_backups(root, now=NOW) == BackupRetentionResult(2, 1, 1)
    assert {item.name for item in root.iterdir()} == {"newest"}


@pytest.mark.parametrize(
    "bad",
    [
        "json",
        "version",
        "missing",
        "future",
        "naive",
        "hash-format",
        "hash-content",
        "unknown-field",
        "unknown-member",
        "symlink-file",
        "symlink-directory",
        "unknown-file",
        "unknown-directory",
    ],
)
def test_M7_08B_TC_DP_12_bad_envelope_positive_control_prevents_all_deletion(
    tmp_path: Path, bad: str
) -> None:
    root = tmp_path / "backups"
    root.mkdir(mode=0o700)
    old = write_backup(root, "old", NOW - timedelta(days=100))
    write_backup(root, "latest", NOW - timedelta(days=1))
    suspect = write_backup(root, "suspect", NOW - timedelta(days=30))
    sentinel = tmp_path / "outside"
    sentinel.mkdir()
    (sentinel / "secret-body-sentinel").write_bytes(b"M708B_SENSITIVE_SENTINEL")
    manifest = json.loads((suspect / "manifest.json").read_text())
    if bad == "json":
        (suspect / "manifest.json").write_bytes(b"M708B_SENSITIVE_SENTINEL")
    elif bad in {"version", "future", "naive", "hash-format", "unknown-field"}:
        if bad == "version":
            manifest["format_version"] = 999
        elif bad == "future":
            manifest["created_at"] = (NOW + timedelta(seconds=1)).isoformat()
        elif bad == "naive":
            manifest["created_at"] = NOW.replace(tzinfo=None).isoformat()
        elif bad == "hash-format":
            manifest["sha256"]["database.dump"] = "M708B_SENSITIVE_SENTINEL"
        else:
            manifest["payload"] = "M708B_SENSITIVE_SENTINEL"
        (suspect / "manifest.json").write_text(json.dumps(manifest))
    elif bad == "missing":
        (suspect / "database.dump").unlink()
    elif bad == "hash-content":
        (suspect / "database.dump").write_bytes(b"M708B_SENSITIVE_SENTINEL")
    elif bad == "unknown-member":
        (suspect / "unexpected").write_bytes(b"M708B_SENSITIVE_SENTINEL")
    elif bad == "symlink-file":
        (suspect / "database.dump").unlink()
        (suspect / "database.dump").symlink_to(sentinel / "secret-body-sentinel")
    elif bad == "symlink-directory":
        (root / "escape").symlink_to(sentinel, target_is_directory=True)
    elif bad == "unknown-file":
        (root / "unexpected").write_bytes(b"M708B_SENSITIVE_SENTINEL")
    else:
        (root / "unexpected").mkdir()
        (root / "unexpected" / "nested").symlink_to(sentinel, target_is_directory=True)
    with pytest.raises(BackupError) as error:
        retain_backups(root, now=NOW)
    assert "M708B_SENSITIVE_SENTINEL" not in str(error.value)
    assert str(tmp_path) not in str(error.value)
    assert {item.name for item in old.iterdir()} == BACKUP_MEMBERS
    assert (sentinel / "secret-body-sentinel").read_bytes() == b"M708B_SENSITIVE_SENTINEL"


def test_M7_08B_TC_DP_12_missing_permissions_root_escape_and_cancellation(tmp_path: Path) -> None:
    with pytest.raises(BackupError):
        retain_backups(tmp_path / "missing", now=NOW)
    root = tmp_path / "backups"
    root.mkdir(mode=0o500)
    with pytest.raises(BackupError, match="unsafe_backup_root_permissions"):
        retain_backups(root, now=NOW)
    root.chmod(0o700)
    root_link = tmp_path / "link"
    root_link.symlink_to(root, target_is_directory=True)
    with pytest.raises(BackupError):
        retain_backups(root_link, now=NOW)
    with pytest.raises(BackupError):
        retain_backups(root_link / "nested", now=NOW)
    with pytest.raises(BackupError):
        retain_backups(root / ".." / "backups", now=NOW)
    cancelled = Event()
    cancelled.set()
    write_backup(root, "old", NOW - timedelta(days=100))
    with pytest.raises(BackupError, match="maintenance_stopping"):
        retain_backups(root, now=NOW, stopping=cancelled)
    assert (root / "old").is_dir()


def test_M7_08B_TC_DP_12_validator_handles_utc_offsets(tmp_path: Path) -> None:
    backup = write_backup(tmp_path, "offset", datetime.fromisoformat("2026-01-11T20:00:00+08:00"))
    assert validate_backup_manifest(backup, now=NOW)["created_at"].endswith("+08:00")


@pytest.mark.parametrize("period", [0, -1, float("nan"), float("inf"), 11])
def test_M7_08B_heartbeat_bounds_reject_invalid_period(period: float) -> None:
    with pytest.raises(ValueError):
        MaintenanceWorker(
            backup_root=Path("/unused"),
            session_factory=None,
            instance_id="test",
            build_version="test",
            heartbeat_seconds=period,
        )  # type: ignore[arg-type]

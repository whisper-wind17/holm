"""UTC seven-day plus four completed ISO-week backup retention."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

from holm_persistence.backup_format import (
    BACKUP_MEMBERS,
    BackupError,
    open_backup_child,
    open_backup_directory,
    validate_backup_at,
)


@dataclass(frozen=True, slots=True)
class BackupRetentionResult:
    validated: int
    retained: int
    deleted: int


def retain_backups(
    root: Path, *, now: datetime | None = None, stopping: Event | None = None
) -> BackupRetentionResult:
    """Validate the entire root before deleting any expired fixed-member backup.

    Unknown entries fail the whole pass. No recursive removal, Secret decryption,
    directory mtime, model access or PostgreSQL DDL participates in this task.
    """
    instant = now or datetime.now(UTC)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise BackupError("invalid_retention_time")
    instant = instant.astimezone(UTC)

    def check_cancelled() -> None:
        if stopping is not None and stopping.is_set():
            raise BackupError("maintenance_stopping")

    if root.absolute() == Path("/"):
        raise BackupError("invalid_backup_directory")
    try:
        with open_backup_directory(root) as directory:
            metadata = os.fstat(directory)
            # An inaccessible or shared writable root is a configuration failure,
            # even when a privileged development fixture could bypass its mode.
            if stat.S_IMODE(metadata.st_mode) & 0o700 != 0o700 or metadata.st_mode & 0o022:
                raise BackupError("unsafe_backup_root_permissions")
            backups: list[tuple[datetime, str]] = []
            for name in sorted(os.listdir(directory)):
                check_cancelled()
                with open_backup_child(directory, name) as child:
                    manifest = validate_backup_at(
                        child, now=instant, check_cancelled=check_cancelled
                    )
                    backups.append(
                        (datetime.fromisoformat(manifest["created_at"]).astimezone(UTC), name)
                    )
            ordered = sorted(backups)
            keep = {name for created, name in ordered if created >= instant - timedelta(days=7)}
            if ordered:
                keep.add(ordered[-1][1])
            monday = instant.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
                days=instant.weekday()
            )
            for offset in range(1, 5):
                start = monday - timedelta(weeks=offset)
                candidates = [
                    (created, name)
                    for created, name in ordered
                    if start <= created < start + timedelta(weeks=1)
                ]
                if candidates:
                    keep.add(candidates[-1][1])
            deleted = 0
            for _, name in ordered:
                if name in keep:
                    continue
                check_cancelled()
                with open_backup_child(directory, name) as child:
                    # Recheck before mutation, pinning all operations to root/child
                    # descriptors. Never follow paths taken from the Manifest.
                    validate_backup_at(child, now=instant, check_cancelled=check_cancelled)
                    check_cancelled()
                    # Finish this fixed five-file operation before responding to
                    # SIGTERM; ordinary shutdown must not create a partial backup.
                    for member in sorted(BACKUP_MEMBERS):
                        if not stat.S_ISREG(
                            os.stat(member, dir_fd=child, follow_symlinks=False).st_mode
                        ):
                            raise BackupError("unsafe_item")
                        os.unlink(member, dir_fd=child)
                os.rmdir(name, dir_fd=directory)
                deleted += 1
            return BackupRetentionResult(len(ordered), len(keep), deleted)
    except OSError:
        raise BackupError("backup_retention_io_failed") from None

"""Backup format validation without decrypting Secrets or executing PG tools.

Directory descriptors and NOFOLLOW pin every read to the selected directory.
The same fixed-member and Manifest checks protect restore and retention.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

BACKUP_FILES: Final = frozenset({"database.dump", "secrets.enc", "spool.sqlite3", "artifacts.json"})
BACKUP_MEMBERS: Final = BACKUP_FILES | {"manifest.json"}
BACKUP_FORMAT_VERSION: Final = 1
BACKUP_NAME: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HASH: Final = re.compile(r"^[0-9a-f]{64}$")
_DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class BackupError(RuntimeError):
    """A stable error code; paths, payloads and underlying errors never escape."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"backup operation failed: {code}")


@contextmanager
def open_backup_directory(path: Path) -> Iterator[int]:
    """Open a directory without following any symlink, including its parents."""
    if ".." in path.parts:
        raise BackupError("invalid_backup_directory")
    absolute = path.absolute()
    descriptor = os.open(absolute.anchor, _DIRECTORY_FLAGS)
    try:
        for part in absolute.parts[1:]:
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    except OSError:
        raise BackupError("backup_directory_unavailable") from None
    finally:
        os.close(descriptor)


@contextmanager
def open_backup_child(root: int, name: str) -> Iterator[int]:
    if BACKUP_NAME.fullmatch(name) is None:
        raise BackupError("invalid_backup_name")
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=root)
    except OSError:
        raise BackupError("invalid_backup_directory") from None
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _read_member(directory: int, name: str, *, maximum: int | None = None) -> int:
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(descriptor)
            raise BackupError("unsafe_item")
        if maximum is not None and metadata.st_size > maximum:
            os.close(descriptor)
            raise BackupError("invalid_backup_manifest")
        return descriptor
    except OSError:
        raise BackupError("missing_or_unsafe_item") from None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def validate_backup_at(
    directory: int, *, now: datetime, check_cancelled: Callable[[], None] = lambda: None
) -> dict[str, Any]:
    """Validate the complete fixed envelope and streaming hashes, without decrypting it."""
    check_cancelled()
    if set(os.listdir(directory)) != BACKUP_MEMBERS:
        raise BackupError("unexpected_backup_items")
    descriptor = _read_member(directory, "manifest.json", maximum=65536)
    try:
        raw = os.read(descriptor, 65537)
    finally:
        os.close(descriptor)
    try:
        manifest = json.loads(raw, object_pairs_hook=_unique_object)
        if (
            not isinstance(manifest, dict)
            or set(manifest)
            != {"format_version", "release", "migration_revision", "created_at", "sha256"}
            or type(manifest["format_version"]) is not int
            or manifest["format_version"] != BACKUP_FORMAT_VERSION
            or not isinstance(manifest["release"], str)
            or not manifest["release"].strip()
            or not isinstance(manifest["migration_revision"], str)
            or not manifest["migration_revision"].strip()
            or not isinstance(manifest["created_at"], str)
            or not isinstance(manifest["sha256"], dict)
            or set(manifest["sha256"]) != BACKUP_FILES
        ):
            raise ValueError
        created = datetime.fromisoformat(manifest["created_at"])
        if created.tzinfo is None or created.utcoffset() is None or created > now:
            raise ValueError
        if any(
            not isinstance(value, str) or _HASH.fullmatch(value) is None
            for value in manifest["sha256"].values()
        ):
            raise ValueError
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise BackupError("invalid_backup_manifest") from None
    for name in sorted(BACKUP_FILES):
        descriptor = _read_member(directory, name)
        digest = hashlib.sha256()
        try:
            while True:
                check_cancelled()
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        finally:
            os.close(descriptor)
        if digest.hexdigest() != manifest["sha256"][name]:
            raise BackupError("backup_hash_mismatch")
    return manifest


def validate_backup_manifest(source: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Read-only format preflight also used by the stronger restore validation."""
    with open_backup_directory(source) as directory:
        return validate_backup_at(directory, now=now or datetime.now(UTC))

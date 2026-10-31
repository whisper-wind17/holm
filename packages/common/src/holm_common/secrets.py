"""Fail-closed Linux Secret file reading without logging or value transformation."""

from __future__ import annotations

import os
import stat
from collections.abc import Collection
from pathlib import Path
from typing import Final, Never

DEFAULT_MAX_SECRET_BYTES: Final[int] = 64 * 1024
ALLOWED_SECRET_MODES: Final[frozenset[int]] = frozenset({0o400, 0o440})


class SecretFileError(RuntimeError):
    """Sanitized Secret file failure with a stable machine-readable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"secret file rejected: {code}")


def _fail(code: str) -> Never:
    raise SecretFileError(code)


def _validate_directory(metadata: os.stat_result, expected_uid: int, expected_gid: int) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        _fail("invalid_path")
    mode = stat.S_IMODE(metadata.st_mode)
    # Docker's /run/secrets is root-owned; as with sshd StrictModes, a root-owned
    # directory that no non-root user can write cannot have its entries replaced.
    if metadata.st_uid == 0 and not mode & 0o022:
        return
    if metadata.st_uid != expected_uid or metadata.st_gid != expected_gid:
        _fail("owner_mismatch")
    if mode & 0o027:
        _fail("directory_permissions")


def _open_parent_directory(path: Path, expected_uid: int, expected_gid: int) -> int:
    """Pin every directory component so later path swaps cannot redirect the read."""
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors: list[int] = []
    try:
        descriptors.append(os.open(path.anchor, flags))
        for component in path.parts[1:-1]:
            if component in {"", ".", ".."}:
                _fail("invalid_path")
            try:
                descriptor = os.open(component, flags, dir_fd=descriptors[-1])
            except OSError:
                try:
                    metadata = os.stat(component, dir_fd=descriptors[-1], follow_symlinks=False)
                except (OSError, ValueError):
                    _fail("unreadable")
                if stat.S_ISLNK(metadata.st_mode):
                    _fail("symlink")
                if not stat.S_ISDIR(metadata.st_mode):
                    _fail("invalid_path")
                _fail("unreadable")
            descriptors.append(descriptor)

        _validate_directory(os.fstat(descriptors[-1]), expected_uid, expected_gid)
        parent = descriptors.pop()
        return parent
    except (OSError, ValueError):
        _fail("unreadable")
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def _validate_file(
    metadata: os.stat_result,
    expected_uid: int,
    expected_gid: int,
    max_bytes: int,
    allowed_modes: frozenset[int],
) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        _fail("not_regular")
    if metadata.st_uid != expected_uid or metadata.st_gid != expected_gid:
        _fail("owner_mismatch")
    if stat.S_IMODE(metadata.st_mode) not in allowed_modes:
        _fail("file_permissions")
    if metadata.st_size > max_bytes:
        _fail("too_large")


def _classify_final_open_failure(
    path: Path,
    parent_descriptor: int,
    expected_uid: int,
    expected_gid: int,
    max_bytes: int,
    allowed_modes: frozenset[int],
) -> Never:
    try:
        metadata = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
    except (OSError, ValueError):
        _fail("unreadable")
    if stat.S_ISLNK(metadata.st_mode):
        _fail("symlink")
    _validate_file(metadata, expected_uid, expected_gid, max_bytes, allowed_modes)
    _fail("unreadable")


def read_secret_file(
    path: str | os.PathLike[str],
    *,
    expected_uid: int | None = None,
    expected_gid: int | None = None,
    max_bytes: int = DEFAULT_MAX_SECRET_BYTES,
    allowed_modes: Collection[int] = ALLOWED_SECRET_MODES,
) -> bytes:
    """Read raw Secret bytes after identity, type, link, mode, and size checks."""
    try:
        raw_path = os.fspath(path)
    except TypeError:
        _fail("invalid_path")
    if not isinstance(raw_path, str) or "\x00" in raw_path:
        _fail("invalid_path")

    secret_path = Path(raw_path)
    normalized_modes = frozenset(allowed_modes)
    if (
        not secret_path.is_absolute()
        or secret_path.name in {"", ".", ".."}
        or max_bytes <= 0
        or not normalized_modes
        or any(mode < 0 or mode > 0o777 for mode in normalized_modes)
    ):
        _fail("invalid_path")

    uid = os.geteuid() if expected_uid is None else expected_uid
    gid = os.getegid() if expected_gid is None else expected_gid
    parent_descriptor = _open_parent_directory(secret_path, uid, gid)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW
    try:
        descriptor = os.open(secret_path.name, flags, dir_fd=parent_descriptor)
    except OSError:
        _classify_final_open_failure(
            secret_path,
            parent_descriptor,
            uid,
            gid,
            max_bytes,
            normalized_modes,
        )
    finally:
        os.close(parent_descriptor)

    try:
        metadata = os.fstat(descriptor)
        _validate_file(metadata, uid, gid, max_bytes, normalized_modes)

        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, min(8192, max_bytes - total + 1)):
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                _fail("too_large")
        return b"".join(chunks)
    except OSError:
        _fail("unreadable")
    finally:
        os.close(descriptor)

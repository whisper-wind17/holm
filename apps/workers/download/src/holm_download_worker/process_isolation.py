"""Shared spawn-child isolation and bounded termination primitives."""

from __future__ import annotations

import ctypes
import os
import signal
import sys
from collections.abc import Callable
from multiprocessing.context import SpawnProcess
from pathlib import Path
from typing import Final

from holm_domain import canonical_artifact_path

DEFAULT_COOPERATIVE_STOP_SECONDS: Final[float] = 10.0
PROCESS_STOP_SECONDS: Final[float] = 1.0
PR_SET_PDEATHSIG: Final[int] = 1
CHILD_ENVIRONMENT: Final[dict[str, str]] = {
    "DO_NOT_TRACK": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "HOME": "/nonexistent",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "TZ": "UTC",
}
_WRITE_OPEN_FLAGS: Final[int] = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
_SINGLE_TARGET_MUTATION_EVENTS: Final[dict[str, int | None]] = {
    "os.chmod": 2,
    "os.chown": 3,
    "os.mkdir": 2,
    "os.remove": 1,
    "os.removexattr": None,
    "os.rmdir": 1,
    "os.setxattr": None,
    "os.truncate": None,
    "os.utime": 3,
}


def install_parent_death_fence(parent_pid: int) -> bool:
    if not sys_platform_is_linux():
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        return False
    if os.getppid() != parent_pid:
        os.kill(os.getpid(), signal.SIGKILL)
    return True


def sys_platform_is_linux() -> bool:
    return os.name == "posix" and Path("/proc/self").exists()


def silence_child_stdio() -> None:
    descriptor = os.open(os.devnull, os.O_RDWR)
    try:
        for target in (0, 1, 2):
            os.dup2(descriptor, target)
    finally:
        os.close(descriptor)


def install_child_environment() -> None:
    """Remove inherited credentials and SDK configuration before source I/O.

    The spawned interpreter may have inherited a bytecode cache prefix before
    this function runs.  Disable every later bytecode write so lazy SDK imports
    cannot escape a download's staging fence through that inherited prefix.
    """

    os.environ.clear()
    os.environ.update(CHILD_ENVIRONMENT)
    sys.dont_write_bytecode = True


def install_staging_write_fence(staging_path: Path) -> Callable[[], None]:
    """Refuse every filesystem mutation whose target is not safe under staging.

    CPython invokes audit hooks before the corresponding filesystem operation.
    The hook is process-wide and cannot be removed, which is a useful property
    here: the download already runs in a fresh child, and every later SDK read
    and worker thread remains behind the same fence.
    """

    staging = staging_path.absolute()
    refused = False

    def assert_target(target: object, dir_fd: object = None) -> None:
        nonlocal refused
        if isinstance(target, int):
            # An already-open descriptor cannot create a new directory entry.
            return
        candidate = Path(os.fsdecode(target))  # type: ignore[arg-type]
        if not candidate.is_absolute():
            base = Path.cwd()
            if isinstance(dir_fd, int) and dir_fd >= 0:
                try:
                    base = Path(os.readlink(f"/proc/self/fd/{dir_fd}"))
                except OSError:
                    refused = True
                    raise PermissionError("download filesystem write was refused") from None
            candidate = base / candidate
        if candidate == Path(os.devnull):
            # SDK subprocess plumbing opens the kernel's discard device for an
            # existing descriptor; this cannot create or overwrite an entry.
            return
        if candidate == staging:
            return
        try:
            relative = candidate.relative_to(staging)
        except ValueError:
            relative = candidate
        try:
            canonical_artifact_path(relative.as_posix())
        except ValueError:
            refused = True
            raise

    def audit(event: str, arguments: tuple[object, ...]) -> None:
        if event == "open":
            target, mode, flags = arguments
            writable_mode = isinstance(mode, str) and any(mark in mode for mark in "wax+")
            writable_flags = isinstance(flags, int) and bool(flags & _WRITE_OPEN_FLAGS)
            if writable_mode or writable_flags:
                assert_target(target)
            return
        if event in _SINGLE_TARGET_MUTATION_EVENTS:
            target = arguments[0]
            dir_fd_index = _SINGLE_TARGET_MUTATION_EVENTS[event]
            dir_fd = arguments[dir_fd_index] if dir_fd_index is not None else None
            assert_target(target, dir_fd)
            return
        if event in {"os.rename", "os.link"}:
            source, destination, source_dir_fd, destination_dir_fd = arguments
            assert_target(source, source_dir_fd)
            assert_target(destination, destination_dir_fd)
            return
        if event == "os.symlink":
            _, destination, destination_dir_fd = arguments
            assert_target(destination, destination_dir_fd)

    def assert_no_refusal() -> None:
        if refused:
            raise PermissionError("download filesystem write was refused")

    sys.addaudithook(audit)
    return assert_no_refusal


def stop_process(process: SpawnProcess) -> None:
    process.join(timeout=PROCESS_STOP_SECONDS)
    if process.is_alive():
        process.terminate()
        process.join(timeout=PROCESS_STOP_SECONDS)
    if process.is_alive():
        process.kill()
        process.join(timeout=PROCESS_STOP_SECONDS)


def cooperatively_stop_process(process: SpawnProcess, timeout_seconds: float) -> None:
    """Ask the child to stop, then force it only after the configured boundary."""

    process.join(timeout=0)
    if not process.is_alive():
        return
    process.terminate()
    process.join(timeout=timeout_seconds)
    if process.is_alive():
        process.kill()
        process.join(timeout=PROCESS_STOP_SECONDS)


__all__ = [
    "CHILD_ENVIRONMENT",
    "DEFAULT_COOPERATIVE_STOP_SECONDS",
    "cooperatively_stop_process",
    "install_child_environment",
    "install_parent_death_fence",
    "install_staging_write_fence",
    "silence_child_stdio",
    "stop_process",
    "sys_platform_is_linux",
]

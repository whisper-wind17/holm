"""Resumable, secret-free installation journal (host standard library)."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

STAGES = (
    "preflight",
    "identities",
    "configuration",
    "secrets",
    "agent",
    "database",
    "platform",
    "smoke",
    "activate",
)


class InstallError(Exception):
    """Only reviewed codes cross the command/log boundary."""

    def __init__(
        self,
        code: str,
        remedy: str = "Correct the configuration and rerun the same install command.",
    ) -> None:
        self.code = code
        self.remedy = remedy
        super().__init__(code)


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name(path.name + ".new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", closefd=False) as handle:
            json.dump(value, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)


@contextmanager
def install_lock(path: Path) -> Iterator[None]:
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        metadata = os.fstat(fd)
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise InstallError("unsafe_lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallError(
                "install_in_progress", "Wait for the other installer to finish."
            ) from None
        yield
    finally:
        os.close(fd)


class Journal:
    def __init__(self, path: Path, config: dict[str, Any]) -> None:
        self.path = path
        fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        self.data: dict[str, Any] = {
            "schema_version": 1,
            "configuration_sha256": fingerprint,
            "completed": [],
            "created": [],
            "status": "installing",
            "uninstall": {
                "project": config.get("project_name", "holm"),
                "external_model_root": config.get(
                    "external_model_root", "/var/lib/holm/external-models"
                ),
            },
        }
        if path.exists():
            if (
                path.is_symlink()
                or path.stat().st_uid != os.geteuid()
                or stat.S_IMODE(path.stat().st_mode) != 0o600
            ):
                raise InstallError("unsafe_journal")
            try:
                value = json.loads(path.read_text())
            except (ValueError, OSError):
                raise InstallError("invalid_journal") from None
            if (
                not isinstance(value, dict)
                or value.get("schema_version") != 1
                or value.get("configuration_sha256") != fingerprint
            ):
                raise InstallError(
                    "different_installation",
                    "Use the original release and installation configuration.",
                )
            completed = value.get("completed")
            if (
                not isinstance(completed, list)
                or completed != list(STAGES[: len(completed)])
                or not isinstance(value.get("created"), list)
            ):
                raise InstallError("invalid_journal")
            self.data = value

    def save(self) -> None:
        atomic_json(self.path, self.data)

    def created(self, kind: str, name: str) -> None:
        if kind not in {
            "directory",
            "file",
            "symlink",
            "user",
            "group",
            "unit",
            "container",
            "network",
        }:
            raise InstallError("invalid_object_kind")
        item = {"kind": kind, "name": name}
        if item not in self.data["created"]:
            self.data["created"].append(item)
            self.save()

    def run(self, actions: dict[str, Callable[[], None]], report: Callable[[str], None]) -> int:
        for index, stage in enumerate(STAGES):
            if stage in self.data["completed"]:
                continue
            try:
                actions[stage]()
            except InstallError as error:
                self.data["status"] = "failed"
                self.data["failed_stage"] = stage
                self.save()
                report(f"stage={stage} result=failed code={error.code} remedy={error.remedy}")
                return 20 + index
            except (Exception, KeyboardInterrupt):
                self.data["status"] = "failed"
                self.data["failed_stage"] = stage
                self.save()
                report(
                    f"stage={stage} result=failed code=operation_failed remedy=Check prerequisites and rerun this installation."
                )
                return 20 + index
            self.data["completed"].append(stage)
            self.data.pop("failed_stage", None)
            self.data["status"] = "complete" if stage == STAGES[-1] else "installing"
            self.save()
            report(f"stage={stage} result=complete")
        return 0

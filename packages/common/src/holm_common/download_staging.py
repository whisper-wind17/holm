"""Safe filesystem and write-throttling primitives for managed downloads."""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

GIBIBYTE = 1024**3
DOWNLOAD_HEADROOM_BYTES = 5 * GIBIBYTE
DOWNLOAD_HEADROOM_PERCENT = 10


class StagingPathRejected(ValueError):
    """A staging path did not resolve to the platform-owned task directory."""


class StagingFilesystemMismatch(RuntimeError):
    """Staging and managed storage cannot support an atomic publication rename."""


@dataclass(frozen=True, slots=True)
class DiskSpaceSnapshot:
    """Byte-accurate free-space values from one real filesystem."""

    free_bytes: int
    capacity_bytes: int

    @classmethod
    def for_path(cls, path: Path) -> DiskSpaceSnapshot:
        values = os.statvfs(path)
        return cls(
            free_bytes=values.f_bavail * values.f_frsize,
            capacity_bytes=values.f_blocks * values.f_frsize,
        )

    def is_critical(self, *, free_percent: int, free_gibibytes: int) -> bool:
        if self.capacity_bytes <= 0:
            return True
        return (
            self.free_bytes * 100 < self.capacity_bytes * free_percent
            or self.free_bytes < free_gibibytes * GIBIBYTE
        )


@dataclass(frozen=True, slots=True)
class DownloadCapacity:
    """Known-size download preflight without exposing host capacity in errors."""

    required_free_bytes: int
    additional_bytes_required: int

    @property
    def accepted(self) -> bool:
        return self.additional_bytes_required == 0


def check_download_capacity(*, total_bytes: int, free_bytes: int) -> DownloadCapacity:
    """Require size plus max(5 GiB, a ceiling-rounded ten-percent margin)."""

    if total_bytes < 0 or free_bytes < 0:
        raise ValueError("download capacity values must be non-negative")
    percentage_margin = (total_bytes * DOWNLOAD_HEADROOM_PERCENT + 99) // 100
    required = total_bytes + max(DOWNLOAD_HEADROOM_BYTES, percentage_margin)
    return DownloadCapacity(
        required_free_bytes=required,
        additional_bytes_required=max(0, required - free_bytes),
    )


def assert_same_filesystem(staging_root: Path, managed_root: Path) -> None:
    """Fail before any model byte is written when atomic rename is impossible."""

    if os.stat(staging_root).st_dev != os.stat(managed_root).st_dev:
        raise StagingFilesystemMismatch("staging filesystem does not support atomic publish")


def _resolved_descendant(path: Path, root: Path) -> bool:
    resolved_path = path.resolve(strict=False)
    resolved_root = root.resolve(strict=False)
    return resolved_path != resolved_root and resolved_path.is_relative_to(resolved_root)


@dataclass(frozen=True, slots=True)
class StagingLayout:
    """Platform-owned layout; source identifiers never participate in its paths."""

    model_root: Path

    def __post_init__(self) -> None:
        if not self.model_root.is_absolute():
            raise ValueError("model root must be absolute")

    @property
    def staging_root(self) -> Path:
        return self.model_root / ".staging"

    @property
    def managed_root(self) -> Path:
        return self.model_root / "managed"

    def task_path(self, task_id: UUID) -> Path:
        candidate = self.staging_root / str(task_id)
        resolved_staging = self.staging_root.resolve(strict=False)
        expected = resolved_staging / str(task_id)
        if (
            self.staging_root.is_symlink()
            or not _resolved_descendant(self.staging_root, self.model_root)
            or candidate.is_symlink()
            or candidate.resolve(strict=False) != expected
        ):
            raise StagingPathRejected("staging task path escaped its managed root")
        return expected

    def preflight(
        self, task_id: UUID, *, total_bytes: int | None
    ) -> tuple[Path, DownloadCapacity | None]:
        """Prepare roots and validate publication prerequisites without a task write."""

        self.model_root.mkdir(parents=True, exist_ok=True)
        self.staging_root.mkdir(exist_ok=True)
        self.managed_root.mkdir(exist_ok=True)
        assert_same_filesystem(self.staging_root, self.managed_root)
        if self.staging_root.is_symlink() or not _resolved_descendant(
            self.staging_root, self.model_root
        ):
            raise StagingPathRejected("staging root escaped model storage")
        if self.managed_root.is_symlink() or not _resolved_descendant(
            self.managed_root, self.model_root
        ):
            raise StagingPathRejected("managed root escaped model storage")

        capacity: DownloadCapacity | None = None
        if total_bytes is not None:
            capacity = check_download_capacity(
                total_bytes=total_bytes,
                free_bytes=DiskSpaceSnapshot.for_path(self.staging_root).free_bytes,
            )
            if not capacity.accepted:
                return self.task_path(task_id), capacity

        return self.task_path(task_id), capacity

    def create_task_directory(self, task_id: UUID) -> Path:
        """Create or reuse the exact UUID directory after preflight succeeds."""

        task_path = self.task_path(task_id)
        task_path.mkdir(exist_ok=True)
        if task_path.is_symlink() or not _resolved_descendant(task_path, self.staging_root):
            raise StagingPathRejected("staging task directory is not platform-owned")
        return task_path

    def prepare(
        self, task_id: UUID, *, total_bytes: int | None
    ) -> tuple[Path, DownloadCapacity | None]:
        """Convenience operation for callers without a persistence transaction."""

        task_path, capacity = self.preflight(task_id, total_bytes=total_bytes)
        if capacity is None or capacity.accepted:
            self.create_task_directory(task_id)
        return task_path, capacity

    def cleanup(self, task_id: UUID, persisted_path: str) -> None:
        """Idempotently delete only the exact platform-derived staging directory."""

        expected = self.task_path(task_id)
        supplied = Path(persisted_path)
        if not supplied.is_absolute() or supplied.resolve(strict=False) != expected:
            raise StagingPathRejected("persisted staging path is outside the task directory")
        if not _resolved_descendant(supplied, self.staging_root):
            raise StagingPathRejected("persisted staging path escaped staging storage")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            staging_descriptor = os.open(self.staging_root, flags)
        except FileNotFoundError:
            return
        except OSError as error:
            raise StagingPathRejected("staging root cannot be opened safely") from error
        try:
            relative_task = str(task_id)
            try:
                task_stat = os.stat(
                    relative_task,
                    dir_fd=staging_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return
            if not stat.S_ISDIR(task_stat.st_mode):
                raise StagingPathRejected("staging cleanup target is not a directory")
            shutil.rmtree(relative_task, dir_fd=staging_descriptor)
        finally:
            os.close(staging_descriptor)


@dataclass(frozen=True, slots=True)
class TokenizerStagingLayout:
    """Platform-owned Tokenizer Bundle staging and publication paths."""

    tokenizer_root: Path

    def __post_init__(self) -> None:
        if not self.tokenizer_root.is_absolute():
            raise ValueError("tokenizer root must be absolute")

    @property
    def staging_root(self) -> Path:
        return self.tokenizer_root / ".staging"

    def staging_path(self, artifact_id: UUID) -> Path:
        return self._safe_child(self.staging_root, artifact_id, "tokenizer staging")

    def destination_path(self, artifact_id: UUID) -> Path:
        return self._safe_child(self.tokenizer_root, artifact_id, "tokenizer destination")

    def preflight(self, artifact_id: UUID, *, model_root: Path) -> Path:
        """Check both filesystems and all owned roots before a Bundle byte is copied."""

        self._require_safe_existing_components(self.tokenizer_root)
        self._require_safe_existing_components(self.staging_root)
        self.tokenizer_root.mkdir(parents=True, exist_ok=True)
        self._require_directory(self.tokenizer_root, parent=self.tokenizer_root.parent)
        self.staging_root.mkdir(exist_ok=True)
        self._require_directory(self.staging_root, parent=self.tokenizer_root)
        model_root.mkdir(parents=True, exist_ok=True)
        assert_same_filesystem(model_root, self.tokenizer_root)
        assert_same_filesystem(self.staging_root, self.tokenizer_root)
        self.destination_path(artifact_id)
        return self.staging_path(artifact_id)

    def create_staging_directory(self, artifact_id: UUID) -> Path:
        staging = self.staging_path(artifact_id)
        staging.mkdir(exist_ok=True)
        self._require_directory(staging, parent=self.staging_root)
        return staging

    def cleanup_staging(self, artifact_id: UUID) -> None:
        """Remove only the exact platform-derived Bundle staging directory."""

        staging = self.staging_path(artifact_id)
        if not staging.exists():
            return
        self._require_directory(staging, parent=self.staging_root)
        shutil.rmtree(staging)

    @staticmethod
    def _safe_child(root: Path, artifact_id: UUID, label: str) -> Path:
        candidate = root / str(artifact_id)
        resolved_root = root.resolve(strict=False)
        expected = resolved_root / str(artifact_id)
        if (
            root.is_symlink()
            or not _resolved_descendant(root, root.parent)
            or candidate.is_symlink()
            or candidate.resolve(strict=False) != expected
        ):
            raise StagingPathRejected(f"{label} path escaped its owned root")
        return expected

    @staticmethod
    def _require_directory(path: Path, *, parent: Path) -> None:
        try:
            metadata = path.lstat()
        except OSError as error:
            raise StagingPathRejected("tokenizer storage root is unavailable") from error
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or (path != parent and not _resolved_descendant(path, parent))
        ):
            raise StagingPathRejected("tokenizer storage root is unsafe")

    @staticmethod
    def _require_safe_existing_components(path: Path) -> None:
        current = Path(path.anchor)
        for component in path.parts[1:]:
            current /= component
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                return
            except OSError as error:
                raise StagingPathRejected("tokenizer storage path is unavailable") from error
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise StagingPathRejected("tokenizer storage path is unsafe")


@dataclass(slots=True)
class ProgressWriteThrottle:
    """Admit writes at one second or one percentage-point progress, whichever comes first."""

    total_bytes: int | None
    minimum_interval_seconds: float = 1.0
    _last_downloaded_bytes: int | None = None
    _last_write_at: float | None = None

    def __post_init__(self) -> None:
        if self.total_bytes is not None and self.total_bytes < 0:
            raise ValueError("total bytes must be non-negative or unknown")
        if self.minimum_interval_seconds <= 0:
            raise ValueError("progress interval must be positive")

    def percentage(self, downloaded_bytes: int) -> int | None:
        self._validate(downloaded_bytes)
        if self.total_bytes is None:
            return None
        if self.total_bytes == 0:
            return 100
        return min(100, downloaded_bytes * 100 // self.total_bytes)

    def is_due(self, downloaded_bytes: int, *, monotonic_now: float) -> bool:
        self._validate(downloaded_bytes)
        if self._last_write_at is None or self._last_downloaded_bytes is None:
            return True
        if monotonic_now < self._last_write_at:
            raise ValueError("monotonic clock moved backwards")
        if monotonic_now - self._last_write_at >= self.minimum_interval_seconds:
            return True
        return (
            self.total_bytes is not None
            and self.total_bytes > 0
            and (downloaded_bytes - self._last_downloaded_bytes) * 100 >= self.total_bytes
        )

    def mark_written(self, downloaded_bytes: int, *, monotonic_now: float) -> None:
        self._validate(downloaded_bytes)
        self._last_downloaded_bytes = downloaded_bytes
        self._last_write_at = monotonic_now

    def _validate(self, downloaded_bytes: int) -> None:
        if downloaded_bytes < 0:
            raise ValueError("downloaded bytes must be non-negative")
        if (
            self._last_downloaded_bytes is not None
            and downloaded_bytes < self._last_downloaded_bytes
        ):
            raise ValueError("download progress must not move backwards")
        if self.total_bytes is not None and downloaded_bytes > self.total_bytes:
            raise ValueError("download progress exceeds the known total")


__all__ = [
    "DOWNLOAD_HEADROOM_BYTES",
    "DOWNLOAD_HEADROOM_PERCENT",
    "GIBIBYTE",
    "DiskSpaceSnapshot",
    "DownloadCapacity",
    "ProgressWriteThrottle",
    "StagingFilesystemMismatch",
    "StagingLayout",
    "StagingPathRejected",
    "assert_same_filesystem",
    "check_download_capacity",
]

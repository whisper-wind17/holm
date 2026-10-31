"""Fail-closed Download Worker configuration loaded from non-secret arguments."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from holm_common import SecretFileError, read_secret_file
from holm_common.config import StorageConfig
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

DEFAULT_DOWNLOAD_MAX_WORKERS: Final[int] = 16


class DownloadWorkerConfigurationError(RuntimeError):
    """A configuration value is absent or unsafe; the value is never rendered."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("download worker configuration rejected")


def load_database_url(path: Path, *, expected_gid: int | None = None) -> URL:
    """Read the role-specific DSN under the worker's fixed identity."""

    try:
        raw = read_secret_file(path, expected_gid=expected_gid)
    except SecretFileError as error:
        raise DownloadWorkerConfigurationError(
            f"database_secret_unavailable:{error.code}"
        ) from None
    try:
        value = raw.decode("utf-8").strip()
        url = make_url(value)
    except (ArgumentError, UnicodeDecodeError, ValueError, TypeError):
        raise DownloadWorkerConfigurationError("database_url_invalid") from None
    if not value or url.drivername != "postgresql+psycopg" or not url.username:
        raise DownloadWorkerConfigurationError("database_url_invalid")
    return url


@dataclass(frozen=True, slots=True)
class DownloadWorkerConfiguration:
    database_url_file: Path = field(repr=False)
    master_keyring_file: Path = field(repr=False)
    instance_id: str
    build_version: str
    external_model_roots: tuple[Path, ...] = ()
    scratch_root: Path | None = None
    agent_socket: Path | None = None
    hugging_face_endpoint: str = "https://huggingface.co"
    modelscope_endpoint: str = "https://www.modelscope.cn"
    lease_seconds: float = 30.0
    heartbeat_seconds: float = 10.0
    poll_seconds: float = 0.25
    resolution_timeout_seconds: float = 15.0
    download_max_workers: int = DEFAULT_DOWNLOAD_MAX_WORKERS
    storage: StorageConfig = StorageConfig()

    def __post_init__(self) -> None:
        if (
            isinstance(self.download_max_workers, bool)
            or not isinstance(self.download_max_workers, int)
            or not 1 <= self.download_max_workers <= 64
        ):
            raise DownloadWorkerConfigurationError("download_max_workers_invalid")
        for value, name in (
            (self.instance_id, "instance_id"),
            (self.build_version, "build_version"),
        ):
            if not value or value != value.strip() or len(value) > 128:
                raise DownloadWorkerConfigurationError(f"{name}_invalid")
        if self.heartbeat_seconds <= 0 or self.lease_seconds <= self.heartbeat_seconds:
            raise DownloadWorkerConfigurationError("lease_timing_invalid")
        if self.poll_seconds <= 0 or self.resolution_timeout_seconds <= 0:
            raise DownloadWorkerConfigurationError("worker_timing_invalid")


__all__ = [
    "DownloadWorkerConfiguration",
    "DownloadWorkerConfigurationError",
    "load_database_url",
]

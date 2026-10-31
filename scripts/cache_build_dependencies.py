#!/usr/bin/env python3
"""Cache the hash-locked Python build backend selected from ``uv.lock``."""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import tomllib
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Final

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
BUILD_BACKEND_NAME: Final[str] = "uv-build"
BUILD_BACKEND_VERSION: Final[str] = "0.11.7"
LOCKED_WHEEL_SUFFIX: Final[str] = "py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
DOWNLOAD_HOST: Final[str] = "files.pythonhosted.org"


@dataclass(frozen=True)
class LockedWheel:
    """An immutable wheel record selected from the workspace lockfile."""

    url: str
    filename: str
    sha256: str
    size: int


def sha256(path: Path) -> str:
    """Return a streaming SHA-256 digest."""
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def locked_build_wheel() -> LockedWheel:
    """Select the certified Linux x86_64 build-backend wheel from ``uv.lock``."""
    if platform.system() != "Linux" or platform.machine().lower() not in {
        "amd64",
        "x86_64",
    }:
        raise RuntimeError(
            "reproducible builds are certified only on Linux x86_64; "
            f"found {platform.system()} {platform.machine()}"
        )

    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    matches = [
        package
        for package in lock["package"]
        if package["name"] == BUILD_BACKEND_NAME
        and package.get("version") == BUILD_BACKEND_VERSION
        and "registry" in package.get("source", {})
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"uv.lock must contain exactly one registry {BUILD_BACKEND_NAME} "
            f"{BUILD_BACKEND_VERSION} package"
        )

    wheel_matches = [
        wheel
        for wheel in matches[0].get("wheels", [])
        if urllib.parse.urlparse(wheel["url"]).path.endswith(LOCKED_WHEEL_SUFFIX)
    ]
    if len(wheel_matches) != 1:
        raise RuntimeError("uv.lock must contain exactly one certified Linux x86_64 uv-build wheel")
    wheel = wheel_matches[0]
    parsed_url = urllib.parse.urlparse(wheel["url"])
    filename = Path(parsed_url.path).name
    digest = wheel.get("hash", "")
    size = wheel.get("size")
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != DOWNLOAD_HOST
        or not filename.endswith(LOCKED_WHEEL_SUFFIX)
        or not digest.startswith("sha256:")
        or not isinstance(size, int)
        or size <= 0
    ):
        raise RuntimeError("uv.lock contains an invalid certified uv-build wheel record")
    return LockedWheel(
        url=wheel["url"],
        filename=filename,
        sha256=digest.removeprefix("sha256:"),
        size=size,
    )


def build_wheelhouse() -> Path:
    """Resolve the task wheelhouse without placing dependencies in the repository."""
    if configured := os.environ.get("HOLM_BUILD_WHEELHOUSE"):
        return Path(configured).expanduser().resolve()
    if uv_cache := os.environ.get("UV_CACHE_DIR"):
        return Path(uv_cache).expanduser().resolve() / "holm-build-wheelhouse"

    result = subprocess.run(
        ["uv", "cache", "dir"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip()).expanduser().resolve() / "holm-build-wheelhouse"


def _offline_requested() -> bool:
    return os.environ.get("UV_OFFLINE", "").lower() in {"1", "true", "yes"}


def _verify_cached_wheel(path: Path, locked: LockedWheel) -> bool:
    return path.is_file() and path.stat().st_size == locked.size and sha256(path) == locked.sha256


def ensure_locked_build_wheel(*, offline: bool | None = None) -> Path:
    """Return a verified cached wheel, downloading only its lockfile URL if allowed."""
    locked = locked_build_wheel()
    wheelhouse = build_wheelhouse()
    cached_path = wheelhouse / locked.filename
    if _verify_cached_wheel(cached_path, locked):
        print(f"verified cached build backend: {cached_path}")
        return cached_path
    if cached_path.exists():
        raise RuntimeError(
            f"cached build backend failed size or SHA-256 verification: {cached_path}"
        )
    effective_offline = _offline_requested() if offline is None else offline
    if effective_offline:
        raise RuntimeError(f"locked build backend is absent from offline cache: {cached_path}")

    wheelhouse.mkdir(parents=True, exist_ok=True)
    temporary_path = wheelhouse / f".{locked.filename}.{os.getpid()}.part"
    request = urllib.request.Request(
        locked.url,
        headers={"User-Agent": "holm-build-cache/1"},
    )
    digest = hashlib.sha256()
    written = 0
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            final_url = urllib.parse.urlparse(response.geturl())
            if final_url.scheme != "https" or final_url.hostname != DOWNLOAD_HOST:
                raise RuntimeError(
                    "build-backend download redirected outside files.pythonhosted.org"
                )
            with temporary_path.open("wb") as file_handle:
                while chunk := response.read(1024 * 1024):
                    file_handle.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
        if written != locked.size or digest.hexdigest() != locked.sha256:
            raise RuntimeError(
                "downloaded build backend failed lockfile size or SHA-256 verification"
            )
        os.replace(temporary_path, cached_path)
    finally:
        temporary_path.unlink(missing_ok=True)

    print(f"cached verified build backend: {cached_path}")
    return cached_path


def main() -> int:
    ensure_locked_build_wheel()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

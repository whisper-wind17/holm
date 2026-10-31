#!/usr/bin/env python3
"""Verify two isolated frozen bootstraps/builds without mutating either lockfile."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Final

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
LOCKFILES: Final[tuple[Path, ...]] = (Path("uv.lock"), Path("package-lock.json"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _repository_files() -> tuple[Path, ...]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return tuple(Path(item.decode()) for item in result.stdout.split(b"\0") if item)


def _copy_workspace(destination: Path, repository_files: tuple[Path, ...]) -> None:
    for relative_path in repository_files:
        source = ROOT / relative_path
        if not source.is_file():
            continue
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _run_command(workspace: Path, command: list[str], label: str) -> None:
    environment = os.environ.copy()
    environment["CI"] = "1"
    result = subprocess.run(
        command,
        cwd=workspace,
        env=environment,
        text=True,
        capture_output=True,
        timeout=600,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"isolated {label} failed "
            f"with exit code {result.returncode}\n"
            f"STDOUT:\n{result.stdout}\n"
            f"STDERR:\n{result.stderr}"
        )


def main() -> int:
    for lockfile in LOCKFILES:
        if not (ROOT / lockfile).is_file():
            raise SystemExit(f"missing required lockfile: {lockfile}")

    source_hashes = {lockfile: _sha256(ROOT / lockfile) for lockfile in LOCKFILES}
    repository_files = _repository_files()

    with tempfile.TemporaryDirectory(prefix="holm-m0-01-") as temporary_root:
        temporary_path = Path(temporary_root)
        for run_number in (1, 2):
            workspace = temporary_path / f"run-{run_number}"
            workspace.mkdir()
            _copy_workspace(workspace, repository_files)
            _run_command(workspace, [str(workspace / "dev"), "bootstrap"], "bootstrap")
            _run_command(
                workspace,
                ["npm", "run", "build", "--workspace", "@holm/web"],
                "web build",
            )

            for lockfile, expected_hash in source_hashes.items():
                actual_hash = _sha256(workspace / lockfile)
                if actual_hash != expected_hash:
                    raise RuntimeError(
                        f"run {run_number} changed {lockfile}: "
                        f"expected {expected_hash}, got {actual_hash}"
                    )
            if not (workspace / ".venv").is_dir():
                raise RuntimeError(f"run {run_number} did not create an isolated .venv")
            if not (workspace / "node_modules").is_dir():
                raise RuntimeError(f"run {run_number} did not create isolated node_modules")
            if not (workspace / "apps" / "web" / "dist" / "workspace.js").is_file():
                raise RuntimeError(f"run {run_number} did not produce the minimal web build")
            print(f"isolated bootstrap/build {run_number}: PASS")

    for lockfile, digest in source_hashes.items():
        print(f"{lockfile} sha256: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

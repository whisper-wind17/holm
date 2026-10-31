"""Exit-code and diagnostic checks for the unified development entrypoint."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEV = PROJECT_ROOT / "dev"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_M0_01_dev_reports_a_missing_declared_prerequisite(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for command_name in ("bash", "dirname"):
        target = shutil.which(command_name)
        assert target is not None
        (fake_bin / command_name).symlink_to(target)
    environment = os.environ.copy()
    environment["PATH"] = str(fake_bin)

    result = subprocess.run(
        [str(DEV), "bootstrap"],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert result.returncode == 127
    assert "required command 'uv' was not found" in result.stderr


def test_M0_01_dev_propagates_a_child_exit_code(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "uv",
        '#!/bin/sh\nif [ "${1:-}" = "--version" ]; then echo "uv 0.11.7"; exit 0; fi\nexit 37\n',
    )
    _write_executable(
        fake_bin / "node",
        '#!/bin/sh\nif [ "${1:-}" = "-e" ]; then exit 0; fi\necho "v24.0.0"\n',
    )
    _write_executable(fake_bin / "npm", '#!/bin/sh\necho "11.9.0"\n')
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"

    result = subprocess.run(
        [str(DEV), "bootstrap"],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert result.returncode == 37
    assert "Sync locked Python workspace failed with exit code 37" in result.stderr


def test_M0_01_dev_rejects_unknown_commands() -> None:
    result = subprocess.run(
        [str(DEV), "not-a-command"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "unknown command 'not-a-command'" in result.stderr

"""ENV-05: strict mypy must cover every uv workspace member."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GUARD = PROJECT_ROOT / "scripts" / "check_mypy_workspace_coverage.py"
DEV = PROJECT_ROOT / "dev"


def test_ENV_05_dev_typecheck_invokes_workspace_coverage_guard() -> None:
    dev_source = DEV.read_text(encoding="utf-8")
    guard_call = "python scripts/check_mypy_workspace_coverage.py"

    assert dev_source.count(guard_call) == 1
    assert dev_source.index(guard_call) < dev_source.index('run_step "Typecheck Python sources"')


def test_ENV_05_mypy_workspace_guard_fails_loudly_for_missing_member(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text(
        """
[tool.uv.workspace]
members = [
  "apps/covered",
  "apps/fake-member",
]
""".strip(),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(GUARD),
            "--manifest",
            str(manifest),
            "apps/covered/src",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "strict mypy is missing workspace member source target(s)" in result.stderr
    assert "apps/fake-member/src" in result.stderr

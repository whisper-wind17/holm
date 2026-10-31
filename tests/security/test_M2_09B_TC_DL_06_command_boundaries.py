"""TC-DL-06: HTTP commands cannot kill processes or delete staging files."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMMAND_SOURCES = (
    ROOT / "apps/control_api/src/holm_control_api/download_command_service.py",
    ROOT / "apps/control_api/src/holm_control_api/download_command_routes.py",
)
DISALLOWED_MODULES = frozenset({"multiprocessing", "os", "signal", "shutil"})
DISALLOWED_CALLS = frozenset({"kill", "remove", "rmtree", "terminate", "unlink"})


def _assert_command_boundary(source: str) -> None:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name.split(".")[0] not in DISALLOWED_MODULES for alias in node.names)
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            assert node.module.split(".")[0] not in DISALLOWED_MODULES
            assert "holm_download_worker" not in node.module
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in DISALLOWED_CALLS


def test_TC_DL_06_command_surface_only_records_intent() -> None:
    for source in COMMAND_SOURCES:
        _assert_command_boundary(source.read_text(encoding="utf-8"))


def test_TC_DL_06_positive_control_http_process_kill_fails_loudly() -> None:
    text = COMMAND_SOURCES[0].read_text(encoding="utf-8")
    mutant = text + "\nimport os\nos.kill(1, 9)\n"
    with pytest.raises(AssertionError):
        _assert_command_boundary(mutant)
    print("TC-DL-06 positive control 7: HTTP process-kill mutant rejected")


def test_TC_DL_06_positive_control_staging_delete_fails_loudly() -> None:
    text = COMMAND_SOURCES[0].read_text(encoding="utf-8")
    mutant = text + "\nimport shutil\nshutil.rmtree('opaque')\n"
    with pytest.raises(AssertionError):
        _assert_command_boundary(mutant)

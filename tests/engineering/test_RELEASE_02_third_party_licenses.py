"""RELEASE-02 fixtures FX-RELEASE-02: synthetic metadata, never real dependencies."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/third_party_licenses.py"
spec = importlib.util.spec_from_file_location("release02_licenses", SCRIPT)
assert spec and spec.loader
inventory = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = inventory
spec.loader.exec_module(inventory)


def test_RELEASE_02_inventory_is_current_and_stable() -> None:
    first = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    assert first.stdout == (ROOT / "THIRD_PARTY_LICENSES.md").read_text(), (
        "uv run --frozen python scripts/third_party_licenses.py > THIRD_PARTY_LICENSES.md"
    )
    second = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
    assert second.returncode == 0, second.stderr
    assert first.stdout == second.stdout
    assert "| pytest |" not in first.stdout
    assert "| vitest |" not in first.stdout
    assert str(ROOT) not in first.stdout


@pytest.mark.parametrize("scenario", ["missing", "gpl", "stale", "empty"])
def test_RELEASE_02_guard_positive_controls_exit_nonzero(scenario: str) -> None:
    # Execute the real CLI entry point in a child, replacing only collection inputs.
    fixture = f"""
import runpy
m = runpy.run_path({str(SCRIPT)!r})
g = m['main'].__globals__
C = g['Component']
scenario = {scenario!r}
rows = [] if scenario == 'empty' else [C('Python', 'FX-RELEASE-02', '1',
    '' if scenario == 'missing' else 'GPL-3.0-only' if scenario == 'gpl' else 'MIT',
    'License-Expression')]
g['python_components'] = lambda root: rows
g['frontend_components'] = lambda root: []
g['asset_components'] = lambda root: []
if scenario == 'stale':
    g['OVERRIDES'] = {{('Python', 'FX-RELEASE-02-stale', '1'): ('MIT', 'fixture')}}
raise SystemExit(m['main'](['--check']))
"""
    result = subprocess.run([sys.executable, "-c", fixture], capture_output=True, text=True)
    assert result.returncode == 1
    expected = "empty" if scenario == "empty" else "FX-RELEASE-02"
    assert expected in result.stderr
    assert result.stdout == ""


def test_RELEASE_02_missing_asset_license_fails(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Inter: missing or empty asset license"):
        inventory.asset_components(tmp_path)


@pytest.mark.parametrize(
    ("expression", "grade"),
    [
        ("MIT", "宽松"),
        ("", "不兼容"),
        ("UNKNOWN", "识别不出"),
        ("(MIT OR MPL-2.0)", "需人工审"),
        ("MIT AND GPL-3.0-only", "不兼容"),
        ("MIT OR Unrecognized", "识别不出"),
        ("MIT OR", "识别不出"),
    ],
)
def test_RELEASE_02_expression_uses_strictest_term(expression: str, grade: str) -> None:
    assert inventory.classify(expression) == grade


def test_RELEASE_02_check_rejects_stale_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(inventory, "ROOT", tmp_path)
    monkeypatch.setattr(inventory, "OVERRIDES", {})
    monkeypatch.setattr(
        inventory,
        "python_components",
        lambda root: [inventory.Component("Python", "FX-RELEASE-02", "1", "MIT", "License")],
    )
    monkeypatch.setattr(inventory, "frontend_components", lambda root: [])
    monkeypatch.setattr(inventory, "asset_components", lambda root: [])
    (tmp_path / "THIRD_PARTY_LICENSES.md").write_text("outdated fixture")
    assert inventory.main(["--check"]) == 1
    assert (
        "regenerate: uv run --frozen python scripts/third_party_licenses.py"
        in capsys.readouterr().err
    )


def test_RELEASE_02_locked_wheel_hash_mismatch_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io

    def absent(name: str) -> None:
        raise inventory.importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(inventory.importlib.metadata, "distribution", absent)
    monkeypatch.setattr(inventory.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(inventory.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(b"bad"))
    (tmp_path / "uv.lock").write_text("""
[[package]]
name = "FX-RELEASE-02"
version = "1"
wheels = [{url = "https://example.invalid/fixture.whl", hash = "sha256:bad", size = 3}]
""")
    with pytest.raises(ValueError, match="FX-RELEASE-02: locked wheel hash/size mismatch"):
        inventory.python_metadata(tmp_path, "FX-RELEASE-02", "1")


def test_RELEASE_02_each_runtime_collection_rejects_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    monkeypatch.setattr(
        inventory.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 0, "")
    )
    with pytest.raises(ValueError, match="Python runtime dependency set is empty"):
        inventory.python_components(tmp_path)
    (tmp_path / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": {}}))
    with pytest.raises(ValueError, match="Frontend runtime dependency set is empty"):
        inventory.frontend_components(tmp_path)

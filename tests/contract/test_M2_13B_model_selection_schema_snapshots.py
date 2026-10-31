"""M2-13B generated model-selection contract snapshots."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_schema_generator() -> ModuleType:
    script_path = PROJECT_ROOT / "scripts" / "generate_m2_13b_model_selection_schemas.py"
    spec = importlib.util.spec_from_file_location("m2_13b_schema_generator", script_path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load M2-13B schema generator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_GENERATOR = _load_schema_generator()
MODELS = _GENERATOR.MODELS
SNAPSHOT_ROOT = _GENERATOR.SNAPSHOT_ROOT
check_snapshots = _GENERATOR.check_snapshots


def _copy_snapshots(target: Path) -> None:
    for snapshot in SNAPSHOT_ROOT.glob("m2_13b_*.json"):
        (target / snapshot.name).write_bytes(snapshot.read_bytes())


def test_M2_13B_ruling_9_schema_snapshots_match_python_contracts() -> None:
    check_snapshots()


def test_M2_13B_ruling_9_positive_control_snapshot_extra_field_fails(
    tmp_path: Path,
) -> None:
    _copy_snapshots(tmp_path)
    target = tmp_path / "m2_13b_download_created_response.json"
    content = target.read_text(encoding="utf-8")
    target.write_text(
        content.replace('"disposition": {', '"future_field": {},\n    "disposition": {'),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="download_created"):
        check_snapshots(tmp_path)


def test_M2_13B_ruling_9_positive_control_python_field_drift_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _copy_snapshots(tmp_path)
    name = "m2_13b_resolve_request.json"
    original = MODELS[name]

    class ChangedResolveRequest(original):
        future_field: str

    monkeypatch.setitem(MODELS, name, ChangedResolveRequest)
    with pytest.raises(RuntimeError, match="resolve_request"):
        check_snapshots(tmp_path)

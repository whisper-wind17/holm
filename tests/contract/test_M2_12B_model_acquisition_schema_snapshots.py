"""M2-12B generated model-acquisition response contract snapshots."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from holm_control_api.model_acquisition_read_routes import DownloadTaskItemResponse

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_schema_generator() -> ModuleType:
    script_path = PROJECT_ROOT / "scripts" / "generate_m2_12b_model_acquisition_schemas.py"
    spec = importlib.util.spec_from_file_location(
        "m2_12b_generate_model_acquisition_schemas", script_path
    )
    if spec is None or spec.loader is None:
        raise AssertionError("could not load scripts/generate_m2_12b_model_acquisition_schemas.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SCHEMA_GENERATOR = _load_schema_generator()
MODELS = _SCHEMA_GENERATOR.MODELS
SNAPSHOT_ROOT = _SCHEMA_GENERATOR.SNAPSHOT_ROOT
check_snapshots = _SCHEMA_GENERATOR.check_snapshots


def test_M2_12B_ruling_6_schema_snapshots_match_python_models() -> None:
    check_snapshots()


def test_M2_12B_ruling_6_schema_snapshot_drift_fails_loudly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for snapshot in SNAPSHOT_ROOT.glob("m2_12b_*.json"):
        (tmp_path / snapshot.name).write_bytes(snapshot.read_bytes())
    target = tmp_path / "m2_12b_download_tasks.json"
    target.write_text(target.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(RuntimeError, match="download_tasks"):
        check_snapshots(tmp_path)

    target.write_bytes((SNAPSHOT_ROOT / target.name).read_bytes())

    class ChangedDownloadTaskItemResponse(DownloadTaskItemResponse):
        future_field: str

    monkeypatch.setitem(
        MODELS,
        target.name,
        _SCHEMA_GENERATOR.CursorPage[ChangedDownloadTaskItemResponse],
    )
    with pytest.raises(RuntimeError, match="download_tasks"):
        check_snapshots(tmp_path)

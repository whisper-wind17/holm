"""M2-13A generated model-source contract snapshots."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_schema_generator() -> ModuleType:
    script_path = PROJECT_ROOT / "scripts" / "generate_m2_13a_model_source_schemas.py"
    spec = importlib.util.spec_from_file_location(
        "m2_13a_generate_model_source_schemas", script_path
    )
    if spec is None or spec.loader is None:
        raise AssertionError("could not load scripts/generate_m2_13a_model_source_schemas.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SCHEMA_GENERATOR = _load_schema_generator()
MODELS = _SCHEMA_GENERATOR.MODELS
ENUMS = _SCHEMA_GENERATOR.ENUMS
ENUM_SNAPSHOT = _SCHEMA_GENERATOR.ENUM_SNAPSHOT
SNAPSHOT_ROOT = _SCHEMA_GENERATOR.SNAPSHOT_ROOT
check_snapshots = _SCHEMA_GENERATOR.check_snapshots


def _copy_snapshots(target: Path) -> None:
    for snapshot in SNAPSHOT_ROOT.glob("m2_13a_*.json"):
        (target / snapshot.name).write_bytes(snapshot.read_bytes())


def test_M2_13A_ruling_7_model_source_snapshots_match_python_contracts() -> None:
    check_snapshots()


def test_M2_13A_ruling_7_snapshot_extra_field_fails_loudly(tmp_path: Path) -> None:
    _copy_snapshots(tmp_path)
    target = tmp_path / "m2_13a_model_source_credential_request.json"
    snapshot = target.read_text(encoding="utf-8")
    target.write_text(
        snapshot.replace('"clear": {', '"future_field": {},\n    "clear": {'), encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="credential_request"):
        check_snapshots(tmp_path)


def test_M2_13A_ruling_7_python_model_field_drift_fails_loudly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _copy_snapshots(tmp_path)
    original = MODELS["m2_13a_model_source_credential_request.json"]

    class ChangedCredentialRequest(original):
        future_field: str

    monkeypatch.setitem(
        MODELS, "m2_13a_model_source_credential_request.json", ChangedCredentialRequest
    )
    with pytest.raises(RuntimeError, match="credential_request"):
        check_snapshots(tmp_path)


def test_M2_13A_ruling_7_enum_member_drift_fails_loudly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _copy_snapshots(tmp_path)

    class FutureSource(str, __import__("enum").Enum):
        HUGGING_FACE = "Hugging Face"
        MODELSCOPE = "ModelScope"
        FUTURE = "FutureSource"

    monkeypatch.setitem(ENUMS, "ModelSourceType", FutureSource)
    with pytest.raises(RuntimeError, match=ENUM_SNAPSHOT):
        check_snapshots(tmp_path)

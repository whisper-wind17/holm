"""M7-07: only one-shot migrate can see its DDL-owner credential."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


def _validator() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "verify_production_compose.py"
    spec = importlib.util.spec_from_file_location("m7_07_compose_validator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_M7_07_TC_DP_13_only_migrate_mounts_ddl_credential() -> None:
    validator = _validator()
    original = validator.COMPOSE_PATH.read_text(encoding="utf-8")
    validator.validate_compose_text(original)
    payload = json.loads(
        "\n".join(line for line in original.splitlines() if not line.lstrip().startswith("#"))
    )
    payload["services"]["gateway"]["secrets"].append("migrate_database_url")
    with pytest.raises(validator.ProductionComposeValidationError, match="only migrate"):
        validator.validate_compose_text(json.dumps(payload))

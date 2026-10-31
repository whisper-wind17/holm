"""Negative and positive checks for the M0-01 dependency guard."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GUARD = PROJECT_ROOT / "scripts" / "check_dependencies.py"


def _write(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _run_guard(workspace: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GUARD), "--root", str(workspace)],
        check=False,
        capture_output=True,
        text=True,
    )


def _minimal_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    _write(workspace / "apps" / "control_api" / "src" / "holm_control_api" / "__init__.py")
    _write(workspace / "packages" / "domain" / "src" / "holm_domain" / "__init__.py")
    return workspace


@pytest.mark.parametrize(
    "forbidden_import",
    [
        "fastapi",
        "sqlalchemy",
        "docker",
        "pynvml",
        "vllm",
        "sglang",
        "cryptography",
        "huggingface_hub",
        "hf_xet",
        "modelscope",
        "modelscope_hub",
    ],
)
def test_M0_01_dependency_guard_rejects_domain_external_imports(
    tmp_path: Path, forbidden_import: str
) -> None:
    workspace = _minimal_workspace(tmp_path)
    violation = workspace / "packages" / "domain" / "src" / "holm_domain" / "violation.py"
    _write(violation, f"import {forbidden_import}\n")

    result = _run_guard(workspace)

    assert result.returncode == 1
    assert "packages/domain/src/holm_domain/violation.py:1" in result.stderr
    assert "DEP-DOMAIN-EXTERNAL" in result.stderr
    assert f"forbidden import '{forbidden_import}'" in result.stderr


def test_M0_01_dependency_guard_rejects_python_package_to_app(tmp_path: Path) -> None:
    workspace = _minimal_workspace(tmp_path)
    violation = workspace / "packages" / "domain" / "src" / "holm_domain" / "violation.py"
    _write(violation, "from holm_control_api import PACKAGE_NAME\n")

    result = _run_guard(workspace)

    assert result.returncode == 1
    assert "DEP-PACKAGE-TO-APP" in result.stderr
    assert "holm_control_api" in result.stderr


def test_M5_01_dependency_guard_rejects_domain_importing_ledger(tmp_path: Path) -> None:
    workspace = _minimal_workspace(tmp_path)
    ledger_module = workspace / "packages" / "ledger" / "src" / "holm_ledger" / "__init__.py"
    _write(ledger_module)
    violation = workspace / "packages" / "domain" / "src" / "holm_domain" / "violation.py"
    _write(violation, "from holm_ledger import open_ledger\n")

    result = _run_guard(workspace)

    assert result.returncode == 1
    assert "DEP-DOMAIN-ADAPTER" in result.stderr
    assert "holm_ledger" in result.stderr


def test_M0_01_dependency_guard_rejects_typescript_package_to_app(tmp_path: Path) -> None:
    workspace = _minimal_workspace(tmp_path)
    manifest = workspace / "apps" / "web" / "package.json"
    _write(manifest, json.dumps({"name": "@holm/web"}))
    violation = workspace / "packages" / "domain" / "src" / "violation.ts"
    _write(violation, 'import { value } from "@holm/web";\nvoid value;\n')

    result = _run_guard(workspace)

    assert result.returncode == 1
    assert "packages/domain/src/violation.ts:1" in result.stderr
    assert "DEP-PACKAGE-TO-APP" in result.stderr


def test_M0_01_dependency_guard_allows_app_to_package(tmp_path: Path) -> None:
    workspace = _minimal_workspace(tmp_path)
    domain_module = workspace / "packages" / "domain" / "src" / "holm_domain" / "identity.py"
    app_module = workspace / "apps" / "control_api" / "src" / "holm_control_api" / "identity.py"
    _write(domain_module, "from dataclasses import dataclass\n")
    _write(app_module, "from holm_domain import PACKAGE_NAME\n")

    result = _run_guard(workspace)

    assert result.returncode == 0, result.stderr
    assert "dependency guard passed" in result.stdout


def test_M4_05_dependency_guard_rejects_core_importing_openai_adapter(
    tmp_path: Path,
) -> None:
    """Positive control: the new D4 guard must loudly reject its target failure."""

    workspace = _minimal_workspace(tmp_path)
    _write(workspace / "apps" / "gateway" / "src" / "holm_gateway" / "openai_adapter.py")
    violation = workspace / "packages" / "protocols" / "src" / "holm_protocols" / "core.py"
    _write(violation, "from holm_gateway.openai_adapter import OpenAIProtocolAdapter\n")

    result = _run_guard(workspace)

    assert result.returncode == 1
    assert "packages/protocols/src/holm_protocols/core.py:1" in result.stderr
    assert "DEP-INFERENCE-CORE-OPENAI" in result.stderr
    assert "holm_gateway.openai_adapter" in result.stderr

"""Task-local checks for the minimal Python workspace."""

import json
import re
import tomllib
from pathlib import Path

from holm_control_api import DOMAIN_PACKAGE_NAME, PACKAGE_NAME
from holm_domain import PACKAGE_NAME as DOMAIN_IMPORT_NAME
from holm_download_worker import PACKAGE_NAME as DOWNLOAD_WORKER_PACKAGE_NAME
from holm_gateway import PACKAGE_NAME as GATEWAY_PACKAGE_NAME
from holm_instance_controller import PACKAGE_NAME as INSTANCE_CONTROLLER_PACKAGE_NAME
from holm_ledger_writer import PACKAGE_NAME as LEDGER_WRITER_PACKAGE_NAME
from holm_maintenance_worker import PACKAGE_NAME as MAINTENANCE_WORKER_PACKAGE_NAME
from holm_telemetry import PACKAGE_NAME as TELEMETRY_PACKAGE_NAME

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXACT_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def test_M0_01_python_workspace_imports_app_and_domain() -> None:
    assert PACKAGE_NAME == "holm-control-api"
    assert DOWNLOAD_WORKER_PACKAGE_NAME == "holm-download-worker"
    assert GATEWAY_PACKAGE_NAME == "holm-gateway"
    assert INSTANCE_CONTROLLER_PACKAGE_NAME == "holm-instance-controller"
    assert LEDGER_WRITER_PACKAGE_NAME == "holm-ledger-writer"
    assert MAINTENANCE_WORKER_PACKAGE_NAME == "holm-maintenance-worker"
    assert TELEMETRY_PACKAGE_NAME == "holm-telemetry"
    assert DOMAIN_PACKAGE_NAME == "holm-domain"
    assert DOMAIN_IMPORT_NAME == "holm-domain"


def test_M0_01_manifests_pin_direct_dependencies_and_have_lockfiles() -> None:
    python_manifests = (
        PROJECT_ROOT / "pyproject.toml",
        PROJECT_ROOT / "apps" / "control_api" / "pyproject.toml",
        PROJECT_ROOT / "apps" / "gateway" / "pyproject.toml",
        PROJECT_ROOT / "apps" / "workers" / "download" / "pyproject.toml",
        PROJECT_ROOT / "apps" / "workers" / "instance" / "pyproject.toml",
        PROJECT_ROOT / "apps" / "workers" / "ledger" / "pyproject.toml",
        PROJECT_ROOT / "apps" / "workers" / "maintenance" / "pyproject.toml",
        PROJECT_ROOT / "packages" / "domain" / "pyproject.toml",
    )
    for manifest in python_manifests:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
        requirements = [*data.get("project", {}).get("dependencies", [])]
        for group in data.get("dependency-groups", {}).values():
            requirements.extend(group)
        requirements.extend(data.get("build-system", {}).get("requires", []))
        assert all("==" in requirement for requirement in requirements), manifest
        assert all("latest" not in requirement.lower() for requirement in requirements), manifest

    for manifest in (PROJECT_ROOT / "package.json", PROJECT_ROOT / "apps/web/package.json"):
        data = json.loads(manifest.read_text(encoding="utf-8"))
        direct_versions = {
            **data.get("dependencies", {}),
            **data.get("devDependencies", {}),
        }
        assert all(EXACT_SEMVER.fullmatch(version) for version in direct_versions.values()), (
            manifest
        )
        assert all(version != "latest" for version in direct_versions.values()), manifest

    root_package = json.loads((PROJECT_ROOT / "package.json").read_text(encoding="utf-8"))
    assert re.fullmatch(r"npm@[0-9]+\.[0-9]+\.[0-9]+", root_package["packageManager"])
    assert (PROJECT_ROOT / "uv.lock").is_file()
    assert (PROJECT_ROOT / "package-lock.json").is_file()

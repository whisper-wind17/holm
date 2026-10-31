"""Supply-chain and repository hygiene checks for M0-02."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PYTHON_MANIFESTS = (
    Path("pyproject.toml"),
    Path("apps/control_api/pyproject.toml"),
    Path("apps/gateway/pyproject.toml"),
    Path("apps/workers/download/pyproject.toml"),
    Path("apps/workers/instance/pyproject.toml"),
    Path("apps/workers/ledger/pyproject.toml"),
    Path("packages/common/pyproject.toml"),
    Path("packages/contracts/pyproject.toml"),
    Path("packages/domain/pyproject.toml"),
    Path("packages/persistence/pyproject.toml"),
    Path("packages/runtimes/pyproject.toml"),
    Path("packages/security/pyproject.toml"),
)
NODE_MANIFESTS = (Path("package.json"), Path("apps/web/package.json"))
MAX_TRACKED_FILE_BYTES = 1024 * 1024
EXACT_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
EXACT_PYTHON_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
INTEGRITY = re.compile(r"^sha(?:256|384|512)-[A-Za-z0-9+/]+={0,2}$")
SECRET_PATTERNS = (
    (
        "private-key",
        re.compile(b"-----BEGIN " + b"(?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
    ("aws-access-key", re.compile(b"AKIA" + b"[0-9A-Z]{16}")),
    ("github-token", re.compile(b"gh" + b"[pousr]_[A-Za-z0-9]{36,}")),
    ("openai-token", re.compile(b"sk-" + b"(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("slack-token", re.compile(b"xox" + b"[aboprs]-[A-Za-z0-9-]{20,}")),
)


def _requirements(manifest: Path) -> list[str]:
    data = tomllib.loads((PROJECT_ROOT / manifest).read_text(encoding="utf-8"))
    requirements = [*data.get("project", {}).get("dependencies", [])]
    for group in data.get("dependency-groups", {}).values():
        requirements.extend(group)
    requirements.extend(data.get("build-system", {}).get("requires", []))
    return requirements


def _repository_files() -> tuple[Path, ...]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
    )
    return tuple(Path(item.decode()) for item in result.stdout.split(b"\0") if item)


def _assert_exact_python_requirement(manifest: Path, requirement: str) -> None:
    assert "latest" not in requirement.lower(), (manifest, requirement)
    assert " @ " not in requirement, (manifest, requirement)
    assert "==" in requirement, (manifest, requirement)
    name, version = requirement.split("==", maxsplit=1)
    assert name and EXACT_PYTHON_VERSION.fullmatch(version), (manifest, requirement)


@pytest.mark.parametrize("version", ["0.1.0", "0.1.0a1", "0.1.0b1", "0.1.0rc1"])
def test_RELEASE_07_M0_02_exact_python_versions_are_accepted(version: str) -> None:
    # FX-RELEASE-07-python-pin: release-card version policy examples.
    _assert_exact_python_requirement(Path("pyproject.toml"), f"holm-x=={version}")


@pytest.mark.parametrize(
    "requirement",
    [
        "holm-x==0.1.*",
        "holm-x>=0.1.0",
        "holm-x==0.1.0+local",
        "holm-x==latest",
        "holm-x==0.1.0.dev1",
    ],
)
def test_RELEASE_07_M0_02_invalid_python_pins_are_rejected(requirement: str) -> None:
    # FX-RELEASE-07-python-pin: release-card forbidden version forms.
    with pytest.raises(AssertionError):
        _assert_exact_python_requirement(Path("pyproject.toml"), requirement)


def test_M0_02_direct_dependencies_and_package_manager_are_exactly_pinned() -> None:
    for manifest in PYTHON_MANIFESTS:
        requirements = _requirements(manifest)
        for requirement in requirements:
            _assert_exact_python_requirement(manifest, requirement)

    for manifest in NODE_MANIFESTS:
        data = json.loads((PROJECT_ROOT / manifest).read_text(encoding="utf-8"))
        direct_versions = {
            **data.get("dependencies", {}),
            **data.get("devDependencies", {}),
            **data.get("optionalDependencies", {}),
        }
        for name, version in direct_versions.items():
            assert version != "latest", (manifest, name)
            assert EXACT_SEMVER.fullmatch(version), (manifest, name, version)

    root_package = json.loads((PROJECT_ROOT / "package.json").read_text(encoding="utf-8"))
    assert re.fullmatch(r"npm@[0-9]+\.[0-9]+\.[0-9]+", root_package["packageManager"])


def test_M0_02_python_lock_registry_entries_have_versions_and_hashes(tmp_path: Path) -> None:
    lock = tomllib.loads((PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8"))
    assert lock["requires-python"] == "==3.12.*"
    assert lock["package"]

    registry_packages = [
        package for package in lock["package"] if "registry" in package.get("source", {})
    ]
    assert registry_packages
    for package in registry_packages:
        assert package.get("version") and package["version"] != "latest", package["name"]
        artifacts = [*package.get("wheels", [])]
        if package.get("sdist"):
            artifacts.append(package["sdist"])
        assert artifacts, package["name"]
        assert all(SHA256.fullmatch(artifact["hash"]) for artifact in artifacts), package["name"]

    registry_by_name = {package["name"].replace("_", "-"): package for package in registry_packages}
    for manifest in PYTHON_MANIFESTS:
        data = tomllib.loads((PROJECT_ROOT / manifest).read_text(encoding="utf-8"))
        for requirement in data.get("build-system", {}).get("requires", []):
            name, version = requirement.split("==", maxsplit=1)
            package = registry_by_name[name.replace("_", "-")]
            assert package["version"] == version, (manifest, requirement)
            assert package.get("sdist") or package.get("wheels"), (manifest, requirement)

    environment = os.environ.copy()
    environment.update(
        {
            "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
            "UV_KEYRING_PROVIDER": "disabled",
            "UV_OFFLINE": "1",
            "UV_PYTHON_DOWNLOADS": "never",
        }
    )
    result = subprocess.run(
        ["uv", "lock", "--check", "--offline"],
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_M0_02_node_lock_is_complete_and_matches_workspace_manifests() -> None:
    lock = json.loads((PROJECT_ROOT / "package-lock.json").read_text(encoding="utf-8"))
    assert lock["lockfileVersion"] == 3
    assert lock["packages"]

    root_manifest = json.loads((PROJECT_ROOT / "package.json").read_text(encoding="utf-8"))
    locked_root = lock["packages"][""]
    assert locked_root["name"] == root_manifest["name"]
    assert locked_root["version"] == root_manifest["version"]
    assert locked_root["workspaces"] == root_manifest["workspaces"]
    workspace_paths = set(root_manifest["workspaces"])
    for workspace_path in workspace_paths:
        manifest = json.loads(
            (PROJECT_ROOT / workspace_path / "package.json").read_text(encoding="utf-8")
        )
        locked_workspace = lock["packages"][workspace_path]
        assert locked_workspace["name"] == manifest["name"]
        assert locked_workspace["version"] == manifest["version"]
        for field in ("dependencies", "devDependencies", "optionalDependencies"):
            assert locked_workspace.get(field, {}) == manifest.get(field, {})

    for package_path, package in lock["packages"].items():
        if not package_path or package_path in workspace_paths:
            continue
        if package.get("link"):
            assert package["resolved"] in workspace_paths
            continue
        assert EXACT_SEMVER.fullmatch(package["version"]), package_path
        assert package["resolved"].startswith("https://registry.npmjs.org/"), package_path
        assert INTEGRITY.fullmatch(package["integrity"]), package_path


def test_M0_02_repository_has_no_large_files_symlinks_or_high_confidence_secrets() -> None:
    violations: list[str] = []
    for relative_path in _repository_files():
        path = PROJECT_ROOT / relative_path
        if not path.exists():
            continue
        if path.is_symlink():
            violations.append(f"symlink:{relative_path.as_posix()}")
            continue
        if not path.is_file():
            continue
        if path.stat().st_size >= MAX_TRACKED_FILE_BYTES:
            violations.append(f"large-file:{relative_path.as_posix()}")
            continue
        content = path.read_bytes()
        for pattern_name, pattern in SECRET_PATTERNS:
            if pattern.search(content):
                violations.append(f"{pattern_name}:{relative_path.as_posix()}")
    assert not violations, violations


@pytest.mark.parametrize(
    "ignored_path",
    [
        ".venv/probe",
        ".uv-cache/probe",
        "node_modules/probe",
        "apps/web/dist/probe",
        "build/m0-02/probe",
        "dist/probe",
        ".env",
        "sample.pem",
        ".claude/settings.local.json",
        ".impeccable/probe",
        "sample.safetensors",
        "sample.gguf",
        "sample.onnx",
    ],
)
def test_M0_02_generated_secret_and_large_artifact_paths_are_ignored(
    ignored_path: str,
) -> None:
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet", ignored_path],
        cwd=PROJECT_ROOT,
        check=False,
    )
    assert result.returncode == 0, ignored_path


def test_M0_02_dependency_direction_guard_still_passes() -> None:
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "check_dependencies.py")],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "apps -> packages" in result.stdout

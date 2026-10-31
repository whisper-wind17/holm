"""Installed-wheel first-party dependency closure gate for M1-01D."""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import tomllib
import zipfile
from email.message import Message
from email.parser import BytesParser
from email.policy import default
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BUILD_ROOT = PROJECT_ROOT / "build" / "m0-02"
REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
CANONICAL_SEPARATORS = re.compile(r"[-_.]+")


def _canonical_project_name(name: str) -> str:
    return CANONICAL_SEPARATORS.sub("-", name).lower()


def _build_release_artifacts() -> None:
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "build_artifacts.py")],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"


def _buildable_workspace_members() -> tuple[Path, ...]:
    root_manifest = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    members = root_manifest["tool"]["uv"]["workspace"]["members"]
    buildable: list[Path] = []
    for member in members:
        member_path = PROJECT_ROOT / member
        manifest = tomllib.loads((member_path / "pyproject.toml").read_text(encoding="utf-8"))
        if "build-system" in manifest:
            buildable.append(member_path)
    return tuple(buildable)


def _first_party_imports(source_root: Path) -> set[str]:
    imports: set[str] = set()
    for source_path in sorted(source_root.rglob("*.py")):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = (alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = (node.module,)
            else:
                continue
            for module in modules:
                top_level = module.partition(".")[0]
                if top_level.startswith("holm_"):
                    imports.add(_canonical_project_name(top_level))
    return imports


def _wheel_metadata(wheel_path: Path) -> Message:
    with zipfile.ZipFile(wheel_path) as wheel:
        metadata_paths = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
        assert len(metadata_paths) == 1, wheel_path
        return BytesParser(policy=default).parsebytes(wheel.read(metadata_paths[0]))


def _wheel_metadata_by_project() -> dict[str, tuple[Path, Message]]:
    wheels: dict[str, tuple[Path, Message]] = {}
    for wheel_path in sorted((BUILD_ROOT / "python").glob("*.whl")):
        metadata = _wheel_metadata(wheel_path)
        project_name = metadata.get("Name")
        assert isinstance(project_name, str), wheel_path
        canonical_name = _canonical_project_name(project_name)
        assert canonical_name not in wheels, canonical_name
        wheels[canonical_name] = (wheel_path, metadata)
    return wheels


def _required_projects(metadata: Message) -> set[str]:
    requirements: set[str] = set()
    for requirement in metadata.get_all("Requires-Dist", []):
        match = REQUIREMENT_NAME.match(requirement)
        assert match is not None, requirement
        requirements.add(_canonical_project_name(match.group(1)))
    return requirements


def test_M1_01D_wheel_metadata_closes_first_party_source_imports() -> None:
    _build_release_artifacts()
    wheels = _wheel_metadata_by_project()
    checked_projects: set[str] = set()

    for member_path in _buildable_workspace_members():
        manifest = tomllib.loads((member_path / "pyproject.toml").read_text(encoding="utf-8"))
        project_name = _canonical_project_name(manifest["project"]["name"])
        wheel_path, metadata = wheels[project_name]
        imported_projects = _first_party_imports(member_path / "src")
        declared_projects = _required_projects(metadata) | {project_name}
        undeclared_projects = imported_projects - declared_projects
        assert not undeclared_projects, (
            f"{wheel_path.name} imports undeclared first-party projects: "
            f"{sorted(undeclared_projects)}"
        )
        checked_projects.add(project_name)

    assert checked_projects == set(wheels)

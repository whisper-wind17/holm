"""RELEASE-07: release versions agree across package and wheel consumers."""

from __future__ import annotations

import json
import re
import shutil
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = Path("deploy/images/application.Dockerfile")
NODE_MANIFESTS = (Path("package.json"), Path("apps/web/package.json"))


def _members(root: Path) -> list[Path]:
    manifest = tomllib.loads((root / "pyproject.toml").read_text())
    return [
        Path(member) / "pyproject.toml" for member in manifest["tool"]["uv"]["workspace"]["members"]
    ]


def _assert_versions(root: Path) -> None:
    manifest = tomllib.loads((root / "pyproject.toml").read_text())
    version = manifest["project"]["version"]
    assert version == "0.1.0rc1", "root version"
    members = _members(root)
    assert len(members) == 16
    projects = [tomllib.loads((root / path).read_text())["project"] for path in members]
    names = {project["name"] for project in projects}
    assert len(names) == 16
    for project in projects:
        assert project["version"] == version, f"member version: {project['name']}"
        for dependency in project.get("dependencies", []):
            if dependency.startswith("holm-"):
                assert dependency in {f"{name}=={version}" for name in names}, "internal pin"
    for path in NODE_MANIFESTS:
        assert json.loads((root / path).read_text())["version"] == "0.1.0-rc.1", str(path)
    wheels = re.findall(
        r"^COPY build/m0-02/python/(\S+\.whl) /wheelhouse/$",
        (root / DOCKERFILE).read_text(),
        re.MULTILINE,
    )
    assert len(wheels) == 16, "wheel count"
    assert set(wheels) == {
        f"{name.replace('-', '_')}-{version}-py3-none-any.whl" for name in names
    }, "wheel versions"


def test_RELEASE_07_versions_are_consistent() -> None:
    _assert_versions(ROOT)


@pytest.mark.parametrize("fault", ["root", "member", "pin", "node_root", "node_web", "wheel"])
def test_RELEASE_07_guard_rejects_version_drift(tmp_path: Path, fault: str) -> None:
    """FX-RELEASE-07: repository manifests copied with one deliberately stale version."""
    for path in [Path("pyproject.toml"), *_members(ROOT), *NODE_MANIFESTS, DOCKERFILE]:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / path, target)
    path, old, new, message = {
        "root": ("pyproject.toml", 'version = "0.1.0rc1"', 'version = "0.0.0"', "root version"),
        "member": (
            "packages/common/pyproject.toml",
            'version = "0.1.0rc1"',
            'version = "0.0.0"',
            "member version",
        ),
        "pin": (
            "apps/host_agent/pyproject.toml",
            "holm-contracts==0.1.0rc1",
            "holm-contracts==0.0.0",
            "internal pin",
        ),
        "node_root": ("package.json", "0.1.0-rc.1", "0.0.0", "package.json"),
        "node_web": ("apps/web/package.json", "0.1.0-rc.1", "0.0.0", "apps/web/package.json"),
        "wheel": (str(DOCKERFILE), "-0.1.0rc1-", "-0.0.0-", "wheel versions"),
    }[fault]
    target = tmp_path / path
    original = target.read_text()
    assert old in original
    target.write_text(original.replace(old, new, 1))
    with pytest.raises(AssertionError, match=message):
        _assert_versions(tmp_path)

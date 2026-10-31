"""RELEASE-04: pinned builder inputs and positive controls for release image guards."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "release04_compose", ROOT / "scripts/verify_production_compose.py"
)
assert spec and spec.loader
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


@pytest.mark.parametrize("name", ["application", "edge", "builder"])
@pytest.mark.parametrize("mutation", ["digest", "prefix", "arg"])
def test_RELEASE_04_base_guard_rejects_unpinned_or_unconfigurable_image(
    name: str, mutation: str
) -> None:
    original = (ROOT / f"deploy/images/{name}.Dockerfile").read_text()
    validator._dockerfile_base_images(original, name)
    if mutation == "digest":
        changed = re.sub(r"@sha256:[0-9a-f]{64}", "", original)
    elif mutation == "prefix":
        changed = original.replace("${HOLM_BASE_REGISTRY}", "fixed.example")
    else:
        changed = original.replace("ARG HOLM_BASE_REGISTRY=docker.io", "# missing argument")
    assert changed != original
    with pytest.raises(validator.ProductionComposeValidationError, match="pinned SHA-256"):
        validator._dockerfile_base_images(changed, name)


def test_RELEASE_04_postgres_local_tag_guard_rejects_remote_tag() -> None:
    text = (ROOT / "deploy/compose/compose.prod.yaml").read_text()
    validator.validate_compose_text(text)
    changed = text.replace("holm/postgres:16.15-alpine", "upstream/postgres:16.15-alpine")
    assert changed != text
    with pytest.raises(validator.ProductionComposeValidationError, match="local release image"):
        validator.validate_compose_text(changed)


def test_RELEASE_04_builder_versions_and_public_shared_generator() -> None:
    text = (ROOT / "deploy/images/builder.Dockerfile").read_text()
    assert "node-v24.14.0-linux-x64.tar.xz" in text
    assert "41cd79bb7877c81605a9e68ec4c91547774f46a40c67a17e34d7179ef11729df" in text
    assert "hashlib.sha256(p.read_bytes()).hexdigest() ==" in text
    assert "uv==0.11.7" in text
    assert "npm --version" in text and "11.9.0" in text
    assert json.loads((ROOT / "package.json").read_text())["packageManager"] == "npm@11.9.0"
    generator = (ROOT / "deploy/build/release_manifest.py").read_text()
    assert "_release_manifest(source, _release_artifact_hashes())" in generator


@pytest.mark.parametrize("configured", [False, True])
def test_RELEASE_04_builder_apt_mirrors_preserve_other_source_fields(
    tmp_path: Path, configured: bool
) -> None:
    source = tmp_path / "debian.sources"
    original = (
        "Types: deb\nURIs: http://deb.debian.org/debian\nSuites: bookworm bookworm-updates\n"
        "Components: main\n\nTypes: deb\nURIs: http://deb.debian.org/debian-security\n"
        "Suites: bookworm-security\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n"
    )
    source.write_text(original)
    dockerfile = (ROOT / "deploy/images/builder.Dockerfile").read_text()
    line = next(line for line in dockerfile.splitlines() if line.startswith("RUN python -c"))
    code = shlex.split(line.removeprefix("RUN ").removesuffix(" \\"))[2]
    code = code.replace("/etc/apt/sources.list.d/debian.sources", str(source))
    env = {key: value for key, value in os.environ.items() if not key.startswith("APT_")}
    expected = original
    if configured:
        env.update(
            APT_MIRROR="https://mirror.example/debian",
            APT_SECURITY_MIRROR="https://security.example/debian-security",
        )
        expected = expected.replace(
            "http://deb.debian.org/debian-security", env["APT_SECURITY_MIRROR"]
        )
        expected = expected.replace("http://deb.debian.org/debian", env["APT_MIRROR"])
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr
    assert source.read_text() == expected
    assert '${UV_DEFAULT_INDEX:+--index-url "$UV_DEFAULT_INDEX"}' in dockerfile

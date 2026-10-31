"""Deterministic artifact contract checks for M0-02."""

from __future__ import annotations

import importlib
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BUILD_ROOT = PROJECT_ROOT / "build" / "m0-02"
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
try:
    build = importlib.import_module("build_artifacts")
    reproducible = importlib.import_module("verify_reproducible_build")
finally:
    sys.path.pop(0)
ARTIFACT_PATHS = build.ARTIFACT_PATHS
SHA256_LINE = re.compile(r"^([0-9a-f]{64})  ([^\r\n]+)$")


def _assert_exact_artifact_set(actual: set[Path], expected: set[Path]) -> None:
    assert actual == expected, (
        f"missing={sorted(expected - actual)}, unexpected={sorted(actual - expected)}"
    )


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def test_M0_02_build_produces_exact_artifacts_and_checksum_manifest() -> None:
    assert sum(path.suffix == ".whl" for path in ARTIFACT_PATHS) == 16
    environment = os.environ.copy()
    environment.update(
        {
            "NODE_OPTIONS": "--m0-02-must-not-reach-node",
            "NPM_TOKEN": "M0_02_AMBIENT_SENTINEL_MUST_NOT_PERSIST",
            "UV_INDEX": "https://invalid.example.test/simple",
            "npm_config_registry": "https://invalid.example.test/",
        }
    )
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "build_artifacts.py")],
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"

    artifact_paths = build.collect_artifact_paths(BUILD_ROOT, require_manifest=True)
    expected_files = {*artifact_paths, Path("SHA256SUMS")}
    actual_files = {
        path.relative_to(BUILD_ROOT) for path in BUILD_ROOT.rglob("*") if path.is_file()
    }
    _assert_exact_artifact_set(actual_files, expected_files)

    manifest_lines = (BUILD_ROOT / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    assert len(manifest_lines) == len(artifact_paths)
    parsed_lines = [SHA256_LINE.fullmatch(line) for line in manifest_lines]
    assert all(parsed_lines)
    manifest_paths = [Path(match.group(2)) for match in parsed_lines if match]
    assert manifest_paths == sorted(artifact_paths)
    for match in parsed_lines:
        assert match is not None
        digest, relative_path = match.groups()
        assert digest == _sha256(BUILD_ROOT / relative_path)


def test_M0_02_python_wheels_are_safe_and_path_independent() -> None:
    project_root_bytes = str(PROJECT_ROOT).encode()
    for relative_path in ARTIFACT_PATHS:
        if relative_path.suffix != ".whl":
            continue
        with zipfile.ZipFile(BUILD_ROOT / relative_path) as wheel:
            assert wheel.testzip() is None
            assert wheel.infolist()
            for member in wheel.infolist():
                member_path = Path(member.filename)
                assert not member_path.is_absolute()
                assert ".." not in member_path.parts
                if not member.is_dir():
                    assert project_root_bytes not in wheel.read(member)
                    assert b"M0_02_AMBIENT_SENTINEL_MUST_NOT_PERSIST" not in wheel.read(member)
                assert member.date_time == (1980, 1, 1, 0, 0, 0)


def test_M7_08B_maintenance_wheel_contains_the_production_entrypoint() -> None:
    artifact = BUILD_ROOT / "python/holm_maintenance_worker-0.1.0rc1-py3-none-any.whl"
    with zipfile.ZipFile(artifact) as wheel:
        entries = wheel.read("holm_maintenance_worker-0.1.0rc1.dist-info/entry_points.txt")
        assert b"holm-maintenance-worker = holm_maintenance_worker.__main__:main" in entries
        assert "holm_maintenance_worker/__main__.py" in wheel.namelist()
        assert "holm_maintenance_worker/worker.py" in wheel.namelist()


def test_M0_02_M1_07A_M2_09A_TC_DP_07_exact_artifact_set_guard_fails_in_both_directions() -> None:
    actual = set(ARTIFACT_PATHS)
    with pytest.raises(AssertionError, match="missing="):
        _assert_exact_artifact_set(actual, actual | {Path("python/not-produced.whl")})

    one_real_artifact = next(iter(actual))
    with pytest.raises(AssertionError, match="unexpected="):
        _assert_exact_artifact_set(actual, actual - {one_real_artifact})


@pytest.fixture
def spa_artifacts(tmp_path: Path) -> Path:
    """FX-M7-04-SPA: minimal Vite-shaped tree derived from preview output conventions."""
    output = tmp_path / "build/m0-02"
    for path in (
        *ARTIFACT_PATHS,
        Path("SHA256SUMS"),
        Path("web/assets/index-abcdefgh.js"),
        Path("web/assets/index-abcdefgh.css"),
        Path("web/assets/inter-latin-400-normal-abcdefgh.woff2"),
        Path("web/assets/noto-sans-sc-chinese-simplified-400-normal-abcdefgh.woff2"),
    ):
        destination = output / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("fixture")
    (output / "web/index.html").write_text(
        '<script type="module" src="/assets/index-abcdefgh.js"></script>'
        '<link rel="stylesheet" href="/assets/index-abcdefgh.css">'
    )
    return output


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("python/holm_gateway-0.1.0rc1-py3-none-any.whl", "missing="),
        ("web/index.html", "missing="),
        ("web/favicon.svg", "missing="),
        ("web/licenses/inter-OFL.txt", "missing="),
        ("web/licenses/noto-sans-sc-OFL.txt", "missing="),
        ("*.js", "missing required Web assets"),
        ("*.css", "missing required Web assets"),
        ("inter-*.woff2", "missing required Web assets"),
        ("noto-sans-sc-*.woff2", "missing required Web assets"),
        ("extra", "unexpected="),
        ("unhashed", "unexpected="),
        ("reference", "index.html references missing assets"),
        ("empty_html", "index.html must reference"),
        ("symlink", "invalid artifact entry"),
        ("SHA256SUMS", "missing="),
    ],
)
def test_M7_04_spa_integrity_guard_rejects_incomplete_or_extra_artifacts(
    spa_artifacts: Path, fault: str, message: str
) -> None:
    output = spa_artifacts
    artifact_paths = build.collect_artifact_paths(output, require_manifest=True)
    assert set(reproducible._artifact_hashes(output.parents[1])) == {
        *artifact_paths,
        Path("SHA256SUMS"),
    }
    if fault == "extra":
        (output / "web/workspace.js").touch()
    elif fault == "unhashed":
        (output / "web/assets/unhashed.js").touch()
    elif fault == "reference":
        with (output / "web/index.html").open("a") as handle:
            handle.write('<script src="/assets/missing-abcdefgh.js"></script>')
    elif fault == "empty_html":
        (output / "web/index.html").write_text("<html></html>")
    elif fault == "symlink":
        path = output / "web/favicon.svg"
        path.unlink()
        path.symlink_to("index.html")
    elif "*" in fault:
        for path in (output / "web/assets").glob(fault):
            path.unlink()
    else:
        (output / fault).unlink()
    with pytest.raises(RuntimeError, match=message):
        build.collect_artifact_paths(output, require_manifest=True)
    with pytest.raises(RuntimeError, match=message):
        reproducible._artifact_hashes(output.parents[1])


def test_M7_04_spa_integrity_guard_rejects_empty_scan(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="missing="):
        build.collect_artifact_paths(tmp_path)

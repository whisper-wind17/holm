#!/usr/bin/env python3
"""Build the deterministic Python and Web artifact set."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Final

from cache_build_dependencies import ensure_locked_build_wheel
from holm_contracts import (
    NamedHash,
    ReleaseArtifactHashes,
    ReleaseManifest,
)
from pydantic import ValidationError

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
BUILD_ROOT: Final[Path] = ROOT / "build" / "m0-02"
PYTHON_OUTPUT: Final[Path] = BUILD_ROOT / "python"
WEB_OUTPUT: Final[Path] = BUILD_ROOT / "web"
CHECKSUM_MANIFEST: Final[Path] = BUILD_ROOT / "SHA256SUMS"
BUILD_CONSTRAINTS: Final[Path] = BUILD_ROOT / "build-constraints.txt"
RELEASE_MANIFEST_OUTPUT: Final[Path] = ROOT / "build" / "release-manifest.json"
RELEASE_MANIFEST_SOURCE: Final[Path] = ROOT / "deploy" / "release-manifest.source.json"
FIXED_SOURCE_DATE_EPOCH: Final[str] = "946684800"
BUILD_MANIFESTS: Final[tuple[Path, ...]] = (
    Path("apps/control_api/pyproject.toml"),
    Path("apps/gateway/pyproject.toml"),
    Path("apps/host_agent/pyproject.toml"),
    Path("apps/workers/download/pyproject.toml"),
    Path("apps/workers/instance/pyproject.toml"),
    Path("apps/workers/ledger/pyproject.toml"),
    Path("apps/workers/maintenance/pyproject.toml"),
    Path("packages/common/pyproject.toml"),
    Path("packages/contracts/pyproject.toml"),
    Path("packages/domain/pyproject.toml"),
    Path("packages/ledger/pyproject.toml"),
    Path("packages/persistence/pyproject.toml"),
    Path("packages/protocols/pyproject.toml"),
    Path("packages/runtimes/pyproject.toml"),
    Path("packages/security/pyproject.toml"),
    Path("packages/telemetry/pyproject.toml"),
)


def _project_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project: object = data.get("project")
    if not isinstance(project, dict):
        raise RuntimeError("root pyproject.toml is missing project")
    version: object = project.get("version")
    if not isinstance(version, str) or not version.strip():
        raise RuntimeError("root pyproject.toml is missing project.version")
    return version.strip()


_PROJECT_VERSION: Final[str] = _project_version()
ARTIFACT_PATHS: Final[tuple[Path, ...]] = (
    Path(f"python/holm_control_api-{_PROJECT_VERSION}-py3-none-any.whl"),
    Path(f"python/holm_gateway-{_PROJECT_VERSION}-py3-none-any.whl"),
    Path(f"python/holm_host_agent-{_PROJECT_VERSION}-py3-none-any.whl"),
    Path(f"python/holm_download_worker-{_PROJECT_VERSION}-py3-none-any.whl"),
    Path(f"python/holm_instance_controller-{_PROJECT_VERSION}-py3-none-any.whl"),
    Path(f"python/holm_ledger_writer-{_PROJECT_VERSION}-py3-none-any.whl"),
    Path(f"python/holm_maintenance_worker-{_PROJECT_VERSION}-py3-none-any.whl"),
    Path(f"python/holm_common-{_PROJECT_VERSION}-py3-none-any.whl"),
    Path(f"python/holm_contracts-{_PROJECT_VERSION}-py3-none-any.whl"),
    Path(f"python/holm_domain-{_PROJECT_VERSION}-py3-none-any.whl"),
    Path(f"python/holm_ledger-{_PROJECT_VERSION}-py3-none-any.whl"),
    Path(f"python/holm_persistence-{_PROJECT_VERSION}-py3-none-any.whl"),
    Path(f"python/holm_protocols-{_PROJECT_VERSION}-py3-none-any.whl"),
    Path(f"python/holm_runtimes-{_PROJECT_VERSION}-py3-none-any.whl"),
    Path(f"python/holm_security-{_PROJECT_VERSION}-py3-none-any.whl"),
    Path(f"python/holm_telemetry-{_PROJECT_VERSION}-py3-none-any.whl"),
    Path("web/index.html"),
    Path("web/favicon.svg"),
    Path("web/licenses/inter-OFL.txt"),
    Path("web/licenses/noto-sans-sc-OFL.txt"),
)
WEB_ASSET_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"web/assets/[A-Za-z0-9_-]+-[A-Za-z0-9_-]{8}\.(?:js|css|woff2?)"
)
SOURCE_REVISION_INPUTS: Final[tuple[Path, ...]] = (
    Path("pyproject.toml"),
    Path("uv.lock"),
    Path("package-lock.json"),
    Path("runtime-compat.yaml"),
    Path("packages/contracts/src/holm_contracts/__init__.py"),
    Path("packages/contracts/src/holm_contracts/release.py"),
    Path("scripts/build_artifacts.py"),
)
BUILD_INJECTED_MANIFEST_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "artifact_hashes",
        "build_version",
        "commit",
        "node_lockfile_hash",
        "python_lockfile_hash",
        "runtime_compat_hash",
    }
)


def sha256(path: Path) -> str:
    """Return a streaming SHA-256 digest for an artifact."""
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_tree(path: Path) -> str:
    files = tuple(sorted(candidate for candidate in path.rglob("*") if candidate.is_file()))
    if not files:
        raise RuntimeError(f"required release input has no files: {path.relative_to(ROOT)}")
    digest = hashlib.sha256()
    for file_path in files:
        digest.update(file_path.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _source_snapshot_revision() -> str:
    """Provide a deterministic revision when an exported source tree has no Git metadata."""
    digest = hashlib.sha1(usedforsecurity=False)
    for relative_path in SOURCE_REVISION_INPUTS:
        digest.update(relative_path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update((ROOT / relative_path).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _build_commit() -> str:
    injected = os.environ.get("HOLM_BUILD_COMMIT", "").strip()
    if injected:
        return injected

    git = shutil.which("git")
    if git is not None:
        result = subprocess.run(
            [git, "rev-parse", "--verify", "HEAD"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    return _source_snapshot_revision()


def _release_artifact_hashes() -> ReleaseArtifactHashes:
    systemd_root = ROOT / "deploy" / "systemd"
    return ReleaseArtifactHashes(
        bundle=sha256(CHECKSUM_MANIFEST),
        compose=_sha256_tree(ROOT / "deploy" / "compose"),
        caddyfile=_sha256_tree(ROOT / "deploy" / "caddy"),
        systemd_units=tuple(
            NamedHash(name=path.relative_to(systemd_root).as_posix(), sha256=sha256(path))
            for path in sorted(
                candidate for candidate in systemd_root.rglob("*") if candidate.is_file()
            )
        ),
    )


def _load_release_manifest_source(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise RuntimeError(
            f"release manifest source could not be read: {path.relative_to(ROOT)}"
        ) from error
    try:
        data: object = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError("release manifest source validation failed: JSON") from None
    if not isinstance(data, dict):
        raise RuntimeError("release manifest source validation failed: root")
    source: dict[str, object] = {}
    for key, value in data.items():
        if not isinstance(key, str):
            raise RuntimeError("release manifest source validation failed: root")
        source[key] = value
    return source


def _release_manifest(
    source: dict[str, object], artifact_hashes: ReleaseArtifactHashes
) -> ReleaseManifest:
    conflicts = sorted(BUILD_INJECTED_MANIFEST_FIELDS.intersection(source))
    if conflicts:
        fields = ", ".join(conflicts)
        raise RuntimeError(
            f"release manifest source must not define build-injected fields: {fields}"
        )

    commit = _build_commit()
    payload = {
        **source,
        "artifact_hashes": artifact_hashes.model_dump(mode="json"),
        "build_version": f"{_project_version()}+g{commit[:12]}",
        "commit": commit,
        "node_lockfile_hash": sha256(ROOT / "package-lock.json"),
        "python_lockfile_hash": sha256(ROOT / "uv.lock"),
        "runtime_compat_hash": sha256(ROOT / "runtime-compat.yaml"),
    }
    try:
        return ReleaseManifest.model_validate(payload)
    except ValidationError as error:
        fields = ", ".join(_validation_fields(error))
        raise RuntimeError(f"release manifest source validation failed: {fields}") from None


def _validation_fields(error: ValidationError) -> tuple[str, ...]:
    return tuple(
        ".".join(str(segment) for segment in item["loc"])
        for item in error.errors(include_input=False, include_url=False)
    )


def _validate_release_manifest(path: Path) -> ReleaseManifest:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise RuntimeError("release manifest could not be read") from error
    try:
        data: object = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError("release manifest validation failed: JSON") from None
    try:
        return ReleaseManifest.model_validate(data)
    except ValidationError as error:
        fields = ", ".join(_validation_fields(error))
        raise RuntimeError(f"release manifest validation failed: {fields}") from None


def _write_release_manifest(manifest: ReleaseManifest) -> None:
    RELEASE_MANIFEST_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True)
    RELEASE_MANIFEST_OUTPUT.write_text(f"{payload}\n", encoding="utf-8", newline="\n")


def _generate_release_manifest(source: dict[str, object] | None) -> Path | None:
    RELEASE_MANIFEST_OUTPUT.unlink(missing_ok=True)
    if source is None:
        print(
            "Release Manifest generation skipped: "
            "deploy/release-manifest.source.json is absent; M7 must freeze release values."
        )
        return None

    manifest = _release_manifest(source, _release_artifact_hashes())
    _write_release_manifest(manifest)
    return RELEASE_MANIFEST_OUTPUT


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--validate-release-manifest",
        type=Path,
        metavar="PATH",
        help="validate one JSON Release Manifest and exit without building",
    )
    return parser.parse_args(argv)


def _reset_build_root() -> None:
    resolved_root = ROOT.resolve()
    resolved_output = BUILD_ROOT.resolve()
    if resolved_output == resolved_root or not resolved_output.is_relative_to(resolved_root):
        raise RuntimeError(f"refusing to clear unsafe build path: {resolved_output}")
    if BUILD_ROOT.exists():
        shutil.rmtree(BUILD_ROOT)
    PYTHON_OUTPUT.mkdir(parents=True)
    WEB_OUTPUT.mkdir(parents=True)


def _build_environment() -> dict[str, str]:
    tool_directories: list[str] = []
    for command in ("uv", "node", "npm"):
        executable = shutil.which(command)
        if executable is None:
            raise RuntimeError(f"required build command is missing: {command}")
        directory = str(Path(executable).resolve().parent)
        if directory not in tool_directories:
            tool_directories.append(directory)
    tool_directories.extend(path for path in ("/usr/bin", "/bin") if path not in tool_directories)

    environment = {
        "CI": "1",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
        "PATH": os.pathsep.join(tool_directories),
        "PYTHONHASHSEED": "0",
        "SOURCE_DATE_EPOCH": FIXED_SOURCE_DATE_EPOCH,
        "TZ": "UTC",
        "UV_KEYRING_PROVIDER": "disabled",
        "UV_NO_PROGRESS": "1",
        "UV_OFFLINE": "1",
        "UV_PYTHON_DOWNLOADS": "never",
        "npm_config_audit": "false",
        "npm_config_fund": "false",
        "npm_config_update_notifier": "false",
        "npm_config_userconfig": "/dev/null",
    }
    for key in ("TMPDIR", "UV_CACHE_DIR", "npm_config_cache"):
        if value := os.environ.get(key):
            environment[key] = value
    return environment


def _build_constraint_line() -> str:
    requirements: set[str] = set()
    for manifest in BUILD_MANIFESTS:
        data = tomllib.loads((ROOT / manifest).read_text(encoding="utf-8"))
        requirements.update(data.get("build-system", {}).get("requires", []))
    if requirements != {"uv_build==0.11.7"}:
        raise RuntimeError(f"unexpected Python build requirements: {sorted(requirements)}")

    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    matches = [package for package in lock["package"] if package["name"] == "uv-build"]
    if len(matches) != 1:
        raise RuntimeError("uv.lock must contain exactly one uv-build package")
    package = matches[0]
    if package.get("version") != "0.11.7" or "registry" not in package.get("source", {}):
        raise RuntimeError("uv.lock uv-build entry must be registry version 0.11.7")

    artifacts = [*package.get("wheels", [])]
    if package.get("sdist"):
        artifacts.append(package["sdist"])
    hashes = sorted({artifact["hash"] for artifact in artifacts})
    if not hashes or any(not value.startswith("sha256:") for value in hashes):
        raise RuntimeError("uv.lock uv-build artifacts must carry SHA-256 hashes")
    hash_options = " ".join(f"--hash={value}" for value in hashes)
    return f"uv_build==0.11.7 {hash_options}\n"


def _run(command: list[str], label: str, environment: dict[str, str]) -> None:
    result = subprocess.run(command, cwd=ROOT, env=environment, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"{label} failed with exit code {result.returncode}")


def collect_artifact_paths(
    output_root: Path, *, require_manifest: bool = False
) -> tuple[Path, ...]:
    """Validate fixed files, hashed SPA assets and HTML references in one output tree.

    Build, reproducibility checks and contract tests share this definition. The
    checksum manifest is metadata and is never included in its own file list.
    """
    actual_files: set[Path] = set()
    for path in output_root.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise RuntimeError(f"invalid artifact entry: {path.relative_to(output_root)}")
        if path.is_file():
            actual_files.add(path.relative_to(output_root))
    expected_files = set(ARTIFACT_PATHS)
    if require_manifest:
        expected_files.add(Path("SHA256SUMS"))
    assets = {path for path in actual_files if WEB_ASSET_PATTERN.fullmatch(path.as_posix())}
    missing = expected_files - actual_files
    unexpected = actual_files - expected_files - assets - {Path("SHA256SUMS")}
    if missing or unexpected:
        raise RuntimeError(
            f"artifact set mismatch; missing={sorted(missing)}, unexpected={sorted(unexpected)}"
        )
    for pattern in ("*.js", "*.css", "inter-*.woff2", "noto-sans-sc-*.woff2"):
        if not any(path.match(pattern) for path in assets):
            raise RuntimeError(f"missing required Web assets: {pattern}")
    html = (output_root / "web/index.html").read_text(encoding="utf-8")
    references = {Path("web" + value) for value in re.findall(r"/assets/[^\s\"'<>]+", html)}
    if not {".js", ".css"} <= {path.suffix for path in references}:
        raise RuntimeError("index.html must reference JavaScript and CSS assets")
    if references - assets:
        raise RuntimeError(f"index.html references missing assets: {sorted(references - assets)}")
    return tuple(sorted(actual_files - {Path("SHA256SUMS")}))


def _write_checksum_manifest(artifact_paths: tuple[Path, ...]) -> None:
    lines = [f"{sha256(BUILD_ROOT / path)}  {path.as_posix()}\n" for path in artifact_paths]
    CHECKSUM_MANIFEST.write_text("".join(lines), encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    validation_path: Path | None = args.validate_release_manifest
    if validation_path is not None:
        _validate_release_manifest(validation_path)
        return 0

    release_manifest_source = _load_release_manifest_source(RELEASE_MANIFEST_SOURCE)
    _reset_build_root()
    environment = _build_environment()
    locked_build_wheel = ensure_locked_build_wheel(offline=True)
    BUILD_CONSTRAINTS.write_text(_build_constraint_line(), encoding="utf-8", newline="\n")

    try:
        _run(
            [
                "uv",
                "build",
                "--all-packages",
                "--wheel",
                "--force-pep517",
                "--build-constraints",
                str(BUILD_CONSTRAINTS),
                "--require-hashes",
                "--offline",
                "--no-index",
                "--find-links",
                str(locked_build_wheel.parent),
                "--out-dir",
                str(PYTHON_OUTPUT),
                "--no-create-gitignore",
            ],
            "hash-constrained offline Python wheel build",
            environment,
        )
    finally:
        BUILD_CONSTRAINTS.unlink(missing_ok=True)
    _run(
        [
            "npm",
            "run",
            "build",
            "--workspace",
            "@holm/web",
            "--",
            "--config",
            "preview/vite.config.ts",
            "--outDir",
            str(WEB_OUTPUT),
            "--emptyOutDir",
        ],
        "Web build",
        environment,
    )

    artifact_paths = collect_artifact_paths(BUILD_ROOT)
    _write_checksum_manifest(artifact_paths)
    release_manifest_path = _generate_release_manifest(release_manifest_source)
    for relative_path in artifact_paths:
        print(f"{relative_path.as_posix()} sha256: {sha256(BUILD_ROOT / relative_path)}")
    print(f"SHA256SUMS sha256: {sha256(CHECKSUM_MANIFEST)}")
    if release_manifest_path is not None:
        print(f"release-manifest.json sha256: {sha256(release_manifest_path)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from None

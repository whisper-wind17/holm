#!/usr/bin/env python3
"""Verify artifacts across cold-online and warm-offline clean builds."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Final

from build_artifacts import collect_artifact_paths, sha256

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
LOCKFILES: Final[tuple[Path, ...]] = (Path("uv.lock"), Path("package-lock.json"))
BUILD_ROOT: Final[Path] = Path("build/m0-02")
CHECKSUM_MANIFEST: Final[Path] = BUILD_ROOT / "SHA256SUMS"
INSTALL_PATHS: Final[tuple[Path, ...]] = (Path(".venv"), Path("node_modules"), BUILD_ROOT)
SOURCE_MTIMES: Final[tuple[int, int]] = (946684800, 1609459200)
NETWORK_ENV_ALLOWLIST: Final[tuple[str, ...]] = (
    "ALL_PROXY",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "all_proxy",
    "https_proxy",
    "http_proxy",
    "no_proxy",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
)


def _repository_files() -> tuple[Path, ...]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return tuple(Path(item.decode()) for item in result.stdout.split(b"\0") if item)


def _copy_workspace(
    destination: Path, repository_files: tuple[Path, ...], *, source_mtime: int
) -> None:
    for relative_path in repository_files:
        source = ROOT / relative_path
        if not source.exists():
            continue
        if source.is_symlink():
            raise RuntimeError(f"source snapshot contains a symlink: {relative_path}")
        if not source.is_file():
            continue
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        os.utime(target, (source_mtime, source_mtime), follow_symlinks=False)

    reused_paths = [path for path in INSTALL_PATHS if (destination / path).exists()]
    if reused_paths:
        raise RuntimeError(f"clean workspace unexpectedly contains generated paths: {reused_paths}")


def _run_command(
    workspace: Path, command: list[str], label: str, environment: dict[str, str]
) -> None:
    result = subprocess.run(
        command,
        cwd=workspace,
        env=environment,
        text=True,
        capture_output=True,
        timeout=600,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{label} failed with exit code {result.returncode}\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )


def _environment(
    uv_cache: Path, npm_cache: Path, temporary_directory: Path, *, offline: bool
) -> dict[str, str]:
    tool_directories: list[str] = []
    for command in ("uv", "node", "npm"):
        executable = shutil.which(command)
        if executable is None:
            raise RuntimeError(f"required verification command is missing: {command}")
        directory = str(Path(executable).resolve().parent)
        if directory not in tool_directories:
            tool_directories.append(directory)
    tool_directories.extend(path for path in ("/usr/bin", "/bin") if path not in tool_directories)

    environment = {key: os.environ[key] for key in NETWORK_ENV_ALLOWLIST if key in os.environ}
    environment.update(
        {
            "CI": "1",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "NO_COLOR": "1",
            "PATH": os.pathsep.join(tool_directories),
            "HOLM_BUILD_WHEELHOUSE": str(uv_cache / "holm-build-wheelhouse"),
            "TMPDIR": str(temporary_directory),
            "UV_CACHE_DIR": str(uv_cache),
            "UV_DEFAULT_INDEX": "https://pypi.org/simple",
            "UV_KEYRING_PROVIDER": "disabled",
            "UV_NO_PROGRESS": "1",
            "UV_PYTHON_DOWNLOADS": "never",
            "npm_config_audit": "false",
            "npm_config_cache": str(npm_cache),
            "npm_config_fund": "false",
            "npm_config_registry": "https://registry.npmjs.org/",
            "npm_config_update_notifier": "false",
            "npm_config_userconfig": "/dev/null",
        }
    )
    temporary_directory.mkdir(parents=True)
    if offline:
        environment["UV_OFFLINE"] = "1"
        environment["npm_config_offline"] = "true"
    return environment


def _verify_lockfiles(
    workspace: Path, expected: dict[Path, str], run_number: int, stage: str
) -> None:
    for lockfile, expected_hash in expected.items():
        actual_hash = sha256(workspace / lockfile)
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"run {run_number} {stage} changed {lockfile}: "
                f"expected {expected_hash}, got {actual_hash}"
            )


def _artifact_hashes(workspace: Path) -> dict[Path, str]:
    output_root = workspace / BUILD_ROOT
    expected_files = {
        *collect_artifact_paths(output_root, require_manifest=True),
        Path("SHA256SUMS"),
    }
    return {path: sha256(output_root / path) for path in sorted(expected_files)}


def _cache_has_content(path: Path) -> bool:
    return path.is_dir() and any(path.rglob("*"))


def main() -> int:
    for lockfile in LOCKFILES:
        if not (ROOT / lockfile).is_file():
            raise SystemExit(f"missing required lockfile: {lockfile}")

    source_hashes = {lockfile: sha256(ROOT / lockfile) for lockfile in LOCKFILES}
    repository_files = _repository_files()

    with tempfile.TemporaryDirectory(prefix="holm-m0-02-") as temporary_root:
        temporary_path = Path(temporary_root)
        uv_cache = temporary_path / "cache" / "uv"
        npm_cache = temporary_path / "cache" / "npm"
        if uv_cache.exists() or npm_cache.exists():
            raise RuntimeError("cold build cache was not empty")

        build_hashes: list[dict[Path, str]] = []
        for run_number, offline in ((1, False), (2, True)):
            workspace = temporary_path / f"run-{run_number}"
            workspace.mkdir()
            _copy_workspace(
                workspace,
                repository_files,
                source_mtime=SOURCE_MTIMES[run_number - 1],
            )
            run_tmpdir = temporary_path / "tmp" / f"run-{run_number}"
            if run_tmpdir.exists():
                raise RuntimeError(f"run {run_number} temporary directory was not clean")
            environment = _environment(
                uv_cache,
                npm_cache,
                run_tmpdir,
                offline=offline,
            )
            cache_mode = "warm-offline" if offline else "cold-online"

            _verify_lockfiles(workspace, source_hashes, run_number, "before bootstrap")
            _run_command(
                workspace,
                [str(workspace / "dev"), "bootstrap"],
                f"run {run_number} {cache_mode} bootstrap",
                environment,
            )
            _verify_lockfiles(workspace, source_hashes, run_number, "after bootstrap")
            _verify_lockfiles(workspace, source_hashes, run_number, "before build")
            _run_command(
                workspace,
                [str(workspace / "dev"), "build"],
                f"run {run_number} {cache_mode} build",
                environment,
            )
            _verify_lockfiles(workspace, source_hashes, run_number, "after build")
            build_hashes.append(_artifact_hashes(workspace))
            print(f"run {run_number} ({cache_mode}): PASS")

            if run_number == 1 and not (
                _cache_has_content(uv_cache) and _cache_has_content(npm_cache)
            ):
                raise RuntimeError("cold build did not populate both task-local dependency caches")

        if build_hashes[0] != build_hashes[1]:
            all_paths = sorted(build_hashes[0].keys() | build_hashes[1].keys())
            differences = [
                f"{path}: cold={build_hashes[0].get(path)}, warm={build_hashes[1].get(path)}"
                for path in all_paths
                if build_hashes[0].get(path) != build_hashes[1].get(path)
            ]
            raise RuntimeError("artifact hash mismatch\n" + "\n".join(differences))

    print("artifact hashes (cold-online == warm-offline):")
    for relative_path, digest in build_hashes[0].items():
        print(f"{relative_path.as_posix()} sha256: {digest}")
    for lockfile, digest in source_hashes.items():
        print(f"{lockfile.as_posix()} sha256: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""FX-RELEASE-04: Docker command responses; no daemon, services or GPU are used."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import runpy
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = load("release04_build", ROOT / "deploy/build/local_build.py")


class Docker:
    def __init__(self, output: Path, *, fail: str = "") -> None:
        self.output = output
        self.fail = fail
        self.commands: list[list[str]] = []
        self.release: Path | None = None

    def run(self, argv: list[str], *, timeout: int = 7200, step: str = "preflight") -> str:
        self.commands.append(argv)
        if self.fail and self.fail in argv:
            raise build.BuildError("fixture failure; check Docker")
        if argv[1] == "version":
            return "29.3.1"
        if argv[1:3] == ["compose", "version"]:
            return "5.1.0"
        if argv[1] == "info":
            return str(self.output.parent)
        if argv[1:3] == ["image", "inspect"]:
            return "sha256:" + "a" * 64
        if argv[1] == "run" and "/build" in argv:
            mount = argv[argv.index("--mount") + 1]
            self.release = Path(mount.removeprefix("type=bind,src=").removesuffix(",dst=/build"))
            artifacts = self.release / "build/m0-02"
            artifacts.mkdir(parents=True)
            (artifacts / "SHA256SUMS").write_text("FX-RELEASE-04 artifacts\n")
        elif argv[1] == "run":
            images = json.loads((self.output / "images.json").read_text())
            (self.output / "release-manifest.json").write_text(
                json.dumps(
                    {
                        "image_digests": [
                            {"name": name, "digest": digest} for name, digest in images.items()
                        ]
                    }
                )
            )
        return ""


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build.os, "geteuid", lambda: 0)
    monkeypatch.setattr(build.sys, "version_info", (3, 12, 3))
    monkeypatch.setattr(build.platform, "system", lambda: "Linux")
    monkeypatch.setattr(build.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(build.shutil, "disk_usage", lambda _: SimpleNamespace(free=100 * 1024**3))
    for key in build.MIRROR_ENV:
        monkeypatch.delenv(key, raising=False)


def arguments(output: Path, *extra: str):
    return build.parse_args(
        ["--public-url", "https://holm.example", "--output", str(output), *extra]
    )


def test_RELEASE_04_order_registry_tags_manifest_and_installer_configuration(
    tmp_path: Path, environment, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    output = tmp_path / "release"
    executor = Docker(output)
    monkeypatch.setenv("UV_DEFAULT_INDEX", "https://pypi.org/simple")
    monkeypatch.setenv("npm_config_registry", "https://registry.npmjs.org")
    monkeypatch.setenv("APT_MIRROR", "https://mirror.example/debian")
    monkeypatch.setenv("APT_SECURITY_MIRROR", "https://mirror.example/debian-security")
    args = arguments(
        output,
        "--base-registry",
        "mirror.example",
        "--no-systemd",
        "--external-model-root",
        str(tmp_path / "models"),
    )
    assert build.build(args, executor) == 0
    assert [line for line in capsys.readouterr().out.splitlines() if line.startswith("step=")] == [
        f"step={step} result=complete"
        for step in (
            "preflight",
            "artifacts",
            "application",
            "edge",
            "runtime-vllm",
            "runtime-sglang",
            "postgres",
            "manifest",
            "configuration",
        )
    ]
    commands = executor.commands
    builds = [command for command in commands if command[1] == "build"]
    project = build.tomllib.loads((ROOT / "pyproject.toml").read_text())
    version = project["project"]["version"]
    runtimes = json.loads((ROOT / "runtime-compat.yaml").read_text())["runtimes"]
    assert [command[command.index("--tag") + 1] for command in builds] == [
        f"holm/builder:{version}",
        f"holm/application:{version}",
        f"holm/edge:{version}",
        *[f"holm/runtime-{runtime['name']}:{runtime['version']}" for runtime in runtimes],
    ]
    assert all("HOLM_BASE_REGISTRY=mirror.example" in command for command in builds)
    for key in build.MIRROR_ENV:
        assert builds[0][builds[0].index(key) - 1] == "--build-arg"
        assert all(key not in command for command in builds[1:])
    assert [command[command.index("--file") + 1].split("/")[-1] for command in builds] == [
        "builder.Dockerfile",
        "application.Dockerfile",
        "edge.Dockerfile",
        "runtime-vllm.Dockerfile",
        "runtime-sglang.Dockerfile",
    ]
    pull = ["docker", "pull", "mirror.example/" + build.POSTGRES]
    assert pull in commands
    assert ["docker", "tag", pull[-1], "holm/postgres:16.15-alpine"] in commands
    assert commands.index(builds[-1]) < commands.index(pull) < len(commands) - 1
    artifact_command = next(command for command in commands if "/build" in command)
    assert "UV_DEFAULT_INDEX" in artifact_command and "npm_config_registry" in artifact_command
    assert "uv sync --frozen" in artifact_command[-1]
    assert "scripts/build_artifacts.py" in artifact_command[-1]
    assert "--network" in commands[-1] and "none" in commands[-1]
    assert commands[-1][commands[-1].index("--entrypoint") + 2] == "sha256:" + "a" * 64
    config = json.loads((output / "install.json").read_text())
    monkeypatch.syspath_prepend(str(ROOT / "deploy/installer"))
    installer = load("release04_installer", ROOT / "deploy/installer/install.py")
    assert installer.load_configuration(output / "install.json") == config
    assert config["release_root"] == str(executor.release)
    assert config["systemd"] == {"enable": False}
    assert config["external_model_root"] == str(tmp_path / "models")
    assert executor.release is not None
    assert (executor.release / "docs/content/help-center.md").is_file()
    assert not (executor.release / "scripts/remote.env").exists()
    assert not (executor.release / "deploy/verify").exists()


@pytest.mark.parametrize(
    "failure",
    ["root", "python", "space", "version", "compose", "info", "url", "registry", "ports", "mirror"],
)
def test_RELEASE_04_preflight_failure_is_zero_mutation(
    tmp_path: Path, environment, monkeypatch: pytest.MonkeyPatch, failure: str, capsys
) -> None:
    output = tmp_path / "release"
    executor = Docker(output, fail=failure if failure in {"version", "compose", "info"} else "")
    args = arguments(output)
    if failure == "root":
        monkeypatch.setattr(build.os, "geteuid", lambda: 1000)
    elif failure == "python":
        monkeypatch.setattr(build.sys, "version_info", (3, 11, 0))
    elif failure == "space":
        monkeypatch.setattr(build.shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
    elif failure == "url":
        args.public_url = "http://holm.example"
    elif failure == "registry":
        args.base_registry = "https://invalid/registry"
    elif failure == "ports":
        args.http_port = 443
    elif failure == "mirror":
        monkeypatch.setenv("UV_DEFAULT_INDEX", "https://user:FX-RELEASE-04-secret@mirror.example")
    assert build.build(args, executor) == 1
    assert not output.exists()
    assert all(command[1] in {"version", "compose", "info"} for command in executor.commands)
    diagnostic = capsys.readouterr().err
    assert "step=preflight result=failed" in diagnostic
    assert "FX-RELEASE-04-secret" not in diagnostic


def test_RELEASE_04_output_refusal_and_explicit_force_preserves_unrelated_files(
    tmp_path: Path, environment
) -> None:
    output = tmp_path / "release"
    output.mkdir()
    preserved = output / "keep.txt"
    preserved.write_text("existing user file")
    executor = Docker(output)
    assert build.build(arguments(output), executor) == 1
    assert executor.commands == []
    assert build.build(arguments(output, "--force-output"), executor) == 0
    first_release = executor.release
    assert build.build(arguments(output, "--force-output"), executor) == 0
    assert first_release and first_release.is_dir() and first_release != executor.release
    assert preserved.read_text() == "existing user file"


@pytest.mark.parametrize("failure", ["build", "pull", "image"])
def test_RELEASE_04_command_failure_stops_before_install_json(
    tmp_path: Path, environment, failure: str, capsys
) -> None:
    output = tmp_path / "release"
    executor = Docker(output, fail=failure)
    assert build.build(arguments(output), executor) == 1
    assert not (output / "install.json").exists()
    assert failure in executor.commands[-1]
    assert "result=failed remedy=" in capsys.readouterr().err


def test_RELEASE_04_rejects_output_symlink_without_following_it(
    tmp_path: Path, environment
) -> None:
    output = tmp_path / "release"
    output.mkdir()
    target = tmp_path / "sensitive"
    target.write_text("FX-RELEASE-04-private")
    (output / "images.json").symlink_to(target)
    executor = Docker(output)
    assert build.build(arguments(output, "--force-output"), executor) == 1
    assert executor.commands == []
    assert target.read_text() == "FX-RELEASE-04-private"


def test_RELEASE_04_executor_hides_subprocess_output(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(argv, **kwargs):
        kwargs["stdout"].write(b"FX-RELEASE-04-private-output")
        return subprocess.CompletedProcess(argv, 1)

    monkeypatch.setattr(build.subprocess, "run", fail)
    with pytest.raises(build.BuildError) as error:
        build.Executor().run(["docker", "build"])
    assert "FX-RELEASE-04-private-output" not in str(error.value)


def test_RELEASE_04_executor_persists_full_output_with_private_permissions(
    tmp_path: Path, capfd
) -> None:
    executor = build.Executor()
    executor.log_directory = tmp_path / "output/logs"
    executor.log_directory.mkdir(parents=True, mode=0o755)
    payload = b"FX-RELEASE-04-stdout\n" * 5000
    for sequence in (1, 2):
        executor.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'FX-RELEASE-04-stdout\\n' * 5000); "
                "sys.stdout.flush(); sys.stderr.write('FX-RELEASE-04-stderr\\n')",
            ],
            step="artifacts",
        )
        path = executor.log_directory / f"{sequence:03d}-artifacts.log"
        assert executor.log_path == path
        assert path.read_bytes() == payload + b"FX-RELEASE-04-stderr\n"
        assert path.stat().st_mode & 0o777 == 0o600
    assert executor.log_directory.stat().st_mode & 0o777 == 0o700
    terminal = capfd.readouterr()
    assert terminal.out.splitlines() == [
        f"step=artifacts result=started log={executor.log_directory / f'{i:03d}-artifacts.log'}"
        for i in (1, 2)
    ]
    assert terminal.err == ""
    assert "FX-RELEASE-04-stdout" not in terminal.out
    assert "FX-RELEASE-04-stderr" not in terminal.out


@pytest.mark.parametrize("failure", ["exit", "timeout", "unavailable"])
def test_RELEASE_04_build_failure_reports_log_after_started_before_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, failure: str
) -> None:
    output = tmp_path / "release"
    executor = build.Executor()
    monkeypatch.setattr(build, "clean_git_commit", lambda _: None)
    monkeypatch.setattr(
        build, "preflight", lambda *_: (output, [], {"application": "holm/application:0.1.0"})
    )
    path = output / "logs/001-artifacts.log"

    def fail(argv, **kwargs):
        assert f"step=artifacts result=started log={path}" in capsys.readouterr().out
        kwargs["stdout"].write(b"FX-RELEASE-04-child-diagnostic")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        if failure == "unavailable":
            raise OSError("FX-RELEASE-04-child-exception")
        return subprocess.CompletedProcess(argv, 1)

    monkeypatch.setattr(build.subprocess, "run", fail)
    assert build.build(arguments(output), executor) == 1
    terminal = capsys.readouterr()
    assert "step=artifacts result=failed" in terminal.err
    assert f"log={path}" in terminal.err
    assert "FX-RELEASE-04-child" not in terminal.out + terminal.err
    assert path.read_bytes() == b"FX-RELEASE-04-child-diagnostic"
    assert not (output / "install.json").exists()


def test_RELEASE_04_logs_preserve_previous_runs_and_refuse_symlink_directory(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "logs"
    directory.mkdir()
    previous = directory / "001-artifacts.log"
    previous.write_text("FX-RELEASE-04-previous-run")
    executor = build.Executor()
    executor.log_directory = directory
    executor.run([sys.executable, "-c", "pass"], step="artifacts")
    assert executor.log_path == directory / "002-artifacts.log"
    assert previous.read_text() == "FX-RELEASE-04-previous-run"
    link = tmp_path / "linked-logs"
    link.symlink_to(directory)
    executor.log_directory = link
    with pytest.raises(build.BuildError, match="symlink"):
        executor.run([sys.executable, "-c", "pass"], step="artifacts")


def test_RELEASE_04_shared_generator_uses_actual_hashes(tmp_path: Path, environment) -> None:
    from holm_contracts import ReleaseManifest

    output = tmp_path / "release"
    executor = Docker(output)
    assert build.build(arguments(output), executor) == 0
    assert executor.release is not None
    command = [
        sys.executable,
        str(executor.release / "deploy/build/release_manifest.py"),
        "--repo",
        str(executor.release),
        "--work",
        str(output),
    ]
    result = subprocess.run(command, text=True, capture_output=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    manifest = ReleaseManifest.model_validate_json((output / "release-manifest.json").read_text())
    assert {item.name: item.digest for item in manifest.image_digests} == json.loads(
        (output / "images.json").read_text()
    )
    assert (
        manifest.artifact_hashes.bundle
        == hashlib.sha256((executor.release / "build/m0-02/SHA256SUMS").read_bytes()).hexdigest()
    )
    assert "signature" not in json.loads((output / "release-manifest.json").read_text())


@pytest.mark.parametrize("state", ["clean", "modified", "staged", "untracked", "export"])
def test_FIX_34_manifest_command_uses_only_clean_git_commit(
    tmp_path: Path, environment, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    # FX-FIX-34: host Git responses; Docker remains the RELEASE-04 command fixture.
    commit = "b" * 40
    calls = []

    def git_run(self, argv, **kwargs):
        calls.append(argv)
        assert kwargs["timeout"] == 10
        assert argv[:3] == ["git", "-C", str(ROOT)]
        if state == "export":
            raise build.BuildError("not a Git repository")
        if "status" in argv:
            assert argv[3:] == ["status", "--porcelain", "--untracked-files=all"]
            return {
                "clean": "",
                "modified": " M file",
                "staged": "M  file",
                "untracked": "?? file",
            }[state]
        assert argv[3:] == ["rev-parse", "HEAD"]
        return commit + "\n"

    monkeypatch.setattr(build.Executor, "run", git_run)
    output = tmp_path / "release"
    executor = Docker(output)
    assert build.build(arguments(output), executor) == 0
    command = executor.commands[-1]
    if state == "clean":
        assert command[-2:] == ["--commit", commit]
        assert len(calls) == 2
    else:
        snapshot = command[command.index("--commit") + 1]
        assert re.fullmatch(r"[0-9a-f]{40}", snapshot)
        assert snapshot != commit
        assert executor.release is not None
        payload = b"".join(
            relative.as_posix().encode("utf-8")
            + b"\0"
            + (executor.release / relative).read_bytes()
            + b"\0"
            for relative in build.source_files(executor.release)
        )
        assert snapshot == hashlib.sha1(payload, usedforsecurity=False).hexdigest()
        assert len(calls) == 1
    assert command[command.index("--network") + 1] == "none"
    assert "--read-only" in command
    assert executor.release is not None
    assert f"type=bind,src={executor.release},dst={executor.release},readonly" in command
    assert not (executor.release / ".git").exists()


@pytest.mark.parametrize("change", ["content", "rename", "add", "delete"])
def test_FIX_35_snapshot_tracks_all_copied_inputs(tmp_path: Path, change: str) -> None:
    # FX-FIX-35: isolated export of the real build input list, without Git or dependencies.
    repo = tmp_path / "source"
    files = build.source_files(ROOT)
    for relative in files:
        destination = repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    original = build.source_snapshot(repo, build.source_files(repo))
    assert re.fullmatch(r"[0-9a-f]{40}", original)
    assert build.source_snapshot(repo, list(reversed(files))) == original
    for name in ("build/output.txt", "logs/build.log", ".env", "secrets/key.txt"):
        excluded = repo / name
        excluded.parent.mkdir(parents=True, exist_ok=True)
        excluded.write_text("FX-FIX-35-excluded")
    assert build.source_snapshot(repo, build.source_files(repo)) == original
    source = repo / "deploy/installer/install.py"
    if change == "content":
        source.write_bytes(source.read_bytes() + b"\n")
    elif change == "rename":
        source.rename(source.with_name("renamed.py"))
    elif change == "add":
        source.with_name("added.py").write_text("# FX-FIX-35\n")
    else:
        source.unlink()
    changed_files = build.source_files(repo)
    changed = build.source_snapshot(repo, changed_files)
    assert changed != original
    assert build.source_snapshot(repo, changed_files) == changed


def test_FIX_34_production_manifest_has_shared_minimums(
    tmp_path: Path, environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    from holm_contracts.release import DEFAULT_HOST_REQUIREMENTS

    output = tmp_path / "release"
    executor = Docker(output)
    assert build.build(arguments(output), executor) == 0
    assert executor.release is not None
    commit = "c" * 40
    result = subprocess.run(
        [
            sys.executable,
            str(executor.release / "deploy/build/release_manifest.py"),
            "--repo",
            str(executor.release),
            "--work",
            str(output),
            "--commit",
            commit,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    artifacts = load("fix34_artifacts", ROOT / "scripts/build_artifacts.py")
    manifest = artifacts._validate_release_manifest(output / "release-manifest.json")
    assert manifest.host_requirements == DEFAULT_HOST_REQUIREMENTS
    assert manifest.host_requirements.model_dump() == {
        "nvidia_driver": "580.65.06",
        "docker": "24.0",
        "compose": "2.20",
        "nvidia_container_toolkit": "1.16",
    }
    assert manifest.commit == commit


def test_RELEASE_04_bad_image_digest_never_emits_manifest_or_config(
    tmp_path: Path, environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "release"
    executor = Docker(output)
    real_run = executor.run

    def invalid_digest(argv, **kwargs):
        result = real_run(argv, **kwargs)
        return "not-an-image-id" if argv[1:3] == ["image", "inspect"] else result

    monkeypatch.setattr(executor, "run", invalid_digest)
    assert build.build(arguments(output), executor) == 1
    assert not (output / "release-manifest.json").exists()
    assert not (output / "install.json").exists()


def test_RELEASE_04_failed_force_invalidates_old_install_configuration(
    tmp_path: Path, environment
) -> None:
    output = tmp_path / "release"
    executor = Docker(output)
    assert build.build(arguments(output), executor) == 0
    assert (output / "install.json").is_file()
    assert build.build(arguments(output, "--force-output"), Docker(output, fail="build")) == 1
    assert not (output / "install.json").exists()
    assert not (output / "release-manifest.json").exists()


def test_RELEASE_04_source_snapshot_excludes_secrets_and_dependency_trees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text("[tool.uv.workspace]\nmembers = []\n")
    tree = source / "src"
    tree.mkdir()
    (tree / "module.py").write_text("pass\n")
    for name in (".env", "private.key", "certificate.pem"):
        (tree / name).write_text("FX-RELEASE-04-private")
    for name in ("secrets", "node_modules", ".cache"):
        (tree / name).mkdir()
        (tree / name / "secret.json").write_text("FX-RELEASE-04-private")
    monkeypatch.setattr(build, "SOURCE_FILES", ("pyproject.toml",))
    monkeypatch.setattr(build, "SOURCE_TREES", ("src",))
    assert build.source_files(source) == [Path("pyproject.toml"), Path("src/module.py")]
    (tree / "linked.py").symlink_to(tree / "module.py")
    with pytest.raises(build.BuildError, match="symlink"):
        build.source_files(source)


@pytest.mark.parametrize("response", ["", "relative-directory"])
def test_RELEASE_04_missing_docker_storage_information_fails_before_writes(
    tmp_path: Path, environment, monkeypatch: pytest.MonkeyPatch, response: str
) -> None:
    output = tmp_path / "release"
    executor = Docker(output)
    real_run = executor.run

    def info(argv, **kwargs):
        result = real_run(argv, **kwargs)
        return response if argv[1] == "info" else result

    monkeypatch.setattr(executor, "run", info)
    assert build.build(arguments(output), executor) == 1
    assert not output.exists()
    assert all(command[1] in {"version", "compose", "info"} for command in executor.commands)


def test_RELEASE_04_launcher_checks_old_python_before_imports(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(sys, "version_info", (3, 10, 12))
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(ROOT / "deploy/build/holm-build"), run_name="__main__")
    assert error.value.code == 1
    assert "step=preflight result=failed remedy=Python 3.12 required" in capsys.readouterr().err


@pytest.mark.parametrize("mode", ["internal", "acme", "provided"])
def test_M7_02B_build_tls_configuration_without_reading_key(tmp_path, monkeypatch, mode):
    extra = ["--tls", mode]
    if mode == "acme":
        extra += ["--acme-email", "admin@example.test"]
    if mode == "provided":
        extra += [
            "--tls-certificate",
            "/not-present/cert.pem",
            "--tls-private-key",
            "/not-present/key.pem",
        ]
    args = arguments(tmp_path, *extra)

    def forbidden(*args, **kwargs):
        raise AssertionError("build must not read certificate or private key")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    config = build.configuration(args, tmp_path / "release", tmp_path)
    assert config["edge"]["tls"] == mode
    if mode == "provided":
        assert config["edge"]["private_key_file"] == "/not-present/key.pem"
    if mode == "acme":
        assert config["edge"]["acme_email"] == "admin@example.test"


@pytest.mark.parametrize(
    "extra",
    [
        ["--acme-email", "admin@example.test"],
        ["--tls-private-key", "/key.pem"],
        ["--tls", "provided"],
        ["--tls", "provided", "--tls-certificate", "relative", "--tls-private-key", "/key.pem"],
        ["--tls", "acme", "--acme-email", "bad\nemail"],
        ["--tls", "acme", "--public-url", "https://127.0.0.1"],
        [
            "--tls",
            "acme",
            "--http-port",
            "18080",
            "--https-port",
            "18443",
            "--public-url",
            "https://example.test:18443",
        ],
    ],
)
def test_M7_02B_build_rejects_invalid_tls_options(tmp_path, extra):
    with pytest.raises(build.BuildError):
        build.configuration(arguments(tmp_path, *extra), tmp_path / "release", tmp_path)

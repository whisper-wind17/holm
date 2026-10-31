"""FX-M7-04: installer failure/resume and effective deployment guards."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from holm_contracts.release import PRODUCTION_SERVICE_IDS
from holm_persistence.install_secrets import provision_secrets
from holm_runtimes import load_runtime_compatibility

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "deploy/installer"


def module(name: str):
    spec = importlib.util.spec_from_file_location(name, INSTALLER / f"{name}.py")
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


engine = module("engine")
guards = module("guards")
install = module("install")


def test_M7_04_TC_DP_03_failure_resume_and_completed_idempotency(tmp_path: Path) -> None:
    path = tmp_path / "journal.json"
    journal = engine.Journal(path, {"release": "FX-M7-04"})
    calls = []
    fail = True

    def step(name: str) -> None:
        calls.append(name)
        if name == "database" and fail:
            raise engine.InstallError("database_unavailable", "Start PostgreSQL and rerun.")

    actions = {name: lambda name=name: step(name) for name in engine.STAGES}
    logs = []
    assert journal.run(actions, logs.append) == 25
    assert "stage=database" in logs[-1] and "remedy=Start PostgreSQL" in logs[-1]
    fail = False
    resumed = engine.Journal(path, {"release": "FX-M7-04"})
    assert resumed.run(actions, logs.append) == 0
    assert calls.count("identities") == 1 and calls.count("database") == 2
    before = list(calls)
    assert engine.Journal(path, {"release": "FX-M7-04"}).run(actions, logs.append) == 0
    assert calls == before


def test_M7_04_TC_DP_03_changed_manifest_identity_refused(tmp_path: Path) -> None:
    path = tmp_path / "journal.json"
    engine.Journal(path, {"source_manifest_sha256": "first"}).save()
    with pytest.raises(engine.InstallError, match="different_installation"):
        engine.Journal(path, {"source_manifest_sha256": "replacement"})


def test_M7_04_secret_exception_never_enters_journal_or_output(tmp_path: Path) -> None:
    marker = "FX-M7-04-SECRET-SENTINEL"

    def fail() -> None:
        raise ValueError(marker)

    logs = []
    path = tmp_path / "journal.json"
    assert engine.Journal(path, {}).run({"preflight": fail}, logs.append) == 20
    assert marker not in path.read_text() + "".join(logs)


def test_M7_04_effective_unit_guard_rejects_added_write_path() -> None:
    base = (ROOT / "deploy/systemd/holm-host-agent.service").read_text()
    command = "/opt/holm/releases/FX-M7-04/agent-venv/bin/holm-host-agent"
    dropin = (
        f"[Service]\nExecStart=\nExecStart={command}\n"
        "ReadWritePaths=/var/lib/holm/runtime-cache\n"
        "SupplementaryGroups=988 10008\n"
    )
    kwargs = dict(
        run_root=Path("/run/holm"),
        cache_root=Path("/var/lib/holm/runtime-cache"),
        command=command,
        groups=(988, 10008),
        agent_name="holm-agent",
    )
    assert guards.effective_agent(base, dropin, **kwargs)["ProtectSystem"] == "strict"
    with pytest.raises(engine.InstallError, match="unsafe_agent_dropin"):
        guards.effective_agent(base, dropin + "ReadWritePaths=/etc\n", **kwargs)
    with pytest.raises(engine.InstallError, match="unsafe_agent_base_unit"):
        guards.effective_agent(
            base.replace("NoNewPrivileges=true", "NoNewPrivileges=false"), dropin, **kwargs
        )


def test_M7_04_runtime_catalog_node_scaled_and_parser_valid(tmp_path: Path) -> None:
    source = json.loads((ROOT / "runtime-compat.yaml").read_text())
    digests = {"runtime-" + entry["name"]: "sha256:" + "1" * 64 for entry in source["runtimes"]}
    small = guards.production_catalog(source, digests, 16)
    large = guards.production_catalog(source, digests, 192)
    assert (
        small["runtimes"][0]["container_resources"]["pids_limit"]
        < large["runtimes"][0]["container_resources"]["pids_limit"]
    )
    assert large["runtimes"][0]["container_resources"]["pids_limit"] > 723
    assert (
        large["runtimes"][0]["container_resources"]["nano_cpus_per_gpu"]
        > small["runtimes"][0]["container_resources"]["nano_cpus_per_gpu"]
    )
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(large))
    load_runtime_compatibility(path)
    assert all(entry["launcher"] == "container" for entry in large["runtimes"])
    with pytest.raises(engine.InstallError, match="runtime_digest_missing"):
        guards.production_catalog(source, {}, 16)


def test_M7_04_secret_generation_preserves_existing_and_pepper_pair(tmp_path: Path) -> None:
    import os

    from holm_security import load_master_keyring

    # FX-M7-04 maps production service names to the executing isolated test UID.
    identities = {name: (os.geteuid(), os.getegid()) for name in PRODUCTION_SERVICE_IDS}
    root = tmp_path / "secrets"
    root.mkdir(mode=0o700)
    arguments = dict(
        database="holm", bootstrap_user="holm_bootstrap", bootstrap_name="bootstrap-token"
    )
    created = provision_secrets(root, identities, **arguments)
    assert len(created) == 14
    load_master_keyring(root / "master_keyring", expected_gid=os.getegid())
    before = {path.name: path.read_bytes() for path in root.iterdir()}
    assert before["api_key_pepper_keyring"] == before["gateway_api_key_pepper_keyring"]
    assert provision_secrets(root, identities, **arguments) == ()
    assert before == {path.name: path.read_bytes() for path in root.iterdir()}
    (root / "gateway_api_key_pepper_keyring").unlink()
    provision_secrets(root, identities, **arguments)
    assert (
        before["api_key_pepper_keyring"] == (root / "gateway_api_key_pepper_keyring").read_bytes()
    )


def fixture_installer(tmp_path: Path):
    manifest = {
        "build_version": "FX-M7-04",
        "image_digests": [
            {"name": name, "digest": "sha256:" + "1" * 64}
            for name in ("application", "edge", "postgres", "runtime-vllm", "runtime-sglang")
        ],
        "artifact_hashes": {
            "compose": "0" * 64,
            "caddyfile": "0" * 64,
            "bundle": "0" * 64,
            "systemd_units": [],
        },
        "runtime_compat_hash": "0" * 64,
        "python_lockfile_hash": "0" * 64,
        "node_lockfile_hash": "0" * 64,
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    config = {
        "release_root": str(ROOT),
        "release_manifest": str(path),
        "public_base_url": "https://127.0.0.1:18443",
        "project_name": "holm-m704",
        "edge": {
            "address": "127.0.0.1",
            "http_port": 18080,
            "https_port": 18443,
            "tls": "internal",
        },
        "systemd": {"enable": False},
    }
    return install.Installer(config)


def test_M7_04_preflight_collects_missing_dependencies_before_mutation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    subject = fixture_installer(tmp_path)

    class Missing:
        def run(self, argv, **kwargs):
            raise engine.InstallError("missing")

    subject.executor = Missing()
    subject.state_path = tmp_path / "state.json"
    with pytest.raises(engine.InstallError, match="preflight_failed"):
        subject.preflight()
    output = capsys.readouterr().out
    for name in ("docker", "compose", "toolkit", "gpu_driver", "filesystem", "image:application"):
        assert f"preflight={name} result=fail" in output
    assert not subject.state_path.exists()


def test_M7_04_actual_compose_guard_rejects_mount_group_and_port_mutations(tmp_path: Path) -> None:
    import copy

    subject = fixture_installer(tmp_path)
    subject.details = {"platform": {"public_base_url": subject.config["public_base_url"]}}
    subject.directory = lambda *args, **kwargs: None
    subject.write = lambda *args, **kwargs: None
    subject.image_operation = lambda *args, **kwargs: {}
    subject.compose = lambda *args: json.dumps(subject.rendered).encode()
    subject.configuration()
    subject.verify_compose(subject.rendered)
    for change in ("mount", "groups", "port", "command", "network", "extra"):
        actual = copy.deepcopy(subject.rendered)
        if change == "mount":
            actual["services"]["gateway"]["volumes"][-1]["read_only"] = False
        elif change == "groups":
            actual["services"]["gateway"]["group_add"] = ["988"]
        elif change == "port":
            actual["services"]["postgres"]["ports"] = ["5432:5432"]
        elif change == "command":
            actual["services"]["maintenance-worker"]["command"] = ["sh"]
        elif change == "network":
            actual["services"]["edge"]["networks"].append("holm-runtime")
        else:
            actual["services"]["gateway"]["pid"] = "host"
        with pytest.raises(engine.InstallError):
            subject.verify_compose(actual)


def fixture_preflight_installer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import hashlib
    from types import SimpleNamespace

    subject = fixture_installer(tmp_path)
    subject.config_root = tmp_path / "config"
    subject.state_path = subject.config_root / "install-state.json"
    subject.current = tmp_path / "current"
    subject.images = {
        name: "sha256:" + str(index) * 64 for index, name in enumerate(subject.images, start=1)
    }
    subject.application = subject.images["application"]

    def tree_hash(path: Path) -> str:
        value = hashlib.sha256()
        for item in sorted(file for file in path.rglob("*") if file.is_file()):
            value.update(item.relative_to(path).as_posix().encode())
            value.update(b"\0")
            value.update(item.read_bytes())
            value.update(b"\0")
        return value.hexdigest()

    hashes = subject.manifest["artifact_hashes"]
    hashes["compose"] = tree_hash(ROOT / "deploy/compose")
    hashes["caddyfile"] = tree_hash(ROOT / "deploy/caddy")
    hashes["systemd_units"] = [
        {"name": name, "sha256": "0" * 64}
        for name in (subject.base_agent_path.name, subject.base_platform_path.name)
    ]
    monkeypatch.setattr(install, "digest", lambda path: "0" * 64)
    monkeypatch.setattr(install.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        install.platform,
        "freedesktop_os_release",
        lambda: {"ID": "ubuntu", "VERSION_ID": "24.04"},
    )
    monkeypatch.setattr(install.os, "cpu_count", lambda: 16)
    monkeypatch.setattr(
        install.shutil, "disk_usage", lambda path: SimpleNamespace(free=100 * 1024**3)
    )
    original_read = Path.read_text
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda path, *args, **kwargs: (
            "MemTotal: 16777216 kB"
            if str(path) == "/proc/meminfo"
            else original_read(path, *args, **kwargs)
        ),
    )

    class AvailableSocket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def bind(self, address):
            return None

    monkeypatch.setattr(install.socket, "socket", AvailableSocket)
    return subject


@pytest.mark.parametrize("failure", ["missing", "mismatch"])
def test_M7_04_TC_DP_11_only_digest_fails_before_persistent_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    subject = fixture_preflight_installer(tmp_path, monkeypatch)
    commands = []

    class Prerequisites:
        def run(self, argv, **kwargs):
            commands.append(argv)
            if argv[:3] == ["docker", "image", "inspect"]:
                if argv[3] == subject.application:
                    if failure == "missing":
                        raise engine.InstallError("image_missing")
                    return ("sha256:" + "f" * 64).encode()
                return argv[3].encode()
            if argv[:2] == ["docker", "ps"] or argv[:3] == ["docker", "network", "ls"]:
                return b""
            return {
                "findmnt": b"ext4",
                "timedatectl": b"yes",
                "docker": b"29.3.1",
                "nvidia-container-cli": b"1.18.0",
                "nvidia-smi": b"GPU-fixture, 590.0.0",
                "systemctl": b"systemd 255",
            }[argv[0]]

    subject.executor = Prerequisites()
    before = set(tmp_path.rglob("*"))
    with pytest.raises(engine.InstallError, match="preflight_failed"):
        subject.run()
    failures = [line for line in capsys.readouterr().out.splitlines() if "result=fail" in line]
    assert failures == ["preflight=image:application result=fail"]
    assert set(tmp_path.rglob("*")) == before
    assert not any(command[:2] in (["docker", "run"], ["docker", "create"]) for command in commands)


@pytest.mark.parametrize("time_sync", ["yes", "no", "command_failed"])
@pytest.mark.parametrize("docker_failed", [False, True])
def test_M7_04_time_sync_warning_preserves_other_preflight_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    time_sync: str,
    docker_failed: bool,
) -> None:
    # FX-M7-04: reuse valid release hashes and isolated host prerequisites.
    subject = fixture_preflight_installer(tmp_path, monkeypatch)
    continued = []

    class Prerequisites:
        def run(self, argv, **kwargs):
            if argv[0] == "timedatectl":
                if time_sync == "command_failed":
                    raise engine.InstallError("command_failed")
                return time_sync.encode()
            if argv[:3] == ["docker", "image", "inspect"]:
                return argv[3].encode()
            if argv[:2] == ["docker", "ps"] or argv[:3] == ["docker", "network", "ls"]:
                return b""
            if argv[:2] == ["docker", "version"] and docker_failed:
                raise engine.InstallError("docker_unavailable")
            return {
                "findmnt": b"ext4",
                "docker": b"29.3.1",
                "nvidia-container-cli": b"1.18.0",
                "nvidia-smi": b"GPU-fixture, 590.0.0",
                "systemctl": b"systemd 255",
            }[argv[0]]

    def describe(operation, payload):
        continued.append(operation)
        return {"identities": {}}

    subject.executor = Prerequisites()
    subject.image_operation = describe
    if docker_failed:
        with pytest.raises(engine.InstallError, match="preflight_failed"):
            subject.preflight()
        assert continued == []
    else:
        subject.preflight()
        assert continued == ["describe"]
    lines = capsys.readouterr().out.splitlines()
    time_lines = [line for line in lines if line.startswith("preflight=time_sync ")]
    if time_sync == "yes":
        assert time_lines == ["preflight=time_sync result=pass"]
    else:
        assert time_lines == [
            "preflight=time_sync result=warn remedy=Enable NTP to synchronize the host clock."
        ]
    failures = [line for line in lines if "result=fail" in line]
    assert failures == (["preflight=docker result=fail"] if docker_failed else [])


def test_M7_04_derived_catalog_hash_is_shared_without_rewriting_source(tmp_path: Path) -> None:
    import hashlib

    subject = fixture_installer(tmp_path)
    subject.details = {"platform": {"public_base_url": subject.config["public_base_url"]}}
    written = {}
    subject.directory = lambda *args, **kwargs: None
    subject.write = lambda path, content, *args: written.setdefault(path, content)
    subject.image_operation = lambda *args, **kwargs: {}
    subject.compose = lambda *args: json.dumps(subject.rendered).encode()
    original = Path(subject.config["release_manifest"]).read_bytes()
    subject.configuration()
    catalog = written[subject.config_root / "runtime-compat.yaml"]
    derived = json.loads(written[subject.config_root / "release-manifest.json"])
    assert derived["runtime_compat_hash"] == hashlib.sha256(catalog).hexdigest()
    assert written[subject.release / "source-release-manifest.json"] == original
    assert Path(subject.config["release_manifest"]).read_bytes() == original
    provenance = json.loads(written[subject.release / "install-derivation.json"])
    assert provenance["source_manifest_sha256"] == hashlib.sha256(original).hexdigest()
    assert provenance["runtime_compat_hash"] == derived["runtime_compat_hash"]


def test_M7_04_runtime_directory_refuses_unowned_agent(tmp_path: Path) -> None:
    root = tmp_path / "run"
    root.mkdir()
    assert install.safe_runtime_directory(root, owned=False)
    (root / "agent.sock.lock").touch()
    assert not install.safe_runtime_directory(root, owned=False)
    assert install.safe_runtime_directory(root, owned=True)
    link = tmp_path / "link"
    link.symlink_to(root)
    assert not install.safe_runtime_directory(link, owned=True)


def test_M7_04_real_compose_5_1_normalized_shape(tmp_path: Path) -> None:
    # FX-M7-04: 在测试宿主只读 `docker compose config` 重采，
    # 脱敏后与本夹具逐字段一致。
    # Only install-specific values are substituted below.
    subject = fixture_installer(tmp_path)
    subject.details = {"platform": {"public_base_url": subject.config["public_base_url"]}}
    subject.directory = lambda *args, **kwargs: None
    subject.write = lambda *args, **kwargs: None
    subject.image_operation = lambda *args, **kwargs: {}
    subject.compose = lambda *args: json.dumps(subject.rendered).encode()
    subject.configuration()
    actual = json.loads(
        (ROOT / "tests/fixtures/installer/FX-M7-04-compose-normalized.json").read_text()
    )
    for name, service in actual["services"].items():
        planned = subject.rendered["services"][name]
        service["image"] = planned["image"]
        if service.get("command") and "--build-version" in service["command"]:
            service["command"][service["command"].index("--build-version") + 1] = "FX-M7-04"
        original_targets = {mount["target"] for mount in service.get("volumes", [])}
        for mount in service.get("volumes", []):
            mount["source"] = next(
                value["source"]
                for value in planned["volumes"]
                if value["target"] == mount["target"]
            )
        for mount in planned.get("volumes", []):
            if mount["target"] not in original_targets:
                service.setdefault("volumes", []).append(
                    {**mount, "bind": {"create_host_path": True}}
                )
        if name == "edge":
            for port in service["ports"]:
                port["host_ip"] = subject.config["edge"]["address"]
                port["published"] = str(
                    subject.config["edge"]["http_port" if port["target"] == 8080 else "https_port"]
                )
    for name, secret in actual["secrets"].items():
        secret["file"] = subject.rendered["secrets"][name]["file"]
    subject.verify_compose(actual)
    actual["services"]["instance-controller"]["healthcheck"]["start_period"] = "1m31s"
    with pytest.raises(engine.InstallError, match="unsafe_container_healthcheck"):
        subject.verify_compose(actual)


def test_M7_04_external_root_configuration_is_one_absolute_path(tmp_path: Path) -> None:
    subject = fixture_installer(tmp_path)
    assert subject.external_model_root == Path("/var/lib/holm/external-models")
    path = tmp_path / "install.json"
    for invalid in ([], ["/one", "/two"], None, "relative"):
        path.write_text(json.dumps({**subject.config, "external_model_root": invalid}))
        with pytest.raises(engine.InstallError, match="invalid_path"):
            install.load_configuration(path)
    root = tmp_path / "FX-M7-04-external"
    path.write_text(json.dumps({**subject.config, "external_model_root": str(root)}))
    configured = install.Installer(install.load_configuration(path))
    assert configured.external_model_root == root
    with pytest.raises(engine.InstallError, match="overlaps_managed_path"):
        install.Installer({**subject.config, "external_model_root": str(subject.data_root)})


def test_M7_04_default_external_root_created_once_and_existing_data_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import stat

    subject = fixture_installer(tmp_path)
    # FX-M7-04 relocates the default directory into the isolated test filesystem.
    root = tmp_path / "external-models"
    subject.default_external_model_root = subject.external_model_root = root
    subject.details = {"identities": PRODUCTION_SERVICE_IDS}
    subject.journal = engine.Journal(tmp_path / "journal.json", {})
    ownership = []
    monkeypatch.setattr(os, "chown", lambda *args: ownership.append(args))
    assert subject.external_model_root_available()
    subject.prepare_external_model_root()
    assert root.is_dir() and list(root.iterdir()) == []
    assert stat.S_IMODE(root.stat().st_mode) == 0o750
    assert ownership == [(root, 0, PRODUCTION_SERVICE_IDS["runtime"][1])]
    assert {"kind": "directory", "name": str(root)} in subject.journal.data["created"]
    marker = root / "FX-M7-04-model"
    marker.write_bytes(b"external model sentinel")
    root.chmod(0o700)
    before = root.stat()
    subject.prepare_external_model_root()
    assert root.stat() == before
    assert marker.read_bytes() == b"external model sentinel"
    assert len(ownership) == 1


@pytest.mark.parametrize("kind", ["missing", "file", "directory"])
def test_M7_04_custom_external_root_preflight_and_zero_mutation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    subject = fixture_installer(tmp_path)
    root = tmp_path / "FX-M7-04-external"
    if kind == "file":
        root.write_bytes(b"not a directory")
    elif kind == "directory":
        root.mkdir(mode=0o700)
        (root / "model").write_bytes(b"external sentinel")
    subject = install.Installer({**subject.config, "external_model_root": str(root)})
    before = {str(p): p.stat() for p in tmp_path.rglob("*")}

    class MissingHost:
        def run(self, argv, **kwargs):
            raise engine.InstallError("FX-M7-04-host-unavailable")

    subject.executor = MissingHost()
    # run() must stop before creating its config root, journal, identities or data.
    with pytest.raises(engine.InstallError, match="preflight_failed"):
        subject.run()
    expected = "pass" if kind == "directory" else "fail"
    assert f"preflight=external_model_root result={expected}" in capsys.readouterr().out
    if kind == "directory":
        subject.prepare_external_model_root()
        assert (root / "model").read_bytes() == b"external sentinel"
    else:
        with pytest.raises(engine.InstallError, match="external_model_root_unavailable"):
            subject.prepare_external_model_root()
    assert {str(p): p.stat() for p in tmp_path.rglob("*")} == before


@pytest.mark.parametrize("custom", [False, True])
def test_M7_04_TC_DP_08_all_consumers_share_external_root_and_download_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, custom: bool
) -> None:
    import copy
    import shlex

    from holm_control_api.__main__ import _load_platform_config
    from holm_download_worker.__main__ import _parse_args as download_args
    from holm_host_agent.__main__ import _parse_args as agent_args
    from holm_maintenance_worker.__main__ import _parse_args as maintenance_args

    subject = fixture_installer(tmp_path)
    if custom:
        root = tmp_path / "FX-M7-04 external"
        root.mkdir()
        subject = install.Installer({**subject.config, "external_model_root": str(root)})
    root = subject.external_model_root
    subject.details = {"platform": {"public_base_url": subject.config["public_base_url"]}}
    written = {}
    subject.directory = lambda *args, **kwargs: None
    subject.write = lambda path, content, *args: written.setdefault(path, content)
    subject.image_operation = lambda *args, **kwargs: {}
    subject.compose = lambda *args: json.dumps(subject.rendered).encode()
    subject.configuration()
    platform_path = tmp_path / "platform.yaml"
    platform_path.write_bytes(written[subject.config_root / "platform.yaml"])
    assert _load_platform_config(platform_path).storage.external_model_roots == (root,)
    services = subject.rendered["services"]
    for name in ("control-api", "download-worker", "maintenance-worker"):
        mounts = [m for m in services[name]["volumes"] if m["target"] == str(root)]
        assert mounts == [
            {"type": "bind", "source": str(root), "target": str(root), "read_only": True}
        ]
        if name != "control-api":
            command = services[name]["command"]
            assert command.count("--external-model-root") == 1
            assert command[command.index("--external-model-root") + 1] == str(root)
        for field, value in (("target", "/wrong-root"), ("read_only", False)):
            changed = copy.deepcopy(subject.rendered)
            mount = next(
                m for m in changed["services"][name]["volumes"] if m["target"] == str(root)
            )
            mount[field] = value
            with pytest.raises(engine.InstallError, match="unsafe_container_mounts"):
                subject.verify_compose(changed)
    download = services["download-worker"]
    command = download["command"]
    socket = command[command.index("--agent-socket") + 1]
    assert download_args(command[1:]).external_model_root == [root]
    assert download_args(command[1:]).agent_socket == Path(socket)
    assert maintenance_args(services["maintenance-worker"]["command"][1:]).external_model_root == [
        root
    ]
    control = services["control-api"]
    assert socket == control["command"][control["command"].index("--agent-socket") + 1]
    assert download["group_add"] == control["group_add"] == ["10009"]
    assert [m for m in download["volumes"] if m["target"] == str(Path(socket).parent)] == [
        m for m in control["volumes"] if m["target"] == str(Path(socket).parent)
    ]
    assert not any("docker.sock" in json.dumps(s) for s in services.values())
    launcher = subject.release / "agent-venv/bin/holm-host-agent"
    agent_command, dropin = subject.agent_dropin(launcher, (988, 10008))
    assert f"ExecStart={agent_command}\n" in dropin
    monkeypatch.setattr(sys, "argv", shlex.split(agent_command))
    assert agent_args().external_model_roots == [root]
    assert (ROOT / "deploy/systemd/holm-host-agent.service").read_text() == subject.base_agent


@pytest.mark.parametrize("hostname", ["127.0.0.1", "holm.example.test"])
def test_M7_04_caddy_global_default_sni_and_skip_trust(tmp_path: Path, hostname: str) -> None:
    # FX-M7-04: render the real configuration without host writes or Docker.
    subject = fixture_installer(tmp_path)
    subject.config["public_base_url"] = f"https://{hostname}:18443"
    subject.details = {"platform": {"public_base_url": subject.config["public_base_url"]}}
    written = {}
    subject.directory = lambda *args, **kwargs: None
    subject.write = lambda path, content, *args: written.setdefault(path, content)
    subject.image_operation = lambda *args, **kwargs: {}
    subject.compose = lambda *args: json.dumps(subject.rendered).encode()
    subject.configuration()
    caddy = written[subject.config_root / "Caddyfile"].decode()
    assert caddy.startswith("{\n")
    global_lines = caddy.split("}\n", 1)[0].splitlines()
    assert f" default_sni {hostname}" in global_lines
    assert " skip_install_trust" in global_lines


def test_RT_02_production_executable_uses_image_constant_without_mutating_process_catalog() -> None:
    source = json.loads((ROOT / "runtime-compat.yaml").read_text())
    before = json.dumps(source, sort_keys=True)
    digests = {"runtime-" + entry["name"]: "sha256:" + "1" * 64 for entry in source["runtimes"]}
    catalog = guards.production_catalog(source, digests, 16)
    assert guards.RUNTIME_EXECUTABLE_DIRECTORY == "/opt/holm/runtime/bin"
    for entry in catalog["runtimes"]:
        assert entry["executable"] == f"{guards.RUNTIME_EXECUTABLE_DIRECTORY}/{entry['name']}"
        assert entry["image_digest"] == digests["runtime-" + entry["name"]]
    assert json.dumps(source, sort_keys=True) == before

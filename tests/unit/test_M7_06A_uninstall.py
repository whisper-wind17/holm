"""FX-M7-06A: isolated filesystem plus recorded commands; no host uninstall."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

INSTALLER = Path(__file__).resolve().parents[2] / "deploy/installer"


def load(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, INSTALLER / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


engine = load("engine")
uninstall = load("uninstall")


class FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.containers = {
            "com.docker.compose.project=holm": ["a" * 12],
            "holm.managed": ["b" * 12, "c" * 12],
        }
        self.networks = ["d" * 12]
        self.images = ["sha256:" + char * 64 for char in "1234"]
        self.fail = False

    def run(self, argv: list[str], *, timeout: int = 120) -> bytes:
        self.calls.append(argv)
        if self.fail:
            raise RuntimeError("FX-SECRET-SENTINEL")
        if argv[:3] == ["docker", "ps", "-a"]:
            query = argv[argv.index("--filter") + 1]
            if query.startswith("ancestor="):
                return b"eeeeeeeeeeee" if query.endswith("4" * 64) else b""
            return "\n".join(self.containers.get(query.removeprefix("label="), [])).encode()
        if argv[:3] == ["docker", "network", "ls"]:
            return "\n".join(self.networks).encode()
        if argv[:2] == ["docker", "rm"]:
            for values in self.containers.values():
                if argv[-1] in values:
                    values.remove(argv[-1])
        if argv[:3] == ["docker", "network", "rm"]:
            self.networks.remove(argv[-1])
        if argv[:3] == ["docker", "image", "ls"]:
            return "\n".join(self.images).encode()
        if argv[:3] == ["docker", "image", "inspect"]:
            return b"[]"
        if argv[:3] == ["docker", "image", "rm"]:
            self.images.remove(argv[-1])
        return b""


@pytest.fixture
def fixture_M7_06A(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    roots = tuple(tmp_path / name for name in ("etc", "data", "opt", "run"))
    for root in roots:
        root.mkdir()
    unit_root = tmp_path / "units"
    unit_root.mkdir()
    monkeypatch.setattr(uninstall, "ROOTS", roots)
    monkeypatch.setattr(uninstall, "UNIT_ROOT", unit_root)
    monkeypatch.setattr(uninstall.os, "geteuid", lambda: 0)
    config, data, program, run = roots
    external = data / "external-models"
    external.mkdir()
    (external / "weights").write_bytes(b"external-weights")
    (data / "postgres").mkdir()
    (data / "postgres/data").write_bytes(b"database")
    (config / "secrets").mkdir()
    (config / "secrets/key").write_bytes(b"FX-SECRET-SENTINEL")
    release = program / "releases/FX-M7-06A"
    (release / "agent-venv/bin").mkdir(parents=True)
    (release / "agent-venv/bin/python").symlink_to("/usr/bin/python3.12")
    (program / "current").symlink_to(release)
    (program / "holm").symlink_to("current/installer/holm")
    (run / "socket").touch()
    for name in uninstall.UNITS:
        (unit_root / name).write_text("unit")
        (unit_root / (name + ".d")).mkdir()
        (unit_root / (name + ".d/10-install.conf")).write_text("dropin")
    runner = FakeExecutor()
    (config / "release-manifest.json").write_text(
        json.dumps(
            {
                "image_digests": [
                    {"name": name, "digest": digest}
                    for name, digest in zip(
                        ("application", "edge", "postgres", "runtime-vllm"),
                        runner.images,
                        strict=True,
                    )
                ]
            }
        )
    )
    identity = {"external_model_root": str(external)}
    journal = engine.Journal(config / "install-state.json", identity)
    journal.data["completed"] = list(engine.STAGES)
    journal.data["status"] = "complete"
    journal.data["created"] = [
        {"kind": "directory", "name": str(program)},
        {"kind": "user", "name": "holm-control-api"},
        {"kind": "user", "name": "holm-download-worker"},
        {"kind": "group", "name": "holm-control-api"},
    ]
    journal.save()
    return SimpleNamespace(
        roots=roots,
        external=external,
        runner=runner,
        journal=journal,
        identity=identity,
        units=unit_root,
    )


def test_M7_06A_TC_DP_06_default_reinstall_journal_and_idempotency(fixture_M7_06A, capsys):
    f = fixture_M7_06A
    config, data, program, run = f.roots
    subject = uninstall.Uninstaller(f.runner)
    assert subject.run() == 0
    assert not program.exists() and not run.exists() and not list(f.units.iterdir())
    assert (data / "postgres/data").read_bytes() == b"database"
    assert (config / "secrets/key").read_bytes() == b"FX-SECRET-SENTINEL"
    assert f.external.joinpath("weights").read_bytes() == b"external-weights"
    assert len(f.runner.images) == 4
    assert not any(argv[0] in {"userdel", "groupdel"} for argv in f.runner.calls)
    assert sum(argv[:2] == ["docker", "rm"] for argv in f.runner.calls) == 3
    assert ["docker", "network", "rm", "d" * 12] in f.runner.calls
    for name in uninstall.UNITS:
        assert ["systemctl", "stop", name] in f.runner.calls
        assert ["systemctl", "disable", name] in f.runner.calls
    calls = list(f.runner.calls)
    assert subject.run() == 0 and f.runner.calls == calls
    assert "uninstall=no_work" in capsys.readouterr().out
    restored = engine.Journal(f.journal.path, f.identity)
    executed = []
    assert (
        restored.run(
            {stage: lambda stage=stage: executed.append(stage) for stage in engine.STAGES},
            lambda _: None,
        )
        == 0
    )
    assert executed == list(engine.STAGES)
    assert (data / "postgres/data").read_bytes() == b"database"


@pytest.mark.parametrize("custom", [False, True])
def test_M7_06A_TC_DP_08_purge_preserves_external_and_created_identities_only(
    fixture_M7_06A, monkeypatch, custom
):
    f = fixture_M7_06A
    if custom:
        external = f.roots[0].parent / "outside-models"
        f.external.rename(external)
        f.external = external
        f.journal.data["uninstall"]["external_model_root"] = str(external)
        f.journal.save()
    assert uninstall.Uninstaller(f.runner).run() == 0
    monkeypatch.setattr(uninstall.Uninstaller, "confirm", lambda _: True)
    monkeypatch.setattr(uninstall.pwd, "getpwnam", lambda _: object())
    monkeypatch.setattr(uninstall.grp, "getgrnam", lambda _: object())
    subject = uninstall.Uninstaller(f.runner, purge_data=True)
    assert subject.run() == 0
    assert f.external.joinpath("weights").read_bytes() == b"external-weights"
    assert not f.roots[0].exists() and not (f.roots[1] / "postgres").exists()
    deletion = [argv for argv in f.runner.calls if argv[0] in {"userdel", "groupdel"}]
    assert deletion == [
        ["userdel", "holm-control-api"],
        ["userdel", "holm-download-worker"],
        ["groupdel", "holm-control-api"],
    ]
    # An empty program root predating installation is intentionally not owned.
    f.roots[2].mkdir()
    assert subject.run() == 0
    assert f.roots[2].is_dir()


@pytest.mark.parametrize("terminal", ["wrong", "absent"])
def test_M7_06A_TC_DP_06_confirmation_failure_zero_delete(fixture_M7_06A, monkeypatch, terminal):
    import io

    f = fixture_M7_06A

    class Terminal(io.StringIO):
        def isatty(self):
            return True

        def readline(self):
            return "no\n"

    def open_terminal(*args, **kwargs):
        if terminal == "absent":
            raise OSError("no tty")
        return Terminal()

    monkeypatch.setattr(uninstall, "open", open_terminal, raising=False)
    before = f.journal.path.read_bytes()
    assert uninstall.Uninstaller(f.runner, purge_data=True).run() == 31
    assert f.runner.calls == [] and f.journal.path.read_bytes() == before
    assert f.roots[2].exists() and (f.roots[1] / "postgres/data").exists()


@pytest.mark.parametrize("hazard", ["root_link", "parent_link", "outside", "mount", "device"])
def test_M7_06A_TC_DP_08_guard_positive_controls(fixture_M7_06A, monkeypatch, hazard):
    f = fixture_M7_06A
    program = f.roots[2]
    if hazard == "root_link":
        moved = program.with_name("moved")
        program.rename(moved)
        program.symlink_to(moved)
    elif hazard == "parent_link":
        f.journal.data["created"].append(
            {"kind": "file", "name": str(program / "current/compose.yaml")}
        )
    elif hazard == "outside":
        f.journal.data["created"].append({"kind": "directory", "name": str(program.parent)})
    else:
        target = program / "mount"
        target.mkdir()
        (target / "keep").write_text("keep")
        if hazard == "mount":
            original = Path.is_mount
            monkeypatch.setattr(Path, "is_mount", lambda path: path == target or original(path))
        else:
            original_stat = Path.lstat

            def lstat(path):
                value = original_stat(path)
                return (
                    SimpleNamespace(st_dev=value.st_dev + 1, st_mode=value.st_mode)
                    if path == target
                    else value
                )

            monkeypatch.setattr(Path, "lstat", lstat)
    f.journal.save()
    assert uninstall.Uninstaller(f.runner).run() == 32
    assert f.runner.calls == []
    assert program.exists() and (f.roots[1] / "postgres/data").exists()


def test_M7_06A_purge_images_digest_only_and_referenced_skip(fixture_M7_06A, capsys):
    f = fixture_M7_06A
    assert uninstall.Uninstaller(f.runner, purge_images=True).run() == 0
    assert f.runner.images == ["sha256:" + "4" * 64]
    assert "reason=container_reference" in capsys.readouterr().out
    assert all(
        "--force" not in argv for argv in f.runner.calls if argv[:3] == ["docker", "image", "rm"]
    )
    assert uninstall.Uninstaller(f.runner, purge_images=True).run() == 0


def test_M7_06A_root_and_error_redaction(fixture_M7_06A, monkeypatch, capsys):
    f = fixture_M7_06A
    monkeypatch.setattr(uninstall.os, "geteuid", lambda: 123)
    assert uninstall.Uninstaller(f.runner).run() == 30
    assert not f.runner.calls
    monkeypatch.setattr(uninstall.os, "geteuid", lambda: 0)
    f.runner.fail = True
    assert uninstall.Uninstaller(f.runner).run() == 32
    assert "FX-SECRET-SENTINEL" not in capsys.readouterr().out
    assert f.roots[2].exists()


def test_M7_06A_cli_and_release_packaging(monkeypatch):
    load("guards")
    install = load("install")
    monkeypatch.setattr(sys, "argv", ["holm", "uninstall", "--purge-data", "--purge-images"])
    seen = []
    monkeypatch.setattr(
        uninstall.Uninstaller,
        "run",
        lambda self: seen.append((self.purge_data, self.purge_images)) or 0,
    )
    assert install.main() == 0 and seen == [(True, True)]
    source = (INSTALLER / "install.py").read_text()
    assert '"uninstall.py"' in source


def test_M7_06A_release_source_includes_uninstaller():
    root = INSTALLER.parents[1]
    spec = importlib.util.spec_from_file_location(
        "m706a_build", root / "deploy/build/local_build.py"
    )
    assert spec and spec.loader
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    assert Path("deploy/installer/uninstall.py") in build.source_files(root)


def test_M7_06A_failed_uninstall_can_retry(fixture_M7_06A):
    f = fixture_M7_06A
    f.runner.fail = True
    assert uninstall.Uninstaller(f.runner).run() == 32
    f.runner.fail = False
    assert uninstall.Uninstaller(f.runner).run() == 0
    assert json.loads(f.journal.path.read_text())["status"] == "uninstalled"


@pytest.mark.parametrize(
    "arguments",
    [
        ["install"],
        ["install", "--config", "file", "--purge-data"],
        ["uninstall", "--config", "file"],
    ],
)
def test_M7_06A_invalid_cli(arguments, monkeypatch):
    load("guards")
    install = load("install")
    monkeypatch.setattr(sys, "argv", ["holm", *arguments])
    with pytest.raises(SystemExit) as error:
        install.main()
    assert error.value.code == 2


def test_M7_06A_TC_DP_08_parent_replacement_cannot_redirect_unlink(fixture_M7_06A, monkeypatch):
    f = fixture_M7_06A
    directory = f.roots[2] / "replaceable"
    directory.mkdir()
    target = directory / "keep"
    target.write_text("managed")
    outside = f.roots[2].parent / "unmanaged"
    outside.mkdir()
    (outside / "keep").write_text("external")
    plan = uninstall.DeletionPlan(f.external)
    plan.add(target)
    original = plan.boundary

    def boundary(path):
        root = original(path)
        directory.rename(directory.with_name("original"))
        directory.symlink_to(outside, target_is_directory=True)
        return root

    monkeypatch.setattr(plan, "boundary", boundary)
    with pytest.raises(OSError):
        plan.execute()
    assert (outside / "keep").read_text() == "external"
    assert (directory.with_name("original") / "keep").read_text() == "managed"


@pytest.mark.parametrize("content", ["missing", "empty", "files", "zero", "link"])
def test_M7_06B_TC_DP_06_model_warning_before_confirmation(
    fixture_M7_06A, monkeypatch, capsys, content
):
    # FX-M7-06B: extend the isolated M7-06A tree; never read model contents.
    f = fixture_M7_06A
    models = f.roots[1] / "models"
    if content != "missing":
        models.mkdir()
    if content in {"files", "zero"}:
        (models / "nested").mkdir()
        (models / "nested/weights").write_bytes(b"12345" if content == "files" else b"")
        (models / "config").write_bytes(b"123" if content == "files" else b"")
    if content in {"files", "link"}:
        (models / "external-link").symlink_to(f.external, target_is_directory=True)
    before = f.journal.path.read_bytes()

    def confirm(_):
        output = capsys.readouterr().out
        assert "preserve external_model_root=" in output
        if content in {"files", "zero"}:
            assert (
                f"warning=managed_models path={models} bytes={8 if content == 'files' else 0} "
                f"remedy=Move them into the external model root {f.external} "
                "and register as a local directory to keep them."
            ) in output
        else:
            assert "warning=managed_models" not in output
        assert "FX-SECRET-SENTINEL" not in output
        return False

    monkeypatch.setattr(uninstall.Uninstaller, "confirm", confirm)
    assert uninstall.Uninstaller(f.runner, purge_data=True).run() == 31
    assert f.runner.calls == [] and f.journal.path.read_bytes() == before
    assert f.external.joinpath("weights").read_bytes() == b"external-weights"


@pytest.mark.parametrize("specific", [True, False])
def test_M7_06B_failure_code_and_redaction(fixture_M7_06A, monkeypatch, capsys, specific):
    f = fixture_M7_06A

    def fail(_):
        if specific:
            error = engine.InstallError("invalid_journal", "FX-SECRET-SENTINEL")
            error.args = ("FX-SECRET-SENTINEL",)
            raise error
        raise RuntimeError("FX-SECRET-SENTINEL")

    monkeypatch.setattr(uninstall, "read_state", fail)
    assert uninstall.Uninstaller(f.runner).run() == 32
    output = capsys.readouterr().out
    code = "invalid_journal" if specific else "uninstall_failed"
    assert f"uninstall=failed code={code} remedy=" in output
    assert "FX-SECRET-SENTINEL" not in output
    assert f.runner.calls == []


@pytest.mark.parametrize(
    "foreign,last_tag_removes_image", [(False, False), (False, True), (True, False)]
)
def test_M7_06B_multiple_image_tags(fixture_M7_06A, capsys, foreign, last_tag_removes_image):
    f = fixture_M7_06A
    digest = f.runner.images[0]
    tags = ["holm/application:v1", "holm/application:current"]
    if foreign:
        tags.append("other/application:keep")
    original = f.runner.run

    def run(argv, *, timeout=120):
        if argv[:3] == ["docker", "image", "inspect"] and argv[3] == digest:
            f.runner.calls.append(argv)
            assert argv[4:] == ["--format", "{{json .RepoTags}}"]
            return json.dumps(tags).encode()
        if argv[:3] == ["docker", "image", "rm"]:
            if argv[-1] in tags:
                f.runner.calls.append(argv)
                assert argv[-1].startswith("holm/")
                tags.remove(argv[-1])
                if not tags and last_tag_removes_image:
                    f.runner.images.remove(digest)
                return b""
            if argv[-1] == digest:
                assert not tags
        return original(argv, timeout=timeout)

    f.runner.run = run
    assert uninstall.Uninstaller(f.runner, purge_images=True).run() == 0
    assert (digest in f.runner.images) == foreign
    assert tags == (["other/application:keep"] if foreign else [])
    output = capsys.readouterr().out
    assert (f"preserve image={digest} reason=foreign_tag" in output) == foreign
    assert not any(
        "--force" in argv for argv in f.runner.calls if argv[:3] == ["docker", "image", "rm"]
    )


def test_M7_06B_invalid_tag_inventory_fails_loud(fixture_M7_06A, capsys):
    f = fixture_M7_06A
    original = f.runner.run

    def run(argv, *, timeout=120):
        if argv[:3] == ["docker", "image", "inspect"]:
            return b'["holm/application:v1", "--FX-SECRET-SENTINEL"]'
        return original(argv, timeout=timeout)

    f.runner.run = run
    assert uninstall.Uninstaller(f.runner, purge_images=True).run() == 32
    output = capsys.readouterr().out
    assert "code=invalid_image_tags" in output
    assert "FX-SECRET-SENTINEL" not in output
    assert not any(argv[:3] == ["docker", "image", "rm"] for argv in f.runner.calls)

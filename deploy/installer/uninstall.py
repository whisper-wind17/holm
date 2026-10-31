"""Plan first, unlink without following links, preserve external models."""

from __future__ import annotations

import grp
import json
import os
import pwd
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from engine import InstallError, atomic_json, install_lock

ROOTS = tuple(map(Path, ("/etc/holm", "/var/lib/holm", "/opt/holm", "/run/holm")))
UNIT_ROOT = Path("/etc/systemd/system")
UNITS = ("holm-platform.service", "holm-host-agent.service")


class Runner(Protocol):
    def run(self, argv: list[str], *, timeout: int = 120) -> bytes: ...


def absolute(value: str) -> Path:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or any(part in {".", ".."} for part in value.split("/"))
        or any(ord(char) < 32 for char in value)
    ):
        raise InstallError("unsafe_uninstall_path")
    return Path(value)


def read_state(path: Path) -> dict:
    if (
        path.is_symlink()
        or path.stat().st_uid != os.geteuid()
        or stat.S_IMODE(path.stat().st_mode) != 0o600
    ):
        raise InstallError("unsafe_journal")
    value = json.loads(path.read_text())
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or not isinstance(value.get("created"), list)
        or not isinstance(value.get("uninstall"), dict)
    ):
        raise InstallError("invalid_journal")
    return value


@contextmanager
def parent_descriptor(path: Path) -> Iterator[int]:
    # Open each ancestor separately so a replaced parent link cannot redirect
    # unlink/rmdir to an external directory between validation and deletion.
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open("/", flags)
    try:
        for part in path.parent.parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


class DeletionPlan:
    def __init__(self, external: Path) -> None:
        self.external = external
        self.paths: list[Path] = []
        self.seen: set[Path] = set()
        self.units = {UNIT_ROOT / name for name in UNITS} | {
            UNIT_ROOT / (name + ".d") for name in UNITS
        }
        for root in ROOTS:
            if root.is_symlink() or root.parent.resolve() != root.parent:
                raise InstallError("unsafe_managed_root")

    def boundary(self, path: Path) -> Path:
        absolute(str(path))
        for root in (*ROOTS, *self.units):
            if path == root or path.is_relative_to(root):
                if path.parent.resolve() != path.parent:
                    raise InstallError("unsafe_uninstall_parent")
                return root
        raise InstallError("unmanaged_uninstall_path")

    def add(self, path: Path, device: int | None = None) -> bool:
        root = self.boundary(path)
        # Never enumerate, resolve or mutate the external root, even if mounted.
        if path == self.external or path.is_relative_to(self.external):
            return False
        if path in self.seen:
            return path in self.paths
        self.seen.add(path)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return True
        if device is None:
            device = root.lstat().st_dev
        if stat.S_ISDIR(metadata.st_mode):
            if metadata.st_dev != device or path.is_mount():
                raise InstallError("uninstall_mount_point")
            removable = True
            for child in sorted(path.iterdir()):
                removable = self.add(child, device) and removable
            if not removable:
                return False
        self.paths.append(path)
        return True

    def execute(self) -> None:
        # Postorder rmdir/unlink never follows a directory symlink or traverses a
        # newly appeared child. Recheck parents in case services changed them.
        for path in self.paths:
            self.boundary(path)
            try:
                with parent_descriptor(path) as parent:
                    metadata = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                    if stat.S_ISDIR(metadata.st_mode):
                        if metadata.st_dev != os.fstat(parent).st_dev or path.is_mount():
                            raise InstallError("uninstall_mount_point")
                        os.rmdir(path.name, dir_fd=parent)
                    else:
                        os.unlink(path.name, dir_fd=parent)
            except FileNotFoundError:
                pass


class Uninstaller:
    def __init__(
        self, executor: Runner, *, purge_data: bool = False, purge_images: bool = False
    ) -> None:
        self.executor = executor
        self.purge_data = purge_data
        self.purge_images = purge_images
        self.config_root, self.data_root, self.program_root, self.run_root = ROOTS
        self.state_path = self.config_root / "install-state.json"

    def identifiers(self, argv: list[str]) -> list[str]:
        values = self.executor.run(argv).decode().split()
        if any(not re.fullmatch(r"[0-9a-f]{12,64}", value) for value in values):
            raise InstallError("invalid_docker_inventory")
        return values

    def confirm(self) -> bool:
        try:
            # Buffered read/write mode requires seeking, which terminals cannot do.
            with open("/dev/tty") as reader, open("/dev/tty", "w") as writer:
                if not reader.isatty() or not writer.isatty():
                    return False
                writer.write("Type purge to delete the listed data and identities: ")
                writer.flush()
                return reader.readline().strip() == "purge"
        except OSError:
            return False

    def run(self) -> int:
        if os.geteuid() != 0:
            print("uninstall=refused code=root_required")
            return 30
        try:
            # Check the roots before opening the lock or reading any journal.
            DeletionPlan(self.data_root / "external-models")
            if not self.state_path.exists():
                leftovers = any(
                    root.exists() and (not root.is_dir() or any(root.iterdir()))
                    for root in (self.config_root, self.program_root, self.run_root)
                )
                if self.data_root.exists():
                    leftovers = leftovers or any(
                        child.name != "external-models" for child in self.data_root.iterdir()
                    )
                if leftovers:
                    raise InstallError("installation_journal_missing")
                print("uninstall=no_work")
                return 0
            with install_lock(self.config_root / "install.lock"):
                return self.perform(read_state(self.state_path))
        except (Exception, KeyboardInterrupt) as error:
            # Never print raw subprocess, filesystem, manifest or journal errors.
            code = error.code if isinstance(error, InstallError) else "uninstall_failed"
            print(
                f"uninstall=failed code={code} "
                "remedy=Check installation state and dependencies, then retry."
            )
            return 32

    def perform(self, state: dict) -> int:
        metadata = state["uninstall"]
        project = metadata.get("project")
        if project not in {"holm", "holm-m704"}:
            raise InstallError("invalid_uninstall_project")
        external = absolute(metadata.get("external_model_root"))
        default_external = self.data_root / "external-models"
        if external != default_external and any(
            external.is_relative_to(root) or root.is_relative_to(external) for root in ROOTS
        ):
            raise InstallError("invalid_external_model_root")
        plan = DeletionPlan(external)
        program_paths: set[Path] = set()
        identities: dict[str, set[str]] = {"user": set(), "group": set()}
        for item in state["created"]:
            kind, name = item["kind"], item["name"]
            if kind in identities:
                if not re.fullmatch(r"holm-[a-z][a-z-]*", name):
                    raise InstallError("invalid_uninstall_identity")
                identities[kind].add(name)
            elif kind in {"directory", "file", "symlink"}:
                path = absolute(name)
                plan.boundary(path)
                if path.is_relative_to(self.program_root):
                    program_paths.add(path)
            elif kind not in {"unit", "container", "network"}:
                raise InstallError("invalid_object_kind")
        # Ancestors are expanded once; journal entries beneath current/venv
        # symlinks have already been rejected by boundary(), not followed.
        for path in sorted(program_paths, key=lambda value: len(value.parts)):
            plan.add(path)
        for name in UNITS:
            plan.add(UNIT_ROOT / (name + ".d"))
            plan.add(UNIT_ROOT / name)
        plan.add(self.run_root)
        if self.purge_data:
            plan.add(self.data_root)
            plan.add(self.config_root)
        images: list[str] = []
        if self.purge_images:
            manifest = self.config_root / "release-manifest.json"
            if manifest.is_symlink():
                raise InstallError("unsafe_manifest")
            images = [item["digest"] for item in json.loads(manifest.read_text())["image_digests"]]
            if not images or any(
                not re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in images
            ):
                raise InstallError("invalid_image_manifest")
        if self.purge_data:
            for path in plan.paths:
                print(f"delete path={path}")
            for kind, names in identities.items():
                for name in sorted(names):
                    print(f"delete {kind}={name}")
            print(f"preserve external_model_root={external}")
            models = self.data_root / "models"
            if models in plan.paths:
                sizes = [
                    metadata.st_size
                    for path in plan.paths
                    if path.is_relative_to(models)
                    and stat.S_ISREG((metadata := path.lstat()).st_mode)
                ]
                if sizes:
                    print(
                        f"warning=managed_models path={models} bytes={sum(sizes)} "
                        f"remedy=Move them into the external model root {external} "
                        "and register as a local directory to keep them."
                    )
            if not self.confirm():
                print("uninstall=refused code=purge_confirmation_required")
                return 31
        if state.get("status") == "uninstalled" and not plan.paths and not images:
            print("uninstall=no_work")
            return 0
        # Reset progress before removing programs: an interrupted uninstall must
        # never leave install believing the removed stages are complete.
        state["completed"] = []
        state["status"] = "uninstalling"
        state.pop("failed_stage", None)
        atomic_json(self.state_path, state)
        for name in UNITS:
            if (UNIT_ROOT / name).exists():
                self.executor.run(["systemctl", "stop", name])
                self.executor.run(["systemctl", "disable", name])
        containers: set[str] = set()
        for label in (f"com.docker.compose.project={project}", "holm.managed"):
            containers.update(
                self.identifiers(
                    ["docker", "ps", "-a", "--filter", f"label={label}", "--format", "{{.ID}}"]
                )
            )
        for identifier in sorted(containers):
            self.executor.run(["docker", "rm", "--force", identifier])
        for identifier in self.identifiers(
            [
                "docker",
                "network",
                "ls",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--format",
                "{{.ID}}",
            ]
        ):
            self.executor.run(["docker", "network", "rm", identifier])
        for image in sorted(set(images)):
            referenced = self.identifiers(
                ["docker", "ps", "-a", "--filter", f"ancestor={image}", "--format", "{{.ID}}"]
            )
            if referenced:
                print(f"preserve image={image} reason=container_reference")
            elif (
                image
                in self.executor.run(["docker", "image", "ls", "--quiet", "--no-trunc"])
                .decode()
                .split()
            ):
                tags = json.loads(
                    self.executor.run(
                        ["docker", "image", "inspect", image, "--format", "{{json .RepoTags}}"]
                    )
                )
                if not isinstance(tags, list) or any(
                    not isinstance(tag, str)
                    or not re.fullmatch(
                        r"[a-zA-Z0-9][a-zA-Z0-9._:/-]*(?:@sha256:[0-9a-f]{64})?", tag
                    )
                    for tag in tags
                ):
                    raise InstallError("invalid_image_tags")
                for tag in sorted(set(tags)):
                    if tag.startswith("holm/"):
                        self.executor.run(["docker", "image", "rm", tag])
                if any(not tag.startswith("holm/") for tag in tags):
                    print(f"preserve image={image} reason=foreign_tag")
                # Removing the last tag may already remove the image itself.
                elif (
                    image
                    in self.executor.run(["docker", "image", "ls", "--quiet", "--no-trunc"])
                    .decode()
                    .split()
                ):
                    self.executor.run(["docker", "image", "rm", image])
        if self.purge_data:
            for kind, lookup, command in (
                ("user", pwd.getpwnam, "userdel"),
                ("group", grp.getgrnam, "groupdel"),
            ):
                for name in sorted(identities[kind]):
                    try:
                        lookup(name)
                    except KeyError:
                        continue
                    self.executor.run([command, name])
        # Services can remove sockets as they stop. Replan fully before unlinking.
        checked = DeletionPlan(external)
        for path in plan.paths:
            checked.add(path)
        checked.execute()
        self.executor.run(["systemctl", "daemon-reload"])
        if not self.purge_data:
            state["status"] = "uninstalled"
            atomic_json(self.state_path, state)
        print("uninstall=complete")
        return 0

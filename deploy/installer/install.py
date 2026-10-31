#!/usr/bin/env python3
"""Holm install --config FILE (Python 3.12 standard library)."""

from __future__ import annotations

import argparse
import copy
import grp
import hashlib
import http.client
import ipaddress
import json
import os
import platform
import pwd
import re
import shlex
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import venv
from pathlib import Path

from engine import InstallError, Journal, install_lock
from guards import (
    effective_agent,
    effective_platform,
    production_catalog,
    unit_values,
    verify_rendered_compose,
)


class Executor:
    def run(self, argv: list[str], *, payload: bytes | None = None, timeout: int = 120) -> bytes:
        try:
            result = subprocess.run(
                argv,
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise InstallError(
                "command_unavailable", "Check the required program and its service, then rerun."
            ) from None
        if result.returncode:
            raise InstallError(
                "command_failed",
                "Check prerequisite versions, permissions and service health, then rerun.",
            )
        return result.stdout


def document(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        raise InstallError("invalid_document") from None
    if not isinstance(value, dict):
        raise InstallError("invalid_document")
    return value


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def certificate_fingerprint(path: Path) -> str:
    """Hash the public root certificate's DER, never a key or PEM text."""
    try:
        pem = path.read_text(encoding="ascii")
        # PEM_cert_to_DER_cert only decodes base64; OpenSSL also checks X.509 syntax.
        ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=pem)
        der = ssl.PEM_cert_to_DER_cert(pem)
    except (OSError, ValueError, UnicodeError):
        raise InstallError(
            "internal_ca_unavailable",
            "Check the internal root certificate and rerun the same install command.",
        ) from None
    return hashlib.sha256(der).digest().hex(":").upper()


def require_absolute(path: str) -> Path:
    if not isinstance(path, str):
        raise InstallError("invalid_path")
    result = Path(path)
    if (
        not result.is_absolute()
        or any(part in {"..", "."} for part in result.parts)
        or any(char in str(result) for char in "\n\r\x00%")
    ):
        raise InstallError("invalid_path")
    for ancestor in (result, *result.parents):
        if ancestor.is_symlink():
            raise InstallError("symlink_path")
    return result


def load_configuration(path: Path) -> dict:
    config = document(path)
    allowed = {
        "release_root",
        "release_manifest",
        "data_root",
        "external_model_root",
        "public_base_url",
        "edge",
        "systemd",
        "project_name",
    }
    if set(config) - allowed or not {"release_root", "release_manifest", "public_base_url"} <= set(
        config
    ):
        raise InstallError("invalid_install_configuration")
    for key in ("release_root", "release_manifest", "data_root", "external_model_root"):
        if key in config:
            require_absolute(config[key])
    edge = config.setdefault(
        "edge", {"address": "0.0.0.0", "http_port": 80, "https_port": 443, "tls": "internal"}
    )
    common = {"address", "http_port", "https_port", "tls"}
    if not isinstance(edge, dict) or edge.get("tls") not in ("internal", "acme", "provided"):
        raise InstallError("invalid_edge_configuration")
    required = common | (
        {"certificate_file", "private_key_file"} if edge["tls"] == "provided" else set()
    )
    optional = {"acme_email"} if edge["tls"] == "acme" else set()
    if not required <= set(edge) or set(edge) - required - optional:
        raise InstallError("invalid_edge_configuration")
    if "acme_email" in edge and (
        not isinstance(edge["acme_email"], str)
        or not re.fullmatch(r"[a-zA-Z0-9_.+%-]+@[a-zA-Z0-9.-]+", edge["acme_email"])
    ):
        raise InstallError("invalid_acme_email")
    if edge["tls"] == "provided":
        for key in ("certificate_file", "private_key_file"):
            require_absolute(edge[key])
    try:
        ipaddress.IPv4Address(edge["address"])
    except ValueError:
        raise InstallError("invalid_edge_address") from None
    if (
        any(
            type(edge[key]) is not int or not 1 <= edge[key] <= 65535
            for key in ("http_port", "https_port")
        )
        or edge["http_port"] == edge["https_port"]
    ):
        raise InstallError("invalid_edge_port")
    systemd = config.setdefault("systemd", {"enable": True})
    if set(systemd) != {"enable"} or type(systemd["enable"]) is not bool:
        raise InstallError("invalid_systemd_configuration")
    from urllib.parse import urlsplit

    url = urlsplit(config["public_base_url"])
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in {"", "/"}
    ):
        raise InstallError("invalid_public_url")
    if not re.fullmatch(r"[a-zA-Z0-9.-]+", url.hostname):
        raise InstallError("invalid_public_hostname")
    if (url.port or 443) != edge["https_port"]:
        raise InstallError("public_port_mismatch")
    if edge["tls"] == "acme":
        try:
            ipaddress.ip_address(url.hostname)
        except ValueError:
            pass
        else:
            raise InstallError("acme_requires_domain")
        if edge["http_port"] != 80 and edge["https_port"] != 443:
            raise InstallError("acme_challenge_port_unavailable")
    return config


def safe_runtime_directory(path: Path, *, owned: bool) -> bool:
    if path.is_symlink():
        return False
    if not path.exists():
        return True
    return path.is_dir() and (owned or not any(path.iterdir()))


class Installer:
    def __init__(self, config: dict, executor: Executor | None = None) -> None:
        self.config = config
        self.executor = executor or Executor()
        self.source = Path(config["release_root"])
        compose_text = (self.source / "deploy/compose/compose.prod.yaml").read_text()
        self.base = json.loads(
            "\n".join(
                line for line in compose_text.splitlines() if not line.lstrip().startswith("#")
            )
        )
        self.manifest = document(Path(config["release_manifest"]))
        self.identity_config = {
            **config,
            "source_manifest_sha256": digest(Path(config["release_manifest"])),
        }
        self.slug = self.base["name"]
        self.project = config.get("project_name", self.slug)
        if self.project not in {self.slug, self.slug + "-m704"}:
            raise InstallError("invalid_project_name")
        self.base_agent_path = self.source / "deploy/systemd" / (self.slug + "-host-agent.service")
        self.base_platform_path = self.source / "deploy/systemd" / (self.slug + "-platform.service")
        self.base_agent = self.base_agent_path.read_text()
        agent = unit_values(self.base_agent)
        platform_unit = unit_values(self.base_platform_path.read_text())
        self.program_root = Path(agent["ExecStart"]).parents[2]
        environments = dict(
            item.split("=", 1) for item in shlex.split(platform_unit["Environment"])
        )
        self.config_root = require_absolute(environments["HOLM_CONFIG_ROOT"])
        self.data_root = require_absolute(config.get("data_root", environments["HOLM_DATA_ROOT"]))
        if str(self.data_root) != environments["HOLM_DATA_ROOT"]:
            raise InstallError(
                "custom_data_root_not_supported",
                "Use the production data root; Runtime cache currently has a fixed consumer path.",
            )
        self.run_root = Path(agent["ReadWritePaths"])
        # The production Compose command is the default path definition source.
        download_command = self.base["services"]["download-worker"]["command"]
        root_template = download_command[download_command.index("--external-model-root") + 1]
        match = re.fullmatch(r"\$\{HOLM_EXTERNAL_MODEL_ROOT:-([^}]+)\}", root_template)
        if match is None:
            raise InstallError("invalid_external_model_root_template")
        self.default_external_model_root = require_absolute(match[1])
        self.external_model_root = require_absolute(
            config.get("external_model_root", str(self.default_external_model_root))
        )
        if any(char in str(self.external_model_root) for char in "\"'\\$"):
            raise InstallError("invalid_external_model_root")
        # A custom read-only root cannot also contain installer-owned writable data.
        if self.external_model_root != self.default_external_model_root and any(
            self.external_model_root.is_relative_to(root)
            or root.is_relative_to(self.external_model_root)
            for root in (self.config_root, self.data_root, self.program_root, self.run_root)
        ):
            raise InstallError("external_model_root_overlaps_managed_path")
        self.agent_name = agent["User"]
        self.secrets_root = self.config_root / "secrets"
        self.cache_root = self.data_root / "runtime-cache"
        version = self.manifest.get("build_version", "")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.+_-]*", version):
            raise InstallError("invalid_release_version")
        self.release = self.program_root / "releases" / version
        self.current = self.program_root / "current"
        self.state_path = self.config_root / "install-state.json"
        self.helper = Path(__file__).with_name("image_helper.py")
        self.images = {entry["name"]: entry["digest"] for entry in self.manifest["image_digests"]}
        self.application = self._image_for("control-api")
        self.details: dict = {}
        self.journal: Journal | None = None
        self.rendered: dict = {}
        self.bootstrap_status = "unknown"
        self.gpu_count = 1

    def _image_for(self, service: str) -> str:
        tag = self.base["services"][service]["image"]
        candidates = (
            tag,
            service,
            "application" if service not in {"edge", "postgres"} else service,
        )
        matches = {self.images[name] for name in candidates if name in self.images}
        if len(matches) != 1:
            raise InstallError("image_manifest_mapping_missing")
        return matches.pop()

    def image_operation(
        self,
        action: str,
        payload: dict,
        *,
        network: str = "none",
        secrets_mount: bool = False,
        user: str = "0:0",
        groups: tuple[int, ...] = (),
        secret_file: Path | None = None,
        tls_files: tuple[Path, Path] | None = None,
    ) -> dict:
        command = [
            "docker",
            "run",
            "--rm",
            "--log-driver",
            "none",
            "--network",
            network,
            "--user",
            user,
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=16m",
            "--security-opt",
            "no-new-privileges:true",
            "--mount",
            f"type=bind,src={self.helper},dst=/installer-helper.py,readonly",
        ]
        if secret_file is not None:
            command += [
                "--mount",
                f"type=bind,src={secret_file},dst=/run/secrets/validate,readonly",
            ]
        if tls_files is not None:
            for source, target in zip(tls_files, ("certificate", "private-key"), strict=True):
                command += ["--mount", f"type=bind,src={source},dst=/run/tls/{target},readonly"]
        if secrets_mount:
            command += ["--mount", f"type=bind,src={self.secrets_root},dst={self.secrets_root}"]
        if action == "smoke":
            command += ["--mount", f"type=bind,src={self.run_root},dst={self.run_root},readonly"]
        for group in groups:
            command += ["--group-add", str(group)]
        command += [
            "-i",
            "--entrypoint",
            "python",
            self.application,
            "/installer-helper.py",
            action,
        ]
        raw = self.executor.run(command, payload=json.dumps(payload).encode(), timeout=180)
        try:
            result = json.loads(raw)
        except ValueError:
            raise InstallError("invalid_image_operation_result") from None
        if not isinstance(result, dict):
            raise InstallError("invalid_image_operation_result")
        return result

    def owned_port(self, port: int) -> bool:
        if not self.state_path.exists():
            return False
        state = document(self.state_path)
        if (
            state.get("configuration_sha256")
            != hashlib.sha256(json.dumps(self.identity_config, sort_keys=True).encode()).hexdigest()
        ):
            return False
        raw = self.executor.run(
            [
                "docker",
                "ps",
                "--filter",
                f"label=com.docker.compose.project={self.project}",
                "--filter",
                "label=com.docker.compose.service=edge",
                "--format",
                "{{.ID}}",
            ]
        )
        identifiers = raw.decode().split()
        if len(identifiers) != 1:
            return False
        inspected = json.loads(self.executor.run(["docker", "inspect", identifiers[0]]))[0]
        if inspected["Image"] != self._image_for("edge"):
            return False
        return any(
            binding["HostPort"] == str(port) and binding["HostIp"] == self.config["edge"]["address"]
            for bindings in inspected["NetworkSettings"]["Ports"].values()
            if bindings
            for binding in bindings
        )

    def preflight(self) -> None:
        results: list[tuple[str, bool]] = []

        def check(name: str, operation) -> None:
            try:
                good = bool(operation())
            except Exception:
                good = False
            results.append((name, good))

        def project_owned() -> bool:
            observed = self.executor.run(
                [
                    "docker",
                    "ps",
                    "-a",
                    "--filter",
                    f"label=com.docker.compose.project={self.project}",
                    "--format",
                    "{{.ID}}",
                ]
            )
            networks = self.executor.run(
                [
                    "docker",
                    "network",
                    "ls",
                    "--filter",
                    f"label=com.docker.compose.project={self.project}",
                    "--format",
                    "{{.ID}}",
                ]
            )
            allowed = set()
            if self.state_path.exists():
                state = document(self.state_path)
                allowed = {
                    item["name"]
                    for item in state.get("created", [])
                    if item["kind"] in {"container", "network"}
                }
            return set(observed.decode().split() + networks.decode().split()) <= allowed

        def journal_matches() -> bool:
            Journal(self.state_path, self.identity_config)
            return True

        check("installation_identity", journal_matches)
        check("project_ownership", project_owned)

        def units_owned() -> bool:
            paths = [
                Path("/etc/systemd/system") / name
                for name in (self.base_agent_path.name, self.base_platform_path.name)
            ]
            for unit in (self.base_agent_path.name, self.base_platform_path.name):
                for prefix in (
                    "/etc/systemd/system",
                    "/run/systemd/system",
                    "/usr/lib/systemd/system",
                ):
                    directory = Path(prefix) / (unit + ".d")
                    if directory.is_symlink():
                        return False
                    if directory.exists():
                        paths.extend(directory.iterdir())
            allowed = set()
            if self.state_path.exists():
                allowed = {
                    item["name"]
                    for item in document(self.state_path).get("created", [])
                    if item["kind"] == "file"
                }
            return all(
                not path.is_symlink() and (not path.exists() or str(path) in allowed)
                for path in paths
            )

        check("unit_ownership", units_owned)

        def runtime_directory_owned() -> bool:
            owned = False
            if self.state_path.exists():
                owned = {"kind": "unit", "name": self.base_agent_path.name} in document(
                    self.state_path
                ).get("created", [])
            return safe_runtime_directory(self.run_root, owned=owned)

        check("runtime_directory_ownership", runtime_directory_owned)
        check(
            "current_release",
            lambda: (
                not self.current.exists()
                or self.current.is_symlink()
                and self.current.resolve() == self.release
            ),
        )
        check("root", lambda: os.geteuid() == 0)
        check("architecture", lambda: platform.machine() == "x86_64")
        check("python", lambda: sys.version_info[:2] == (3, 12))
        check(
            "os",
            lambda: (
                platform.freedesktop_os_release().get("ID") == "ubuntu"
                and platform.freedesktop_os_release().get("VERSION_ID") in {"22.04", "24.04"}
            ),
        )
        check("external_model_root", self.external_model_root_available)
        check("cpu", lambda: (os.cpu_count() or 0) >= 4)
        check(
            "memory",
            lambda: (
                int(re.search(r"MemTotal:\s+(\d+)", Path("/proc/meminfo").read_text())[1])
                >= 8 * 1024**2
            ),
        )
        ancestor = self.data_root
        while not ancestor.exists():
            ancestor = ancestor.parent
        check(
            "filesystem",
            lambda: (
                self.executor.run(["findmnt", "-n", "-o", "FSTYPE", "-T", str(ancestor)])
                .decode()
                .strip()
                in {"ext4", "xfs"}
            ),
        )
        check("space", lambda: shutil.disk_usage(ancestor).free >= 20 * 1024**3)
        check(
            "time_sync",
            lambda: (
                self.executor.run(
                    ["timedatectl", "show", "--property=NTPSynchronized", "--value"]
                ).strip()
                == b"yes"
            ),
        )

        def minimum(argv: list[str], required: tuple[int, ...]) -> bool:
            match = re.search(rb"(\d+)\.(\d+)\.(\d+)", self.executor.run(argv))
            return match is not None and tuple(map(int, match.groups())) >= required

        check(
            "docker",
            lambda: minimum(["docker", "version", "--format", "{{.Server.Version}}"], (24, 0, 0)),
        )
        check("compose", lambda: minimum(["docker", "compose", "version", "--short"], (2, 20, 0)))
        check("toolkit", lambda: minimum(["nvidia-container-cli", "--version"], (1, 14, 0)))

        def gpu_inventory() -> bool:
            lines = (
                self.executor.run(
                    ["nvidia-smi", "--query-gpu=uuid,driver_version", "--format=csv,noheader"]
                )
                .decode()
                .strip()
                .splitlines()
            )
            self.gpu_count = len(lines)
            return bool(lines) and all(
                re.fullmatch(r"GPU-[a-zA-Z0-9-]+,\s*[0-9.]+", line) for line in lines
            )

        check("gpu_driver", gpu_inventory)
        check("systemd", lambda: bool(self.executor.run(["systemctl", "--version"])))
        for key in ("http_port", "https_port"):

            def available(port=self.config["edge"][key]) -> bool:
                try:
                    with socket.socket() as probe:
                        probe.bind((self.config["edge"]["address"], port))
                    return True
                except OSError:
                    return self.owned_port(port)

            check(key, available)
        # Every manifest image, including dynamic Runtimes, is checked before any
        # users, journal, directories, Secrets, networks or persistent data exist.
        for name, expected in self.images.items():
            check(
                "image:" + name,
                lambda expected=expected: (
                    bool(re.fullmatch(r"sha256:[0-9a-f]{64}", expected))
                    and self.executor.run(
                        ["docker", "image", "inspect", expected, "--format", "{{.Id}}"]
                    )
                    .decode()
                    .strip()
                    == expected
                ),
            )

        def tree_hash(path: Path) -> str:
            value = hashlib.sha256()
            files = sorted(item for item in path.rglob("*") if item.is_file())
            if not files:
                raise InstallError("release_assets_missing")
            for item in files:
                value.update(item.relative_to(path).as_posix().encode())
                value.update(b"\0")
                value.update(item.read_bytes())
                value.update(b"\0")
            return value.hexdigest()

        hashes = self.manifest["artifact_hashes"]
        check(
            "compose_hash", lambda: tree_hash(self.source / "deploy/compose") == hashes["compose"]
        )
        check("caddy_hash", lambda: tree_hash(self.source / "deploy/caddy") == hashes["caddyfile"])
        check(
            "runtime_catalog_hash",
            lambda: (
                digest(self.source / "runtime-compat.yaml") == self.manifest["runtime_compat_hash"]
            ),
        )
        check(
            "python_lock_hash",
            lambda: digest(self.source / "uv.lock") == self.manifest["python_lockfile_hash"],
        )
        check(
            "node_lock_hash",
            lambda: (
                digest(self.source / "package-lock.json") == self.manifest["node_lockfile_hash"]
            ),
        )
        check(
            "bundle_hash",
            lambda: digest(self.source / "build/m0-02/SHA256SUMS") == hashes["bundle"],
        )
        check(
            "unit_hash_set",
            lambda: (
                {item["name"] for item in hashes["systemd_units"]}
                == {self.base_agent_path.name, self.base_platform_path.name}
            ),
        )
        for unit in hashes["systemd_units"]:
            check(
                "unit_hash:" + unit["name"],
                lambda unit=unit: (
                    Path(unit["name"]).name == unit["name"]
                    and digest(self.source / "deploy/systemd" / unit["name"]) == unit["sha256"]
                ),
            )
        for name, good in results:
            if name == "time_sync" and not good:
                print(
                    "preflight=time_sync result=warn remedy=Enable NTP to synchronize the host clock."
                )
            else:
                print(f"preflight={name} result={'pass' if good else 'fail'}")
        if any(not good and name != "time_sync" for name, good in results):
            raise InstallError(
                "preflight_failed",
                "Resolve all failed prerequisites; install missing host dependencies as administrator, then rerun.",
            )
        if self.config["edge"]["tls"] == "provided":
            self.validate_provided_tls()
        self.details = self.image_operation(
            "describe",
            {
                "manifest": self.manifest,
                "platform": {"public_base_url": self.config["public_base_url"]},
            },
        )
        for name, pair in self.details["identities"].items():
            declared = next(
                (
                    entry
                    for entry in self.manifest["service_identities"]
                    if entry["service"] == name
                ),
                None,
            )
            if declared is None or [declared["uid"], declared["gid"]] != pair:
                raise InstallError("manifest_identity_mismatch")

    def validate_provided_tls(self) -> tuple[bytes, bytes]:
        """Validate through read-only mounts; only fingerprints cross the helper boundary."""
        from urllib.parse import urlsplit

        paths = tuple(
            require_absolute(self.config["edge"][key])
            for key in ("certificate_file", "private_key_file")
        )
        try:
            for path in paths:
                if not stat.S_ISREG(path.lstat().st_mode):
                    raise InstallError("tls_invalid_file")
            key_stat = paths[1].stat()
            if key_stat.st_uid != 0 or key_stat.st_mode & 0o077:
                raise InstallError("tls_private_key_permissions")
            result = self.image_operation(
                "validate-tls",
                {"hostname": urlsplit(self.config["public_base_url"]).hostname},
                tls_files=paths,
            )
            if result.get("error") is not None:
                code = result["error"]
                if code not in {
                    "tls_invalid_material",
                    "tls_hostname_mismatch",
                    "tls_not_valid",
                    "tls_key_mismatch",
                }:
                    code = "tls_validation_failed"
                raise InstallError(code)
            contents = []
            for path, field in zip(
                paths, ("certificate_sha256", "private_key_sha256"), strict=True
            ):
                with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as handle:
                    content = handle.read()
                if hashlib.sha256(content).hexdigest() != result.get(field):
                    raise InstallError("tls_source_changed")
                contents.append(content)
            self.tls_leaf = bytes.fromhex(result["leaf_der"])
            if result["expires_soon"]:
                print("warning=tls_certificate_expires_within_30_days")
            return contents[0], contents[1]
        except (OSError, ValueError, KeyError, TypeError):
            raise InstallError(
                "tls_validation_failed", "Check certificate and private key files, then rerun."
            ) from None

    def install_provided_tls(self) -> None:
        # Revalidate before replacing either managed file; source files are never modified.
        certificate, key = self.validate_provided_tls()
        root = require_absolute(str(self.data_root / "caddy/tls"))
        uid, gid = self.details["identities"]["edge"]
        for name, content in (("certificate.pem", certificate), ("private-key.pem", key)):
            target = require_absolute(str(root / name))
            if target.exists() and not target.is_file():
                raise InstallError("tls_invalid_file")
            fd, temporary = tempfile.mkstemp(prefix=".tls-", dir=root)
            try:
                with os.fdopen(fd, "wb") as handle:
                    os.fchmod(handle.fileno(), 0o400)
                    os.fchown(handle.fileno(), uid, gid)
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
                self.record("file", target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def record(self, kind: str, name: Path | str) -> None:
        if self.journal is not None:
            self.journal.created(kind, str(name))

    def directory(self, path: Path, uid: int, gid: int, mode: int) -> None:
        require_absolute(str(path))
        if path.exists():
            observed = path.stat()
            if (
                not path.is_dir()
                or observed.st_uid != uid
                or observed.st_gid != gid
                or stat.S_IMODE(observed.st_mode) != mode
            ):
                raise InstallError(
                    "existing_directory_permissions",
                    "Inspect ownership and use an empty dedicated product directory; existing data is never silently re-owned.",
                )
            return
        path.mkdir(mode=mode)
        os.chown(path, uid, gid)
        os.chmod(path, mode)
        self.record("directory", path)

    def write(self, path: Path, content: bytes, mode: int = 0o644) -> None:
        if path.exists() or path.is_symlink():
            if (
                path.is_symlink()
                or not path.is_file()
                or path.read_bytes() != content
                or stat.S_IMODE(path.stat().st_mode) != mode
            ):
                raise InstallError(
                    "existing_file_differs",
                    "Use the original release/configuration; this installer does not perform upgrades.",
                )
            return
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, mode)
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(content)
        self.record("file", path)

    def external_model_root_available(self) -> bool:
        root = require_absolute(str(self.external_model_root))
        return root.is_dir() or (root == self.default_external_model_root and not root.exists())

    def prepare_external_model_root(self) -> None:
        if not self.external_model_root_available():
            raise InstallError("external_model_root_unavailable")
        if (
            self.external_model_root == self.default_external_model_root
            and not self.external_model_root.exists()
        ):
            self.directory(
                self.external_model_root, 0, self.details["identities"]["runtime"][1], 0o750
            )

    def identities(self) -> None:
        identities = self.details["identities"]
        for gid in sorted({pair[1] for pair in identities.values()} | {self.details["agent_gid"]}):
            name = (
                self.agent_name
                if gid == self.details["agent_gid"]
                else self.slug
                + "-"
                + next(service for service, pair in identities.items() if pair[1] == gid)
            )
            try:
                existing = grp.getgrgid(gid)
                if existing.gr_name != name:
                    raise InstallError("gid_conflict")
            except KeyError:
                try:
                    grp.getgrnam(name)
                except KeyError:
                    self.executor.run(["groupadd", "--gid", str(gid), name])
                    self.record("group", name)
                else:
                    raise InstallError("group_name_conflict") from None
        users = {
            self.slug + "-" + service: tuple(pair)
            for service, pair in identities.items()
            if service != "ledger-writer"
        }
        users[self.agent_name] = (self.details["agent_gid"], self.details["agent_gid"])
        for name, (uid, gid) in users.items():
            try:
                existing = pwd.getpwuid(uid)
                if existing.pw_name != name or existing.pw_gid != gid:
                    raise InstallError("uid_conflict")
            except KeyError:
                self.executor.run(
                    [
                        "useradd",
                        "--uid",
                        str(uid),
                        "--gid",
                        str(gid),
                        "--no-create-home",
                        "--home-dir",
                        "/nonexistent",
                        "--shell",
                        "/usr/sbin/nologin",
                        name,
                    ]
                )
                self.record("user", name)
        self.directory(self.program_root, 0, 0, 0o755)
        self.directory(self.program_root / "releases", 0, 0, 0o755)
        self.directory(self.release, 0, 0, 0o755)
        self.directory(self.data_root, 0, self.details["agent_gid"], 0o750)
        for name, owner, mode in (
            ("postgres", identities["postgres"], 0o700),
            ("models", (identities["download-worker"][0], identities["runtime"][1]), 0o2750),
            ("tokenizers", (identities["download-worker"][0], identities["gateway"][1]), 0o2750),
            ("spool", identities["gateway"], 0o700),
            ("backups", identities["maintenance-worker"], 0o700),
            ("caddy", identities["edge"], 0o700),
            ("runtime-cache", (self.details["agent_gid"], identities["runtime"][1]), 0o2770),
        ):
            self.directory(self.data_root / name, *owner, mode)
        self.prepare_external_model_root()
        for name in ("data", "config", "tls"):
            self.directory(self.data_root / "caddy" / name, *identities["edge"], 0o700)
        self.directory(self.secrets_root, 0, 0, 0o700)

    def compose(self, *arguments: str) -> bytes:
        return self.executor.run(
            [
                "docker",
                "compose",
                "--project-name",
                self.project,
                "--file",
                str(self.release / "compose.yaml"),
                *arguments,
            ],
            timeout=240,
        )

    def configuration(self) -> None:
        if self.config["edge"]["tls"] == "provided":
            self.install_provided_tls()
        self.directory(self.release / "installer", 0, 0, 0o755)
        for filename in (
            "holm",
            "install.py",
            "engine.py",
            "guards.py",
            "image_helper.py",
            "uninstall.py",
        ):
            self.write(
                self.release / "installer" / filename,
                (Path(__file__).parent / filename).read_bytes(),
                0o755 if filename == "holm" else 0o644,
            )
        self.rendered = copy.deepcopy(self.base)
        self.rendered["name"] = self.project
        variables = {
            "HOLM_CONFIG_ROOT": str(self.config_root),
            "HOLM_DATA_ROOT": str(self.data_root),
            "HOLM_SECRETS_ROOT": str(self.secrets_root),
            "HOLM_EXTERNAL_MODEL_ROOT": str(self.external_model_root),
        }
        text = json.dumps(self.rendered)
        for key, value in variables.items():
            text = re.sub(
                r"\$\{" + key + r":-[^}]+\}", lambda _, value=value: json.dumps(value)[1:-1], text
            )
        self.rendered = json.loads(text)
        for name, service in self.rendered["services"].items():
            service["image"] = self._image_for(name)
            if name in {"control-api", "gateway", "instance-controller"}:
                service.setdefault("volumes", []).append(
                    {
                        "type": "bind",
                        "source": str(self.config_root / "runtime-compat.yaml"),
                        "target": "/opt/holm/runtime-compat.yaml",
                        "read_only": True,
                    }
                )
            if "command" in service:
                command = service["command"]
                if "--build-version" in command:
                    command[command.index("--build-version") + 1] = self.manifest["build_version"]
        edge = self.config["edge"]
        self.rendered["services"]["edge"]["ports"] = [
            f"{edge['address']}:{edge['http_port']}:8080",
            f"{edge['address']}:{edge['https_port']}:8443",
        ]
        self.verify_compose(self.rendered)
        catalog = production_catalog(
            document(self.source / "runtime-compat.yaml"),
            self.images,
            os.cpu_count() or 1,
            self.gpu_count,
        )
        catalog_bytes = (json.dumps(catalog, indent=2) + "\n").encode()
        installed_manifest = copy.deepcopy(self.manifest)
        installed_manifest["runtime_compat_hash"] = hashlib.sha256(catalog_bytes).hexdigest()
        platform_config = copy.deepcopy(self.details["platform"])
        platform_config.setdefault("storage", {})["external_model_roots"] = [
            str(self.external_model_root)
        ]
        self.image_operation(
            "describe",
            {
                "manifest": installed_manifest,
                "platform": platform_config,
                "catalog": catalog,
            },
        )
        self.write(
            self.release / "source-release-manifest.json",
            Path(self.config["release_manifest"]).read_bytes(),
        )
        self.write(
            self.release / "install-derivation.json",
            (
                json.dumps(
                    {
                        "source_manifest_sha256": self.identity_config["source_manifest_sha256"],
                        "cpu_count": os.cpu_count(),
                        "gpu_count": self.gpu_count,
                        "runtime_compat_hash": installed_manifest["runtime_compat_hash"],
                        "kind": "unsigned-installation-derived-manifest",
                    },
                    indent=2,
                )
                + "\n"
            ).encode(),
        )
        self.write(self.config_root / "runtime-compat.yaml", catalog_bytes)
        self.write(
            self.config_root / "release-manifest.json",
            (json.dumps(installed_manifest, indent=2) + "\n").encode(),
        )
        self.write(
            self.config_root / "platform.yaml",
            (
                json.dumps(
                    {"platform": platform_config, "release_manifest": installed_manifest},
                    indent=2,
                )
                + "\n"
            ).encode(),
        )
        self.write(
            self.release / "compose.yaml", (json.dumps(self.rendered, indent=2) + "\n").encode()
        )
        from urllib.parse import urlsplit

        hostname = urlsplit(self.config["public_base_url"]).hostname
        caddy = (self.source / "deploy/caddy/Caddyfile.tmpl").read_text()
        replacements = {
            "SITE_ADDRESS": "https://" + hostname + ":8443",
            "TLS_DIRECTIVE": (
                "tls internal"
                if self.config["edge"]["tls"] == "internal"
                else "tls /etc/caddy/tls/certificate.pem /etc/caddy/tls/private-key.pem"
                if self.config["edge"]["tls"] == "provided"
                else "tls " + self.config["edge"]["acme_email"]
                if "acme_email" in self.config["edge"]
                else ""
            ),
            "CONTROL_UPSTREAM": "control-api:8000",
            "GATEWAY_UPSTREAM": "gateway:" + self.base["services"]["gateway"]["command"][-1],
            "WEB_ROOT": "/srv/" + self.slug,
            "LOG_DIR": "/data",
        }
        for name, value in replacements.items():
            caddy = caddy.replace("{{" + name + "}}", value)
        caddy = (
            "{\n http_port 8080\n https_port 8443\n auto_https disable_redirects\n"
            f" default_sni {hostname}\n skip_install_trust\n}}\n" + caddy
        )
        caddy += (
            "\nhttp://"
            + hostname
            + ":8080 {\n redir "
            + self.config["public_base_url"].rstrip("/")
            + "{uri} 308\n}\n"
        )
        self.write(self.config_root / "Caddyfile", caddy.encode())
        self.verify_compose(json.loads(self.compose("config", "--format", "json")))

    def verify_compose(self, rendered: dict) -> None:
        verify_rendered_compose(
            self.base,
            rendered,
            config_root=self.config_root,
            data_root=self.data_root,
            external_model_root=self.external_model_root,
            secrets_root=self.secrets_root,
            project=self.project,
            images={name: self._image_for(name) for name in self.base["services"]},
            version=self.manifest["build_version"],
            edge=self.config["edge"],
        )

    def secrets(self) -> None:
        before = set(self.secrets_root.iterdir())
        try:
            self.image_operation("secrets", self.secret_payload(), secrets_mount=True)
            for service, filename in (
                ("control-api", "api_key_pepper_keyring"),
                ("gateway", "gateway_api_key_pepper_keyring"),
            ):
                uid, gid = self.details["identities"][service]
                self.image_operation(
                    "validate-pepper",
                    {},
                    user=f"{uid}:{gid}",
                    secret_file=self.secrets_root / filename,
                )
        finally:
            # Record partial success even if validation or the helper fails.
            for path in set(self.secrets_root.iterdir()) - before:
                self.record("file", path)

    def secret_payload(self) -> dict:
        environment = self.base["services"]["postgres"]["environment"]
        return {
            "root": str(self.secrets_root),
            "identities": self.details["identities"],
            "database": environment["POSTGRES_DB"],
            "bootstrap_user": environment["POSTGRES_USER"],
            "bootstrap_name": Path(self.details["bootstrap_path"]).name,
        }

    def agent_dropin(self, launcher: Path, groups: tuple[int, ...]) -> tuple[str, str]:
        argv = [
            str(launcher),
            "--runtime-compatibility",
            str(self.config_root / "runtime-compat.yaml"),
            "--release-manifest",
            str(self.config_root / "release-manifest.json"),
            "--model-root",
            str(self.data_root / "models"),
            "--external-model-root",
            str(self.external_model_root),
        ]
        command_text = " ".join(shlex.quote(arg) for arg in argv)
        dropin = (
            "[Service]\nExecStart=\nExecStart="
            + command_text
            + "\nReadWritePaths="
            + str(self.cache_root)
            + "\nSupplementaryGroups="
            + " ".join(map(str, groups))
            + "\n"
        )
        effective_agent(
            self.base_agent,
            dropin,
            run_root=self.run_root,
            cache_root=self.cache_root,
            command=command_text,
            groups=groups,
            agent_name=self.slug + "-agent",
        )
        return command_text, dropin

    def agent(self) -> None:
        agent_venv = self.release / "agent-venv"
        if not agent_venv.exists():
            venv.EnvBuilder(with_pip=False, symlinks=True).create(agent_venv)
            self.record("directory", agent_venv)
        site = agent_venv / "lib/python3.12/site-packages"
        command = [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--log-driver",
            "none",
            "--read-only",
            "--mount",
            f"type=bind,src={self.helper},dst=/installer-helper.py,readonly",
            "--entrypoint",
            "python",
            self.application,
            "/installer-helper.py",
            "export-agent",
        ]
        content = self.executor.run(command, timeout=180)
        import io

        with tarfile.open(fileobj=io.BytesIO(content)) as archive:
            archive.extractall(site, filter="data")
        launcher = agent_venv / "bin" / (self.slug + "-host-agent")
        self.write(
            launcher,
            (
                f"#!{agent_venv}/bin/python\nfrom holm_host_agent.__main__ import main\nmain()\n"
            ).encode(),
            0o755,
        )
        docker_gid = Path("/var/run/docker.sock").stat().st_gid
        runtime_gid = self.details["identities"]["runtime"][1]
        groups = tuple(sorted({docker_gid, runtime_gid}))
        # Validate native imports on the Ubuntu host before installing any Unit.
        probe = "import grpc, pydantic_core, pynvml; from holm_host_agent.runtime_container import DockerEngine; pynvml.nvmlInit(); assert pynvml.nvmlDeviceGetCount()>0; pynvml.nvmlShutdown(); assert DockerEngine().request('GET', '/version')[0] == 200"
        self.executor.run(
            [
                "setpriv",
                "--reuid",
                self.agent_name,
                "--regid",
                self.agent_name,
                "--groups",
                ",".join(map(str, groups)),
                str(agent_venv / "bin/python"),
                "-c",
                probe,
            ]
        )
        command_text, dropin = self.agent_dropin(launcher, groups)
        unit_root = Path("/etc/systemd/system")
        self.write(unit_root / self.base_agent_path.name, self.base_agent.encode())
        dropin_root = unit_root / (self.base_agent_path.name + ".d")
        self.directory(dropin_root, 0, 0, 0o755)
        self.write(dropin_root / "10-install.conf", dropin.encode())
        self.record("unit", self.base_agent_path.name)
        effective_agent(
            (unit_root / self.base_agent_path.name).read_text(),
            (dropin_root / "10-install.conf").read_text(),
            run_root=self.run_root,
            cache_root=self.cache_root,
            command=command_text,
            groups=groups,
            agent_name=self.slug + "-agent",
        )
        self.executor.run(["systemctl", "daemon-reload"])
        created_run_root = not self.run_root.exists()
        try:
            self.executor.run(["systemctl", "start", self.base_agent_path.name])
        finally:
            if created_run_root and self.run_root.exists():
                self.record("directory", self.run_root)
        deadline = time.monotonic() + 30
        agent_socket = Path(self.details["agent_socket"])
        while not agent_socket.exists() and time.monotonic() < deadline:
            time.sleep(0.25)
        metadata = agent_socket.stat()
        if (
            metadata.st_uid != pwd.getpwnam(self.agent_name).pw_uid
            or not stat.S_ISSOCK(metadata.st_mode)
            or metadata.st_gid != self.details["agent_gid"]
            or stat.S_IMODE(metadata.st_mode) != 0o660
        ):
            raise InstallError("agent_socket_permissions")

    def track_compose_objects(self) -> None:
        containers = self.executor.run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"label=com.docker.compose.project={self.project}",
                "--format",
                "{{.ID}}",
            ]
        )
        for name in containers.decode().split():
            self.record("container", name)
        networks = self.executor.run(
            [
                "docker",
                "network",
                "ls",
                "--filter",
                f"label=com.docker.compose.project={self.project}",
                "--format",
                "{{.ID}}",
            ]
        )
        for name in networks.decode().split():
            self.record("network", name)

    def database(self) -> None:
        try:
            self.compose("up", "--detach", "--wait", "--wait-timeout", "120", "postgres")
            self.compose("run", "--rm", "migrate")
            self.bootstrap_status = self.image_operation(
                "database",
                self.secret_payload(),
                network=self.project + "_" + next(iter(self.base["networks"])),
                secrets_mount=True,
            )["bootstrap"]
        finally:
            self.track_compose_objects()

    def platform(self) -> None:
        try:
            names = [
                name
                for name in self.base["services"]
                if name not in {"postgres", "migrate", "edge"}
            ]
            self.compose("up", "--detach", "--wait", "--wait-timeout", "120", *names)
            self.compose("up", "--detach", "--wait", "--wait-timeout", "60", "edge")
        finally:
            self.track_compose_objects()

    def smoke(self) -> None:
        identity = self.details["identities"]["control-api"]
        self.image_operation(
            "smoke",
            {"socket": self.details["agent_socket"]},
            user=f"{identity[0]}:{identity[1]}",
            groups=(self.details["agent_gid"],),
        )
        self.compose(
            "exec",
            "--no-TTY",
            "gateway",
            "python",
            "-c",
            "from pathlib import Path; p=Path('/var/lib/holm/spool/.install-probe'); p.write_bytes(b''); p.unlink()",
        )
        self.compose(
            "exec",
            "--no-TTY",
            "postgres",
            "pg_isready",
            "--username",
            self.base["services"]["postgres"]["environment"]["POSTGRES_USER"],
        )
        if self.config["edge"]["tls"] != "internal":
            self.trusted_tls_smoke()
            return
        trust = self.data_root / "caddy/data/caddy/pki/authorities/local/root.crt"
        context = ssl.create_default_context(cafile=str(trust))
        for suffix in ("/", "/setup"):
            with urllib.request.urlopen(
                self.config["public_base_url"].rstrip("/") + suffix, context=context, timeout=15
            ) as response:
                if response.status != 200:
                    raise InstallError("https_smoke_failed")

    def trusted_tls_smoke(self) -> None:
        from urllib.parse import urlsplit

        edge = self.config["edge"]
        hostname = urlsplit(self.config["public_base_url"]).hostname
        if edge["tls"] == "provided":
            self.validate_provided_tls()
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        else:
            context = ssl.create_default_context()
        address = "127.0.0.1" if edge["address"] == "0.0.0.0" else edge["address"]
        deadline = time.monotonic() + (180 if edge["tls"] == "acme" else 15)
        while True:
            try:
                for suffix in ("/", "/setup"):
                    timeout = max(0.01, min(10, deadline - time.monotonic()))
                    with socket.create_connection(
                        (address, edge["https_port"]), timeout=timeout
                    ) as raw:
                        with context.wrap_socket(raw, server_hostname=hostname) as connection:
                            if (
                                edge["tls"] == "provided"
                                and connection.getpeercert(binary_form=True) != self.tls_leaf
                            ):
                                raise InstallError("tls_peer_mismatch")
                            connection.sendall(
                                (
                                    f"GET {suffix} HTTP/1.1\r\nHost: {hostname}\r\nConnection: close\r\n\r\n"
                                ).encode("ascii")
                            )
                            response = http.client.HTTPResponse(connection)
                            response.begin()
                            if response.status != 200:
                                raise InstallError("https_smoke_failed")
                return
            except (OSError, http.client.HTTPException, InstallError):
                if edge["tls"] != "acme":
                    raise InstallError(
                        "provided_tls_smoke_failed",
                        "Check the served certificate and Edge health, then rerun.",
                    ) from None
                if time.monotonic() >= deadline:
                    raise InstallError(
                        "acme_certificate_timeout",
                        "Check DNS, public access to ports 80/443 and outbound access, then rerun install.",
                    ) from None
                time.sleep(min(2, max(0, deadline - time.monotonic())))

    def activate(self) -> None:
        if self.current.is_symlink():
            if self.current.resolve() != self.release:
                raise InstallError("upgrade_not_supported")
        elif self.current.exists():
            raise InstallError("current_path_conflict")
        else:
            temporary = self.program_root / ".current-new"
            temporary.symlink_to(self.release)
            os.replace(temporary, self.current)
            self.record("symlink", self.current)
        entrypoint = self.program_root / self.slug
        expected_entrypoint = self.current / "installer" / "holm"
        if entrypoint.is_symlink():
            if entrypoint.resolve() != expected_entrypoint.resolve():
                raise InstallError("entrypoint_conflict")
        elif entrypoint.exists():
            raise InstallError("entrypoint_conflict")
        else:
            entrypoint.symlink_to(expected_entrypoint)
            self.record("symlink", entrypoint)
        platform_unit = self.base_platform_path.read_text()
        self.write(
            Path("/etc/systemd/system") / self.base_platform_path.name, platform_unit.encode()
        )
        self.record("unit", self.base_platform_path.name)
        dropin_root = Path("/etc/systemd/system") / (self.base_platform_path.name + ".d")
        self.directory(dropin_root, 0, 0, 0o755)
        compose_path = self.current / "compose.yaml"
        dropin = f"[Service]\nExecStart=\nExecStart=/usr/bin/docker compose --project-name {self.project} --file {compose_path} up --detach\n"
        self.write(dropin_root / "10-install.conf", dropin.encode())
        effective_platform(
            (Path("/etc/systemd/system") / self.base_platform_path.name).read_text(),
            (dropin_root / "10-install.conf").read_text(),
            original_project=self.slug,
            project=self.project,
            compose_path=compose_path,
        )
        self.executor.run(["systemctl", "daemon-reload"])
        self.executor.run(["systemctl", "start", self.base_platform_path.name])
        if self.config["systemd"]["enable"]:
            self.executor.run(
                ["systemctl", "enable", self.base_agent_path.name, self.base_platform_path.name]
            )

    def run(self) -> int:
        # Read-only preflight is repeated even for a completed journal.
        self.preflight()
        new_config_root = not self.config_root.exists()
        if new_config_root:
            self.config_root.mkdir(mode=0o755)
        elif (
            self.config_root.is_symlink()
            or self.config_root.stat().st_uid != 0
            or stat.S_IMODE(self.config_root.stat().st_mode) != 0o755
        ):
            raise InstallError("unsafe_config_root")
        new_lock = not (self.config_root / "install.lock").exists()
        new_state = not self.state_path.exists()
        with install_lock(self.config_root / "install.lock"):
            self.journal = Journal(self.state_path, self.identity_config)
            self.journal.save()
            if new_config_root:
                self.record("directory", self.config_root)
            if new_lock:
                self.record("file", self.config_root / "install.lock")
            if new_state:
                self.record("file", self.state_path)
            actions = {
                "preflight": lambda: None,
                "identities": self.identities,
                "configuration": self.configuration,
                "secrets": self.secrets,
                "agent": self.agent,
                "database": self.database,
                "platform": self.platform,
                "smoke": self.smoke,
                "activate": self.activate,
            }
            result = self.journal.run(actions, print)
            if result == 0:
                if self.config["edge"]["tls"] == "internal":
                    trust = self.data_root / "caddy/data/caddy/pki/authorities/local/root.crt"
                    fingerprint = certificate_fingerprint(trust)
                    print(f"tls=internal ca_certificate={trust} sha256={fingerprint}")
                    print(
                        "warning=Internal certificates are only for testing and isolated networks; "
                        "some Node/Electron clients do not read system trust stores or "
                        "NODE_EXTRA_CA_CERTS and cannot connect. "
                        "Production deployments require trusted certificates (acme or provided); see README."
                    )
                status = self.image_operation(
                    "database",
                    self.secret_payload(),
                    network=self.project + "_" + next(iter(self.base["networks"])),
                    secrets_mount=True,
                )["bootstrap"]
                if status == "valid":
                    # /dev/tty is the only delivery stream; redirecting install
                    # stdout/stderr into logs must never capture a bearer token.
                    token_path = self.secrets_root / Path(self.details["bootstrap_path"]).name
                    try:
                        with open("/dev/tty", "w") as terminal:
                            terminal.write(
                                "Initialization link (30 minutes from issuance): "
                                + self.config["public_base_url"].rstrip("/")
                                + "/setup#token="
                                + token_path.read_text().strip()
                                + "\n"
                            )
                    except OSError:
                        print("bootstrap=valid delivery=root_only_file")
                elif status == "expired_or_used":
                    print(
                        "bootstrap=expired_or_used remedy=Contact the administrator for the reviewed recovery procedure; rerunning install does not rotate this Token."
                    )
                else:
                    print("bootstrap=initialized")
            return result


def main() -> int:
    parser = argparse.ArgumentParser(prog="holm")
    parser.add_argument("command", choices=("install", "uninstall"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--purge-data", action="store_true")
    parser.add_argument("--purge-images", action="store_true")
    args = parser.parse_args()
    if args.command == "install":
        if args.config is None:
            parser.error("install requires --config")
        if args.purge_data or args.purge_images:
            parser.error("purge options require uninstall")
    elif args.config is not None:
        parser.error("--config is only accepted by install")

    def interrupted(signum: int, frame: object) -> None:
        raise InstallError("interrupted", "Rerun the same installation command to continue.")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    if args.command == "uninstall":
        from uninstall import Uninstaller

        return Uninstaller(
            Executor(), purge_data=args.purge_data, purge_images=args.purge_images
        ).run()
    try:
        return Installer(load_configuration(args.config)).run()
    except InstallError as error:
        print(
            f"stage=preflight result=failed code={error.code} remedy={error.remedy}",
            file=sys.stderr,
        )
        return 20
    except Exception:
        print(
            "stage=preflight result=failed code=invalid_installation remedy=Check the release files and configuration.",
            file=sys.stderr,
        )
        return 20


if __name__ == "__main__":
    raise SystemExit(main())

"""Isolated source build, actual image IDs and installer configuration."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
POSTGRES = (
    "library/postgres:16.15-alpine@sha256:"
    "721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"
)
POSTGRES_TAG = "holm/postgres:16.15-alpine"
MIN_FREE_BYTES = 80 * 1024**3  # Includes both large upstream Runtime images and build layers.
OUTPUT_FILES = ("images.json", "release-manifest.json", "install.json")
MIRROR_ENV = ("UV_DEFAULT_INDEX", "npm_config_registry", "APT_MIRROR", "APT_SECURITY_MIRROR")
SOURCE_FILES = (
    "pyproject.toml",
    "uv.lock",
    "package.json",
    "package-lock.json",
    "runtime-compat.yaml",
    "docs/content/help-center.md",
    "scripts/build_artifacts.py",
    "scripts/cache_build_dependencies.py",
    "apps/web/package.json",
    "apps/web/tsconfig.json",
    "apps/web/vite.config.ts",
)
SOURCE_TREES = (
    "apps/web/src",
    "apps/web/preview",
    "migrations",
    "deploy/compose",
    "deploy/caddy",
    "deploy/systemd",
    "deploy/images",
    "deploy/build",
    "deploy/installer",
)
SOURCE_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".ts",
        ".tsx",
        ".js",
        ".mjs",
        ".css",
        ".json",
        ".toml",
        ".yaml",
        ".yml",
        ".html",
        ".svg",
        ".txt",
        ".md",
        ".ini",
        ".mako",
        ".tmpl",
        ".service",
        ".Dockerfile",
        ".typed",
        ".proto",
        ".LICENSE",
        ".woff",
        ".woff2",
    }
)


class BuildError(RuntimeError):
    """A safe diagnostic, never raw subprocess output."""


class Executor:
    def __init__(self) -> None:
        self.log_directory: Path | None = None
        self.log_path: Path | None = None
        self.sequence = 0

    @contextmanager
    def _output(self, step: str) -> Iterator[BinaryIO]:
        # Keep preflight read-only; persistent build diagnostics are private to the operator.
        self.log_path = None
        if self.log_directory is None:
            with tempfile.TemporaryFile() as output:
                yield output
        else:
            directory = safe_path(self.log_directory)
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)
            if re.fullmatch(r"[a-z][a-z0-9-]*", step) is None:
                raise BuildError("invalid build step name")
            while True:
                self.sequence += 1
                path = directory / f"{self.sequence:03d}-{step}.log"
                try:
                    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                except FileExistsError:
                    continue
                break
            self.log_path = path
            with os.fdopen(descriptor, "w+b") as output:
                yield output

    def run(self, argv: list[str], *, timeout: int = 7200, step: str = "preflight") -> str:
        with self._output(step) as output:
            if self.log_path is not None:
                print(f"step={step} result=started log={self.log_path}", flush=True)
            try:
                result = subprocess.run(
                    argv, stdout=output, stderr=subprocess.STDOUT, timeout=timeout, check=False
                )
            except (OSError, subprocess.TimeoutExpired):
                raise BuildError(
                    "command unavailable or timed out; check Docker and network access"
                ) from None
            if result.returncode:
                raise BuildError(
                    f"command exited {result.returncode}; "
                    "check Docker, registry access and disk space"
                )
            output.seek(0)
            # Inspection responses are short; the full build output remains in the log file.
            return output.read(65536).decode(errors="replace")


def safe_path(path: Path) -> Path:
    path = path.absolute()
    if any(char in str(path) for char in "\n\r\x00%,") or ".." in path.parts:
        raise BuildError("invalid path; use an absolute path without parent traversal or commas")
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise BuildError("symlink path refused; choose a regular directory")
    return path


def source_files(repo: Path) -> list[Path]:
    """Copy only build inputs; never traverse dependency caches, env files or secret directories."""
    files = {Path(name) for name in SOURCE_FILES}
    project = tomllib.loads((repo / "pyproject.toml").read_text())
    trees = list(SOURCE_TREES)
    for member in project["tool"]["uv"]["workspace"]["members"]:
        member_path = Path(member)
        if member_path.is_absolute() or ".." in member_path.parts or "*" in member:
            raise BuildError("unsupported workspace member; review source layout")
        files.add(member_path / "pyproject.toml")
        trees.append(str(member_path / "src"))
    for name in trees:
        tree = repo / name
        if not tree.is_dir() or tree.is_symlink():
            raise BuildError("missing or linked source directory; restore a clean checkout")
        for directory, dirs, names in os.walk(tree, followlinks=False):
            dirs[:] = [
                d
                for d in dirs
                if not d.startswith(".")
                and d not in {"__pycache__", "node_modules", "secrets", "build", "dist"}
            ]
            if any((Path(directory) / d).is_symlink() for d in dirs):
                raise BuildError("linked source directory; restore a clean checkout")
            for filename in names:
                if filename.startswith("."):
                    continue
                path = Path(directory) / filename
                if path.suffix in SOURCE_SUFFIXES or filename in {"holm", "holm-build"}:
                    files.add(path.relative_to(repo))
    for relative in files:
        path = safe_path(repo / relative)
        if not path.is_file():
            raise BuildError("missing source input; restore a clean checkout")
    return sorted(files)


def source_snapshot(repo: Path, files: list[Path]) -> str:
    """Identify exactly the source inputs selected for copying, excluding build outputs."""
    digest = hashlib.sha1(usedforsecurity=False)
    for relative in sorted(files):
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update((repo / relative).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def configuration(args: argparse.Namespace, release: Path, output: Path) -> dict[str, object]:
    url = urlsplit(args.public_url)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in {"", "/"}
        or re.fullmatch(r"[a-zA-Z0-9.-]+", url.hostname) is None
    ):
        raise BuildError("invalid public URL; use https://hostname[:port]")
    ipaddress.IPv4Address(args.edge_address)
    if not (1 <= args.http_port <= 65535 and 1 <= args.https_port <= 65535):
        raise BuildError("invalid Edge ports; use ports between 1 and 65535")
    if args.http_port == args.https_port or (url.port or 443) != args.https_port:
        raise BuildError("Edge ports conflict or public URL does not match the HTTPS port")
    edge = {
        "address": args.edge_address,
        "http_port": args.http_port,
        "https_port": args.https_port,
        "tls": args.tls,
    }
    if args.tls != "acme" and args.acme_email is not None:
        raise BuildError("--acme-email requires --tls acme")
    if args.tls != "provided" and (
        args.tls_certificate is not None or args.tls_private_key is not None
    ):
        raise BuildError("certificate paths require --tls provided")
    if args.tls == "acme":
        try:
            ipaddress.ip_address(url.hostname)
        except ValueError:
            pass
        else:
            raise BuildError("acme requires a domain name")
        if args.http_port != 80 and args.https_port != 443:
            raise BuildError("acme requires public port 80 or 443")
        if args.acme_email is not None:
            if not re.fullmatch(r"[a-zA-Z0-9_.+%-]+@[a-zA-Z0-9.-]+", args.acme_email):
                raise BuildError("invalid ACME email")
            edge["acme_email"] = args.acme_email
    if args.tls == "provided":
        for source, target in (
            (args.tls_certificate, "certificate_file"),
            (args.tls_private_key, "private_key_file"),
        ):
            if source is None or not source.is_absolute():
                raise BuildError("provided requires absolute certificate and private key paths")
            # The source files belong to the installation host; never read their contents here.
            if any(part == ".." for part in source.parts) or any(
                c in str(source) for c in "\n\r\x00%"
            ):
                raise BuildError("invalid TLS file path")
            if any(p.is_symlink() for p in (source, *source.parents)):
                raise BuildError("TLS file paths must not be symlinks")
            edge[target] = str(source)
    config: dict[str, object] = {
        "release_root": str(release),
        "release_manifest": str(output / "release-manifest.json"),
        "public_base_url": args.public_url,
        "edge": edge,
        "systemd": {"enable": args.systemd},
    }
    if args.external_model_root is not None:
        external = safe_path(args.external_model_root)
        if any(char in str(external) for char in "\"'\\$"):
            raise BuildError("invalid external model root")
        config["external_model_root"] = str(external)
    return config


def preflight(
    args: argparse.Namespace, executor: Executor, repo: Path
) -> tuple[Path, list[Path], dict[str, str]]:
    if os.geteuid() != 0:
        raise BuildError("root required; rerun with sudo python3")
    if sys.version_info[:2] != (3, 12):
        raise BuildError("Python 3.12 required; select python3.12")
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "AMD64"}:
        raise BuildError("Linux x86_64 required for the certified build tools")
    output = safe_path(args.output)
    if output == repo or repo.is_relative_to(output):
        raise BuildError("output must not contain the source checkout")
    if output.exists() and (not output.is_dir() or not args.force_output):
        raise BuildError("output exists; choose a new directory or explicitly use --force-output")
    for name in OUTPUT_FILES:
        target = safe_path(output / name)
        if target.exists() and not target.is_file():
            raise BuildError("output metadata is not a regular file; choose a new directory")
    configuration(args, output / "release-root", output)
    if re.fullmatch(r"[a-zA-Z0-9.-]+(?::[0-9]+)?(?:/[a-zA-Z0-9._-]+)*", args.base_registry) is None:
        raise BuildError("invalid base registry; provide host[:port][/prefix] without credentials")
    for key in MIRROR_ENV:
        if value := os.environ.get(key):
            mirror = urlsplit(value)
            if (
                mirror.scheme not in {"https", "http"}
                or not mirror.hostname
                or mirror.username
                or mirror.password
                or mirror.query
                or mirror.fragment
            ):
                raise BuildError(
                    "package mirror must be an HTTP(S) URL without credentials or query"
                )
    files = source_files(repo)
    version = tomllib.loads((repo / "pyproject.toml").read_text())["project"]["version"]
    runtimes = json.loads((repo / "runtime-compat.yaml").read_text())["runtimes"]
    if (
        not isinstance(runtimes, list)
        or {r["name"] for r in runtimes} != {"vllm", "sglang"}
        or len(runtimes) != 2
    ):
        raise BuildError("unsupported runtime catalog; review the Runtime Dockerfile contract")
    tags = {"application": f"holm/application:{version}", "edge": f"holm/edge:{version}"}
    tags.update(
        {f"runtime-{r['name']}": f"holm/runtime-{r['name']}:{r['version']}" for r in runtimes}
    )
    if any(
        re.fullmatch(r"holm/[a-z-]+:[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}", tag) is None
        for tag in tags.values()
    ):
        raise BuildError("invalid image version; review project and runtime versions")
    for command, minimum in (
        (["docker", "version", "--format", "{{.Server.Version}}"], (24, 0, 0)),
        (["docker", "compose", "version", "--short"], (2, 20, 0)),
    ):
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", executor.run(command, timeout=30))
        if match is None or tuple(map(int, match.groups())) < minimum:
            raise BuildError("Docker >=24.0.0 and Compose >=2.20.0 required")
    docker_root_value = executor.run(
        ["docker", "info", "--format", "{{.DockerRootDir}}"], timeout=30
    ).strip()
    if not Path(docker_root_value).is_absolute():
        raise BuildError("Docker data directory unknown; check the local Docker daemon")
    docker_root = safe_path(Path(docker_root_value))
    if not docker_root.is_dir():
        raise BuildError("Docker data directory unavailable; use a local Docker daemon")
    ancestor = output
    while not ancestor.exists():
        ancestor = ancestor.parent
    if any(shutil.disk_usage(path).free < MIN_FREE_BYTES for path in (ancestor, docker_root)):
        raise BuildError(
            "at least 80 GiB free required on output and Docker storage; "
            "free space or choose another location"
        )
    return output, files, tags


def clean_git_commit(repo: Path) -> str | None:
    """Use Git identity only for clean sources; exports retain the snapshot fallback."""
    executor = Executor()
    try:
        status = executor.run(
            ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
            timeout=10,
        )
        if status.strip():
            return None
        commit = executor.run(["git", "-C", str(repo), "rev-parse", "HEAD"], timeout=10).strip()
    except BuildError:
        return None
    return commit if re.fullmatch(r"[0-9a-f]{40}", commit) else None


def build(args: argparse.Namespace, executor: Executor, repo: Path = ROOT) -> int:
    step = "preflight"
    executor.log_directory = None
    executor.log_path = None

    def run(argv: list[str], *, timeout: int = 7200) -> str:
        return executor.run(argv, timeout=timeout, step=step)

    try:
        output, files, tags = preflight(args, executor, repo)
        commit = clean_git_commit(repo) or source_snapshot(repo, files)
        print(f"step={step} result=complete", flush=True)
        step = "artifacts"
        output.mkdir(parents=True, exist_ok=True)
        executor.log_directory = output / "logs"
        (output / "install.json").unlink(missing_ok=True)
        (output / "release-manifest.json").unlink(missing_ok=True)
        release = Path(tempfile.mkdtemp(prefix="release-root-", dir=output))
        for relative in files:
            destination = release / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(repo / relative, destination)
        (release / ".dockerignore").write_text(".venv/\nnode_modules/\n__pycache__/\n")
        builder = "holm/builder:" + tags["application"].split(":")[1]

        def image_build(name: str, tag: str) -> None:
            run(
                [
                    "docker",
                    "build",
                    "--build-arg",
                    "HOLM_BASE_REGISTRY=" + args.base_registry,
                    *[
                        arg
                        for key in MIRROR_ENV
                        if name == "builder" and os.environ.get(key)
                        for arg in ("--build-arg", key)
                    ],
                    "--tag",
                    tag,
                    "--file",
                    str(release / f"deploy/images/{name}.Dockerfile"),
                    str(release),
                ]
            )

        image_build("builder", builder)
        run(
            [
                "docker",
                "run",
                "--rm",
                "--log-driver",
                "none",
                "--mount",
                f"type=bind,src={release},dst=/build",
                "--workdir",
                "/build",
                *[arg for key in MIRROR_ENV if key in os.environ for arg in ("--env", key)],
                builder,
                "sh",
                "-ec",
                "uv sync --frozen --all-packages --no-dev && "
                "uv run --frozen --no-dev python scripts/cache_build_dependencies.py && "
                "npm ci --no-audit --no-fund && "
                "uv run --frozen --no-dev python scripts/build_artifacts.py",
            ]
        )
        if not (release / "build/m0-02/SHA256SUMS").is_file():
            raise BuildError("artifact build produced no SHA256SUMS; inspect the builder setup")
        print(f"step={step} result=complete", flush=True)
        images: dict[str, str] = {}

        def inspect(name: str, tag: str) -> None:
            digest = run(
                ["docker", "image", "inspect", tag, "--format", "{{.Id}}"], timeout=30
            ).strip()
            if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
                raise BuildError("image digest unavailable; rebuild the named image")
            images[name] = digest

        for name, tag in tags.items():
            step = name
            image_build(name, tag)
            inspect(name, tag)
            print(f"step={step} result=complete", flush=True)
        step = "postgres"
        upstream = args.base_registry + "/" + POSTGRES
        run(["docker", "pull", upstream])
        run(["docker", "tag", upstream, POSTGRES_TAG])
        inspect("postgres", POSTGRES_TAG)
        print(f"step={step} result=complete", flush=True)
        step = "manifest"
        (output / "images.json").write_text(json.dumps(images, indent=2) + "\n")
        run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--log-driver",
                "none",
                "--read-only",
                "--user",
                "0:0",
                "--mount",
                f"type=bind,src={release},dst={release},readonly",
                "--mount",
                f"type=bind,src={output},dst=/output",
                "--entrypoint",
                "python",
                images["application"],
                str(release / "deploy/build/release_manifest.py"),
                "--repo",
                str(release),
                "--work",
                "/output",
                "--commit",
                commit,
            ]
        )
        # An absent or malformed generator result must never yield an installation command.
        manifest = json.loads((output / "release-manifest.json").read_text())
        if {item["name"]: item["digest"] for item in manifest["image_digests"]} != images:
            raise BuildError("Manifest image IDs differ; rerun into a fresh output directory")
        print(f"step={step} result=complete", flush=True)
        step = "configuration"
        (output / "install.json").write_text(
            json.dumps(configuration(args, release, output), indent=2) + "\n"
        )
        print(f"step={step} result=complete", flush=True)
        print(
            "Next: "
            + shlex.join(
                [
                    "sudo",
                    sys.executable,
                    str(repo / "deploy/installer/holm"),
                    "install",
                    "--config",
                    str(output / "install.json"),
                ]
            )
        )
        return 0
    except (BuildError, OSError, ValueError, KeyError, TypeError):
        # Invalid source/config documents and subprocess exceptions cannot leak their contents.
        error = sys.exception()
        remedy = (
            str(error)
            if isinstance(error, BuildError)
            else "check source files and output permissions; rerun into a fresh directory"
        )
        log = f" log={executor.log_path}" if executor.log_path is not None else ""
        print(f"step={step} result=failed remedy={remedy}{log}", file=sys.stderr, flush=True)
        return 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-url", required=True)
    parser.add_argument("--base-registry", default="docker.io")
    parser.add_argument("--output", type=Path, default=ROOT / "build/release")
    parser.add_argument(
        "--force-output",
        action="store_true",
        help="replace output metadata; retain previous build workspaces",
    )
    parser.add_argument("--edge-address", default="0.0.0.0")
    parser.add_argument("--http-port", type=int, default=80)
    parser.add_argument("--https-port", type=int, default=443)
    parser.add_argument("--systemd", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--external-model-root", type=Path)
    parser.add_argument("--tls", choices=("internal", "acme", "provided"), default="internal")
    parser.add_argument("--acme-email")
    parser.add_argument("--tls-certificate", type=Path)
    parser.add_argument("--tls-private-key", type=Path)
    return parser.parse_args(argv)


def main() -> int:
    return build(parse_args(), Executor())

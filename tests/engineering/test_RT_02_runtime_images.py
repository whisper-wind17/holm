"""FX-RT-02: static image contract and corrupted Dockerfile positive controls.

Expected upstream index digests come from the RT-02 registry inspection.
No Docker/GPU execution is represented by these tests.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UPSTREAM = {
    "vllm": (
        "vllm/vllm-openai",
        "c2914767605584b6d8f45686b82de173ecc99e781897aa3d0a66dacd72c51ae1",
    ),
    "sglang": (
        "lmsysorg/sglang",
        "952ebb195c41b10dc01fa63c41c9bfc14f2ee02ffe8da71e11aeab5f3f7c7772",
    ),
}


def image_text(name: str) -> str:
    return (ROOT / f"deploy/images/runtime-{name}.Dockerfile").read_text()


def guard(text: str, name: str) -> None:
    lines = [
        line.strip()
        for line in text.replace("\\\n", " ").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    instructions = [line.split(maxsplit=1)[0] for line in lines]
    assert instructions == ["ARG", "FROM", "RUN", "ENV", "ENTRYPOINT", "CMD", "USER"], "thin layers"
    versions = {
        e["name"]: e["version"]
        for e in json.loads((ROOT / "runtime-compat.yaml").read_text())["runtimes"]
    }
    repo, digest = UPSTREAM[name]
    assert lines[0] == "ARG HOLM_BASE_REGISTRY=docker.io", "registry argument"
    assert lines[1] == f"FROM ${{HOLM_BASE_REGISTRY}}/{repo}:v{versions[name]}@sha256:{digest}", (
        "pinned upstream"
    )
    run = " ".join(lines[2].split())
    assert run.startswith(
        "RUN --mount=type=bind,source=deploy/installer/guards.py,"
        "target=/tmp/holm-runtime-guards.py "
    ), "shared constant mount"
    assert "rw" not in run.split("set -eu;")[0], "read-only source"
    assert "set -eu;" in run, "fail on errors"
    assert "getent passwd 10008 >/dev/null || getent group 10008 >/dev/null" in run, (
        "identity conflict"
    )
    assert "upstream UID/GID 10008 is occupied; refusing to renumber' >&2; exit 1;" in run, (
        "identity failure"
    )
    assert "groupadd --gid 10008 runtime;" in run, "runtime group"
    assert (
        "useradd --uid 10008 --gid 10008 --no-create-home --home-dir /var/cache/holm runtime;"
        in run
    ), "runtime user"
    assert "mkdir -p /var/cache/holm;" in run and "chown 10008:10008 /var/cache/holm;" in run, (
        "cache ownership"
    )
    assert (
        "runtime_bin=$(python3 -c " in run and 'target.id == "RUNTIME_EXECUTABLE_DIRECTORY"' in run
    ), "shared constant"
    assert "/opt/holm/runtime/bin" not in text, "no duplicate path constant"
    assert (
        f"executable=$(command -v {name}) || {{ "
        f"echo 'Runtime image: upstream {name} command missing' >&2; exit 1; }};" in run
    ), "missing executable"
    assert 'test -x "$executable";' in run, "executable check"
    assert f'mkdir -p "$runtime_bin"; ln -s "$executable" "$runtime_bin/{name}"' in run, (
        "stable symlink"
    )
    assert not re.search(r"\b(?:pip|apt-get|apt|uv)\s+(?:install|sync)\b", text), "no installers"
    assert lines[3].split() == [
        "ENV",
        'HOME="/var/cache/holm"',
        'HF_HOME="/var/cache/holm/huggingface"',
        'XDG_CACHE_HOME="/var/cache/holm"',
    ], "cache environment"
    assert lines[4:] == ["ENTRYPOINT []", "CMD []", "USER 10008:10008"], (
        "image launch configuration"
    )


@pytest.mark.parametrize("name", UPSTREAM)
def test_RT_02_static_image_contract(name: str) -> None:
    guard(image_text(name), name)


# Each invariant above has an intentional broken input; these are guard controls,
# not a claim that a Docker build or a GPU inference has run.
@pytest.mark.parametrize("name", UPSTREAM)
@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("ARG HOLM_BASE_REGISTRY=docker.io", "ARG BASE_REGISTRY=docker.io"),
        ("${HOLM_BASE_REGISTRY}/", "docker.io/"),
        ("@sha256:", "@sha512:"),
        ("USER 10008:10008", "USER 0:0"),
        ("ENTRYPOINT []", 'ENTRYPOINT ["serve"]'),
        ("CMD []", 'CMD ["serve"]'),
        ("CMD []", "CMD []\nCOPY external-venv/ /opt/venv/"),
        ("type=bind", "type=cache"),
        ("target=/tmp/holm-runtime-guards.py", "target=/tmp/holm-runtime-guards.py,rw"),
        ("set -eu;", "set -u;"),
        ("getent passwd 10008", "getent passwd 10009"),
        ("getent group 10008", "getent group 10009"),
        ("refusing to renumber' >&2; exit 1;", "refusing to renumber' >&2; true;"),
        ("groupadd --gid 10008", "groupadd --gid 10009"),
        ("useradd --uid 10008", "useradd --uid 10009"),
        ("chown 10008:10008 /var/cache/holm;", "true;"),
        ('target.id == "RUNTIME_EXECUTABLE_DIRECTORY"', 'target.id == "WRONG_CONSTANT"'),
        ("runtime_bin=$(python3 -c ", "runtime_bin=/opt/holm/runtime/bin; unused=$(python3 -c "),
        ("command -v", "which"),
        ("command missing' >&2; exit 1;", "command missing' >&2; true;"),
        ('test -x "$executable";', "true;"),
        ('ln -s "$executable"', "ln -s /missing/executable"),
        ('mkdir -p "$runtime_bin";', 'pip install runtime; mkdir -p "$runtime_bin";'),
        ('HOME="/var/cache/holm"', 'HOME="/root"'),
        ('HF_HOME="/var/cache/holm/huggingface"', 'HF_HOME="/root"'),
        ('XDG_CACHE_HOME="/var/cache/holm"', 'XDG_CACHE_HOME="/root"'),
    ],
)
def test_RT_02_static_guard_rejects_positive_controls(name: str, old: str, new: str) -> None:
    original = image_text(name)
    guard(original, name)
    assert old in original
    with pytest.raises(AssertionError):
        guard(original.replace(old, new), name)


@pytest.mark.parametrize("name", UPSTREAM)
def test_RT_02_build_constant_extractor_reads_literal_and_fails_if_missing(
    name: str, tmp_path: Path
) -> None:
    text = image_text(name)
    expression = re.search(r"runtime_bin=\$\(python3 -c '([^']+)'\)", text)
    assert expression is not None
    source = tmp_path / "guards.py"
    script = expression[1].replace("/tmp/holm-runtime-guards.py", str(source))
    source.write_text((ROOT / "deploy/installer/guards.py").read_text())
    assert (
        subprocess.check_output(["python3", "-c", script], text=True).strip()
        == "/opt/holm/runtime/bin"
    )
    source.write_text('RUNTIME_EXECUTABLE_DIRECTORY = "/fixture/changed/bin"\n')
    assert (
        subprocess.check_output(["python3", "-c", script], text=True).strip()
        == "/fixture/changed/bin"
    )
    source.write_text("# FX-RT-02 missing constant\n")
    assert subprocess.run(["python3", "-c", script], capture_output=True).returncode != 0

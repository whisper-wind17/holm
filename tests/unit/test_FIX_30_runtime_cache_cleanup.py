"""FIX-30: FX-FIX-30 synthetic caches and certified image; no Docker or GPU."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from holm_host_agent.runtime_container import _CACHE_CLEANUP_SCRIPT
from holm_host_agent.runtime_process import RuntimeRequestError
from test_M3_07C_runtime_cleanup import setup


def run_script(
    cache: Path, mode: str, *, cross_device: bool = False
) -> subprocess.CompletedProcess[str]:
    # Only the fixture substitutes the mount location. Production uses a fixed path.
    script = _CACHE_CLEANUP_SCRIPT.replace("'/var/cache/holm'", repr(str(cache)))
    if cross_device:
        script = script.replace("validate(fd, os.fstat(fd).st_dev)", "validate(fd, -1)")
    return subprocess.run(
        [sys.executable, "-I", "-B", "-c", script, mode],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )


def test_FIX_30_FIX_33_runtime_owned_permissions_and_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, engine, cache, sibling = setup(tmp_path)
    for name, mode in ((".nv", 0o700), ("vllm", 0o755)):
        directory = cache / name
        directory.mkdir(mode=mode)
        (directory / "compiled").write_text("FX-FIX-30")
        (directory / "compiled").chmod(0o644)
    real_open = os.open

    def agent_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if path in (".nv", "vllm"):
            raise PermissionError("FX-FIX-30 Agent cannot traverse Runtime cache")
        return real_open(path, flags, *args, **kwargs)

    # Agent access is denied; helper runs the actual fixed program separately.
    real_request = engine.request

    def request(
        method: str, path: str, payload: Any = None, *, timeout: float = 5.0
    ) -> tuple[int, object]:
        if "/wait?" in path:
            helper = engine.helpers[path.split("/")[2]]
            result = run_script(cache, helper["Cmd"][0])
            assert result.stdout == result.stderr == ""
            return 200, {"StatusCode": result.returncode}
        return real_request(method, path, payload, timeout=timeout)

    monkeypatch.setattr(os, "open", agent_open)
    monkeypatch.setattr(engine, "request", request)
    manager.remove(cache.name)
    assert not cache.exists() and (sibling / "keep").read_text() == "brother"
    assert engine.deleted == 1 and engine.helpers == {}
    assert [p["Cmd"] for p in engine.payloads] == [["check"], ["delete"]]
    for payload in engine.payloads:
        assert payload["Image"] == "sha256:" + "a" * 64
        assert payload["User"] == "10008:10008"
        assert payload["Entrypoint"] == ["python3", "-I", "-B", "-c", _CACHE_CLEANUP_SCRIPT]
        assert payload["Labels"] == {"holm.managed": "true", "holm.instance_id": cache.name}
        host = payload["HostConfig"]
        assert payload["Env"] == ["NVIDIA_VISIBLE_DEVICES=void"]
        assert host["Runtime"] == "runc"
        assert host["PidsLimit"] == 16
        assert host["Memory"] == 128 * 1024 * 1024
        assert host["NanoCpus"] == 10**9
        assert host["NetworkMode"] == "none"
        assert host["CapDrop"] == ["ALL"]
        assert host["SecurityOpt"] == ["no-new-privileges:true"]
        assert host["ReadonlyRootfs"] is True and host["Privileged"] is False
        assert host["PublishAllPorts"] is False and host["LogConfig"] == {"Type": "none"}
        assert "DeviceRequests" not in host and "Binds" not in host
        assert host["Mounts"] == [
            {
                "Type": "bind",
                "Source": str(cache),
                "Target": "/var/cache/holm",
                "ReadOnly": payload["Cmd"] == ["check"],
            }
        ]


@pytest.mark.parametrize("kind", ["symlink", "fifo", "device"])
@pytest.mark.parametrize("mode", ["check", "delete"])
def test_FIX_30_script_unsafe_tree_never_mutates(tmp_path: Path, kind: str, mode: str) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    safe = cache / "safe"
    safe.mkdir(mode=0o500)
    (safe / "sentinel").write_text("FX-FIX-30")
    if kind == "symlink":
        (cache / "unsafe").symlink_to(tmp_path)
    elif kind == "fifo":
        os.mkfifo(cache / "unsafe")
    result = run_script(cache, mode, cross_device=kind == "device")
    assert result.returncode == 3
    assert result.stdout == result.stderr == ""
    assert (safe / "sentinel").read_text() == "FX-FIX-30"
    assert stat.S_IMODE(safe.stat().st_mode) == 0o500


@pytest.mark.parametrize("code", [1, 3])
def test_FIX_30_helper_rejection_preserves_runtime_and_cache(tmp_path: Path, code: int) -> None:
    manager, engine, cache, _ = setup(tmp_path)
    engine.exit_code = code
    expected = "runtime_cache_unsafe" if code == 3 else "runtime_cache_cleanup_failed"
    with pytest.raises(RuntimeRequestError, match=f"^{expected}$"):
        manager.remove(cache.name)
    assert engine.deleted == 0 and engine.helpers == {}
    assert (cache / "compiled.bin").read_bytes() == b"FX-M3-07C cache"


def test_FIX_30_delete_failure_and_missing_runtime_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, engine, cache, _ = setup(tmp_path)
    original = engine.request

    def request(
        method: str, path: str, payload: Any = None, *, timeout: float = 5.0
    ) -> tuple[int, object]:
        if "/wait?" in path and engine.helpers[path.split("/")[2]]["Cmd"] == ["delete"]:
            return 200, {"StatusCode": 1, "Error": {"Message": "/secret/private/output"}}
        return original(method, path, payload, timeout=timeout)

    monkeypatch.setattr(engine, "request", request)
    with pytest.raises(RuntimeRequestError, match="^runtime_cache_cleanup_failed$"):
        manager.remove(cache.name)
    assert engine.value is None and cache.exists() and engine.helpers == {}
    monkeypatch.setattr(engine, "request", original)
    manager.remove(cache.name)
    assert not cache.exists() and engine.helpers == {}


@pytest.mark.parametrize("missing", ["identity", "image"])
def test_FIX_30_missing_configuration_fails_closed(tmp_path: Path, missing: str) -> None:
    manager, engine, cache, _ = setup(tmp_path)
    if missing == "identity":
        manager._identity = None
    else:
        manager.set_runtimes({})
    with pytest.raises(RuntimeRequestError, match="^runtime_cache_cleanup_failed$"):
        manager.remove(cache.name)
    assert cache.exists() and engine.deleted == 0 and engine.payloads == []


def test_FIX_30_agent_cross_device_boundary_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, engine, cache, _ = setup(tmp_path)
    original = os.fstat

    def fstat(fd: int) -> os.stat_result:
        value = original(fd)
        if value.st_ino == cache.stat().st_ino:
            fields = list(value)
            fields[2] += 1
            return os.stat_result(fields)
        return value

    monkeypatch.setattr(os, "fstat", fstat)
    with pytest.raises(RuntimeRequestError, match="^runtime_cache_unsafe$"):
        manager.remove(cache.name)
    assert engine.deleted == 0 and engine.payloads == [] and cache.exists()

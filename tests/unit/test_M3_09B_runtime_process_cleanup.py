"""M3-09B cleanup coverage for Runtime groups whose leader already exited."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Final

import holm_host_agent.runtime_process as runtime_process
import pytest
from holm_host_agent.runtime_process import (
    DEFAULT_STOP_GRACE_SECONDS,
    RuntimeLaunchSpec,
    RuntimeManager,
    RuntimeObservation,
    RuntimeStateValue,
)

INSTANCE_ID: Final[str] = "0199f7d8-22a0-7000-8000-000000000009"


def test_M3_07_TC_GPU_20_host_agent_default_grace_is_sixty_seconds() -> None:
    assert DEFAULT_STOP_GRACE_SECONDS == 60


def _manager(tmp_path: Path) -> RuntimeManager:
    return RuntimeManager(
        compatibility_path=tmp_path / "unused-runtime-compat.yaml",
        state_root=tmp_path / "state",
        model_path_policy=None,
        runtime_identity=None,
        nvml_factory=lambda: pytest.fail("GPU discovery must not run in cleanup tests"),
    )


def _write_state(
    tmp_path: Path,
    *,
    pid: int,
    start_ticks: int = 1,
    state: str = RuntimeStateValue.RUNNING.value,
) -> None:
    state_root = tmp_path / "state" / INSTANCE_ID
    state_root.mkdir(parents=True)
    (state_root / "runtime.json").write_text(
        json.dumps(
            {
                "instance_id": INSTANCE_ID,
                "pid": pid,
                "start_ticks": start_ticks,
                "config_version": "m3-09b-v1",
                "endpoint": "127.0.0.1:38123",
                "started_at": "2026-09-16T00:00:00Z",
                "state": state,
                "exit_code": None,
            }
        ),
        encoding="utf-8",
    )


def _wait_for_file(path: Path) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.01)
    pytest.fail(f"timed out waiting for {path.name}")


def test_M3_09B_stop_cleans_live_group_after_leader_exit_and_is_idempotent(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "child-ready"
    release = tmp_path / "release-leader"
    script = """
import os
import pathlib
import sys
import time

ready = pathlib.Path(sys.argv[1])
release = pathlib.Path(sys.argv[2])
if os.fork() == 0:
    while True:
        time.sleep(60)
ready.write_text("ready", encoding="ascii")
while not release.exists():
    time.sleep(0.01)
"""
    leader = subprocess.Popen(  # noqa: S603 - fixed interpreter and inline fixture.
        (sys.executable, "-c", script, str(ready), str(release)),
        start_new_session=True,
    )
    try:
        _wait_for_file(ready)
        _state, start_ticks, process_group = runtime_process._process_identity(leader.pid)
        assert process_group == leader.pid
        _write_state(tmp_path, pid=leader.pid, start_ticks=start_ticks)
        release.touch()
        assert leader.wait(timeout=5) == 0
        assert runtime_process._group_has_live_processes(leader.pid)

        manager = _manager(tmp_path)
        first = asyncio.run(manager.stop(INSTANCE_ID, grace_seconds=1))
        second = asyncio.run(manager.stop(INSTANCE_ID, grace_seconds=1))

        assert first.state is RuntimeStateValue.EXITED
        assert second.state is RuntimeStateValue.EXITED
        assert not runtime_process._group_has_live_processes(leader.pid)
    finally:
        if runtime_process._group_has_live_processes(leader.pid):
            os.killpg(leader.pid, signal.SIGKILL)
        leader.wait(timeout=5)


def test_M3_09B_empty_group_never_calls_killpg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(tmp_path, pid=2_000_000)
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(runtime_process, "_group_has_live_processes", lambda _group: False)
    monkeypatch.setattr(
        runtime_process,
        "_signal_group",
        lambda group, selected: signals.append((group, selected)),
    )

    observation = asyncio.run(_manager(tmp_path).stop(INSTANCE_ID, grace_seconds=1))

    assert observation.state is RuntimeStateValue.EXITED
    assert signals == []


def test_M3_09B_ensure_cleans_residual_group_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_group = 2_000_001
    _write_state(tmp_path, pid=old_group)
    events: list[str] = []
    monkeypatch.setattr(runtime_process, "_group_has_live_processes", lambda _group: True)
    monkeypatch.setattr(
        runtime_process,
        "_signal_group",
        lambda group, selected: events.append(f"signal:{group}:{selected.name}"),
    )

    async def group_exits(_group: int, _timeout: float) -> bool:
        events.append("wait")
        return True

    monkeypatch.setattr(runtime_process, "_wait_for_group_exit", group_exits)
    manager = _manager(tmp_path)
    monkeypatch.setattr(manager, "_validate", lambda _spec: object())

    async def launch(
        _canonical_id: str,
        _runtime: object,
        _spec: RuntimeLaunchSpec,
    ) -> RuntimeObservation:
        events.append("launch")
        return RuntimeObservation(RuntimeStateValue.RUNNING, endpoint="127.0.0.1:38124")

    monkeypatch.setattr(manager, "_launch", launch)
    spec = RuntimeLaunchSpec(
        instance_id=INSTANCE_ID,
        runtime_name="vllm",
        model_path="/unused",
        gpu_uuids=("GPU-unused",),
        tensor_parallel_size=1,
        context_length=4096,
        gpu_memory_utilization=0.9,
        served_model_name="mi_m309b",
        config_version="m3-09b-v1",
    )

    observation = asyncio.run(manager.ensure(spec))

    assert observation.state is RuntimeStateValue.RUNNING
    assert events == [f"signal:{old_group}:SIGTERM", "wait", "launch"]


def test_M3_09B_cleanup_escalates_when_group_ignores_sigterm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[signal.Signals] = []
    waits = iter((False, True))
    monkeypatch.setattr(runtime_process, "_group_has_live_processes", lambda _group: True)
    monkeypatch.setattr(
        runtime_process,
        "_signal_group",
        lambda _group, selected: signals.append(selected),
    )

    async def wait(_group: int, _timeout: float) -> bool:
        return next(waits)

    monkeypatch.setattr(runtime_process, "_wait_for_group_exit", wait)

    asyncio.run(runtime_process._terminate_process_group_if_live(12345, 0.01))

    assert signals == [signal.SIGTERM, signal.SIGKILL]

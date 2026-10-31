"""PROTO-02 fake-vLLM lifecycle through the real Host Agent UDS."""

# ruff: noqa: I001, E402 -- standalone fixture must configure Host Agent before grpc.

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pwd
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

# The standalone fixture has the same initialization order as the production
# entrypoint, independent of other pytest modules that have already loaded grpc.
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import holm_host_agent  # noqa: F401
import grpc
import pytest
from fixtures.fake_nvml import FixtureId, scenario_for
from holm_contracts.agent.v1 import agent_pb2, agent_pb2_grpc
from holm_contracts.external_paths import ExternalPathPolicy
from holm_host_agent import runtime_process as runtime_process_module
from holm_host_agent import server as server_module
from holm_host_agent.authorization import IdentityCatalog
from holm_host_agent.path_validation import RuntimeIdentity
from holm_host_agent.runtime_process import (
    MAX_RUNTIME_PORT_LAUNCH_ATTEMPTS,
    RuntimeLaunchSpec,
    RuntimeManager,
    RuntimeRequestError,
    RuntimeStateValue,
)
from holm_host_agent.server import HostAgentServer

GPU_UUID: Final[str] = "GPU-00000000-0000-0002-0000-000000000001"
INSTANCE_ID: Final[str] = "0199f7d8-22a0-7000-8000-000000000102"
PEER_UID: Final[int] = 21_003

_FAKE_VLLM = r"""#!/usr/bin/python3
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

parser = argparse.ArgumentParser()
parser.add_argument("serve")
parser.add_argument("model_path")
parser.add_argument("--host", required=True)
parser.add_argument("--port", required=True, type=int)
parser.add_argument("--tensor-parallel-size", required=True, type=int)
parser.add_argument("--max-model-len", required=True, type=int)
parser.add_argument("--gpu-memory-utilization", required=True, type=float)
parser.add_argument("--served-model-name", required=True)
parser.add_argument("--disable-log-requests", action="store_true")
args = parser.parse_args()
assert args.serve == "serve"
assert args.disable_log_requests

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/v1/models":
            self.send_error(404)
            return
        body = json.dumps({"data": [{"id": args.served_model_name}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *values):
        pass

ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
"""


def _uds_channel(socket_path: Path) -> grpc.aio.Channel:
    return grpc.aio.insecure_channel(
        f"unix:{socket_path}",
        options=(("grpc.use_local_subchannel_pool", 1),),
    )


def _runtime_identity() -> RuntimeIdentity:
    if os.geteuid() != 0:
        return RuntimeIdentity(uid=os.geteuid(), gid=os.getegid())
    account = pwd.getpwnam("nobody")
    return RuntimeIdentity(uid=account.pw_uid, gid=account.pw_gid)


def _write_catalog(path: Path, executable: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runtimes": [
                    {
                        "name": "vllm",
                        "version": "0.29.0",
                        "launcher": "process",
                        "executable": str(executable),
                        "supported_architectures": ["Qwen2ForCausalLM"],
                        "reasoning_parsers": {},
                        "tool_call_parsers": {},
                        "request_capabilities": {"openai_chat_completions": ["messages", "model"]},
                        "gpu_memory_utilization": {"minimum": 0.1, "maximum": 0.95},
                        "gpu_policy": {
                            "certified_gpu_counts": [1],
                            "max_gpus_per_instance": 1,
                            "require_homogeneous_devices": True,
                            "p2p_topology": "preferred",
                        },
                        "tp_policy": {
                            "size_source": "selected_gpu_count",
                            "model_partition_check": "runtime_adapter",
                        },
                        "context_policy": {
                            "allowed_steps": [2048, 4096],
                            "default": 2048,
                        },
                        "failure_signatures": {
                            "out_of_memory": [
                                "CUDA out of memory",
                                "torch.OutOfMemoryError",
                            ]
                        },
                        "arguments": {
                            "reasoning_parser": "--reasoning-parser",
                            "tool_call_parser": "--tool-call-parser",
                            "enable_auto_tool_choice": None,
                            "serve": "serve",
                            "host": "--host",
                            "port": "--port",
                            "tensor_parallel_size": "--tensor-parallel-size",
                            "max_model_len": "--max-model-len",
                            "gpu_memory_utilization": "--gpu-memory-utilization",
                            "served_model_name": "--served-model-name",
                            "disable_request_logging": "--disable-log-requests",
                            "enable_metrics": None,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _get_json(url: str) -> object:
    with urllib.request.urlopen(url, timeout=0.2) as response:  # noqa: S310 - loopback only
        return json.loads(response.read())


async def _wait_for_models(endpoint: str) -> None:
    for _attempt in range(100):
        try:
            payload = await asyncio.to_thread(_get_json, f"http://{endpoint}/v1/models")
        except (OSError, TimeoutError, urllib.error.URLError):
            await asyncio.sleep(0.02)
            continue
        assert isinstance(payload, dict)
        assert payload["data"] == [{"id": "mi_proto02"}]
        return
    pytest.fail("fake vLLM did not become ready")


def _request(*, config_version: str = "proto-02-v1") -> agent_pb2.EnsureRuntimeRequest:
    return agent_pb2.EnsureRuntimeRequest(
        instance_id=INSTANCE_ID,
        runtime_name="vllm",
        model_path="",
        gpu_uuids=[GPU_UUID],
        tensor_parallel_size=1,
        context_length=4096,
        gpu_memory_utilization=0.9,
        served_model_name="mi_proto02",
        config_version=config_version,
    )


def _runtime_manager_case(
    runtime_root: Path,
    *,
    executable_source: str = _FAKE_VLLM,
) -> tuple[RuntimeManager, RuntimeLaunchSpec]:
    runtime_root.chmod(0o755)
    executable = runtime_root / "fake-vllm"
    executable.write_text(executable_source, encoding="utf-8")
    executable.chmod(0o755)
    model_root = runtime_root / "models"
    model = model_root / "qwen"
    model.mkdir(parents=True)
    # FX-FIX-19: valid metadata keeps launch-failure diagnostics isolated.
    (model / "config.json").write_text('{"architectures": ["Qwen2ForCausalLM"]}')
    model_root.chmod(0o755)
    model.chmod(0o755)
    config = runtime_root / "runtime-compat.yaml"
    _write_catalog(config, executable)
    manager = RuntimeManager(
        compatibility_path=config,
        state_root=runtime_root / "runtime-state",
        model_path_policy=ExternalPathPolicy.for_configured_roots((model_root,)),
        runtime_identity=_runtime_identity(),
        nvml_factory=lambda: scenario_for(FixtureId.FX_NV_02).open(),
    )
    spec = RuntimeLaunchSpec(
        instance_id=INSTANCE_ID,
        runtime_name="vllm",
        model_path=str(model),
        gpu_uuids=(GPU_UUID,),
        tensor_parallel_size=1,
        context_length=4096,
        gpu_memory_utilization=0.9,
        served_model_name="mi_proto02",
        config_version="fix-07-v1",
    )
    return manager, spec


async def _start_server(
    socket_path: Path,
    *,
    config: Path,
    state_root: Path,
    model_root: Path,
    identity: RuntimeIdentity,
) -> HostAgentServer:
    server = HostAgentServer(
        socket_path,
        identity_catalog=IdentityCatalog.from_pairs(((PEER_UID, "instance-controller"),)),
        runtime_identity=identity,
        model_roots=(model_root,),
        runtime_compatibility_path=config,
        runtime_state_root=state_root,
        nvml_factory=lambda: scenario_for(FixtureId.FX_NV_02).open(),
    )
    await server.start()
    return server


def _fix_07_receive(connection: socket.socket) -> bytes:
    try:
        message = connection.recv(1)
    except TimeoutError as error:
        raise AssertionError("FIX-07A child handshake timed out") from error
    assert message, "FIX-07A child handshake closed before its outcome"
    return message


@pytest.fixture
def fix_07_launch_order(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, list[str]]]:
    """FX-FIX-07A: real child bind, ordered after the parent's /proc identity read."""
    original_spawn = subprocess.Popen
    original_identity = runtime_process_module._process_identity
    processes: list[subprocess.Popen[bytes]] = []
    events: list[str] = []
    synchronized: set[int] = set()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(3)
        handshake_port = listener.getsockname()[1]

        def spawn(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
            process = original_spawn(*args, **kwargs)
            processes.append(process)
            return process

        def identify(pid: int) -> tuple[str, int, int]:
            identity = original_identity(pid)
            if pid in synchronized or not processes or processes[-1].pid != pid:
                return identity
            synchronized.add(pid)
            process = processes[-1]
            try:
                connection, _address = listener.accept()
            except TimeoutError as error:
                raise AssertionError("FIX-07A child did not reach the bind barrier") from error
            with connection:
                connection.settimeout(3)
                assert _fix_07_receive(connection) == b"W"
                assert process.poll() is None
                events.append("blocked-before-bind")
                connection.sendall(b"G")
                outcome = _fix_07_receive(connection)
                assert outcome in (b"B", b"F", b"E")
                if outcome != b"B":
                    # Reap only after the real /proc read; the production poll
                    # still observes the actual exit code, never a fabricated one.
                    assert process.wait(timeout=3) != 0
                events.append({b"B": "bound", b"F": "failed", b"E": "exit"}[outcome])
            return identity

        monkeypatch.setattr(subprocess, "Popen", spawn)
        monkeypatch.setattr(runtime_process_module, "_process_identity", identify)
        source = _FAKE_VLLM.replace(
            "ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()",
            f"""import socket
with socket.create_connection(("127.0.0.1", {handshake_port}), timeout=3) as handshake:
    handshake.sendall(b"W")
    assert handshake.recv(1) == b"G"
    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError:
        handshake.sendall(b"F")
        raise
    handshake.sendall(b"B")
server.serve_forever()
""",
        )
        try:
            yield source, events
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=3)


def test_FIX_07A_missing_child_handshake_fails_with_bounded_wait() -> None:
    parent, child = socket.socketpair()
    with parent, child:
        parent.settimeout(0.01)
        # Keep the child end open but deliberately withhold its handshake.
        with pytest.raises(AssertionError, match="child handshake timed out"):
            _fix_07_receive(parent)
        child.close()
        with pytest.raises(AssertionError, match="closed before its outcome"):
            _fix_07_receive(parent)


def test_FIX_07_occupied_selected_port_is_reselected_before_runtime_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    fix_07_launch_order: tuple[str, list[str]],
) -> None:
    runtime_root = Path(tempfile.mkdtemp(prefix="holm-fix-07-reselect-"))
    original_selector = runtime_process_module._unused_loopback_port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupant:
        occupant.bind(("127.0.0.1", 0))
        occupant.listen()
        occupied_port = int(occupant.getsockname()[1])
        selections = 0

        def select_port() -> int:
            nonlocal selections
            selections += 1
            if selections == 1:
                return occupied_port
            return original_selector()

        monkeypatch.setattr(runtime_process_module, "_unused_loopback_port", select_port)
        source, events = fix_07_launch_order
        manager, spec = _runtime_manager_case(runtime_root, executable_source=source)

        async def scenario() -> None:
            observation = await manager.ensure(spec)
            assert observation.state is RuntimeStateValue.RUNNING
            assert observation.endpoint != f"127.0.0.1:{occupied_port}"
            assert selections == 2
            assert events == ["blocked-before-bind", "failed", "blocked-before-bind", "bound"]
            await manager.stop(INSTANCE_ID, grace_seconds=1)

        try:
            asyncio.run(scenario())
        finally:
            shutil.rmtree(runtime_root)


def test_FIX_07_persistently_occupied_port_exhausts_bounded_attempts(
    monkeypatch: pytest.MonkeyPatch,
    fix_07_launch_order: tuple[str, list[str]],
) -> None:
    runtime_root = Path(tempfile.mkdtemp(prefix="holm-fix-07-exhausted-"))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupant:
        occupant.bind(("127.0.0.1", 0))
        occupant.listen()
        occupied_port = int(occupant.getsockname()[1])
        selections = 0

        def select_occupied_port() -> int:
            nonlocal selections
            selections += 1
            return occupied_port

        monkeypatch.setattr(
            runtime_process_module,
            "_unused_loopback_port",
            select_occupied_port,
        )
        source, events = fix_07_launch_order
        manager, spec = _runtime_manager_case(runtime_root, executable_source=source)

        async def scenario() -> None:
            with pytest.raises(RuntimeRequestError) as captured:
                await manager.ensure(spec)
            assert captured.value.code == "runtime_start_failed"
            assert selections == MAX_RUNTIME_PORT_LAUNCH_ATTEMPTS
            assert events == ["blocked-before-bind", "failed"] * MAX_RUNTIME_PORT_LAUNCH_ATTEMPTS
            await manager.stop(INSTANCE_ID, grace_seconds=1)

        try:
            asyncio.run(scenario())
        finally:
            shutil.rmtree(runtime_root)


def test_FIX_07_non_port_immediate_exit_does_not_retry(
    monkeypatch: pytest.MonkeyPatch,
    fix_07_launch_order: tuple[str, list[str]],
) -> None:
    runtime_root = Path(tempfile.mkdtemp(prefix="holm-fix-07-non-port-"))
    original_selector = runtime_process_module._unused_loopback_port
    selections = 0

    def select_port() -> int:
        nonlocal selections
        selections += 1
        return original_selector()

    monkeypatch.setattr(runtime_process_module, "_unused_loopback_port", select_port)
    source, events = fix_07_launch_order
    source = source.replace(
        "    try:\n        server =",
        '    handshake.sendall(b"E")\n    raise SystemExit(23)\n    try:\n        server =',
    )
    manager, spec = _runtime_manager_case(runtime_root, executable_source=source)

    async def scenario() -> None:
        with pytest.raises(RuntimeRequestError) as captured:
            await manager.ensure(spec)
        assert captured.value.code == "runtime_start_failed"
        assert selections == 1
        assert events == ["blocked-before-bind", "exit"]
        await manager.stop(INSTANCE_ID, grace_seconds=1)

    try:
        asyncio.run(scenario())
    finally:
        shutil.rmtree(runtime_root)


def test_PROTO_02_PROTO_17_fake_runtime_lifecycle_keeps_spawn_security(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(  # noqa: S603 - fixed fake Runtime lifecycle fixture.
        [sys.executable, str(Path(__file__).resolve()), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "FIX-13 lifecycle assertions completed" in completed.stdout
    assert "skipping fork() handlers" not in completed.stderr


def _fake_runtime_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = Path(tempfile.mkdtemp(prefix="holm-proto-02-"))
    runtime_root.chmod(0o755)
    sentinel_path = runtime_root / "parent-only-fd"
    sentinel_path.touch()
    sentinel_fd = os.open(sentinel_path, os.O_RDONLY)
    os.set_inheritable(sentinel_fd, True)

    async def scenario() -> None:
        executable = runtime_root / "fake-vllm"
        executable.write_text(_FAKE_VLLM, encoding="utf-8")
        executable.chmod(0o755)
        model_root = runtime_root / "models"
        model = model_root / "qwen"
        model.mkdir(parents=True)
        model_root.chmod(0o755)
        model.chmod(0o755)
        config = runtime_root / "runtime-compat.yaml"
        _write_catalog(config, executable)
        state_root = runtime_root / "runtime-state"
        socket_path = tmp_path / "agent.sock"
        identity = _runtime_identity()
        monkeypatch.setattr(server_module, "_read_peer_uid", lambda _socket: PEER_UID)
        request = _request()
        request.model_path = str(model)

        first_server = await _start_server(
            socket_path,
            config=config,
            state_root=state_root,
            model_root=model_root,
            identity=identity,
        )
        endpoint = ""
        first_phase_complete = False
        try:
            async with _uds_channel(socket_path) as channel:
                await asyncio.wait_for(channel.channel_ready(), timeout=2)
                client = agent_pb2_grpc.HostAgentStub(channel)
                first = await client.EnsureRuntime(request, timeout=3)
                assert first.status == agent_pb2.RUNTIME_PROCESS_STATE_RUNNING
                endpoint = first.endpoint
                same = await client.EnsureRuntime(request, timeout=3)
                assert same.endpoint == endpoint

                conflicting = _request(config_version="proto-02-v2")
                conflicting.model_path = str(model)
                with pytest.raises(grpc.aio.AioRpcError) as conflict:
                    await client.EnsureRuntime(conflicting, timeout=3)
                assert conflict.value.code() == grpc.StatusCode.FAILED_PRECONDITION
                assert conflict.value.details() == "runtime_config_conflict"
                await _wait_for_models(endpoint)

                state = json.loads(
                    (state_root / INSTANCE_ID / "runtime.json").read_text(encoding="utf-8")
                )
                pid = int(state["pid"])
                command = (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
                assert b"--trust-remote-code" not in command
                status_lines = (Path("/proc") / str(pid) / "status").read_text().splitlines()
                process_status = {
                    key: value.strip()
                    for key, value in (line.split(":", 1) for line in status_lines if ":" in line)
                }
                assert {int(value) for value in process_status["Uid"].split()} == {identity.uid}
                assert {int(value) for value in process_status["Gid"].split()} == {identity.gid}
                assert process_status["Groups"] == ""
                assert os.getsid(pid) == pid
                inherited_targets: set[str] = set()
                for descriptor in (Path("/proc") / str(pid) / "fd").iterdir():
                    with contextlib.suppress(OSError):
                        inherited_targets.add(os.readlink(descriptor))
                assert str(sentinel_path) not in inherited_targets
                runtime_log = state_root / INSTANCE_ID / "runtime.log"
                assert stat.S_IMODE(runtime_log.stat().st_mode) == 0o600
                first_phase_complete = True
        finally:
            if not first_phase_complete:
                state_path = state_root / INSTANCE_ID / "runtime.json"
                if state_path.is_file():
                    failed_pid = int(json.loads(state_path.read_text(encoding="utf-8"))["pid"])
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(failed_pid, signal.SIGKILL)
            await first_server.stop(grace=0)

        second_server = await _start_server(
            socket_path,
            config=config,
            state_root=state_root,
            model_root=model_root,
            identity=identity,
        )
        try:
            async with _uds_channel(socket_path) as channel:
                await asyncio.wait_for(channel.channel_ready(), timeout=2)
                client = agent_pb2_grpc.HostAgentStub(channel)
                inspected = await client.InspectRuntime(
                    agent_pb2.InspectRuntimeRequest(instance_id=INSTANCE_ID),
                    timeout=3,
                )
                assert inspected.status == agent_pb2.RUNTIME_PROCESS_STATE_RUNNING
                assert inspected.endpoint == endpoint
                assert inspected.started_at.endswith("Z")
                assert not inspected.HasField("oom_killed")

                stopped = await client.StopRuntime(
                    agent_pb2.StopRuntimeRequest(instance_id=INSTANCE_ID, grace_seconds=1),
                    timeout=5,
                )
                assert stopped.status == agent_pb2.RUNTIME_PROCESS_STATE_EXITED
                exited = await client.InspectRuntime(
                    agent_pb2.InspectRuntimeRequest(instance_id=INSTANCE_ID),
                    timeout=3,
                )
                assert exited.status == agent_pb2.RUNTIME_PROCESS_STATE_EXITED
                # The restarted Agent has no Popen handle from which to collect
                # this intentional stop's exit code, so OOM is honestly absent.
                assert not exited.HasField("oom_killed")
        finally:
            state_path = state_root / INSTANCE_ID / "runtime.json"
            if state_path.is_file():
                final_pid = int(json.loads(state_path.read_text(encoding="utf-8"))["pid"])
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(final_pid, signal.SIGKILL)
            await second_server.stop(grace=0)

    try:
        asyncio.run(scenario())
    except BaseException:
        # FIX-13: this fake serves only /v1/models, never inference bodies.
        # Emit evidence before the existing cleanup removes its private root.
        runtime_log = runtime_root / "runtime-state" / INSTANCE_ID / "runtime.log"
        if runtime_log.is_file():
            with runtime_log.open("rb") as log:
                log.seek(max(0, runtime_log.stat().st_size - 8192))
                print("FIX-13 fake runtime.log tail:", log.read(8192).decode(errors="replace"))
        else:
            print("FIX-13 fake runtime.log missing")
        raise
    finally:
        os.close(sentinel_fd)
        shutil.rmtree(runtime_root)


def test_PROTO_02_proto_has_no_raw_command_or_environment_surface() -> None:
    fields: set[str] = set()
    service = agent_pb2.DESCRIPTOR.services_by_name["HostAgent"]
    for method_name in ("EnsureRuntime", "StopRuntime", "InspectRuntime"):
        fields.update(
            field.name for field in service.methods_by_name[method_name].input_type.fields
        )
    assert not fields.intersection({"args", "command", "entrypoint", "env", "secret"})


@pytest.mark.parametrize("stage", ["spawn", "identity"])
def test_FIX_13_start_exception_diagnostic_is_content_free(
    stage: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """FX-FIX-13: exception sentinel at the two existing launch boundaries."""
    runtime_root = Path(tempfile.mkdtemp(prefix="holm-fix-13-"))
    manager, spec = _runtime_manager_case(runtime_root)
    original_spawn = subprocess.Popen
    processes: list[subprocess.Popen[bytes]] = []
    sentinel = f"PWSSNTL-SRCTOK-FIX13-{secrets.token_hex(6).upper()}"

    def spawn(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        if stage == "spawn":
            raise OSError(sentinel)
        process = original_spawn(*args, **kwargs)
        processes.append(process)
        return process

    def identify(pid: int) -> tuple[str, int, int]:
        raise ProcessLookupError(sentinel)

    monkeypatch.setattr(subprocess, "Popen", spawn)
    if stage == "identity":
        monkeypatch.setattr(runtime_process_module, "_process_identity", identify)

    async def scenario() -> None:
        with pytest.raises(RuntimeRequestError) as captured:
            await manager.ensure(spec)
        assert captured.value.code == "runtime_start_failed"

    try:
        asyncio.run(scenario())
        records = [r for r in caplog.records if r.name == runtime_process_module.__name__]
        assert len(records) == 1
        assert json.loads(records[0].getMessage()) == {
            "event": "runtime_launch_failure",
            "reason": stage,
            "exit_code": None,
            "port_probe": "not_run",
        }
        assert records[0].exc_info is None
        assert sentinel not in caplog.text
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
        shutil.rmtree(runtime_root)


def test_FIX_13_early_exit_diagnostic_reports_actual_exit_and_port(
    fix_07_launch_order: tuple[str, list[str]], caplog: pytest.LogCaptureFixture
) -> None:
    """FX-FIX-07A orders a real bind failure after the /proc identity read."""
    runtime_root = Path(tempfile.mkdtemp(prefix="holm-fix-13-exit-"))
    source, events = fix_07_launch_order
    source = source.replace(
        "    try:\n        server =",
        '    handshake.sendall(b"E")\n    raise SystemExit(23)\n    try:\n        server =',
    )
    manager, spec = _runtime_manager_case(runtime_root, executable_source=source)

    async def scenario() -> None:
        with pytest.raises(RuntimeRequestError) as captured:
            await manager.ensure(spec)
        assert captured.value.code == "runtime_start_failed"
        assert events == ["blocked-before-bind", "exit"]
        await manager.stop(INSTANCE_ID, grace_seconds=1)

    try:
        asyncio.run(scenario())
        records = [r for r in caplog.records if r.name == runtime_process_module.__name__]
        assert len(records) == 1
        assert json.loads(records[0].getMessage()) == {
            "event": "runtime_launch_failure",
            "reason": "early_exit",
            "exit_code": 23,
            "port_probe": "free",
        }
        assert records[0].exc_info is None
    finally:
        shutil.rmtree(runtime_root)


def test_FIX_13_lifecycle_failure_emits_fake_runtime_tail_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """FX-FIX-13: fail after writing a harmless fake startup log, before cleanup."""
    log_paths: list[Path] = []

    async def fail_start(_socket_path: Path, **kwargs: Any) -> HostAgentServer:
        log = kwargs["state_root"] / INSTANCE_ID / "runtime.log"
        log.parent.mkdir(parents=True)
        log.write_text("old-prefix" + "x" * 8192 + "FX-FIX-13-startup-evidence")
        log_paths.append(log)
        raise RuntimeError("FX-FIX-13-startup-failed")

    monkeypatch.setattr(sys.modules[__name__], "_start_server", fail_start)
    with pytest.raises(RuntimeError, match="FX-FIX-13-startup-failed"):
        _fake_runtime_lifecycle(tmp_path, monkeypatch)
    output = capsys.readouterr().out
    assert "FIX-13 fake runtime.log tail:" in output
    assert "FX-FIX-13-startup-evidence" in output
    assert "old-prefix" not in output
    assert len(log_paths) == 1
    assert not log_paths[0].exists()


def test_FIX_13_lifecycle_bootstrap_configures_fork_before_grpc(tmp_path: Path) -> None:
    """FX-FIX-13: force unsafe ambient config and observe the first native import."""
    code = r"""
import os
import runpy
import sys
from importlib.abc import MetaPathFinder

class CheckGrpcImport(MetaPathFinder):
    observed = False

    def find_spec(self, fullname, path, target=None):
        if fullname == "grpc":
            self.observed = True
            assert os.environ.get("GRPC_ENABLE_FORK_SUPPORT") == "false", (
                "FIX-13: grpc imported before Host Agent fork configuration"
            )
        return None

check = CheckGrpcImport()
sys.meta_path.insert(0, check)
sys.path.insert(0, "tests")
sys.argv = [sys.argv[1], sys.argv[2]]
runpy.run_path(sys.argv[0], run_name="__main__")
assert check.observed, "FIX-13: did not observe grpc import"
"""
    environment = os.environ.copy()
    environment["GRPC_ENABLE_FORK_SUPPORT"] = "true"
    completed = subprocess.run(  # noqa: S603 - fixed test-only bootstrap probe.
        [sys.executable, "-c", code, str(Path(__file__).resolve()), str(tmp_path)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "FIX-13 lifecycle assertions completed" in completed.stdout
    assert "skipping fork() handlers" not in completed.stderr


if __name__ == "__main__":
    with pytest.MonkeyPatch.context() as patch:
        _fake_runtime_lifecycle(Path(sys.argv[1]), patch)
    print("FIX-13 lifecycle assertions completed")

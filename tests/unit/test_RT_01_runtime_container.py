"""RT-01: Docker API requests are checked without a Docker daemon or GPU."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socketserver
import threading
from pathlib import Path
from typing import Any

import pytest
from fixtures.fake_nvml import FixtureId, scenario_for
from holm_contracts.agent.v1 import agent_pb2
from holm_contracts.external_paths import ExternalPathPolicy
from holm_host_agent.path_validation import RuntimeIdentity
from holm_host_agent.runtime_container import ContainerRuntimeManager, DockerEngine
from holm_host_agent.runtime_process import (
    RuntimeLaunchSpec,
    RuntimeManager,
    RuntimeRequestError,
    RuntimeStateValue,
)
from holm_host_agent.server import HostAgentService
from holm_runtimes import RuntimeCompatibilityError, load_runtime_compatibility

ROOT = Path(__file__).resolve().parents[2]
INSTANCE_ID = "0199f7d8-22a0-7000-8000-000000000002"
RESOURCES = {
    "port": 8000,
    "shm_bytes_per_gpu": 2 * 1024**3,
    "tmpfs_bytes": 1024**3,
    "memory_bytes_per_gpu": 16 * 1024**3,
    "nano_cpus_per_gpu": 2 * 10**9,
    "pids_limit": 512,
}


class _FakeDocker(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path: str) -> None:
        super().__init__(path, _DockerHandler)
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []
        self.containers: dict[str, dict[str, Any]] = {}
        self.containers["runtime-0199f7d8-22a0-7000-8000-000000000003"] = {
            "Config": {"Labels": {}},
            "State": {"Running": True},
            "HostConfig": {"DeviceRequests": []},
        }


class _DockerHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline().decode("ascii")
        method, url, _version = line.split()
        headers: dict[str, str] = {}
        while header := self.rfile.readline().decode("ascii").strip():
            key, value = header.split(":", 1)
            headers[key.lower()] = value.strip()
        length = int(headers.get("content-length", "0"))
        payload = json.loads(self.rfile.read(length)) if length else None
        fake = self.server
        assert isinstance(fake, _FakeDocker)
        fake.requests.append((method, url, payload))
        path = url.removeprefix("/v1.47")
        status = 200
        result: object = None
        if path == "/networks":
            result = [
                {
                    "Id": "isolated-runtime-network",
                    "Internal": True,
                    "Labels": {"com.docker.compose.network": "holm-runtime"},
                }
            ]
        elif path.startswith("/containers/create?name=") and method == "POST":
            name = path.partition("=")[2]
            assert payload is not None
            fake.containers[name] = {
                "Config": {"Labels": payload["Labels"]},
                "Cmd": payload["Cmd"],
                "Image": payload["Image"],
                "State": {"Running": False, "ExitCode": 0, "StartedAt": ""},
                "HostConfig": payload["HostConfig"],
            }
            status = 201
            result = {"Id": name}
        elif path == "/containers/json?all=1":
            result = [{"Labels": value["Config"]["Labels"]} for value in fake.containers.values()]
        elif path.startswith("/containers/"):
            parts = path.split("/")
            name = parts[2].split("?")[0]
            container = fake.containers.get(name)
            if container is None:
                status = 404
            elif method == "GET" and parts[-1] == "json":
                result = container
            elif method == "POST" and parts[-1] == "start":
                container["State"] = {"Running": True, "StartedAt": "2026-09-23T00:00:00Z"}
                status = 204
            elif method == "POST" and parts[-1].startswith("wait?"):
                if container["Cmd"] == ["delete"]:
                    cache = Path(container["HostConfig"]["Mounts"][0]["Source"])
                    for entry in cache.iterdir():
                        if entry.is_dir():
                            shutil.rmtree(entry)
                        else:
                            entry.unlink()
                container["State"] = {"Running": False, "ExitCode": 0}
                result = {"StatusCode": 0}
            elif method == "POST" and parts[-1].startswith("stop?"):
                container["State"] = {"Running": False, "ExitCode": 0}
                status = 204
            elif method == "DELETE":
                del fake.containers[name]
                status = 204
            else:
                status = 500
        else:
            status = 500
        body = b"" if result is None else json.dumps(result).encode()
        headers = f"HTTP/1.1 {status} fake\r\n"
        headers += f"Content-Length: {len(body)}\r\n"
        headers += "Connection: close\r\n\r\n"
        self.wfile.write(headers.encode() + body)


def _catalog(tmp_path: Path) -> Path:
    payload = json.loads((ROOT / "runtime-compat.yaml").read_text())
    runtime = payload["runtimes"][0]
    runtime["launcher"] = "container"
    runtime["image_digest"] = "sha256:" + "a" * 64
    runtime["container_resources"] = RESOURCES
    payload["runtimes"] = [runtime]
    config = tmp_path / "runtime-compat.yaml"
    config.write_text(json.dumps(payload))
    return config


def _spec(model_path: Path, count: int) -> RuntimeLaunchSpec:
    return RuntimeLaunchSpec(
        instance_id=INSTANCE_ID,
        runtime_name="vllm",
        model_path=str(model_path),
        gpu_uuids=tuple(f"GPU-{index:04d}" for index in range(count)),
        tensor_parallel_size=count,
        context_length=4096,
        gpu_memory_utilization=0.9,
        served_model_name="mi_rt0100",
        config_version="rt01-v1",
    )


@pytest.mark.parametrize("count", [1, 2, 8])
def test_RT_01_FX_DOCKER_01_create_and_reconcile_only_owned_containers(
    tmp_path: Path, count: int
) -> None:
    """FX-DOCKER-01: UDS fake captures the exact Engine create payload."""
    config = _catalog(tmp_path)
    runtime = load_runtime_compatibility(config).runtime("vllm")
    assert runtime is not None
    model = tmp_path / "model"
    model.mkdir()
    cache_root = tmp_path / "runtime-cache"
    cache_root.mkdir()
    os.chown(cache_root, os.getuid(), 10008)
    cache_root.chmod(0o2770)
    socket_path = tmp_path / "docker.sock"
    fake = _FakeDocker(str(socket_path))
    worker = threading.Thread(target=fake.serve_forever, daemon=True)
    worker.start()
    try:
        manager = ContainerRuntimeManager(
            cache_root=cache_root,
            runtime_identity=RuntimeIdentity(10008, 10008),
            engine=DockerEngine(socket_path),
        )
        spec = _spec(model, count)
        launched = manager.ensure(INSTANCE_ID, runtime, spec)
        assert launched.state is RuntimeStateValue.RUNNING
        assert launched.endpoint == f"runtime-{INSTANCE_ID}:8000"
        assert manager.ensure(INSTANCE_ID, runtime, spec).state is RuntimeStateValue.RUNNING
        assert (
            sum(method == "POST" and "/create?" in path for method, path, _ in fake.requests) == 1
        )
        create = next(
            body for method, path, body in fake.requests if method == "POST" and "/create?" in path
        )
        assert create is not None
        host = create["HostConfig"]
        assert create["Image"] == runtime.image_digest
        assert create["User"] == "10008:10008"
        assert create["Labels"]["holm.managed"] == "true"
        assert create["NetworkingConfig"] == {
            "EndpointsConfig": {"isolated-runtime-network": {"Aliases": [f"runtime-{INSTANCE_ID}"]}}
        }
        assert host["NetworkMode"] == "isolated-runtime-network"
        assert host["DeviceRequests"][0]["DeviceIDs"] == list(spec.gpu_uuids)
        assert host["Mounts"][0] == {
            "Type": "bind",
            "Source": str(model),
            "Target": str(model),
            "ReadOnly": True,
        }
        assert host["Mounts"][1]["Target"] == "/var/cache/holm"
        assert host["Mounts"][1]["ReadOnly"] is False
        assert host["ReadonlyRootfs"] is True
        assert host["CapDrop"] == ["ALL"]
        assert host["SecurityOpt"] == ["no-new-privileges:true"]
        assert host["ShmSize"] == RESOURCES["shm_bytes_per_gpu"] * count
        assert host["Tmpfs"]["/tmp"].startswith("rw,nosuid,nodev,size=")
        assert host["Memory"] == RESOURCES["memory_bytes_per_gpu"] * count
        assert host["LogConfig"] == {
            "Type": "local",
            "Config": {"max-size": "20m", "max-file": "5"},
        }
        assert host["RestartPolicy"] == {"Name": "no"}
        assert "PortBindings" not in host and "ExposedPorts" not in create
        assert "docker.sock" not in json.dumps(create)
        assert "secret" not in json.dumps(create).lower()
        assert (
            manager.inspect("0199f7d8-22a0-7000-8000-000000000003").state
            is RuntimeStateValue.NOT_FOUND
        )
        assert [summary.instance_id for summary in manager.list()] == [INSTANCE_ID]
        assert manager.stop(INSTANCE_ID, 1).state is RuntimeStateValue.EXITED
        manager.remove(INSTANCE_ID)
        assert len(fake.containers) == 1
        assert not any(
            method in ("POST", "DELETE") and "000000000003" in path
            for method, path, _ in fake.requests
        )
    finally:
        fake.shutdown()
        fake.server_close()
        worker.join(timeout=2)


@pytest.mark.parametrize("formal", ["owned", "missing", "unowned"])
@pytest.mark.parametrize("cleanup_running", [False, True])
def test_FIX_31_FX_DOCKER_01_list_deduplicates_formal_runtime(
    tmp_path: Path, formal: str, cleanup_running: bool
) -> None:
    """FX-DOCKER-01: synthetic duplicate labels from FIX-31's production incident."""
    socket_path = tmp_path / "docker.sock"
    fake = _FakeDocker(str(socket_path))
    labels = {
        "holm.managed": "true",
        "holm.instance_id": INSTANCE_ID,
        "holm.runtime_name": "vllm",
    }
    # Put stray containers first: their state and GPU set must never supply the summary.
    for name in ("probe", "runtime-cache-cleanup-fixture"):
        fake.containers[name] = {
            "Config": {"Labels": labels},
            "State": {"Running": cleanup_running},
            "HostConfig": {"DeviceRequests": []},
        }
    if formal != "missing":
        fake.containers[f"runtime-{INSTANCE_ID}"] = {
            "Config": {"Labels": labels if formal == "owned" else {}},
            "State": {"Running": False, "ExitCode": 0},
            "HostConfig": {"DeviceRequests": [{"DeviceIDs": ["GPU-0001"]}]},
        }
    before = json.dumps(fake.containers, sort_keys=True)
    worker = threading.Thread(target=fake.serve_forever, daemon=True)
    worker.start()
    try:
        manager = ContainerRuntimeManager(
            cache_root=tmp_path / "runtime-cache",
            runtime_identity=None,
            engine=DockerEngine(socket_path),
        )
        runtime = load_runtime_compatibility(_catalog(tmp_path)).runtime("vllm")
        assert runtime is not None
        manager.set_runtimes({"vllm": runtime})
        summaries = manager.list()
        if formal == "owned":
            assert len(summaries) == 1
            assert summaries[0].instance_id == INSTANCE_ID
            assert summaries[0].state is RuntimeStateValue.EXITED
        else:
            assert summaries == ()
        assert fake.requests == [
            ("GET", "/v1.47/containers/json?all=1", None),
            ("GET", f"/v1.47/containers/runtime-{INSTANCE_ID}/json", None),
        ]
        assert json.dumps(fake.containers, sort_keys=True) == before
    finally:
        fake.shutdown()
        fake.server_close()
        worker.join(timeout=2)


def test_RT_01_FX_DOCKER_01_remove_and_stopped_recreate(tmp_path: Path) -> None:
    config = _catalog(tmp_path)
    runtime = load_runtime_compatibility(config).runtime("vllm")
    assert runtime is not None
    model = tmp_path / "model"
    model.mkdir()
    cache_root = tmp_path / "runtime-cache"
    cache_root.mkdir()
    os.chown(cache_root, os.getuid(), 10008)
    cache_root.chmod(0o2770)
    socket_path = tmp_path / "docker.sock"
    fake = _FakeDocker(str(socket_path))
    worker = threading.Thread(target=fake.serve_forever, daemon=True)
    worker.start()
    try:
        manager = ContainerRuntimeManager(
            cache_root=cache_root,
            runtime_identity=RuntimeIdentity(10008, 10008),
            engine=DockerEngine(socket_path),
        )
        spec = _spec(model, 1)
        manager.ensure(INSTANCE_ID, runtime, spec)
        with pytest.raises(RuntimeRequestError, match="runtime_state_conflict"):
            manager.remove(INSTANCE_ID)
        manager.stop(INSTANCE_ID, 1)
        fake.containers[f"runtime-{INSTANCE_ID}"]["Config"]["Labels"]["config_version"] = (
            "old-version"
        )
        assert manager.ensure(INSTANCE_ID, runtime, spec).state is RuntimeStateValue.RUNNING
        assert sum(method == "DELETE" for method, _, _ in fake.requests) == 1
        assert sum("/create?" in path for _, path, _ in fake.requests) == 2

        manager.stop(INSTANCE_ID, 1)
        fake.containers[f"runtime-{INSTANCE_ID}"]["Config"]["Labels"] = {}
        with pytest.raises(RuntimeRequestError, match="runtime_ownership_conflict"):
            manager.remove(INSTANCE_ID)
        with pytest.raises(RuntimeRequestError, match="runtime_ownership_conflict"):
            manager.ensure(INSTANCE_ID, runtime, spec)
        assert f"runtime-{INSTANCE_ID}" in fake.containers
        assert sum(method == "DELETE" for method, _, _ in fake.requests) == 1
        del fake.containers[f"runtime-{INSTANCE_ID}"]
        manager.remove(INSTANCE_ID)

        manager.ensure(INSTANCE_ID, runtime, spec)
        manager.stop(INSTANCE_ID, 1)
        runtime_manager = RuntimeManager(
            compatibility_path=config,
            state_root=tmp_path / "state",
            model_path_policy=None,
            runtime_identity=RuntimeIdentity(10008, 10008),
            nvml_factory=lambda: scenario_for(FixtureId.FX_NV_02).open(),
        )
        runtime_manager._container = manager
        service = HostAgentService(runtime_manager=runtime_manager)
        request = agent_pb2.RemoveRuntimeRequest(instance_id=INSTANCE_ID)
        assert isinstance(
            asyncio.run(service.RemoveRuntime(request, None)),  # type: ignore[arg-type]
            agent_pb2.RemoveRuntimeResponse,
        )
        assert f"runtime-{INSTANCE_ID}" not in fake.containers
        assert isinstance(
            asyncio.run(service.RemoveRuntime(request, None)),  # type: ignore[arg-type]
            agent_pb2.RemoveRuntimeResponse,
        )
    finally:
        fake.shutdown()
        fake.server_close()
        worker.join(timeout=2)


def test_RT_01_process_remove_is_noop_or_explicitly_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = RuntimeManager(
        compatibility_path=tmp_path / "missing.yaml",
        state_root=tmp_path / "state",
        model_path_policy=None,
        runtime_identity=None,
        nvml_factory=lambda: scenario_for(FixtureId.FX_NV_02).open(),
    )
    service = HostAgentService(runtime_manager=manager)
    response = asyncio.run(
        service.RemoveRuntime(
            agent_pb2.RemoveRuntimeRequest(instance_id=INSTANCE_ID),
            None,  # type: ignore[arg-type]
        )
    )
    assert isinstance(response, agent_pb2.RemoveRuntimeResponse)
    monkeypatch.setattr(manager, "_read_state", lambda _instance_id: object())
    with pytest.raises(RuntimeRequestError, match="runtime_remove_unsupported"):
        asyncio.run(manager.remove(INSTANCE_ID))


@pytest.mark.parametrize("digest", [None, "latest", "holm/runtime:latest"])
def test_RT_01_container_digest_guard_fails_loudly(tmp_path: Path, digest: str | None) -> None:
    config = _catalog(tmp_path)
    payload = json.loads(config.read_text())
    runtime = payload["runtimes"][0]
    if digest is None:
        del runtime["image_digest"]
    else:
        runtime["image_digest"] = digest
    config.write_text(json.dumps(payload))
    with pytest.raises(RuntimeCompatibilityError, match="image_digest"):
        load_runtime_compatibility(config)


def test_RT_01_configured_container_engine_failure_never_falls_back_to_process(
    tmp_path: Path,
) -> None:
    config = _catalog(tmp_path)
    model = tmp_path / "model"
    model.mkdir()
    model.chmod(0o755)
    tmp_path.chmod(0o755)
    manager = RuntimeManager(
        compatibility_path=config,
        state_root=tmp_path / "state",
        model_path_policy=ExternalPathPolicy.for_configured_roots((tmp_path,)),
        runtime_identity=RuntimeIdentity(10008, 10008),
        nvml_factory=lambda: scenario_for(FixtureId.FX_NV_02).open(),
    )
    manager._container._engine = DockerEngine(tmp_path / "absent-docker.sock")
    spec = _spec(model, 1)
    spec = RuntimeLaunchSpec(
        instance_id=spec.instance_id,
        runtime_name=spec.runtime_name,
        model_path=spec.model_path,
        gpu_uuids=("GPU-00000000-0000-0002-0000-000000000001",),
        tensor_parallel_size=1,
        context_length=spec.context_length,
        gpu_memory_utilization=spec.gpu_memory_utilization,
        served_model_name=spec.served_model_name,
        config_version=spec.config_version,
    )
    with pytest.raises(RuntimeRequestError, match="runtime_engine_unavailable"):
        asyncio.run(manager.ensure(spec))
    assert not (tmp_path / "state" / INSTANCE_ID / "runtime.json").exists()

"""M3-07C / TC-SEC-20: FX-M3-07C synthetic cache and in-memory Docker API."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from holm_host_agent.path_validation import RuntimeIdentity
from holm_host_agent.runtime_container import ContainerRuntimeManager, DockerEngine
from holm_host_agent.runtime_process import RuntimeRequestError
from holm_runtimes import load_runtime_compatibility
from test_RT_01_runtime_container import _catalog


class Engine(DockerEngine):
    def __init__(self, instance_id: str) -> None:
        self.value: dict[str, Any] | None = {
            "Config": {"Labels": {"holm.managed": "true", "holm.instance_id": instance_id}},
            "State": {"Running": False},
        }
        self.deleted = 0
        self.helpers: dict[str, Any] = {}
        self.payloads: list[dict[str, Any]] = []
        self.exit_code = 0

    @staticmethod
    def clear_cache(cache: Path) -> None:
        for entry in cache.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()

    def request(
        self, method: str, path: str, payload: Any = None, *, timeout: float = 5.0
    ) -> tuple[int, object]:
        if path.startswith("/containers/create?"):
            self.helpers[path.partition("=")[2]] = payload
            self.payloads.append(payload)
            return 201, {"Id": path.partition("=")[2]}
        name = path.split("/")[2].split("?")[0]
        if name in self.helpers:
            helper = self.helpers[name]
            if method == "DELETE":
                del self.helpers[name]
                return 204, None
            if path.endswith("/start"):
                return 204, None
            cache = Path(helper["HostConfig"]["Mounts"][0]["Source"])
            code = self.exit_code
            if not code:
                fd = os.open(cache, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    ContainerRuntimeManager._validate_cache_tree(fd, cache.stat().st_dev)
                except RuntimeRequestError:
                    code = 3
                finally:
                    os.close(fd)
            if not code and helper["Cmd"] == ["delete"]:
                try:
                    self.clear_cache(cache)
                except OSError:
                    code = 1
            return 200, {"StatusCode": code}
        if method == "GET":
            return (200, self.value) if self.value is not None else (404, None)
        assert method == "DELETE"
        self.deleted += 1
        self.value = None
        return 204, None


def setup(tmp_path: Path) -> tuple[ContainerRuntimeManager, Engine, Path, Path]:
    root = tmp_path / "runtime-cache"
    root.mkdir()
    cache = root / str(uuid4())
    cache.mkdir()
    (cache / "compiled.bin").write_bytes(b"FX-M3-07C cache")
    sentinel = root / str(uuid4())
    sentinel.mkdir()
    (sentinel / "keep").write_text("brother")
    engine = Engine(cache.name)
    # FX-FIX-30: certified synthetic Runtime digest, never a pulled image.
    manager = ContainerRuntimeManager(
        cache_root=root, runtime_identity=RuntimeIdentity(10008, 10008), engine=engine
    )
    runtime = load_runtime_compatibility(_catalog(tmp_path)).runtime("vllm")
    assert runtime is not None
    manager.set_runtimes({runtime.name: runtime})
    return (
        manager,
        engine,
        cache,
        sentinel,
    )


@pytest.mark.parametrize("missing_container", [False, True])
def test_M3_07C_TC_DP_08_cleanup_exact_cache_and_missing_container(
    tmp_path: Path, missing_container: bool
) -> None:
    manager, engine, cache, sentinel = setup(tmp_path)
    model = tmp_path / "external-model"
    model.mkdir()
    (model / "weights").write_bytes(b"never delete")
    if missing_container:
        engine.value = None
    manager.remove(cache.name)
    manager.remove(cache.name)
    assert not cache.exists()
    assert engine.value is None
    assert (sentinel / "keep").read_text() == "brother"
    assert (model / "weights").read_bytes() == b"never delete"


@pytest.mark.parametrize(
    "kind", ["running", "unowned", "cache_link", "nested_link", "root_link", "fifo", "invalid_id"]
)
def test_M3_07C_TC_SEC_20_reject_unsafe_target_without_mutation(tmp_path: Path, kind: str) -> None:
    manager, engine, cache, sentinel = setup(tmp_path)
    instance_id = cache.name
    assert engine.value is not None
    if kind == "running":
        engine.value["State"]["Running"] = True
    elif kind == "unowned":
        engine.value["Config"]["Labels"] = {}
    elif kind == "cache_link":
        (cache / "compiled.bin").unlink()
        cache.rmdir()
        cache.symlink_to(sentinel, target_is_directory=True)
    elif kind == "nested_link":
        (cache / "link").symlink_to(sentinel, target_is_directory=True)
    elif kind == "root_link":
        alias = tmp_path / "alias"
        alias.symlink_to(cache.parent, target_is_directory=True)
        manager._cache_root = alias
    elif kind == "fifo":
        import os

        os.mkfifo(cache / "pipe")
    else:
        instance_id = "../" + sentinel.name
    with pytest.raises(RuntimeRequestError):
        manager.remove(instance_id)
    assert engine.deleted == 0
    assert (sentinel / "keep").read_text() == "brother"
    if kind != "cache_link":
        assert (cache / "compiled.bin").read_bytes() == b"FX-M3-07C cache"


def test_M3_07C_cleanup_failure_is_explicit_and_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, engine, cache, sentinel = setup(tmp_path)
    # FX-FIX-30: inject the same deletion failure at the fake container boundary.
    real_remove = Engine.clear_cache

    def fail(*args: Any, **kwargs: Any) -> None:
        raise PermissionError("secret path must not escape")

    monkeypatch.setattr(Engine, "clear_cache", staticmethod(fail))
    with pytest.raises(RuntimeRequestError, match="runtime_cache_cleanup_failed") as error:
        manager.remove(cache.name)
    assert "secret" not in str(error.value)
    assert engine.value is None and cache.exists()
    monkeypatch.setattr(Engine, "clear_cache", staticmethod(real_remove))
    manager.remove(cache.name)
    assert not cache.exists() and sentinel.exists()


def test_M3_07C_internal_replacement_preserves_cache(tmp_path: Path) -> None:
    manager, engine, cache, _ = setup(tmp_path)
    assert engine.value is not None
    manager._remove_owned(cache.name, engine.value)
    assert (cache / "compiled.bin").read_bytes() == b"FX-M3-07C cache"


@pytest.mark.parametrize(
    "detail", [None, "runtime_remove_unsupported", "runtime_cache_unsafe", "private/raw/secret"]
)
def test_M3_07C_grpc_remove_only_persists_whitelisted_reason(
    tmp_path: Path, detail: str | None
) -> None:
    import asyncio

    import grpc
    from holm_contracts.agent.v1 import agent_pb2, agent_pb2_grpc
    from holm_instance_controller.agent_client import GrpcRuntimeAgentClient, RuntimeAgentError

    class Service(agent_pb2_grpc.HostAgentServicer):
        async def RemoveRuntime(self, request: Any, context: Any) -> Any:
            if detail:
                await context.abort(grpc.StatusCode.FAILED_PRECONDITION, detail)
            return agent_pb2.RemoveRuntimeResponse()

    async def scenario() -> None:
        server = grpc.aio.server()
        agent_pb2_grpc.add_HostAgentServicer_to_server(Service(), server)
        socket = tmp_path / "agent.sock"
        server.add_insecure_port(f"unix:{socket}")
        await server.start()
        try:
            client = GrpcRuntimeAgentClient(socket, timeout_seconds=1)
            if detail:
                with pytest.raises(RuntimeAgentError) as error:
                    await client.remove(str(uuid4()))
                expected = detail if detail.startswith("runtime_") else "runtime_agent_unavailable"
                assert error.value.code == expected
                assert "private/raw" not in str(error.value)
            else:
                assert await client.remove(str(uuid4())) is None
        finally:
            await server.stop(0)

    asyncio.run(scenario())


@pytest.mark.parametrize("listing_failed", [False, True])
def test_M3_07C_two_concurrent_cleanups_do_not_block_regular_reconciliation(
    listing_failed: bool,
) -> None:
    import asyncio
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager
    from uuid import UUID

    from test_M3_09C_instance_reconciliation import _Agent, _controller, _Store

    async def scenario() -> None:
        ids = tuple(uuid4() for _ in range(8))
        first_two = asyncio.Event()
        release = asyncio.Event()
        normal_reconciled = asyncio.Event()

        class Store(_Store):
            async def pending_cleanup(self, after: UUID | None) -> tuple[UUID, ...]:
                return ids

            @asynccontextmanager
            async def lifecycle_lock(self, instance_id: UUID) -> AsyncIterator[None]:
                yield

            async def status(self, instance_id: UUID) -> None:
                return None

            async def cleanup_result(self, instance_id: UUID, failure: Any) -> None:
                assert failure is None

            async def list_for_reconciliation(self) -> tuple[Any, ...]:
                normal_reconciled.set()
                return ()

        class Agent(_Agent):
            active = 0
            maximum = 0
            calls = 0

            async def list(self) -> tuple[Any, ...]:
                if listing_failed:
                    from holm_instance_controller import RuntimeAgentError

                    raise RuntimeAgentError()
                return ()

            async def remove(self, instance_id: str) -> None:
                self.active += 1
                self.calls += 1
                self.maximum = max(self.maximum, self.active)
                if self.active == 2:
                    first_two.set()
                await release.wait()
                self.active -= 1

        store = Store()
        agent = Agent()
        task = asyncio.create_task(_controller(store, agent).recover_once())
        await asyncio.wait_for(first_two.wait(), timeout=1)
        await asyncio.wait_for(normal_reconciled.wait(), timeout=1)
        assert agent.calls == 2 and agent.active == 2
        assert not task.done()
        release.set()
        if listing_failed:
            with pytest.raises(RuntimeError, match="runtime listing failed"):
                await task
        else:
            await task
        assert agent.maximum == 2 and agent.calls == 8

    asyncio.run(scenario())


@pytest.mark.parametrize("blocked", ["lock", "agent"])
def test_M3_07C_timeout_isolated_from_other_cleanup_and_normal_pass(
    blocked: str, caplog: pytest.LogCaptureFixture
) -> None:
    import asyncio
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager
    from uuid import UUID

    from holm_domain.instance_cleanup import CleanupFailure
    from test_M3_09C_instance_reconciliation import _Agent, _controller, _Store

    async def scenario() -> None:
        slow, fast = uuid4(), uuid4()
        results: dict[UUID, Any] = {}
        regular = asyncio.Event()

        class Store(_Store):
            async def pending_cleanup(self, after: UUID | None) -> tuple[UUID, ...]:
                return slow, fast

            @asynccontextmanager
            async def lifecycle_lock(self, instance_id: UUID) -> AsyncIterator[None]:
                if instance_id == slow and blocked == "lock":
                    await asyncio.Event().wait()
                yield

            async def status(self, instance_id: UUID) -> None:
                return None

            async def cleanup_result(self, instance_id: UUID, failure: Any) -> None:
                results[instance_id] = failure

            async def list_for_reconciliation(self) -> tuple[Any, ...]:
                regular.set()
                return ()

        class Agent(_Agent):
            async def remove(self, instance_id: str) -> None:
                if instance_id == str(slow):
                    await asyncio.Event().wait()

        controller = _controller(Store(), Agent())
        controller._dependency_timeout_seconds = 0.1
        task = asyncio.create_task(controller.recover_once())
        await asyncio.wait_for(regular.wait(), timeout=1)
        await asyncio.wait_for(task, timeout=2)
        assert fast in results and results[fast] is None
        if blocked == "agent":
            assert results[slow] is CleanupFailure.AGENT_UNAVAILABLE
        else:
            assert slow not in results
            assert "cleanup_dependency_timeout" in caplog.text

    asyncio.run(scenario())

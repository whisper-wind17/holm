"""M3-07C / TC-GPU-08/09: FX-M3-07C real PG roles, synthetic model/cache."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from holm_control_api.model_instance_service import (
    ModelInstanceInvalidState,
    ModelInstanceNotFound,
    ModelInstanceService,
)
from holm_domain import ModelInstanceStatus
from holm_domain.instance_cleanup import CleanupFailure
from holm_host_agent.path_validation import RuntimeIdentity
from holm_host_agent.runtime_container import ContainerRuntimeManager, DockerEngine
from holm_instance_controller import PostgresControllerStore, RuntimeAgentError
from holm_persistence import ModelInstanceRepository, database_utc_now
from holm_persistence.instance_cleanup_repository import InstanceCleanupRepository
from holm_runtimes import load_runtime_compatibility
from test_M2_10C_artifact_reverification import _files, _role_factory
from test_M7_10A_TC_DL_14_artifact_deletion import _migrated as _migrated
from test_PROTO_03_instance_controller_postgres import (
    _controller,
    _InventoryAgent,
    _RuntimeAgent,
    _value,
)

pytestmark = pytest.mark.usefixtures("_migrated")


@pytest.fixture(autouse=True)
def _cleanup_events(ephemeral_postgres: EphemeralPostgres, _migrated: None) -> Iterator[None]:
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(sa.text("TRUNCATE core.domain_events"))
    yield
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(sa.text("TRUNCATE core.domain_events"))


class Engine(DockerEngine):
    def __init__(self) -> None:
        self.removed = False
        self.helpers: dict[str, Any] = {}

    def request(
        self, method: str, path: str, payload: Any = None, *, timeout: float = 5.0
    ) -> tuple[int, object]:
        if path.startswith("/containers/create?"):
            self.helpers[path.partition("=")[2]] = payload
            return 201, {"Id": path.partition("=")[2]}
        name = path.split("/")[2].split("?")[0]
        if name in self.helpers:
            helper = self.helpers[name]
            if method == "DELETE":
                del self.helpers[name]
                return 204, None
            if path.endswith("/start"):
                return 204, None
            if helper["Cmd"] == ["delete"]:
                cache = Path(helper["HostConfig"]["Mounts"][0]["Source"])
                for entry in cache.iterdir():
                    if entry.is_dir():
                        shutil.rmtree(entry)
                    else:
                        entry.unlink()
            return 200, {"StatusCode": 0}
        instance_id = path.split("/")[2].removeprefix("runtime-")
        if method == "DELETE":
            self.removed = True
            return 204, None
        if self.removed:
            return 404, None
        return 200, {
            "State": {"Running": False},
            "Config": {
                "Labels": {
                    "holm.managed": "true",
                    "holm.instance_id": instance_id,
                }
            },
        }


class Agent(_RuntimeAgent):
    def __init__(self) -> None:
        super().__init__()
        self.removed: list[str] = []
        self.failure: str | None = None
        self.container: ContainerRuntimeManager | None = None

    async def remove(self, instance_id: str) -> None:
        self.removed.append(instance_id)
        if self.failure:
            raise RuntimeAgentError(self.failure)
        if self.container is not None:
            await asyncio.to_thread(self.container.remove, instance_id)


@pytest.mark.parametrize("terminal", [ModelInstanceStatus.STOPPED, ModelInstanceStatus.FAILED])
@pytest.mark.parametrize(
    "failure",
    [
        None,
        "runtime_agent_unavailable",
        "runtime_remove_unsupported",
        "runtime_cache_cleanup_failed",
        "raw-secret-path",
    ],
)
def test_M3_07C_TC_GPU_09_delete_durable_cleanup_real_roles(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    terminal: ModelInstanceStatus,
    failure: str | None,
) -> None:
    artifact_id, _, _ = _files(ephemeral_postgres, tmp_path)

    async def scenario() -> None:
        ce, control = await _role_factory(ephemeral_postgres, "holm_control")
        ie, instance = await _role_factory(ephemeral_postgres, "holm_instance_controller")
        try:
            service = ModelInstanceService(session_factory=control, agent=_InventoryAgent())
            created = await service.create(replace(_value(), artifact_id=artifact_id))
            store = PostgresControllerStore(instance)
            with pytest.raises(ModelInstanceInvalidState):
                await service.delete(created.id)
            with pytest.raises(ModelInstanceNotFound):
                await service.delete(uuid4())
            assert await store.pending_cleanup(None) == ()
            async with instance() as session, session.begin():
                await ModelInstanceRepository(session).release_and_transition(
                    created.id,
                    expected_statuses=(ModelInstanceStatus.STARTING,),
                    target_status=terminal,
                    error_code=None,
                    now=await database_utc_now(session),
                )
            agent = Agent()
            root = tmp_path / "runtime-cache"
            root.mkdir()
            cache = root / str(created.id)
            cache.mkdir()
            (cache / "compiled").write_text("FX-M3-07C")
            engine = Engine()
            agent.container = ContainerRuntimeManager(
                cache_root=root, runtime_identity=RuntimeIdentity(10008, 10008), engine=engine
            )
            # FX-FIX-30: fixed synthetic certified digest; no Docker/GPU required.
            runtime = load_runtime_compatibility(
                Path(__file__).resolve().parents[2] / "runtime-compat.yaml"
            ).runtime("vllm")
            assert runtime is not None
            agent.container.set_runtimes(
                {runtime.name: replace(runtime, image_digest="sha256:" + "a" * 64)}
            )
            # Existing terminal configurations are never cleanup candidates.
            await _controller(store, agent).cleanup_once()
            assert agent.removed == []
            await service.delete(created.id)
            assert await store.status(created.id) is None
            assert await store.pending_cleanup(None) == (created.id,)
            agent.failure = failure
            for _ in range(3):
                # Fresh Controller simulates restart; no in-memory ownership needed.
                await _controller(store, agent).recover_once()
            async with instance() as session:
                rows = (
                    await session.execute(
                        sa.text(
                            "SELECT event, payload FROM core.domain_events WHERE aggregate_id=:id "
                            "AND event LIKE 'instance.cleanup_%' ORDER BY version"
                        ),
                        {"id": created.id},
                    )
                ).all()
            assert rows[0] == ("instance.cleanup_requested", {})
            if failure:
                reason = (
                    failure
                    if failure in {x.value for x in CleanupFailure}
                    else "runtime_agent_invalid_response"
                )
                assert rows[1:] == [("instance.cleanup_failed", {"reason": reason})]
                assert len(agent.removed) == 3
                assert await store.pending_cleanup(None) == (created.id,)
                agent.failure = None
                await _controller(store, agent).cleanup_once()
            else:
                assert rows[1:] == [("instance.cleanup_completed", {})]
                assert len(agent.removed) == 1
            assert await store.pending_cleanup(None) == ()
            assert not cache.exists() and engine.removed
            async with instance() as session:
                assert (
                    await session.scalar(
                        sa.text("SELECT id FROM core.model_artifacts WHERE id=:id"),
                        {"id": artifact_id},
                    )
                    == artifact_id
                )
        finally:
            await ce.dispose()
            await ie.dispose()

    asyncio.run(scenario())


def test_M3_07C_failed_batch_does_not_starve_later_requests(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> None:
        ce, control = await _role_factory(ephemeral_postgres, "holm_control")
        ie, instance = await _role_factory(ephemeral_postgres, "holm_instance_controller")
        try:
            ids = tuple(UUID(int=i) for i in range(1, 11))
            async with control() as session, session.begin():
                for instance_id in ids:
                    await InstanceCleanupRepository(session).request(instance_id)
            agent = Agent()
            agent.failure = "runtime_remove_unsupported"
            store = PostgresControllerStore(instance)
            controller = _controller(store, agent)
            await controller.cleanup_once()
            assert len(agent.removed) == 8
            assert set(agent.removed) == {str(i) for i in ids[:8]}
            await controller.cleanup_once()
            assert set(agent.removed) == {str(i) for i in ids}
            await controller.cleanup_once()
            assert len(agent.removed) == 18
        finally:
            await ce.dispose()
            await ie.dispose()

    asyncio.run(scenario())


def test_M3_07C_request_rolls_back_with_configuration_delete(
    ephemeral_postgres: EphemeralPostgres, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_id, _, _ = _files(ephemeral_postgres, tmp_path)

    async def scenario() -> None:
        ce, control = await _role_factory(ephemeral_postgres, "holm_control")
        ie, instance = await _role_factory(ephemeral_postgres, "holm_instance_controller")
        try:
            service = ModelInstanceService(session_factory=control, agent=_InventoryAgent())
            created = await service.create(replace(_value(), artifact_id=artifact_id))
            store = PostgresControllerStore(instance)
            await store.mark_failed(
                created.id,
                expected_status=ModelInstanceStatus.STARTING,
                error_code="runtime_start_failed",
            )
            original = ModelInstanceRepository.delete

            async def fail(self: ModelInstanceRepository, instance_id: UUID) -> None:
                await original(self, instance_id)
                raise RuntimeError("FX-M3-07C rollback")

            monkeypatch.setattr(ModelInstanceRepository, "delete", fail)
            with pytest.raises(RuntimeError, match="rollback"):
                await service.delete(created.id)
            assert await store.status(created.id) is ModelInstanceStatus.FAILED
            assert await store.pending_cleanup(None) == ()
        finally:
            await ce.dispose()
            await ie.dispose()

    asyncio.run(scenario())


def test_M3_07C_TC_GPU_08_api_delete_permissions_and_guards(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    from test_PROTO_01_model_instances import (
        ADMIN_USERNAME,
        GPU_0,
        MEMBER_USERNAME,
        READY_ARTIFACT_ID,
        _create_accounts,
        _create_body,
        _environment,
        _login,
        _seed_model_and_artifacts,
    )

    _seed_model_and_artifacts(ephemeral_postgres.engine)

    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as env:
            await _create_accounts(env)
            admin = await _login(env, ADMIN_USERNAME)
            member = await _login(env, MEMBER_USERNAME)
            response = await env.client.request(
                "POST",
                "/api/v1/instances",
                cookies=admin,
                json_body=_create_body(READY_ARTIFACT_ID, [GPU_0], 1),
            )
            assert response.status == 201
            instance_id = UUID(response.json()["id"])
            url = f"/api/v1/instances/{instance_id}"
            assert (await env.client.request("DELETE", url)).status == 401
            assert (await env.client.request("DELETE", url, cookies=member)).status == 403
            assert (
                await env.client.request("DELETE", f"/api/v1/instances/{uuid4()}", cookies=admin)
            ).status == 404
            assert (await env.client.request("DELETE", url, cookies=admin)).status == 409
            store = PostgresControllerStore(env.session_factory)
            assert await store.pending_cleanup(None) == ()
            await store.mark_failed(
                instance_id,
                expected_status=ModelInstanceStatus.STARTING,
                error_code="runtime_start_failed",
            )
            async with env.session_factory() as session, session.begin():
                service_id = uuid4()
                await session.execute(
                    sa.text(
                        "INSERT INTO core.ai_services (id, name, public_model_name, description, "
                        "enabled, route_version, created_at, updated_at) "
                        "VALUES (:id, 'fx-m307c', 'fx-m307c', '', true, 1, now(), now())"
                    ),
                    {"id": service_id},
                )
                await session.execute(
                    sa.text(
                        "INSERT INTO core.ai_service_backends (id, service_id, instance_id, "
                        "instance_name_snapshot, runtime_snapshot, "
                        "backend_key, weight, enabled, health_gate_passed, joined_at) "
                        "VALUES (:id, :service, :instance, 'FX-M3-07C', 'vllm', "
                        "'primary', 100, true, true, now())"
                    ),
                    {"id": uuid4(), "service": service_id, "instance": instance_id},
                )
            rejected = await env.client.request("DELETE", url, cookies=admin)
            assert (
                rejected.status == 409 and rejected.json()["error"]["code"] == "instance_referenced"
            )
            assert await store.pending_cleanup(None) == ()
            async with env.session_factory() as session, session.begin():
                await session.execute(
                    sa.text(
                        "UPDATE core.ai_service_backends SET retired_at=now() WHERE instance_id=:id"
                    ),
                    {"id": instance_id},
                )
            assert (await env.client.request("DELETE", url, cookies=admin)).status == 204
            assert await store.pending_cleanup(None) == (instance_id,)
            await _controller(store, Agent()).recover_once()
            assert await store.pending_cleanup(None) == ()

    asyncio.run(scenario())

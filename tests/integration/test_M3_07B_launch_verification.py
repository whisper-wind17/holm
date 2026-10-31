"""M3-07B / TC-RT-07: FX-M3-07B synthetic small files, real PostgreSQL roles.

Reuses FX-M2-10C's canonical manifest and eight synthetic weight bytes; never
requires real model content, Runtime processes, or GPU hardware.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from holm_control_api.model_instance_service import ModelInstanceService
from holm_domain import ArtifactStorageType, ModelInstanceStatus
from holm_domain.launch_verification import LaunchVerificationResult
from holm_instance_controller import (
    PostgresControllerStore,
    RuntimeLaunchRequest,
    RuntimeObservation,
    RuntimeProcessState,
)
from holm_persistence import ModelInstanceRepository, database_utc_now
from holm_persistence.artifact_verification_repository import ArtifactVerificationRepository
from sqlalchemy.ext.asyncio import AsyncEngine
from test_M2_10C_artifact_reverification import _files, _role_factory, _worker
from test_M7_10A_TC_DL_14_artifact_deletion import _migrated as _migrated
from test_PROTO_03_instance_controller_postgres import (
    _controller,
    _InventoryAgent,
    _RuntimeAgent,
    _value,
)

pytestmark = pytest.mark.usefixtures("_migrated")


@pytest.mark.parametrize("new_process", [False, True])
def test_FIX_21_TC_GPU_09_recovery_uses_durable_attempt_and_retains_allocation(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    new_process: bool,
) -> None:
    """FX-FIX-21: real authorization/roles, with only Runtime/GPU replaced."""
    artifact_id, _, storage = _files(ephemeral_postgres, tmp_path)

    class ProcessAgent(_RuntimeAgent):
        def __init__(self) -> None:
            super().__init__()
            self.active = False
            self.started_at = datetime.now(UTC) - timedelta(seconds=30)
            self.events: list[str] = []

        async def inspect(self, instance_id: str) -> RuntimeObservation:
            return RuntimeObservation(
                RuntimeProcessState.RUNNING if self.active else RuntimeProcessState.NOT_FOUND,
                "127.0.0.1:8123" if self.active else "",
                started_at=self.started_at,
            )

        async def ensure(self, request: RuntimeLaunchRequest) -> RuntimeObservation:
            assert not self.active
            self.active = True
            self.events.append("ensure")
            return await super().ensure(request)

        async def stop(self, instance_id: str, *, grace_seconds: int) -> RuntimeObservation:
            self.active = False
            self.events.append("stop")
            return await super().stop(instance_id, grace_seconds=grace_seconds)

    async def scenario() -> None:
        async with _launch_environment(ephemeral_postgres, artifact_id, storage) as env:
            service, store, worker, created, _ = env
            await worker.run_once()
            starting = await store.claim(ModelInstanceStatus.STARTING)
            assert starting is not None
            agent = ProcessAgent()
            await _controller(store, agent).process_starting(starting)
            with ephemeral_postgres.engine.connect() as connection:
                before = connection.execute(
                    sa.text("SELECT id, gpu_uuid, status FROM core.gpu_allocations")
                ).all()
            await service.restart(created.id)
            assert await store.record_restart_attempt(created.id) == 1
            await worker.run_once()
            verification = await store.launch_verification(created.id)
            assert verification is not None
            assert await store.authorize_launch(verification)
            if new_process:
                # The previous controller has already replaced the process, but
                # has not yet published Running. Only the Agent timestamp survives.
                agent.started_at = verification.requested_at + timedelta(milliseconds=100)
            agent.events.clear()
            recovered = _controller(store, agent)
            await recovered.recover_once()
            await asyncio.wait_for(asyncio.gather(*recovered._tasks.values()), 2)
            current = await store.launch_verification(created.id)
            assert current is None  # Running no longer has a matching launch request.
            assert await store.status(created.id) is ModelInstanceStatus.RUNNING
            assert agent.events == ([] if new_process else ["stop", "ensure"])
            with ephemeral_postgres.engine.connect() as connection:
                after = connection.execute(
                    sa.text("SELECT id, gpu_uuid, status FROM core.gpu_allocations")
                ).all()
                assert after == before
                assert (
                    connection.execute(
                        sa.text("SELECT restart_count FROM core.model_instances")
                    ).scalar_one()
                    == 1
                )
                assert (
                    connection.execute(
                        sa.text(
                            "SELECT count(*) FROM core.domain_events "
                            "WHERE event='instance.verification_requested' AND aggregate_id=:id"
                        ),
                        {"id": created.id},
                    ).scalar_one()
                    == 2
                )  # Initial start plus this restart; recovery adds none.

    asyncio.run(scenario())


@pytest.mark.parametrize("storage_type", list(ArtifactStorageType))
@pytest.mark.parametrize("damage", [None, "missing", "same_size"])
def test_M3_07B_TC_RT_07_real_roles_require_full_hash_before_ensure(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    storage_type: ArtifactStorageType,
    damage: str | None,
) -> None:
    artifact_id, path, storage = _files(ephemeral_postgres, tmp_path, storage_type)
    if damage == "missing":
        (path / "model.safetensors").unlink()
    elif damage == "same_size":
        (path / "model.safetensors").write_bytes(b"87654321")
    snapshot = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in path.iterdir()}

    async def scenario() -> None:
        engines: list[AsyncEngine] = []
        try:
            ce, control = await _role_factory(ephemeral_postgres, "holm_control")
            de, download = await _role_factory(ephemeral_postgres, "holm_download_worker")
            ie, instance = await _role_factory(ephemeral_postgres, "holm_instance_controller")
            engines.extend((ce, de, ie))
            created = await ModelInstanceService(
                session_factory=control,
                agent=_InventoryAgent(),
            ).create(replace(_value(), artifact_id=artifact_id))
            store = PostgresControllerStore(instance)
            before = await store.launch_verification(created.id)
            assert before is not None and before.result is LaunchVerificationResult.PENDING
            worker = _worker(download, storage)
            async with control() as session:
                assert (
                    await ArtifactVerificationRepository(session).last_completed_at(artifact_id)
                    is None
                )
            await worker.run_once()
            async with control() as session:
                verification = ArtifactVerificationRepository(session)
                assert await verification.last_completed_at(artifact_id) is not None
                assert await verification.latest_job(artifact_id) is None
            after = await store.launch_verification(created.id)
            assert after is not None and after.request_id == before.request_id
            expected = (
                LaunchVerificationResult.READY
                if damage is None
                else LaunchVerificationResult.MISSING
                if damage == "missing"
                else LaunchVerificationResult.CORRUPTED
            )
            assert after.result is expected
            item = await store.claim(ModelInstanceStatus.STARTING)
            assert item is not None
            agent = _RuntimeAgent()
            await _controller(store, agent).process_starting(item)
            assert len(agent.launches) == (1 if damage is None else 0)
            assert await store.status(created.id) is (
                ModelInstanceStatus.RUNNING if damage is None else ModelInstanceStatus.FAILED
            )
            async with control() as session:
                status = await session.scalar(
                    sa.text("SELECT status FROM core.model_artifacts WHERE id=:id"),
                    {"id": artifact_id},
                )
                assert status == expected.value
                allocated = await session.scalar(
                    sa.text(
                        "SELECT count(*) FROM core.gpu_allocations WHERE instance_id=:id "
                        "AND status <> 'Released'"
                    ),
                    {"id": created.id},
                )
                assert allocated == (1 if damage is None else 0)
        finally:
            for engine in engines:
                await engine.dispose()

    asyncio.run(scenario())
    assert snapshot == {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in path.iterdir()}


def test_M3_07B_TC_RT_07_each_start_has_new_request_and_old_result_cannot_authorize(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    artifact_id, path, storage = _files(ephemeral_postgres, tmp_path)

    async def scenario() -> None:
        ce, control = await _role_factory(ephemeral_postgres, "holm_control")
        de, download = await _role_factory(ephemeral_postgres, "holm_download_worker")
        ie, instance = await _role_factory(ephemeral_postgres, "holm_instance_controller")
        try:
            service = ModelInstanceService(session_factory=control, agent=_InventoryAgent())
            created = await service.create(replace(_value(), artifact_id=artifact_id))
            store = PostgresControllerStore(instance)
            worker = _worker(download, storage)
            await worker.run_once()
            old = await store.launch_verification(created.id)
            assert old is not None and old.result is LaunchVerificationResult.READY
            async with instance() as session, session.begin():
                repo = ModelInstanceRepository(session)
                await repo.release_and_transition(
                    created.id,
                    expected_statuses=(ModelInstanceStatus.STARTING,),
                    target_status=ModelInstanceStatus.STOPPED,
                    error_code=None,
                    now=await database_utc_now(session),
                )
            await service.start(created.id)
            new = await store.launch_verification(created.id)
            assert new is not None and new.request_id != old.request_id
            assert new.config_version == old.config_version
            assert not await store.authorize_launch(old)
            for _ in range(3):
                async with download() as session, session.begin():
                    from holm_persistence.launch_verification_repository import (
                        LaunchVerificationRepository,
                    )

                    await LaunchVerificationRepository(session).dispatch_pending()
            async with download() as session:
                assert (
                    await session.scalar(
                        sa.text(
                            "SELECT count(*) FROM core.jobs WHERE job_type='artifact.verify_launch'"
                        )
                    )
                    == 2
                )
            (path / "model.safetensors").write_bytes(b"87654321")
            await worker.run_once()
            item = await store.claim(ModelInstanceStatus.STARTING)
            assert item is not None
            agent = _RuntimeAgent()
            await _controller(store, agent).process_starting(item)
            assert not agent.launches
            assert await store.status(created.id) is ModelInstanceStatus.FAILED
        finally:
            for engine in (ce, de, ie):
                await engine.dispose()

    asyncio.run(scenario())


@asynccontextmanager
async def _launch_environment(database: EphemeralPostgres, artifact_id: Any, storage: Any) -> Any:
    ce, control = await _role_factory(database, "holm_control")
    de, download = await _role_factory(database, "holm_download_worker")
    ie, instance = await _role_factory(database, "holm_instance_controller")
    try:
        service = ModelInstanceService(session_factory=control, agent=_InventoryAgent())
        created = await service.create(replace(_value(), artifact_id=artifact_id))
        yield (
            service,
            PostgresControllerStore(instance),
            _worker(download, storage),
            created,
            download,
        )
    finally:
        for engine in (ce, de, ie):
            await engine.dispose()


def test_M3_07B_TC_RT_07_worker_unavailable_times_out_without_launch(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    artifact_id, _, storage = _files(ephemeral_postgres, tmp_path)

    async def scenario() -> None:
        async with _launch_environment(ephemeral_postgres, artifact_id, storage) as env:
            _, store, _, created, _ = env
            item = await store.claim(ModelInstanceStatus.STARTING)
            assert item is not None
            agent = _RuntimeAgent()
            controller = _controller(store, agent)
            controller._start_timeout_seconds = 0.05
            await controller.process_starting(item)
            assert not agent.launches
            assert await store.status(created.id) is ModelInstanceStatus.FAILED

    asyncio.run(scenario())
    with ephemeral_postgres.engine.connect() as connection:
        assert (
            connection.execute(sa.text("SELECT error_code FROM core.model_instances")).scalar_one()
            == "artifact_verification_timeout"
        )
        assert (
            connection.execute(
                sa.text("SELECT count(*) FROM core.gpu_allocations WHERE status <> 'Released'")
            ).scalar_one()
            == 0
        )


def test_M3_07B_TC_RT_07_stop_and_waiting_cancel_cannot_launch_late(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    artifact_id, _, storage = _files(ephemeral_postgres, tmp_path)

    async def scenario() -> None:
        async with _launch_environment(ephemeral_postgres, artifact_id, storage) as env:
            service, store, worker, created, _ = env
            waiting = await service.create(replace(_value(), artifact_id=artifact_id))
            assert waiting.status is ModelInstanceStatus.WAITING_FOR_RESOURCE
            await service.cancel(waiting.id, actor_id=uuid4(), request_id=uuid4())
            assert await store.status(waiting.id) is None
            item = await store.claim(ModelInstanceStatus.STARTING)
            assert item is not None
            agent = _RuntimeAgent()
            controller = _controller(store, agent)
            operation = asyncio.create_task(controller.process_starting(item))
            await asyncio.sleep(0.01)
            await service.stop(created.id)
            await operation
            await worker.run_once()
            assert not agent.launches
            stopping = await store.claim(ModelInstanceStatus.STOPPING)
            assert stopping is not None
            await controller.process_stopping(stopping)
            assert await store.status(created.id) is ModelInstanceStatus.STOPPED

    asyncio.run(scenario())


def test_M3_07B_TC_RT_07_controller_recovery_resumes_same_pending_request(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    artifact_id, _, storage = _files(ephemeral_postgres, tmp_path)

    class ObservedAgent(_RuntimeAgent):
        async def inspect(self, instance_id: str) -> Any:
            if not self.launches:
                from holm_instance_controller import RuntimeObservation, RuntimeProcessState

                return RuntimeObservation(RuntimeProcessState.NOT_FOUND)
            return await super().inspect(instance_id)

    async def scenario() -> None:
        async with _launch_environment(ephemeral_postgres, artifact_id, storage) as env:
            _, store, worker, created, _ = env
            before = await store.launch_verification(created.id)
            agent = ObservedAgent()
            first = _controller(store, agent)
            await first.recover_once()
            await asyncio.sleep(0.01)
            assert await store.status(created.id) is ModelInstanceStatus.STARTING
            assert not agent.launches
            for task in first._tasks.values():
                task.cancel()
            await asyncio.gather(*first._tasks.values(), return_exceptions=True)
            second = _controller(store, agent)
            await second.recover_once()
            stop = asyncio.Event()
            production_loop = asyncio.create_task(worker.run(stop))
            try:
                await asyncio.wait_for(asyncio.gather(*second._tasks.values()), 2)
            finally:
                stop.set()
                await production_loop
            assert before is not None
            assert len(agent.launches) == 1
            assert await store.status(created.id) is ModelInstanceStatus.RUNNING

    asyncio.run(scenario())
    with ephemeral_postgres.engine.connect() as connection:
        assert (
            connection.execute(
                sa.text(
                    "SELECT count(*) FROM core.domain_events "
                    "WHERE event='instance.verification_requested' "
                    "AND aggregate_id IN (SELECT id FROM core.model_instances)"
                )
            ).scalar_one()
            == 1
        )


@pytest.mark.parametrize("manual", [True, False])
def test_M3_07B_TC_RT_07_restart_attempt_recovery_rechecks_files(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    manual: bool,
) -> None:
    artifact_id, path, storage = _files(ephemeral_postgres, tmp_path)

    async def scenario() -> None:
        async with _launch_environment(ephemeral_postgres, artifact_id, storage) as env:
            service, store, worker, created, _ = env
            await worker.run_once()
            first = await store.launch_verification(created.id)
            item = await store.claim(ModelInstanceStatus.STARTING)
            assert item is not None
            agent = _RuntimeAgent()
            await _controller(store, agent).process_starting(item)
            assert await store.status(created.id) is ModelInstanceStatus.RUNNING
            if manual:
                await service.restart(created.id)
            else:
                await store.mark_unhealthy(
                    created.id,
                    expected_status=ModelInstanceStatus.RUNNING,
                    error_code="runtime_exited",
                )
                await store.begin_restart(created.id, restart_window_seconds=600)
            assert await store.record_restart_attempt(created.id) == 1
            pending = await store.launch_verification(created.id)
            assert pending is not None and first is not None
            assert pending.request_id != first.request_id
            assert pending.result is LaunchVerificationResult.PENDING
            # Restart the controller while its first attempt is waiting for the Worker.
            item = await store.claim(ModelInstanceStatus.RESTARTING)
            assert item is not None
            (path / "model.safetensors").write_bytes(b"87654321")
            operation = asyncio.create_task(_controller(store, agent).process_restarting(item))
            await worker.run_once()
            await asyncio.wait_for(operation, 2)
            assert len(agent.launches) == 1
            assert await store.status(created.id) is ModelInstanceStatus.FAILED

    asyncio.run(scenario())
    with ephemeral_postgres.engine.connect() as connection:
        assert (
            connection.execute(
                sa.text("SELECT restart_count FROM core.model_instances")
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                sa.text("SELECT count(*) FROM core.gpu_allocations WHERE status <> 'Released'")
            ).scalar_one()
            == 0
        )


def test_M3_07B_TC_RT_07_execution_failure_is_safe_and_preserves_inventory(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from holm_download_worker.artifact_publishing import ArtifactRevalidator
    from holm_persistence.job_executor import JobFailure

    artifact_id, _, storage = _files(ephemeral_postgres, tmp_path)

    def denied(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError("private-path-and-content-sentinel")

    monkeypatch.setattr(ArtifactRevalidator, "_judge_record", staticmethod(denied))

    async def scenario() -> None:
        async with _launch_environment(ephemeral_postgres, artifact_id, storage) as env:
            _, store, worker, created, download = env
            for _ in range(3):
                with pytest.raises(JobFailure, match="job operation failed"):
                    await worker.run_once()
                async with download() as session, session.begin():
                    await session.execute(
                        sa.text("UPDATE core.jobs SET run_after=clock_timestamp()")
                    )
            await worker.run_once()
            result = await store.launch_verification(created.id)
            assert result is not None and result.result is LaunchVerificationResult.FAILED
            item = await store.claim(ModelInstanceStatus.STARTING)
            assert item is not None
            agent = _RuntimeAgent()
            await _controller(store, agent).process_starting(item)
            assert not agent.launches
            assert await store.status(created.id) is ModelInstanceStatus.FAILED

    asyncio.run(scenario())
    with ephemeral_postgres.engine.connect() as connection:
        assert (
            connection.execute(sa.text("SELECT status FROM core.model_artifacts")).scalar_one()
            == "Ready"
        )
        assert (
            connection.execute(sa.text("SELECT last_error_code FROM core.jobs")).scalar_one()
            == "artifact_verify_failed"
        )
        assert "private-path-and-content-sentinel" not in str(
            connection.execute(sa.text("SELECT payload FROM core.domain_events")).all()
        )


def test_M3_07B_TC_RT_07_waiting_wakeup_and_failed_retry_create_fresh_requests(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    artifact_id, _, storage = _files(ephemeral_postgres, tmp_path)

    async def scenario() -> None:
        async with _launch_environment(ephemeral_postgres, artifact_id, storage) as env:
            service, store, worker, first, _ = env
            first_request = await store.launch_verification(first.id)
            waiting = await service.create(replace(_value(), artifact_id=artifact_id))
            assert waiting.status is ModelInstanceStatus.WAITING_FOR_RESOURCE
            assert await store.launch_verification(waiting.id) is None
            await store.mark_failed(
                first.id,
                expected_status=ModelInstanceStatus.STARTING,
                error_code="runtime_start_failed",
            )
            assert await store.status(waiting.id) is ModelInstanceStatus.STARTING
            waiting_request = await store.launch_verification(waiting.id)
            assert (
                waiting_request is not None
                and waiting_request.result is LaunchVerificationResult.PENDING
            )
            await worker.run_once()
            item = await store.claim(ModelInstanceStatus.STARTING)
            assert item is not None and item.instance.id == waiting.id
            agent = _RuntimeAgent()
            await _controller(store, agent).process_starting(item)
            assert len(agent.launches) == 1
            await service.stop(waiting.id)
            stopping = await store.claim(ModelInstanceStatus.STOPPING)
            assert stopping is not None
            await _controller(store, agent).process_stopping(stopping)
            await service.retry(first.id)
            retried = await store.launch_verification(first.id)
            assert retried is not None and first_request is not None
            assert retried.request_id != first_request.request_id
            assert retried.result is LaunchVerificationResult.PENDING
            await worker.run_once()
            item = await store.claim(ModelInstanceStatus.STARTING)
            assert item is not None and item.instance.id == first.id
            await _controller(store, agent).process_starting(item)
            assert len(agent.launches) == 2

    asyncio.run(scenario())


def test_M3_07B_TC_RT_07_inventory_changed_after_hash_fails_safely(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    artifact_id, _, storage = _files(ephemeral_postgres, tmp_path)

    async def scenario() -> None:
        async with _launch_environment(ephemeral_postgres, artifact_id, storage) as env:
            _, store, worker, created, download = env
            await worker.run_once()
            verified = await store.launch_verification(created.id)
            assert verified is not None and verified.result is LaunchVerificationResult.READY
            async with download() as session, session.begin():
                await session.execute(
                    sa.text("UPDATE core.model_artifacts SET status='Missing' WHERE id=:id"),
                    {"id": artifact_id},
                )
            item = await store.claim(ModelInstanceStatus.STARTING)
            assert item is not None
            agent = _RuntimeAgent()
            await _controller(store, agent).process_starting(item)
            assert not agent.launches
            assert await store.status(created.id) is ModelInstanceStatus.FAILED

    asyncio.run(scenario())

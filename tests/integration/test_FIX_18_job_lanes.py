"""FX-FIX-18: real PG leases and FX-M2-10C tiny manifests; no network or GPU."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from holm_control_api.model_instance_service import ModelInstanceService
from holm_domain import ArtifactStorageType, JobStatus
from holm_download_worker import DownloadResolutionJobHandler, DownloadWorker
from holm_download_worker.artifact_deletion import ArtifactDeletionHandler
from holm_download_worker.artifact_verification import ArtifactVerificationHandler
from holm_persistence import AsyncSessionFactory, JobDraft, JobHandlerContext, JobRepository
from holm_persistence.jobs import JOBS
from test_M2_10C_artifact_reverification import _files, _UnusedResolver, _worker
from test_M7_10A_TC_DL_14_artifact_deletion import _factory
from test_M7_10A_TC_DL_14_artifact_deletion import _migrated as _migrated
from test_PROTO_03_instance_controller_postgres import _InventoryAgent, _value

pytestmark = pytest.mark.usefixtures("_migrated")


async def _enqueue(factory: AsyncSessionFactory, job_type: str, target: UUID | None = None) -> UUID:
    async with factory() as session, session.begin():
        job = await JobRepository(session).enqueue(
            JobDraft(
                job_type=job_type,
                payload={},
                target_object_id=target or uuid4(),
                target_object_version=0,
            )
        )
        return job.id


def test_FIX_18_claim_filters_candidates_but_expires_all_types(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> None:
        engine, factory = await _factory(ephemeral_postgres)
        try:
            download = await _enqueue(factory, "download.resolve")
            verify = await _enqueue(factory, "artifact.verify")
            expired = await _enqueue(factory, "artifact.delete")
            async with factory() as session, session.begin():
                await session.execute(
                    sa.update(JOBS)
                    .where(JOBS.c.id == expired)
                    .values(
                        status="Running",
                        attempt=JOBS.c.max_attempts,
                        lease_owner="expired",
                        lease_until=sa.func.clock_timestamp() - sa.text("interval '1 second'"),
                    )
                )
            async with factory() as session, session.begin():
                repository = JobRepository(session)
                selected = await repository.claim(owner="verify", job_types={"artifact.verify"})
                assert [job.id for job in selected] == [verify]
                ended = await repository.get(expired)
                assert ended is not None and ended.status is JobStatus.FAILED
                assert ended.last_error_code == "attempts_exhausted_after_lease_loss"
                assert await repository.claim(owner="verify", job_types={"artifact.verify"}) == ()
                assert [job.id for job in await repository.claim(owner="legacy")] == [download]
            extra = await _enqueue(factory, "download.cleanup")
            async with factory() as session, session.begin():
                selected = await JobRepository(session).claim(owner="empty", job_types=set())
                assert [job.id for job in selected] == [extra]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "kind", ["artifact.verify", "artifact.light_verify", "artifact.verify_launch"]
)
def test_FIX_18_TC_RT_07_verification_completes_during_blocked_download(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    artifact_id, _, storage = _files(ephemeral_postgres, tmp_path, ArtifactStorageType.MANAGED)

    async def scenario() -> None:
        engine, factory = await _factory(ephemeral_postgres)
        started, cancelled, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def block(_self: DownloadResolutionJobHandler, context: JobHandlerContext) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        monkeypatch.setattr(DownloadResolutionJobHandler, "__call__", block)
        worker = _worker(factory, storage)
        running: asyncio.Task[None] | None = None
        try:
            download = await _enqueue(factory, "download.resolve")
            running = asyncio.create_task(worker.run(stop))
            await asyncio.wait_for(started.wait(), 5)
            queued = [
                await _enqueue(factory, kind)
                for kind in (
                    "artifact.delete",
                    "artifact.trash_cleanup",
                    "download.cleanup",
                    "download.resolve",
                )
            ]
            if kind == "artifact.verify_launch":
                await ModelInstanceService(session_factory=factory, agent=_InventoryAgent()).create(
                    replace(_value(), artifact_id=artifact_id)
                )
            else:
                await _enqueue(factory, kind, artifact_id)
            async with asyncio.timeout(10):
                while True:
                    async with factory() as session:
                        status = await session.scalar(
                            sa.select(JOBS.c.status).where(
                                JOBS.c.job_type == kind, JOBS.c.target_object_id == artifact_id
                            )
                        )
                    if status == "Succeeded":
                        break
                    await asyncio.sleep(0.02)
            assert not cancelled.is_set()
            async with factory() as session:
                repository = JobRepository(session)
                active = await repository.get(download)
                assert active is not None and active.status is JobStatus.RUNNING
                for job_id in queued:
                    pending = await repository.get(job_id)
                    assert pending is not None and pending.status is JobStatus.QUEUED
                    assert pending.attempt == 0
        finally:
            stop.set()
            if running is not None:
                await asyncio.wait_for(running, 5)
            await engine.dispose()
        assert cancelled.is_set()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["artifact.delete", "artifact.trash_cleanup"])
def test_FIX_18_deletion_is_claimed_only_by_download_lane(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    _, _, storage = _files(ephemeral_postgres, tmp_path, ArtifactStorageType.MANAGED)

    async def scenario() -> None:
        engine, factory = await _factory(ephemeral_postgres)
        handled: list[UUID] = []

        async def handle(_self: ArtifactDeletionHandler, context: JobHandlerContext) -> None:
            handled.append(context.job.id)

        monkeypatch.setattr(ArtifactDeletionHandler, "delete", handle)
        monkeypatch.setattr(ArtifactDeletionHandler, "cleanup", handle)
        worker = DownloadWorker(
            session_factory=factory,
            handler=DownloadResolutionJobHandler(
                session_factory=factory, resolver=_UnusedResolver()
            ),
            owner="FX-FIX-18",
            build_version="fixture",
            artifact_deletion=ArtifactDeletionHandler(session_factory=factory, storage=storage),
            artifact_verification=ArtifactVerificationHandler(
                session_factory=factory, storage=storage
            ),
        )
        try:
            job_id = await _enqueue(factory, kind)
            assert await worker._run_verification_once() is None  # noqa: SLF001 - lane boundary
            assert handled == []
            result = await worker._executor.run_once()  # noqa: SLF001 - lane boundary
            assert result is not None and result.completed and result.job_id == job_id
            assert handled == [job_id]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_FIX_18_TC_DL_13_stop_cancels_both_lanes_and_releases_leases(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, storage = _files(ephemeral_postgres, tmp_path, ArtifactStorageType.MANAGED)

    async def scenario() -> None:
        engine, factory = await _factory(ephemeral_postgres)
        started = {kind: asyncio.Event() for kind in ("download.resolve", "artifact.verify")}
        cancelled: set[str] = set()
        stop = asyncio.Event()

        async def block(_self: object, context: JobHandlerContext) -> None:
            started[context.job.job_type].set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.add(context.job.job_type)

        monkeypatch.setattr(DownloadResolutionJobHandler, "__call__", block)
        monkeypatch.setattr(ArtifactVerificationHandler, "__call__", block)
        running: asyncio.Task[None] | None = None
        try:
            jobs = [await _enqueue(factory, kind) for kind in started]
            running = asyncio.create_task(_worker(factory, storage).run(stop))
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), 5)
            stop.set()
            await asyncio.wait_for(running, 5)
            assert cancelled == set(started)
            async with factory() as session:
                for job_id in jobs:
                    job = await JobRepository(session).get(job_id)
                    assert job is not None and job.status is JobStatus.QUEUED
                    assert job.attempt == 0
                    assert job.lease_owner is None and job.lease_until is None
        finally:
            stop.set()
            if running is not None:
                await asyncio.wait_for(running, 5)
            await engine.dispose()

    asyncio.run(scenario())

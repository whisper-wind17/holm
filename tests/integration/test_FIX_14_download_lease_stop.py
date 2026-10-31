"""FX-FIX-14: real PG jobs/tasks, seeded with the TC-DL-13 model fixture."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from holm_control_api.download_command_service import DownloadCommand, DownloadCommandService
from holm_domain import ComponentStatus, JobStatus
from holm_download_worker import DownloadResolutionJobHandler, DownloadWorker
from holm_persistence import (
    JobExecutor,
    JobHandlerContext,
    JobHandlerRegistry,
    JobRepository,
    create_database_engine,
    create_session_factory,
)
from holm_persistence.download_task_repository import DownloadTaskRepository
from holm_persistence.jobs import JOBS
from holm_persistence.model_domain import DOWNLOAD_TASKS
from migration_test_support import upgrade_head
from test_M2_09A_TC_DL_13_download_worker import (
    _clear_database,
    _RejectingResolver,
    _seed_download,
)


@pytest.fixture(autouse=True)
def database(ephemeral_postgres: EphemeralPostgres) -> Iterator[None]:
    upgrade_head(ephemeral_postgres.engine)
    _clear_database(ephemeral_postgres.engine)
    yield
    _clear_database(ephemeral_postgres.engine)


@pytest.mark.parametrize("cleanup_error", [False, True])
def test_FIX_14_TC_DL_13_cooperative_stop_releases_after_handler_cleanup(
    ephemeral_postgres: EphemeralPostgres,
    cleanup_error: bool,
) -> None:
    _, job_id = _seed_download(ephemeral_postgres.engine)

    async def scenario() -> None:
        engine = create_database_engine(ephemeral_postgres.engine.url)
        factory = create_session_factory(engine)
        started, cleaned = asyncio.Event(), asyncio.Event()
        registry = JobHandlerRegistry()

        async def handler(context: JobHandlerContext) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                async with factory() as session:
                    job = await JobRepository(session).get(context.job.id)
                    assert job is not None and job.status is JobStatus.RUNNING
                cleaned.set()
                if cleanup_error:
                    raise RuntimeError("fixture_cleanup_failed")

        registry.register("download.resolve", handler)
        executor = JobExecutor(factory, registry, owner="fix14")
        try:
            running = asyncio.create_task(executor.run_once())
            await asyncio.wait_for(started.wait(), 5)
            begin = time.monotonic()
            running.cancel()
            with pytest.raises(RuntimeError if cleanup_error else asyncio.CancelledError):
                await running
            elapsed = time.monotonic() - begin
            print(f"FIX_14 cancellation_and_release_seconds={elapsed:.6f}")
            assert elapsed < 15
            assert cleaned.is_set()
            async with factory() as session, session.begin():
                repository = JobRepository(session)
                job = await repository.get(job_id)
                assert job is not None
                assert job.status is JobStatus.QUEUED
                assert job.attempt == 0
                assert job.lease_owner is None and job.lease_until is None
                assert job.run_after <= datetime.now(UTC)
                assert not await repository.release(job_id, owner="fix14")
                claimed = await repository.claim(owner="next")
                assert len(claimed) == 1 and claimed[0].attempt == 1
                assert claimed[0].execution_key == job.execution_key
                assert not await repository.release(job_id, owner="fix14")
                await session.execute(
                    sa.update(JOBS)
                    .where(JOBS.c.id == job_id)
                    .values(lease_until=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
                )
                assert not await repository.release(job_id, owner="next")
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("task_status", "latest_status", "old_attempt", "expected"),
    [
        ("Resolving", "Failed", False, "Failed"),
        ("Downloading", "Failed", False, "Failed"),
        ("Verifying", "Failed", False, "Failed"),
        ("Downloading", "Queued", False, "Downloading"),
        ("Downloading", "Running", False, "Downloading"),
        ("Downloading", "Succeeded", False, "Downloading"),
        ("Publishing", "Failed", False, "Publishing"),
        ("Queued", "Failed", False, "Queued"),
        ("Paused", "Failed", False, "Paused"),
        ("Downloading", "Failed", True, "Downloading"),
    ],
)
def test_FIX_14_TC_DL_13_heartbeat_reconciles_latest_current_job_and_resume(
    ephemeral_postgres: EphemeralPostgres,
    task_status: str,
    latest_status: str,
    old_attempt: bool,
    expected: str,
) -> None:
    task_id, job_id = _seed_download(ephemeral_postgres.engine)
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(
            sa.update(DOWNLOAD_TASKS)
            .where(DOWNLOAD_TASKS.c.id == task_id)
            .values(
                status=task_status,
                resolved_revision="a" * 40,
                downloaded_bytes=123,
                attempt=2 if old_attempt else 1,
            )
        )
        connection.execute(
            sa.update(JOBS)
            .where(JOBS.c.id == job_id)
            .values(status="Failed", completed_at=sa.func.clock_timestamp())
        )
        # Equal timestamps exercise the id tie-breaker; the older failed row must not win.
        row = dict(connection.execute(sa.select(JOBS).where(JOBS.c.id == job_id)).mappings().one())
        row.update(
            id=UUID(int=job_id.int + 1),
            status=latest_status,
            completed_at=datetime.now(UTC) if latest_status in {"Failed", "Succeeded"} else None,
            lease_owner="fixture" if latest_status == "Running" else None,
            lease_until=datetime.now(UTC) if latest_status == "Running" else None,
        )
        connection.execute(sa.insert(JOBS).values(**row))

    async def scenario() -> None:
        engine = create_database_engine(ephemeral_postgres.engine.url)
        factory = create_session_factory(engine)
        worker = DownloadWorker(
            session_factory=factory,
            handler=DownloadResolutionJobHandler(
                session_factory=factory, resolver=_RejectingResolver()
            ),
            owner="fix14",
            build_version="test",
        )
        try:
            await worker.write_heartbeat(ComponentStatus.HEALTHY)
            async with factory() as session, session.begin():
                row = (
                    (
                        await session.execute(
                            sa.select(DOWNLOAD_TASKS).where(DOWNLOAD_TASKS.c.id == task_id)
                        )
                    )
                    .mappings()
                    .one()
                )
                assert row["status"] == expected
                assert row["error_code"] == (
                    "download_process_failed" if expected == "Failed" else None
                )
                assert (
                    await DownloadTaskRepository(session).fail_tasks_with_failed_jobs(
                        datetime.now(UTC)
                    )
                    == 0
                )
            if expected == "Failed":
                result = await DownloadCommandService(session_factory=factory).execute(
                    task_id=task_id,
                    command=DownloadCommand.RESUME,
                    actor_id=uuid4(),
                    request_id=uuid4(),
                )
                assert result.task.status.value == "Downloading"
                await worker.write_heartbeat(ComponentStatus.HEALTHY)
                async with factory() as session:
                    row = (
                        (
                            await session.execute(
                                sa.select(DOWNLOAD_TASKS).where(DOWNLOAD_TASKS.c.id == task_id)
                            )
                        )
                        .mappings()
                        .one()
                    )
                    assert row["status"] == "Downloading"
                    assert row["attempt"] == 1 and row["downloaded_bytes"] == 123
        finally:
            await engine.dispose()

    asyncio.run(scenario())

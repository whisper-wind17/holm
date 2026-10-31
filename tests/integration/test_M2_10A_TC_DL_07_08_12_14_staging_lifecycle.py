"""M2-10A integration coverage for the real staging filesystem and PostgreSQL."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Final
from uuid import UUID, uuid4

import holm_common.download_staging as staging_policy
import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from holm_common import DiskSpaceSnapshot
from holm_common.config import StorageConfig
from holm_control_api.download_command_service import DownloadCommand, DownloadCommandService
from holm_domain import (
    DOWNLOAD_CLEANUP_JOB_TYPE,
    DOWNLOAD_RESOLUTION_JOB_TYPE,
    DownloadFailureReason,
    DownloadTaskStatus,
    MetadataStatus,
    ModelOriginType,
)
from holm_download_worker import (
    DownloadProgressRejected,
    DownloadResolutionJobHandler,
    DownloadStagingError,
    DownloadStagingLifecycle,
    DownloadWorker,
)
from holm_download_worker.resolution_subprocess import (
    ResolutionSubprocessRequest,
    ResolutionSubprocessResult,
)
from holm_persistence import (
    JobDraft,
    JobHandlerContext,
    JobRepository,
    LeaseLostError,
    create_database_engine,
    create_session_factory,
)
from holm_persistence.jobs import JOBS
from holm_persistence.model_domain import DOWNLOAD_TASKS, MODELS
from holm_persistence.model_sources import MODEL_SOURCES
from migration_test_support import upgrade_head
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

SOURCE: Final[ModelOriginType] = ModelOriginType.HUGGING_FACE


@pytest.fixture(autouse=True)
def _migrated_empty_database(ephemeral_postgres: EphemeralPostgres) -> Iterator[None]:
    upgrade_head(ephemeral_postgres.engine)
    yield


def _storage(model_root: Path, *, critical_free_gib: int = 5) -> StorageConfig:
    return StorageConfig(
        model_root=model_root,
        disk_critical_free_gib=critical_free_gib,
    )


def _seed_task(
    fixture: EphemeralPostgres,
    *,
    status: DownloadTaskStatus,
    total_bytes: int | None,
    staging_path: str | None = None,
    attempt: int = 1,
    repository_id: str | None = None,
    task_id: UUID | None = None,
) -> UUID:
    task_id = uuid4() if task_id is None else task_id
    model_id = uuid4()
    source_id = uuid4()
    repository = repository_id or f"owner/repository-{task_id}"
    with fixture.engine.begin() as connection:
        now = connection.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        existing_source_id = connection.execute(
            sa.select(MODEL_SOURCES.c.id).where(MODEL_SOURCES.c.type == SOURCE.value)
        ).scalar_one_or_none()
        if existing_source_id is None:
            connection.execute(
                sa.insert(MODEL_SOURCES).values(
                    id=source_id,
                    type=SOURCE.value,
                    credential_key_id=None,
                    credential_nonce=None,
                    credential_ciphertext=None,
                    credential_tag=None,
                    verification_status=None,
                    verified_at=None,
                    verification_reason_code=None,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            source_id = existing_source_id
        connection.execute(
            sa.insert(MODELS).values(
                id=model_id,
                source_id=source_id,
                source_type=SOURCE.value,
                source_key=repository,
                repository_id=repository,
                display_name="M2-10A fixture",
                external_path=None,
                architecture=None,
                modalities=None,
                precision=None,
                quantization=None,
                context_length=None,
                supported_runtimes=None,
                estimated_size_bytes=None,
                license=None,
                metadata_status=MetadataStatus.UNCONFIRMED.value,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            sa.insert(DOWNLOAD_TASKS).values(
                id=task_id,
                model_id=model_id,
                source_type=SOURCE.value,
                repository_id=repository,
                requested_revision_norm="main",
                resolved_revision="a" * 40,
                status=status.value,
                attempt=attempt,
                downloaded_bytes=0,
                total_bytes=total_bytes,
                staging_path=staging_path,
                error_code=None,
                created_at=now,
                updated_at=now,
                completed_at=None,
            )
        )
    return task_id


def _task_row(fixture: EphemeralPostgres, task_id: UUID) -> sa.RowMapping:
    with fixture.engine.connect() as connection:
        return (
            connection.execute(sa.select(DOWNLOAD_TASKS).where(DOWNLOAD_TASKS.c.id == task_id))
            .mappings()
            .one()
        )


async def _engine_factory(
    fixture: EphemeralPostgres,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_database_engine(fixture.engine.url)
    return engine, create_session_factory(engine)


async def _claimed_context(
    factory: async_sessionmaker[AsyncSession],
    *,
    task_id: UUID,
    attempt: int = 1,
    job_type: str = DOWNLOAD_RESOLUTION_JOB_TYPE,
    payload: dict[str, object] | None = None,
    owner: str = "M2-10A-owner",
) -> JobHandlerContext:
    async with factory() as session, session.begin():
        await JobRepository(session).enqueue(
            JobDraft(
                job_type=job_type,
                payload={} if payload is None else payload,
                target_object_id=task_id,
                target_object_version=attempt,
            )
        )
    async with factory() as session, session.begin():
        job = (await JobRepository(session).claim(owner=owner))[0]
    return JobHandlerContext(job=job, lease_lost=asyncio.Event())


class _UnexpectedResolver:
    async def resolve(self, request: ResolutionSubprocessRequest) -> ResolutionSubprocessResult:
        del request
        raise AssertionError("cleanup job invoked the model resolver")


class _RetryCleanupObserved(RuntimeError):
    pass


class _CleanupObservingResolver:
    def __init__(self, task_path: Path) -> None:
        self._task_path = task_path

    async def resolve(self, request: ResolutionSubprocessRequest) -> ResolutionSubprocessResult:
        del request
        if self._task_path.exists():
            raise AssertionError("retry resolver started before staging cleanup")
        raise _RetryCleanupObserved()


def _worker(
    factory: async_sessionmaker[AsyncSession],
    lifecycle: DownloadStagingLifecycle,
    *,
    resolver: _UnexpectedResolver | _CleanupObservingResolver | None = None,
) -> DownloadWorker:
    handler = DownloadResolutionJobHandler(
        session_factory=factory,
        resolver=_UnexpectedResolver() if resolver is None else resolver,
    )
    handler.attach_staging_lifecycle(lifecycle)
    return DownloadWorker(
        session_factory=factory,
        handler=handler,
        owner="M2-10A-worker",
        build_version="m2-10a-test",
        lease_seconds=3,
        heartbeat_seconds=1,
    )


def test_TC_DL_07_worker_prepares_and_persists_only_the_uuid_staging_path(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        root = tmp_path / "models"
        engine, factory = await _engine_factory(ephemeral_postgres)
        try:
            task_id = _seed_task(
                ephemeral_postgres,
                status=DownloadTaskStatus.DOWNLOADING,
                total_bytes=1,
                repository_id="../Ａ/../../host-path",
            )
            context = await _claimed_context(factory, task_id=task_id)
            lifecycle = DownloadStagingLifecycle(
                session_factory=factory,
                storage=_storage(root),
            )

            assert await lifecycle.prepare(context) is True
            expected = (root / ".staging" / str(task_id)).resolve()
            assert expected.is_dir()
            assert _task_row(ephemeral_postgres, task_id)["staging_path"] == str(expected)
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_TC_DL_07_downloading_job_replay_repairs_pre_prepare_crash_window(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        root = tmp_path / "models"
        task_id = uuid4()
        expected = root / ".staging" / str(task_id)
        expected.mkdir(parents=True)
        partial = expected / "partial.bin"
        partial.write_bytes(b"resume-me")
        task_id = _seed_task(
            ephemeral_postgres,
            status=DownloadTaskStatus.DOWNLOADING,
            total_bytes=None,
            staging_path=str(expected.resolve()),
            task_id=task_id,
        )
        engine, factory = await _engine_factory(ephemeral_postgres)
        lifecycle = DownloadStagingLifecycle(
            session_factory=factory,
            storage=_storage(root),
        )
        try:
            async with factory() as session, session.begin():
                await JobRepository(session).enqueue(
                    JobDraft(
                        job_type=DOWNLOAD_RESOLUTION_JOB_TYPE,
                        payload={},
                        target_object_id=task_id,
                        target_object_version=1,
                    )
                )
            result = await _worker(factory, lifecycle).run_once()
            assert result is not None and result.completed is True
            assert expected.is_dir()
            assert partial.read_bytes() == b"resume-me"
            assert _task_row(ephemeral_postgres, task_id)["staging_path"] == str(expected.resolve())
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_TC_DL_07_known_size_preflight_pauses_before_creating_task_directory(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        root = tmp_path / "models"
        root.mkdir()
        total = DiskSpaceSnapshot.for_path(root).free_bytes
        task_id = _seed_task(
            ephemeral_postgres,
            status=DownloadTaskStatus.DOWNLOADING,
            total_bytes=total,
        )
        engine, factory = await _engine_factory(ephemeral_postgres)
        try:
            context = await _claimed_context(factory, task_id=task_id)
            lifecycle = DownloadStagingLifecycle(
                session_factory=factory,
                storage=_storage(root),
            )
            assert await lifecycle.prepare(context) is False
            row = _task_row(ephemeral_postgres, task_id)
            assert row["status"] == DownloadTaskStatus.PAUSED.value
            assert row["error_code"] == DownloadFailureReason.INSUFFICIENT_STORAGE.value
            assert row["staging_path"] is None
            assert not (root / ".staging" / str(task_id)).exists()
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_TC_DL_07_cross_device_staging_fails_before_task_directory_creation(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        root = tmp_path / "models"
        root.mkdir()
        foreign_staging = Path("/dev/shm") / f"m2-10a-{uuid4()}"
        foreign_staging.mkdir()
        (root / ".staging").symlink_to(foreign_staging, target_is_directory=True)
        observed_devices: list[tuple[int, int]] = []
        original_assertion = staging_policy.assert_same_filesystem

        def observe_real_device_check(staging_root: Path, managed_root: Path) -> None:
            devices = (staging_root.stat().st_dev, managed_root.stat().st_dev)
            observed_devices.append(devices)
            original_assertion(staging_root, managed_root)

        monkeypatch.setattr(
            staging_policy,
            "assert_same_filesystem",
            observe_real_device_check,
        )
        task_id = _seed_task(
            ephemeral_postgres,
            status=DownloadTaskStatus.DOWNLOADING,
            total_bytes=None,
        )
        engine, factory = await _engine_factory(ephemeral_postgres)
        try:
            context = await _claimed_context(factory, task_id=task_id)
            lifecycle = DownloadStagingLifecycle(
                session_factory=factory,
                storage=_storage(root),
            )
            with pytest.raises(DownloadStagingError) as captured:
                await lifecycle.prepare(context)
            assert captured.value.reason is DownloadFailureReason.STAGING_FILESYSTEM_MISMATCH
            row = _task_row(ephemeral_postgres, task_id)
            assert row["status"] == DownloadTaskStatus.FAILED.value
            assert row["error_code"] == DownloadFailureReason.STAGING_FILESYSTEM_MISMATCH.value
            assert not (foreign_staging / str(task_id)).exists()
            assert len(observed_devices) == 1
            assert observed_devices[0][0] != observed_devices[0][1]
        finally:
            await engine.dispose()
            foreign_staging.rmdir()

    asyncio.run(exercise())


def test_TC_DL_08_unknown_size_monitor_uses_five_second_tick_and_pauses_without_total(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        root = tmp_path / "models"
        task_id = _seed_task(
            ephemeral_postgres,
            status=DownloadTaskStatus.DOWNLOADING,
            total_bytes=None,
        )
        engine, factory = await _engine_factory(ephemeral_postgres)
        snapshots = iter(
            (
                DiskSpaceSnapshot(free_bytes=10 * 1024**3, capacity_bytes=100 * 1024**3),
                DiskSpaceSnapshot(free_bytes=0, capacity_bytes=100 * 1024**3),
            )
        )
        try:
            context = await _claimed_context(factory, task_id=task_id)
            lifecycle = DownloadStagingLifecycle(
                session_factory=factory,
                storage=_storage(root, critical_free_gib=1),
                disk_snapshot=lambda _path: next(snapshots),
            )
            assert await lifecycle.prepare(context) is True
            observed_waits: list[float] = []

            async def no_sleep(seconds: float) -> None:
                observed_waits.append(seconds)

            paused = await lifecycle.monitor_unknown_size(
                context,
                stop=asyncio.Event(),
                wait=no_sleep,
            )
            assert paused is True
            assert observed_waits == [5.0]
            row = _task_row(ephemeral_postgres, task_id)
            assert row["status"] == DownloadTaskStatus.PAUSED.value
            assert row["total_bytes"] is None
            assert row["error_code"] == DownloadFailureReason.INSUFFICIENT_STORAGE.value
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_TC_DL_08_unknown_size_preflight_pauses_before_creating_task_directory(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        root = tmp_path / "models"
        task_id = _seed_task(
            ephemeral_postgres,
            status=DownloadTaskStatus.DOWNLOADING,
            total_bytes=None,
        )
        engine, factory = await _engine_factory(ephemeral_postgres)
        try:
            context = await _claimed_context(factory, task_id=task_id)
            lifecycle = DownloadStagingLifecycle(
                session_factory=factory,
                storage=_storage(root, critical_free_gib=1),
                disk_snapshot=lambda _path: DiskSpaceSnapshot(
                    free_bytes=0,
                    capacity_bytes=100 * 1024**3,
                ),
            )

            assert await lifecycle.prepare(context) is False
            row = _task_row(ephemeral_postgres, task_id)
            assert row["status"] == DownloadTaskStatus.PAUSED.value
            assert row["error_code"] == DownloadFailureReason.INSUFFICIENT_STORAGE.value
            assert row["total_bytes"] is None
            assert row["staging_path"] is None
            assert not (root / ".staging" / str(task_id)).exists()
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_TC_DL_12_high_frequency_callbacks_produce_only_admitted_database_updates(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        task_id = _seed_task(
            ephemeral_postgres,
            status=DownloadTaskStatus.DOWNLOADING,
            total_bytes=10_000,
        )
        engine, factory = await _engine_factory(ephemeral_postgres)
        statements: list[str] = []

        def observe_update(
            conn: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            executemany: bool,
        ) -> None:
            del conn, cursor, parameters, context, executemany
            if statement.lstrip().upper().startswith("UPDATE CORE.DOWNLOAD_TASKS"):
                statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", observe_update)
        clock = [10.0]
        try:
            context = await _claimed_context(factory, task_id=task_id)
            writer = await DownloadStagingLifecycle(
                session_factory=factory,
                storage=_storage(tmp_path / "unused-models"),
            ).progress_writer(
                context=context,
                monotonic_clock=lambda: clock[0],
            )
            assert await writer.record(0) is True
            for value in range(1, 100):
                clock[0] += 0.001
                assert await writer.record(value) is False
            clock[0] = 10.2
            assert await writer.record(100) is True
            clock[0] = 10.9
            assert await writer.record(150) is False
            clock[0] = 11.2
            assert await writer.record(150) is True

            assert len(statements) == 3
            assert _task_row(ephemeral_postgres, task_id)["downloaded_bytes"] == 150
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", observe_update)
            await engine.dispose()

    asyncio.run(exercise())


def test_TC_DL_12_unknown_progress_remains_indeterminate_and_time_throttled(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        task_id = _seed_task(
            ephemeral_postgres,
            status=DownloadTaskStatus.DOWNLOADING,
            total_bytes=None,
        )
        engine, factory = await _engine_factory(ephemeral_postgres)
        clock = [0.0]
        try:
            context = await _claimed_context(factory, task_id=task_id)
            writer = await DownloadStagingLifecycle(
                session_factory=factory,
                storage=_storage(tmp_path / "models"),
            ).progress_writer(
                context=context,
                monotonic_clock=lambda: clock[0],
            )
            assert writer.percentage(10_000_000) is None
            assert await writer.record(1) is True
            clock[0] = 0.999
            assert await writer.record(10_000_000) is False
            clock[0] = 1.0
            assert await writer.record(10_000_000) is True
            assert _task_row(ephemeral_postgres, task_id)["total_bytes"] is None
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_TC_DL_12_stale_lease_and_regressing_progress_both_fail_loudly(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        task_id = _seed_task(
            ephemeral_postgres,
            status=DownloadTaskStatus.DOWNLOADING,
            total_bytes=100,
        )
        engine, factory = await _engine_factory(ephemeral_postgres)
        lifecycle = DownloadStagingLifecycle(
            session_factory=factory,
            storage=_storage(tmp_path / "models"),
        )
        try:
            first_context = await _claimed_context(
                factory,
                task_id=task_id,
                owner="M2-10A-first-owner",
            )
            first_clock = [0.0]
            first_writer = await lifecycle.progress_writer(
                context=first_context,
                monotonic_clock=lambda: first_clock[0],
            )
            assert await first_writer.record(80) is True

            with ephemeral_postgres.engine.begin() as connection:
                connection.execute(
                    sa.update(JOBS)
                    .where(JOBS.c.id == first_context.job.id)
                    .values(lease_until=sa.func.clock_timestamp() - sa.text("INTERVAL '1 second'"))
                )
            async with factory() as session, session.begin():
                replacement_job = (
                    await JobRepository(session).claim(owner="M2-10A-replacement-owner")
                )[0]
            replacement_context = JobHandlerContext(
                job=replacement_job,
                lease_lost=asyncio.Event(),
            )

            first_clock[0] = 1.0
            with pytest.raises(LeaseLostError):
                await first_writer.record(90)
            assert first_context.lease_lost.is_set()

            replacement_writer = await lifecycle.progress_writer(
                context=replacement_context,
                monotonic_clock=lambda: 2.0,
            )
            with pytest.raises(DownloadProgressRejected):
                await replacement_writer.record(70)
            assert _task_row(ephemeral_postgres, task_id)["downloaded_bytes"] == 80
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_TC_DL_14_cancel_cleanup_job_deletes_then_clears_and_replay_is_idempotent(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        root = tmp_path / "models"
        task_id = _seed_task(
            ephemeral_postgres,
            status=DownloadTaskStatus.DOWNLOADING,
            total_bytes=None,
        )
        task_path = root / ".staging" / str(task_id)
        task_path.mkdir(parents=True)
        (task_path / "partial.bin").write_bytes(b"fixture")
        with ephemeral_postgres.engine.begin() as connection:
            connection.execute(
                sa.update(DOWNLOAD_TASKS)
                .where(DOWNLOAD_TASKS.c.id == task_id)
                .values(staging_path=str(task_path.resolve()))
            )
        engine, factory = await _engine_factory(ephemeral_postgres)
        lifecycle = DownloadStagingLifecycle(
            session_factory=factory,
            storage=_storage(root),
        )
        try:
            await DownloadCommandService(session_factory=factory).execute(
                task_id=task_id,
                command=DownloadCommand.CANCEL,
                actor_id=uuid4(),
                request_id=uuid4(),
            )
            with ephemeral_postgres.engine.connect() as connection:
                queued_type = connection.execute(
                    sa.select(JOBS.c.job_type).where(JOBS.c.target_object_id == task_id)
                ).scalar_one()
            assert queued_type == DOWNLOAD_CLEANUP_JOB_TYPE

            result = await _worker(factory, lifecycle).run_once()
            assert result is not None and result.completed is True
            assert not task_path.exists()
            assert _task_row(ephemeral_postgres, task_id)["staging_path"] is None

            # A crash after mkdir but before path persistence still converges:
            # the UUID layout is platform-confirmed even when the column is NULL.
            task_path.mkdir()
            (task_path / "orphaned-partial.bin").write_bytes(b"fixture")
            async with factory() as session, session.begin():
                await JobRepository(session).enqueue(
                    JobDraft(
                        job_type=DOWNLOAD_CLEANUP_JOB_TYPE,
                        payload={},
                        target_object_id=task_id,
                        target_object_version=1,
                    )
                )
            replay = await _worker(factory, lifecycle).run_once()
            assert replay is not None and replay.completed is True
            assert not task_path.exists()
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_TC_DL_14_retry_payload_cleans_staging_before_resolution(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        root = tmp_path / "models"
        task_id = uuid4()
        task_path = root / ".staging" / str(task_id)
        task_path.mkdir(parents=True)
        (task_path / "partial.bin").write_bytes(b"fixture")
        _seed_task(
            ephemeral_postgres,
            status=DownloadTaskStatus.FAILED,
            total_bytes=None,
            staging_path=str(task_path.resolve()),
            task_id=task_id,
        )
        engine, factory = await _engine_factory(ephemeral_postgres)
        lifecycle = DownloadStagingLifecycle(
            session_factory=factory,
            storage=_storage(root),
        )
        try:
            await DownloadCommandService(session_factory=factory).execute(
                task_id=task_id,
                command=DownloadCommand.RETRY,
                actor_id=uuid4(),
                request_id=uuid4(),
            )
            with pytest.raises(_RetryCleanupObserved):
                await _worker(
                    factory,
                    lifecycle,
                    resolver=_CleanupObservingResolver(task_path),
                ).run_once()
            row = _task_row(ephemeral_postgres, task_id)
            assert row["status"] == DownloadTaskStatus.RESOLVING.value
            assert row["attempt"] == 2
            assert row["staging_path"] is None
            assert not task_path.exists()
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize("unsafe_kind", ["outside", "regular_file"])
def test_TC_DL_14_cleanup_failure_retains_the_only_staging_locator(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    async def exercise() -> None:
        root = tmp_path / "models"
        task_id = uuid4()
        expected = root / ".staging" / str(task_id)
        if unsafe_kind == "outside":
            unsafe = tmp_path / "external" / str(task_id)
            unsafe.mkdir(parents=True)
        else:
            expected.parent.mkdir(parents=True)
            expected.write_bytes(b"not-a-directory")
            unsafe = expected
        seeded_id = _seed_task(
            ephemeral_postgres,
            status=DownloadTaskStatus.CANCELLED,
            total_bytes=None,
            staging_path=str(unsafe.resolve()),
            task_id=task_id,
        )
        assert seeded_id == task_id
        engine, factory = await _engine_factory(ephemeral_postgres)
        try:
            context = await _claimed_context(
                factory,
                task_id=seeded_id,
                job_type=DOWNLOAD_CLEANUP_JOB_TYPE,
            )
            lifecycle = DownloadStagingLifecycle(
                session_factory=factory,
                storage=_storage(root),
            )
            with pytest.raises(DownloadStagingError):
                await lifecycle.cleanup(
                    context,
                    expected_status=DownloadTaskStatus.CANCELLED,
                )
            row = _task_row(ephemeral_postgres, seeded_id)
            assert row["staging_path"] == str(unsafe.resolve())
            assert row["error_code"] == DownloadFailureReason.STAGING_CLEANUP_FAILED.value
            assert unsafe.exists()
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize("symlink_kind", ["task", "parent"])
def test_TC_DL_14_null_locator_symlink_cleanup_fails_without_deleting_target(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    symlink_kind: str,
) -> None:
    async def exercise() -> None:
        root = tmp_path / "models"
        task_id = uuid4()
        outside_task = tmp_path / "outside" / str(task_id)
        outside_task.mkdir(parents=True)
        sentinel = outside_task / "must-survive.bin"
        sentinel.write_bytes(b"fixture")
        if symlink_kind == "parent":
            root.mkdir()
            (root / ".staging").symlink_to(
                outside_task.parent,
                target_is_directory=True,
            )
        else:
            task_link = root / ".staging" / str(task_id)
            task_link.parent.mkdir(parents=True)
            task_link.symlink_to(outside_task, target_is_directory=True)
        _seed_task(
            ephemeral_postgres,
            status=DownloadTaskStatus.CANCELLED,
            total_bytes=None,
            staging_path=None,
            task_id=task_id,
        )
        engine, factory = await _engine_factory(ephemeral_postgres)
        try:
            context = await _claimed_context(
                factory,
                task_id=task_id,
                job_type=DOWNLOAD_CLEANUP_JOB_TYPE,
            )
            lifecycle = DownloadStagingLifecycle(
                session_factory=factory,
                storage=_storage(root),
            )
            with pytest.raises(DownloadStagingError) as captured:
                await lifecycle.cleanup(
                    context,
                    expected_status=DownloadTaskStatus.CANCELLED,
                )

            assert captured.value.reason is DownloadFailureReason.STAGING_CLEANUP_FAILED
            row = _task_row(ephemeral_postgres, task_id)
            assert row["staging_path"] is None
            assert row["error_code"] == DownloadFailureReason.STAGING_CLEANUP_FAILED.value
            assert sentinel.read_bytes() == b"fixture"
        finally:
            await engine.dispose()

    asyncio.run(exercise())

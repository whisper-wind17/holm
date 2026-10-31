"""Download staging lifecycle coordinated behind the active job lease."""

from __future__ import annotations

import asyncio
import errno
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Final
from uuid import UUID

from holm_common import (
    DiskSpaceSnapshot,
    ProgressWriteThrottle,
    StagingFilesystemMismatch,
    StagingLayout,
    StagingPathRejected,
)
from holm_common.config import StorageConfig
from holm_domain import DownloadFailureReason, DownloadTaskStatus
from holm_persistence import (
    AsyncSessionFactory,
    JobHandlerContext,
    JobRepository,
    LeaseLostError,
    database_utc_now,
)
from holm_persistence.download_task_repository import (
    DownloadResolutionWorkRecord,
    DownloadTaskRepository,
)
from sqlalchemy.ext.asyncio import AsyncSession

UNKNOWN_SIZE_DISK_CHECK_SECONDS: Final[float] = 5.0


class DownloadStagingError(RuntimeError):
    """A sanitized staging failure; host paths never enter its message."""

    def __init__(self, reason: DownloadFailureReason) -> None:
        self.reason = reason
        super().__init__("download staging operation failed")


class DownloadProgressRejected(RuntimeError):
    """The active task attempt no longer accepts progress."""

    def __init__(self) -> None:
        super().__init__("download progress update rejected")


class DownloadProgressWriter:
    """One bounded progress state machine for one task attempt."""

    __slots__ = (
        "_clock",
        "_context",
        "_lock",
        "_session_factory",
        "_throttle",
    )

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory,
        context: JobHandlerContext,
        total_bytes: int | None,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session_factory = session_factory
        self._context = context
        self._clock = monotonic_clock
        self._lock = asyncio.Lock()
        self._throttle = ProgressWriteThrottle(total_bytes)

    def percentage(self, downloaded_bytes: int) -> int | None:
        return self._throttle.percentage(downloaded_bytes)

    async def record(self, downloaded_bytes: int) -> bool:
        """Persist only samples admitted by the one-second/one-percent policy."""

        async with self._lock:
            observed_at = self._clock()
            if not self._throttle.is_due(downloaded_bytes, monotonic_now=observed_at):
                return False
            self._context.ensure_lease()
            owner = self._context.job.lease_owner
            if owner is None:
                raise LeaseLostError()
            async with self._session_factory() as session, session.begin():
                if not await JobRepository(session).lock_owned_lease(
                    self._context.job.id,
                    owner=owner,
                ):
                    self._context.lease_lost.set()
                    raise LeaseLostError()
                repository = DownloadTaskRepository(session)
                updated = await repository.update_download_progress(
                    self._context.job.target_object_id,
                    expected_attempt=self._context.job.target_object_version,
                    downloaded_bytes=downloaded_bytes,
                    now=await database_utc_now(session),
                )
            if not updated:
                raise DownloadProgressRejected()
            self._throttle.mark_written(downloaded_bytes, monotonic_now=observed_at)
            return True


class DownloadStagingLifecycle:
    """Own platform-created staging directories without owning download bytes."""

    __slots__ = ("_disk_snapshot", "_session_factory", "_storage", "layout")

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory,
        storage: StorageConfig,
        disk_snapshot: Callable[[Path], DiskSpaceSnapshot] = DiskSpaceSnapshot.for_path,
    ) -> None:
        self._session_factory = session_factory
        self._storage = storage
        self._disk_snapshot = disk_snapshot
        self.layout = StagingLayout(storage.model_root)

    async def progress_writer(
        self,
        *,
        context: JobHandlerContext,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> DownloadProgressWriter:
        task = await self._load_owned_task(context)
        if task.status is not DownloadTaskStatus.DOWNLOADING:
            raise DownloadProgressRejected()
        return DownloadProgressWriter(
            session_factory=self._session_factory,
            context=context,
            total_bytes=task.total_bytes,
            monotonic_clock=monotonic_clock,
        )

    async def prepare(self, context: JobHandlerContext) -> bool:
        """Return false after either storage preflight pauses the task."""

        task = await self._load_owned_task(context)
        if task.status is not DownloadTaskStatus.DOWNLOADING:
            raise DownloadStagingError(DownloadFailureReason.DOWNLOAD_TASK_STATE_MISMATCH)
        try:
            path, capacity = await asyncio.to_thread(
                self.layout.preflight,
                task.id,
                total_bytes=task.total_bytes,
            )
            if task.total_bytes is None:
                snapshot = await asyncio.to_thread(
                    self._disk_snapshot,
                    self.layout.staging_root,
                )
                if snapshot.is_critical(
                    free_percent=self._storage.disk_critical_percent,
                    free_gibibytes=self._storage.disk_critical_free_gib,
                ):
                    await self._pause_for_storage(context, task)
                    return False
        except OSError as error:
            if error.errno in {errno.ENOSPC, errno.EDQUOT}:
                await self._pause_for_storage(context, task)
                return False
            await self._record_failure(
                context,
                task,
                reason=DownloadFailureReason.STAGING_FILESYSTEM_MISMATCH,
                terminal=True,
            )
            raise DownloadStagingError(DownloadFailureReason.STAGING_FILESYSTEM_MISMATCH) from None
        except (StagingFilesystemMismatch, StagingPathRejected):
            await self._record_failure(
                context,
                task,
                reason=DownloadFailureReason.STAGING_FILESYSTEM_MISMATCH,
                terminal=True,
            )
            raise DownloadStagingError(DownloadFailureReason.STAGING_FILESYSTEM_MISMATCH) from None
        context.ensure_lease()
        if capacity is not None and not capacity.accepted:
            await self._pause_for_storage(context, task)
            return False
        if task.staging_path not in (None, str(path)):
            await self._record_failure(
                context,
                task,
                reason=DownloadFailureReason.DOWNLOAD_TASK_IDENTITY_MISMATCH,
                terminal=True,
            )
            raise DownloadStagingError(DownloadFailureReason.DOWNLOAD_TASK_IDENTITY_MISMATCH)
        try:
            async with self._session_factory() as session, session.begin():
                await self._lock_owned_job(session, context)
                saved = await DownloadTaskRepository(session).save_staging_path(
                    task.id,
                    expected_attempt=task.attempt,
                    staging_path=str(path),
                    now=await database_utc_now(session),
                )
                if not saved:
                    raise DownloadStagingError(
                        DownloadFailureReason.DOWNLOAD_TASK_IDENTITY_MISMATCH
                    )
                await asyncio.to_thread(self.layout.create_task_directory, task.id)
        except OSError as error:
            if error.errno in {errno.ENOSPC, errno.EDQUOT}:
                await self._pause_for_storage(context, task)
                return False
            await self._record_failure(
                context,
                task,
                reason=DownloadFailureReason.STAGING_FILESYSTEM_MISMATCH,
                terminal=True,
            )
            raise DownloadStagingError(DownloadFailureReason.STAGING_FILESYSTEM_MISMATCH) from None
        except StagingPathRejected:
            await self._record_failure(
                context,
                task,
                reason=DownloadFailureReason.STAGING_FILESYSTEM_MISMATCH,
                terminal=True,
            )
            raise DownloadStagingError(DownloadFailureReason.STAGING_FILESYSTEM_MISMATCH) from None
        return True

    async def cleanup(
        self,
        context: JobHandlerContext,
        *,
        expected_status: DownloadTaskStatus,
    ) -> None:
        """Delete first, then clear the only persisted path that can find the directory."""

        context.ensure_lease()
        cleanup_failed = False
        async with self._session_factory() as session, session.begin():
            await self._lock_owned_job(session, context)
            repository = DownloadTaskRepository(session)
            task = await repository.lock_resolution_work(context.job.target_object_id)
            if task is None and expected_status is DownloadTaskStatus.CANCELLED:
                # A terminal task may be deleted while its cancellation cleanup
                # is queued. UUID-derived paths retain the staging-root fence.
                await self._delete_while_fenced(
                    context.job.target_object_id,
                    str(self.layout.task_path(context.job.target_object_id)),
                )
                return
            if (
                task is None
                or task.attempt != context.job.target_object_version
                or task.status is not expected_status
            ):
                raise DownloadStagingError(DownloadFailureReason.DOWNLOAD_TASK_IDENTITY_MISMATCH)
            try:
                cleanup_path = task.staging_path or str(self.layout.task_path(task.id))
                await self._delete_while_fenced(task.id, cleanup_path)
            except (OSError, StagingPathRejected):
                target = (
                    DownloadTaskStatus.FAILED
                    if expected_status is DownloadTaskStatus.RESOLVING
                    else task.status
                )
                transitioned = await repository.transition_staging_failure(
                    task.id,
                    expected_attempt=task.attempt,
                    expected_status=task.status,
                    status=target,
                    reason=DownloadFailureReason.STAGING_CLEANUP_FAILED,
                    now=await database_utc_now(session),
                )
                if not transitioned:
                    raise DownloadStagingError(
                        DownloadFailureReason.STAGING_CLEANUP_FAILED
                    ) from None
                cleanup_failed = True
            else:
                cleared = await repository.clear_staging_path(
                    task.id,
                    expected_attempt=task.attempt,
                    expected_status=expected_status,
                    now=await database_utc_now(session),
                )
                if not cleared:
                    raise DownloadStagingError(
                        DownloadFailureReason.DOWNLOAD_TASK_IDENTITY_MISMATCH
                    )
        if cleanup_failed:
            raise DownloadStagingError(DownloadFailureReason.STAGING_CLEANUP_FAILED)

    async def _delete_while_fenced(self, task_id: UUID, cleanup_path: str) -> None:
        deletion = asyncio.create_task(
            asyncio.to_thread(self.layout.cleanup, task_id, cleanup_path)
        )
        try:
            await asyncio.shield(deletion)
        except asyncio.CancelledError:
            with suppress(Exception):
                await deletion
            raise

    async def monitor_unknown_size(
        self,
        context: JobHandlerContext,
        *,
        stop: asyncio.Event,
        interval_seconds: float = UNKNOWN_SIZE_DISK_CHECK_SECONDS,
        wait: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> bool:
        """Poll unknown-size work and pause at the first configured critical watermark."""

        if interval_seconds <= 0:
            raise ValueError("disk check interval must be positive")
        task = await self._load_owned_task(context)
        if task.status is not DownloadTaskStatus.DOWNLOADING or task.total_bytes is not None:
            raise ValueError("unknown-size monitor requires an active unknown-size download")
        while not stop.is_set():
            await wait(interval_seconds)
            if stop.is_set():
                return False
            context.ensure_lease()
            snapshot = await asyncio.to_thread(
                self._disk_snapshot,
                self.layout.staging_root,
            )
            if snapshot.is_critical(
                free_percent=self._storage.disk_critical_percent,
                free_gibibytes=self._storage.disk_critical_free_gib,
            ):
                await self._pause_for_storage(context, task)
                return True
        return False

    async def _load_owned_task(self, context: JobHandlerContext) -> DownloadResolutionWorkRecord:
        context.ensure_lease()
        async with self._session_factory() as session, session.begin():
            await self._lock_owned_job(session, context)
            task = await DownloadTaskRepository(session).lock_resolution_work(
                context.job.target_object_id
            )
            if task is None or task.attempt != context.job.target_object_version:
                raise DownloadStagingError(DownloadFailureReason.DOWNLOAD_TASK_IDENTITY_MISMATCH)
            return task

    @staticmethod
    async def _lock_owned_job(session: AsyncSession, context: JobHandlerContext) -> None:
        owner = context.job.lease_owner
        if owner is None:
            raise LeaseLostError()
        if not await JobRepository(session).lock_owned_lease(context.job.id, owner=owner):
            context.lease_lost.set()
            raise LeaseLostError()

    async def _pause_for_storage(
        self,
        context: JobHandlerContext,
        task: DownloadResolutionWorkRecord,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            await self._lock_owned_job(session, context)
            if not await DownloadTaskRepository(session).pause_for_storage(
                task.id,
                expected_attempt=task.attempt,
                now=await database_utc_now(session),
            ):
                raise DownloadStagingError(DownloadFailureReason.INSUFFICIENT_STORAGE)

    async def _record_failure(
        self,
        context: JobHandlerContext,
        task: DownloadResolutionWorkRecord,
        *,
        reason: DownloadFailureReason,
        terminal: bool,
    ) -> None:
        context.ensure_lease()
        async with self._session_factory() as session, session.begin():
            await self._lock_owned_job(session, context)
            target = DownloadTaskStatus.FAILED if terminal else task.status
            if not await DownloadTaskRepository(session).transition_staging_failure(
                task.id,
                expected_attempt=task.attempt,
                expected_status=task.status,
                status=target,
                reason=reason,
                now=await database_utc_now(session),
            ):
                raise DownloadStagingError(reason)


__all__ = [
    "UNKNOWN_SIZE_DISK_CHECK_SECONDS",
    "DownloadProgressRejected",
    "DownloadProgressWriter",
    "DownloadStagingError",
    "DownloadStagingLifecycle",
]

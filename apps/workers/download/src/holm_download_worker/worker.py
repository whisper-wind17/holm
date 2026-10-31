"""Leased Download Worker loop and resolution-stage job handler."""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol, assert_never
from uuid import UUID

from holm_control_api.download_resolution_merge_service import (
    DownloadResolutionMergeService,
)
from holm_domain import (
    DOWNLOAD_CLEANUP_JOB_TYPE,
    DOWNLOAD_RESOLUTION_JOB_TYPE,
    EXTERNAL_REGISTRATION_JOB_TYPE,
    PRODUCTION_EVENT_REGISTRY,
    Component,
    ComponentStatus,
    DomainEventType,
    DownloadFailureReason,
    DownloadTaskStatus,
    EventTypeRegistry,
    Identified,
    ModelOriginType,
    ResolvedModel,
)
from holm_domain.download_merge import (
    DownloadCompleted,
    DownloadMerged,
    DownloadMergeResult,
    DownloadStarted,
)
from holm_domain.model_acquisition import DownloadCancellationReason
from holm_domain.model_layout import DeployableLayout, deployable_layout
from holm_persistence import (
    DEFAULT_HEARTBEAT_SECONDS,
    AsyncSessionFactory,
    ComponentHeartbeatRepository,
    DomainEventRepository,
    JobExecutor,
    JobHandlerContext,
    JobHandlerRegistry,
    JobRepository,
    JobRunResult,
    LeaseLostError,
    database_utc_now,
)
from holm_persistence.artifact_check_repository import ARTIFACT_LIGHT_VERIFY_JOB_TYPE
from holm_persistence.artifact_deletion_repository import (
    ARTIFACT_DELETE_JOB_TYPE,
    ARTIFACT_TRASH_JOB_TYPE,
)
from holm_persistence.artifact_verification_repository import ARTIFACT_VERIFY_JOB_TYPE
from holm_persistence.download_task_repository import (
    DownloadResolutionWorkRecord,
    DownloadTaskRepository,
)
from holm_persistence.launch_verification_repository import (
    LAUNCH_VERIFY_JOB_TYPE,
    LaunchVerificationRepository,
)
from sqlalchemy.exc import DBAPIError, DisconnectionError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from holm_download_worker.artifact_deletion import ArtifactDeletionHandler
from holm_download_worker.artifact_publishing import (
    ArtifactPublicationError,
    ArtifactValidationError,
    DownloadArtifactPublisher,
)
from holm_download_worker.artifact_verification import ArtifactVerificationHandler
from holm_download_worker.download_subprocess import (
    AsyncDownloadProgressCallback,
    DownloadSubprocessError,
    DownloadSubprocessRequest,
    DownloadSubprocessResult,
)
from holm_download_worker.resolution_subprocess import (
    ResolutionSubprocessRequest,
    ResolutionSubprocessResult,
)
from holm_download_worker.staging_lifecycle import (
    DownloadProgressRejected,
    DownloadStagingLifecycle,
)

_LOGGER: Final[logging.Logger] = logging.getLogger("holm.download_worker")
_DATABASE_RETRY_BACKOFF_SECONDS: Final[tuple[float, ...]] = (1.0, 2.0, 4.0, 5.0)
_SQLALCHEMY_DATABASE_ERRORS: Final[tuple[type[BaseException], ...]] = (
    DBAPIError,
    DisconnectionError,
    SQLAlchemyTimeoutError,
)
_DATABASE_ERRORS: Final[tuple[type[BaseException], ...]] = (
    *_SQLALCHEMY_DATABASE_ERRORS,
    OSError,
)


class DownloadWorkerError(RuntimeError):
    """Stable worker failure whose message cannot contain source data."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("download worker operation failed")


class DownloadWorkDisposition(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    MERGED = "merged"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    VERIFYING = "verifying"
    FAILED = "failed"


class ModelResolver(Protocol):
    async def resolve(self, request: ResolutionSubprocessRequest) -> ResolutionSubprocessResult: ...


class ResolutionMerger(Protocol):
    async def merge(self, task_id: UUID, resolved: ResolvedModel) -> DownloadMergeResult: ...


class ModelDownloader(Protocol):
    async def download(
        self,
        request: DownloadSubprocessRequest,
        progress: AsyncDownloadProgressCallback,
    ) -> DownloadSubprocessResult: ...


class DownloadResolutionJobHandler:
    """Advance only a database-leased Download Task through resolution."""

    __slots__ = (
        "_command_poll_seconds",
        "_downloader",
        "_events",
        "_hugging_face_endpoint",
        "_modelscope_endpoint",
        "_merger",
        "_publisher",
        "_resolver",
        "_session_factory",
        "_staging",
    )

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory,
        resolver: ModelResolver,
        merger: ResolutionMerger | None = None,
        event_registry: EventTypeRegistry = PRODUCTION_EVENT_REGISTRY,
        command_poll_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    ) -> None:
        if command_poll_seconds <= 0:
            raise ValueError("download command poll interval must be positive")
        self._session_factory = session_factory
        self._resolver = resolver
        self._merger = (
            DownloadResolutionMergeService(session_factory=session_factory)
            if merger is None
            else merger
        )
        self._events = event_registry
        self._command_poll_seconds = command_poll_seconds
        self._staging: DownloadStagingLifecycle | None = None
        self._downloader: ModelDownloader | None = None
        self._hugging_face_endpoint: str | None = None
        self._modelscope_endpoint: str | None = None
        self._publisher: DownloadArtifactPublisher | None = None

    def attach_staging_lifecycle(self, lifecycle: DownloadStagingLifecycle) -> None:
        """Attach the file lifecycle without changing the frozen constructor contract."""

        if self._staging is not None:
            raise RuntimeError("download staging lifecycle is already attached")
        self._staging = lifecycle

    def attach_download_executor(
        self,
        downloader: ModelDownloader,
        *,
        hugging_face_endpoint: str,
        modelscope_endpoint: str,
    ) -> None:
        """Attach download execution without changing the frozen constructor contract."""

        if self._downloader is not None:
            raise RuntimeError("download executor is already attached")
        if not hugging_face_endpoint or hugging_face_endpoint != hugging_face_endpoint.strip():
            raise ValueError("Hugging Face endpoint must be non-empty and trimmed")
        if not modelscope_endpoint or modelscope_endpoint != modelscope_endpoint.strip():
            raise ValueError("ModelScope endpoint must be non-empty and trimmed")
        self._downloader = downloader
        self._hugging_face_endpoint = hugging_face_endpoint
        self._modelscope_endpoint = modelscope_endpoint

    def attach_artifact_publisher(self, publisher: DownloadArtifactPublisher) -> None:
        """Attach the terminal pipeline without changing prior test compositions."""

        if self._publisher is not None:
            raise RuntimeError("download artifact publisher is already attached")
        self._publisher = publisher

    async def __call__(self, context: JobHandlerContext) -> None:
        await self.process(context)

    async def process(self, context: JobHandlerContext) -> DownloadWorkDisposition:
        if context.job.payload.get("staging_cleanup_requested") is True:
            if self._staging is None:
                raise DownloadWorkerError("download_staging_lifecycle_unavailable")
            await self._staging.cleanup(
                context,
                expected_status=DownloadTaskStatus.RESOLVING,
            )
        task = await self._prepare_resolution(context)
        if task.status in {DownloadTaskStatus.VERIFYING, DownloadTaskStatus.PUBLISHING} and (
            self._publisher is not None
        ):
            return await self._publish(context, task, resolved=None)
        replayed = self._replayed_disposition(task)
        if replayed is not None:
            if replayed is DownloadWorkDisposition.STARTED and self._staging is not None:
                prepared = await self._staging.prepare(context)
                if not prepared:
                    return DownloadWorkDisposition.PAUSED
                if self._downloader is not None:
                    disposition = await self._download(context)
                    return await self._after_download(context, disposition, resolved=None)
            return replayed

        context.ensure_lease()
        resolution = await self._resolve_until_commanded(
            context,
            task,
            ResolutionSubprocessRequest(
                source=task.source_type,
                repository_id=task.repository_id,
                requested_revision_norm=task.requested_revision_norm,
                execution_key=context.idempotency_key,
            ),
        )
        if isinstance(resolution, DownloadWorkDisposition):
            return resolution
        context.ensure_lease()
        files = resolution.resolved.files
        if (
            deployable_layout(
                (entry.path for entry in files.value) if isinstance(files, Identified) else None
            )
            is DeployableLayout.UNSUPPORTED
        ):
            return await self._reject_layout(context)
        result = await self._merge_while_owned(context, resolution.resolved)
        if isinstance(result, DownloadWorkDisposition):
            return result
        disposition = self._merge_disposition(context, result)
        if disposition is DownloadWorkDisposition.STARTED and self._staging is not None:
            prepared = await self._staging.prepare(context)
            if not prepared:
                return DownloadWorkDisposition.PAUSED
            if self._downloader is not None:
                downloaded = await self._download(context)
                return await self._after_download(
                    context,
                    downloaded,
                    resolved=resolution.resolved,
                )
        return disposition

    async def _reject_layout(self, context: JobHandlerContext) -> DownloadWorkDisposition:
        context.ensure_lease()
        owner = context.job.lease_owner
        if owner is None:
            raise LeaseLostError()
        async with self._session_factory() as session, session.begin():
            repository = DownloadTaskRepository(session)
            if not await JobRepository(session).lock_owned_lease(context.job.id, owner=owner):
                context.lease_lost.set()
                raise LeaseLostError()
            task = await repository.lock_resolution_work(context.job.target_object_id)
            if task is None or task.attempt != context.job.target_object_version:
                raise DownloadWorkerError("download_job_identity_mismatch")
            commanded = self._commanded_disposition(task)
            if commanded is not None:
                return commanded
            if not await repository.transition_staging_failure(
                task.id,
                expected_attempt=task.attempt,
                expected_status=DownloadTaskStatus.RESOLVING,
                status=DownloadTaskStatus.FAILED,
                reason=DownloadFailureReason.MODEL_LAYOUT_UNSUPPORTED,
                now=await database_utc_now(session),
            ):
                raise DownloadWorkerError("download_task_state_mismatch")
        return DownloadWorkDisposition.FAILED

    async def _after_download(
        self,
        context: JobHandlerContext,
        disposition: DownloadWorkDisposition,
        *,
        resolved: ResolvedModel | None,
    ) -> DownloadWorkDisposition:
        if disposition is not DownloadWorkDisposition.VERIFYING or self._publisher is None:
            return disposition
        task = await self._load_download_task(context)
        return await self._publish(context, task, resolved=resolved)

    async def _publish(
        self,
        context: JobHandlerContext,
        task: DownloadResolutionWorkRecord,
        *,
        resolved: ResolvedModel | None,
    ) -> DownloadWorkDisposition:
        publisher = self._publisher
        if publisher is None:
            return DownloadWorkDisposition.VERIFYING
        if await publisher.requires_resolution(task) and resolved is None:
            if task.resolved_revision is None:
                raise DownloadWorkerError("download_task_identity_mismatch")
            resolution = await self._resolve_until_commanded(
                context,
                task,
                ResolutionSubprocessRequest(
                    source=task.source_type,
                    repository_id=task.repository_id,
                    requested_revision_norm=task.resolved_revision,
                    execution_key=context.idempotency_key,
                ),
            )
            if isinstance(resolution, DownloadWorkDisposition):
                return resolution
            resolved = resolution.resolved
        try:
            await publisher.publish(context, task, resolved)
        except ArtifactValidationError:
            return DownloadWorkDisposition.FAILED
        except ArtifactPublicationError as error:
            if not error.renamed:
                return DownloadWorkDisposition.FAILED
            raise
        return DownloadWorkDisposition.COMPLETED

    async def _download(self, context: JobHandlerContext) -> DownloadWorkDisposition:
        if self._staging is None or self._downloader is None:
            raise DownloadWorkerError("download_execution_unavailable")
        task = await self._load_download_task(context)
        if task.status is not DownloadTaskStatus.DOWNLOADING:
            raise DownloadWorkerError("download_task_state_mismatch")
        if task.resolved_revision is None or task.staging_path is None:
            raise DownloadWorkerError("download_task_identity_mismatch")
        endpoint = self._download_endpoint(task.source_type)

        writer = await self._staging.progress_writer(context=context)
        transferred_bytes = 0

        async def progress(increment: int) -> None:
            nonlocal transferred_bytes
            if increment <= 0:
                raise DownloadWorkerError("download_progress_invalid")
            context.ensure_lease()
            transferred_bytes += increment
            observed = task.downloaded_bytes + transferred_bytes
            if task.total_bytes is not None:
                observed = min(observed, task.total_bytes)
            await writer.record(observed)

        request = DownloadSubprocessRequest(
            source=task.source_type,
            repository_id=task.repository_id,
            resolved_revision=task.resolved_revision,
            staging_path=Path(task.staging_path),
            endpoint=endpoint,
            execution_key=context.idempotency_key,
        )
        download = asyncio.create_task(self._downloader.download(request, progress))
        monitor_stop = asyncio.Event()
        monitor = (
            asyncio.create_task(self._staging.monitor_unknown_size(context, stop=monitor_stop))
            if task.total_bytes is None
            else None
        )
        try:
            while True:
                waiting: set[asyncio.Task[DownloadSubprocessResult] | asyncio.Task[bool]] = {
                    download
                }
                if monitor is not None:
                    waiting.add(monitor)
                done, _ = await asyncio.wait(
                    waiting,
                    timeout=self._command_poll_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if monitor is not None and monitor in done:
                    if monitor.result():
                        download.cancel()
                        with suppress(asyncio.CancelledError):
                            await download
                        return DownloadWorkDisposition.PAUSED
                    monitor = None
                if download in done:
                    try:
                        download.result()
                    except DownloadProgressRejected:
                        commanded = await self._observed_command(context, task)
                        if commanded is not None:
                            return commanded
                        raise
                    except DownloadSubprocessError:
                        await self._record_download_failure(context, task)
                        return DownloadWorkDisposition.FAILED
                    final_bytes = (
                        task.total_bytes
                        if task.total_bytes is not None
                        else task.downloaded_bytes + transferred_bytes
                    )
                    await writer.record(final_bytes)
                    await self._finish_download(context, task, final_bytes=final_bytes)
                    return DownloadWorkDisposition.VERIFYING
                commanded = await self._observed_command(context, task)
                if commanded is not None:
                    download.cancel()
                    with suppress(asyncio.CancelledError):
                        await download
                    return commanded
        finally:
            monitor_stop.set()
            if monitor is not None and not monitor.done():
                monitor.cancel()
            if not download.done():
                download.cancel()
            await asyncio.gather(
                *(candidate for candidate in (monitor, download) if candidate is not None),
                return_exceptions=True,
            )

    def _download_endpoint(self, source: ModelOriginType) -> str:
        if source is ModelOriginType.HUGGING_FACE:
            endpoint = self._hugging_face_endpoint
        elif source is ModelOriginType.MODELSCOPE:
            endpoint = self._modelscope_endpoint
        else:
            # The process rejects unsupported sources before it touches this
            # value. Preserve that stable failure instead of inventing a
            # source-specific endpoint failure in the shared Worker path.
            endpoint = self._hugging_face_endpoint
        if endpoint is None:
            raise DownloadWorkerError("download_endpoint_unavailable")
        return endpoint

    async def _load_download_task(
        self,
        context: JobHandlerContext,
    ) -> DownloadResolutionWorkRecord:
        context.ensure_lease()
        owner = context.job.lease_owner
        if owner is None:
            raise LeaseLostError()
        async with self._session_factory() as session, session.begin():
            if not await JobRepository(session).lock_owned_lease(context.job.id, owner=owner):
                context.lease_lost.set()
                raise LeaseLostError()
            task = await DownloadTaskRepository(session).lock_resolution_work(
                context.job.target_object_id
            )
            if task is None or task.attempt != context.job.target_object_version:
                raise DownloadWorkerError("download_task_identity_mismatch")
            return task

    async def _finish_download(
        self,
        context: JobHandlerContext,
        task: DownloadResolutionWorkRecord,
        *,
        final_bytes: int,
    ) -> None:
        context.ensure_lease()
        owner = context.job.lease_owner
        if owner is None:
            raise LeaseLostError()
        async with self._session_factory() as session, session.begin():
            if not await JobRepository(session).lock_owned_lease(context.job.id, owner=owner):
                context.lease_lost.set()
                raise LeaseLostError()
            if not await DownloadTaskRepository(session).finish_download(
                task.id,
                expected_attempt=task.attempt,
                downloaded_bytes=final_bytes,
                now=await database_utc_now(session),
            ):
                raise DownloadWorkerError("download_task_state_mismatch")

    async def _record_download_failure(
        self,
        context: JobHandlerContext,
        task: DownloadResolutionWorkRecord,
    ) -> None:
        context.ensure_lease()
        owner = context.job.lease_owner
        if owner is None:
            raise LeaseLostError()
        async with self._session_factory() as session, session.begin():
            if not await JobRepository(session).lock_owned_lease(context.job.id, owner=owner):
                context.lease_lost.set()
                raise LeaseLostError()
            if not await DownloadTaskRepository(session).transition_staging_failure(
                task.id,
                expected_attempt=task.attempt,
                expected_status=DownloadTaskStatus.DOWNLOADING,
                status=DownloadTaskStatus.FAILED,
                reason=DownloadFailureReason.DOWNLOAD_PROCESS_FAILED,
                now=await database_utc_now(session),
            ):
                raise DownloadWorkerError("download_task_state_mismatch")

    async def cleanup(self, context: JobHandlerContext) -> None:
        if self._staging is None:
            raise DownloadWorkerError("download_staging_lifecycle_unavailable")
        await self._staging.cleanup(
            context,
            expected_status=DownloadTaskStatus.CANCELLED,
        )

    async def _resolve_until_commanded(
        self,
        context: JobHandlerContext,
        task: DownloadResolutionWorkRecord,
        request: ResolutionSubprocessRequest,
    ) -> ResolutionSubprocessResult | DownloadWorkDisposition:
        resolution = asyncio.create_task(self._resolver.resolve(request))
        try:
            while True:
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(resolution),
                        timeout=self._command_poll_seconds,
                    )
                except TimeoutError:
                    # ``wait_for`` and a resolver failure can both surface as
                    # TimeoutError. A completed resolver must propagate its
                    # own result instead of being mistaken for a poll tick.
                    if resolution.done():
                        return resolution.result()
                    commanded = await self._observed_command(context, task)
                    if commanded is None:
                        continue
                    resolution.cancel()
                    with suppress(asyncio.CancelledError):
                        await resolution
                    return commanded
        finally:
            if not resolution.done():
                resolution.cancel()
                with suppress(asyncio.CancelledError):
                    await resolution

    async def _observed_command(
        self,
        context: JobHandlerContext,
        expected: DownloadResolutionWorkRecord,
    ) -> DownloadWorkDisposition | None:
        context.ensure_lease()
        owner = context.job.lease_owner
        if owner is None:
            raise LeaseLostError()
        async with self._session_factory() as session, session.begin():
            if not await JobRepository(session).lock_owned_lease(context.job.id, owner=owner):
                context.lease_lost.set()
                raise LeaseLostError()
            current = await DownloadTaskRepository(session).lock_resolution_work(expected.id)
            if current is None or current.attempt != expected.attempt:
                raise DownloadWorkerError("download_job_identity_mismatch")
            return self._commanded_disposition(current)

    async def _prepare_resolution(self, context: JobHandlerContext) -> DownloadResolutionWorkRecord:
        context.ensure_lease()
        owner = context.job.lease_owner
        if owner is None:
            raise LeaseLostError()
        async with self._session_factory() as session, session.begin():
            if not await JobRepository(session).lock_owned_lease(context.job.id, owner=owner):
                context.lease_lost.set()
                raise LeaseLostError()
            repository = DownloadTaskRepository(session)
            task = await repository.lock_resolution_work(context.job.target_object_id)
            if task is None or task.attempt != context.job.target_object_version:
                raise DownloadWorkerError("download_job_identity_mismatch")
            if task.status is DownloadTaskStatus.QUEUED:
                now = await database_utc_now(session)
                if not await repository.start_resolution(
                    task.id,
                    expected_attempt=task.attempt,
                    now=now,
                ):
                    raise DownloadWorkerError("download_resolution_transition_rejected")
                await DomainEventRepository(session).append(
                    self._events.build_draft(
                        DomainEventType.DOWNLOAD_RESOLUTION_STARTED.value,
                        aggregate_id=task.id,
                        version=task.attempt,
                        candidates={"status": DownloadTaskStatus.RESOLVING.value},
                    )
                )
                return DownloadResolutionWorkRecord(
                    id=task.id,
                    model_id=task.model_id,
                    source_type=task.source_type,
                    repository_id=task.repository_id,
                    requested_revision_norm=task.requested_revision_norm,
                    status=DownloadTaskStatus.RESOLVING,
                    attempt=task.attempt,
                    error_code=None,
                )
            return task

    @staticmethod
    def _replayed_disposition(
        task: DownloadResolutionWorkRecord,
    ) -> DownloadWorkDisposition | None:
        if task.status is DownloadTaskStatus.RESOLVING:
            return None
        if task.status is DownloadTaskStatus.DOWNLOADING:
            return DownloadWorkDisposition.STARTED
        if task.status is DownloadTaskStatus.VERIFYING:
            return DownloadWorkDisposition.VERIFYING
        if task.status is DownloadTaskStatus.FAILED:
            return DownloadWorkDisposition.FAILED
        if task.status is DownloadTaskStatus.COMPLETED:
            return DownloadWorkDisposition.COMPLETED
        if (
            task.status is DownloadTaskStatus.CANCELLED
            and task.error_code == DownloadCancellationReason.DUPLICATE_RESOLVED_REVISION.value
        ):
            return DownloadWorkDisposition.MERGED
        if task.status is DownloadTaskStatus.CANCELLED:
            return DownloadWorkDisposition.CANCELLED
        commanded = DownloadResolutionJobHandler._commanded_disposition(task)
        if commanded is not None:
            return commanded
        raise DownloadWorkerError("download_task_state_rejected")

    @staticmethod
    def _commanded_disposition(
        task: DownloadResolutionWorkRecord,
    ) -> DownloadWorkDisposition | None:
        if task.status is DownloadTaskStatus.PAUSED:
            return DownloadWorkDisposition.PAUSED
        if task.status is DownloadTaskStatus.CANCELLED and task.error_code is None:
            return DownloadWorkDisposition.CANCELLED
        return None

    async def _merge_while_owned(
        self,
        context: JobHandlerContext,
        resolved: ResolvedModel,
    ) -> DownloadMergeResult | DownloadWorkDisposition:
        owner = context.job.lease_owner
        if owner is None:
            raise LeaseLostError()
        async with self._session_factory() as session, session.begin():
            if not await JobRepository(session).lock_owned_lease(context.job.id, owner=owner):
                context.lease_lost.set()
                raise LeaseLostError()
            context.ensure_lease()
            task = await DownloadTaskRepository(session).get(context.job.target_object_id)
            if task is None or task.attempt != context.job.target_object_version:
                raise DownloadWorkerError("download_job_identity_mismatch")
            if task.status is DownloadTaskStatus.PAUSED:
                return DownloadWorkDisposition.PAUSED
            if task.status is DownloadTaskStatus.CANCELLED:
                return DownloadWorkDisposition.CANCELLED
            return await self._merger.merge(context.job.target_object_id, resolved)

    @staticmethod
    def _merge_disposition(
        context: JobHandlerContext,
        result: DownloadMergeResult,
    ) -> DownloadWorkDisposition:
        if result.task_id != context.job.target_object_id:
            raise DownloadWorkerError("download_merge_identity_mismatch")
        if isinstance(result, DownloadStarted):
            return DownloadWorkDisposition.STARTED
        if isinstance(result, DownloadCompleted):
            return DownloadWorkDisposition.COMPLETED
        if isinstance(result, DownloadMerged):
            return DownloadWorkDisposition.MERGED
        assert_never(result)


class DownloadWorker:
    """Publish one heartbeat and execute one leased job per lane at a time."""

    __slots__ = (
        "_launch_verification_enabled",
        "_build_version",
        "_executor",
        "_verification_executor",
        "_heartbeat_seconds",
        "_instance_id",
        "_poll_seconds",
        "_session_factory",
        "_stop_event",
    )

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory,
        handler: DownloadResolutionJobHandler,
        owner: str,
        build_version: str,
        lease_seconds: float = 30.0,
        heartbeat_seconds: float = 10.0,
        poll_seconds: float = 0.25,
        artifact_deletion: ArtifactDeletionHandler | None = None,
        artifact_verification: ArtifactVerificationHandler | None = None,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("worker poll interval must be positive")
        registry = JobHandlerRegistry()
        verification_registry = JobHandlerRegistry()
        registry.register(DOWNLOAD_RESOLUTION_JOB_TYPE, handler)
        registry.register(EXTERNAL_REGISTRATION_JOB_TYPE, handler)
        if handler._staging is not None:  # noqa: SLF001 - one process-owned composition root
            registry.register(DOWNLOAD_CLEANUP_JOB_TYPE, handler.cleanup)
        if artifact_deletion is not None:
            if not isinstance(artifact_deletion, ArtifactDeletionHandler):
                raise TypeError("artifact deletion handler is invalid")
            registry.register(ARTIFACT_DELETE_JOB_TYPE, artifact_deletion.delete)
            registry.register(ARTIFACT_TRASH_JOB_TYPE, artifact_deletion.cleanup)
        if artifact_verification is not None:
            verification_registry.register(ARTIFACT_VERIFY_JOB_TYPE, artifact_verification)
            verification_registry.register(
                ARTIFACT_LIGHT_VERIFY_JOB_TYPE, artifact_verification.verify_light
            )
            verification_registry.register(
                LAUNCH_VERIFY_JOB_TYPE, artifact_verification.verify_launch
            )
        self._launch_verification_enabled = artifact_verification is not None
        self._executor = JobExecutor(
            session_factory,
            registry,
            owner=owner,
            lease_seconds=lease_seconds,
            heartbeat_seconds=heartbeat_seconds,
            # Deletion must stay serialized with Ready Artifact reuse by downloads.
            job_types=frozenset(
                {
                    DOWNLOAD_RESOLUTION_JOB_TYPE,
                    EXTERNAL_REGISTRATION_JOB_TYPE,
                    DOWNLOAD_CLEANUP_JOB_TYPE,
                    ARTIFACT_DELETE_JOB_TYPE,
                    ARTIFACT_TRASH_JOB_TYPE,
                }
            ),
        )
        self._verification_executor = JobExecutor(
            session_factory,
            verification_registry,
            owner=owner,
            lease_seconds=lease_seconds,
            heartbeat_seconds=heartbeat_seconds,
            job_types=frozenset(
                {ARTIFACT_VERIFY_JOB_TYPE, ARTIFACT_LIGHT_VERIFY_JOB_TYPE, LAUNCH_VERIFY_JOB_TYPE}
            ),
        )
        self._session_factory = session_factory
        self._instance_id = owner
        self._build_version = build_version
        self._heartbeat_seconds = heartbeat_seconds
        self._poll_seconds = poll_seconds
        self._stop_event: asyncio.Event | None = None

    async def run_once(self) -> JobRunResult | None:
        """Compatibility entry point: run each lane once, sequentially."""
        download = await self._executor.run_once()
        verification = await self._run_verification_once()
        return download if download is not None else verification

    async def _run_verification_once(self) -> JobRunResult | None:
        if self._launch_verification_enabled:
            async with self._session_factory() as session, session.begin():
                await LaunchVerificationRepository(session).dispatch_pending()
            return await self._verification_executor.run_once()
        return None

    async def write_heartbeat(self, status: ComponentStatus) -> None:
        attempt = 0
        while True:
            try:
                async with self._session_factory() as session, session.begin():
                    await ComponentHeartbeatRepository(session).write(
                        component=Component.DOWNLOAD_WORKER,
                        instance_id=self._instance_id,
                        version=self._build_version,
                        status=status,
                    )
                    if status is ComponentStatus.HEALTHY:
                        await DownloadTaskRepository(session).fail_tasks_with_failed_jobs(
                            now=await database_utc_now(session)
                        )
                return
            except _DATABASE_ERRORS:
                self._log_heartbeat_failure()
                stop = self._stop_event
                if stop is None:
                    raise
                if stop.is_set():
                    return
                delay = _DATABASE_RETRY_BACKOFF_SECONDS[
                    min(attempt, len(_DATABASE_RETRY_BACKOFF_SECONDS) - 1)
                ]
                attempt += 1
                if await self._wait_or_stop(stop, delay):
                    return

    async def run(self, stop: asyncio.Event) -> None:
        """Run until signalled, cancelling the active handler before returning."""

        self._stop_event = stop
        await self.write_heartbeat(ComponentStatus.HEALTHY)
        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(self._heartbeat_loop(stop))
                tasks.create_task(self._work_loop(stop))
                tasks.create_task(self._work_loop(stop, verification=True))
        finally:
            stop.set()
            await self.write_heartbeat(ComponentStatus.STOPPING)
            self._stop_event = None

    async def _heartbeat_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._heartbeat_seconds)
            except TimeoutError:
                await self.write_heartbeat(ComponentStatus.HEALTHY)

    async def _wait_or_stop(self, stop: asyncio.Event, delay: float) -> bool:
        if stop.is_set():
            return True
        sleeping = asyncio.create_task(asyncio.sleep(delay))
        stopping = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait({sleeping, stopping}, return_when=asyncio.FIRST_COMPLETED)
            return stopping in done and stopping.result()
        finally:
            for task in (sleeping, stopping):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sleeping, stopping, return_exceptions=True)

    @staticmethod
    def _log_heartbeat_failure() -> None:
        _LOGGER.warning(
            "component=%s error_code=%s",
            Component.DOWNLOAD_WORKER.value,
            "heartbeat_failed",
            extra={
                "component": Component.DOWNLOAD_WORKER.value,
                "error_code": "heartbeat_failed",
            },
        )

    async def _work_loop(self, stop: asyncio.Event, *, verification: bool = False) -> None:
        database_failure_count = 0
        while not stop.is_set():
            retry_delay = self._poll_seconds
            execution = asyncio.create_task(
                self._run_verification_once() if verification else self._executor.run_once()
            )
            stopping = asyncio.create_task(stop.wait())
            try:
                done, _ = await asyncio.wait(
                    {execution, stopping}, return_when=asyncio.FIRST_COMPLETED
                )
                if stopping in done and execution not in done:
                    execution.cancel()
                    with suppress(asyncio.CancelledError):
                        await execution
                    return
                stopping.cancel()
                with suppress(asyncio.CancelledError):
                    await stopping
                result = execution.result()
            except asyncio.CancelledError:
                execution.cancel()
                stopping.cancel()
                await asyncio.gather(execution, stopping, return_exceptions=True)
                raise
            except LeaseLostError:
                _LOGGER.info(
                    "download job lease lost",
                    extra={
                        "component": Component.DOWNLOAD_WORKER.value,
                        "error_code": "lease_lost",
                    },
                )
                result = None
            except _SQLALCHEMY_DATABASE_ERRORS:
                retry_delay = _DATABASE_RETRY_BACKOFF_SECONDS[
                    min(database_failure_count, len(_DATABASE_RETRY_BACKOFF_SECONDS) - 1)
                ]
                database_failure_count += 1
                _LOGGER.warning(
                    "component=%s error_code=%s",
                    Component.DOWNLOAD_WORKER.value,
                    "database_unavailable",
                    extra={
                        "component": Component.DOWNLOAD_WORKER.value,
                        "error_code": "database_unavailable",
                    },
                )
                result = None
            except Exception:  # noqa: BLE001 - raw details never cross the log boundary
                _LOGGER.warning(
                    "download job failed",
                    extra={
                        "component": Component.DOWNLOAD_WORKER.value,
                        "error_code": "job_failed",
                    },
                )
                result = None
            else:
                database_failure_count = 0
            if result is None:
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=retry_delay)


async def run_until_signalled(worker: DownloadWorker) -> None:
    """Translate SIGINT/SIGTERM into cooperative worker shutdown."""

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for selected in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(selected, stop.set)
        except (NotImplementedError, RuntimeError):
            continue
        installed.append(selected)
    try:
        await worker.run(stop)
    finally:
        for selected in installed:
            loop.remove_signal_handler(selected)


__all__ = [
    "DownloadResolutionJobHandler",
    "DownloadWorkDisposition",
    "DownloadWorker",
    "DownloadWorkerError",
    "ModelResolver",
    "ResolutionMerger",
    "run_until_signalled",
]

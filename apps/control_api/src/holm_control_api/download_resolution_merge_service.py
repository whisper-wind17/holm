"""Resolve-stage database transaction, independent of the download worker."""

from uuid import UUID

from holm_domain import DownloadTaskStatus, RevisionImmutability, to_persisted_column
from holm_domain.download_merge import (
    ContentsMatch,
    DownloadCompleted,
    DownloadMerged,
    DownloadMergeResult,
    DownloadStarted,
    compare_contents,
)
from holm_domain.model_acquisition import DownloadCancellationReason
from holm_domain.model_resolution import ResolvedModel
from holm_persistence import AsyncSessionFactory, database_utc_now
from holm_persistence.download_task_repository import (
    DownloadIdentityAcquired,
    DownloadTaskRepository,
)

MERGE_ATTEMPTS = 3


class DownloadMergeRejected(ValueError):
    """Invalid task identity/state; no source identifiers in exception text."""


class DownloadMergeContended(RuntimeError):
    def __init__(self) -> None:
        super().__init__("download resolution contention; retry later")


class DownloadResolutionMergeService:
    def __init__(self, *, session_factory: AsyncSessionFactory) -> None:
        self._session_factory = session_factory

    async def merge(self, task_id: UUID, resolved: ResolvedModel) -> DownloadMergeResult:
        async with self._session_factory() as session:
            repository = DownloadTaskRepository(session)
            task = await repository.lock_resolution_task(task_id)
            if task is None:
                raise DownloadMergeRejected("download task unavailable")
            if (
                task["source_type"] != resolved.source.value
                or task["repository_id"] != resolved.repository_id
                or task["requested_revision_norm"] != resolved.requested_revision_norm
                or task["resolved_revision"] not in (None, resolved.resolved_revision)
            ):
                raise DownloadMergeRejected("download resolution identity mismatch")
            status = DownloadTaskStatus(task["status"])
            # Replays project durable state, never re-query a former winner.
            if status is DownloadTaskStatus.COMPLETED and task["artifact_id"] is not None:
                return DownloadCompleted(task_id, task["artifact_id"])
            if (
                status is DownloadTaskStatus.CANCELLED
                and task["error_code"]
                == DownloadCancellationReason.DUPLICATE_RESOLVED_REVISION.value
            ):
                return DownloadMerged(task_id)
            if status is DownloadTaskStatus.DOWNLOADING:
                return DownloadStarted(task_id)
            if status is not DownloadTaskStatus.RESOLVING:
                raise DownloadMergeRejected("download task is not resolving")
            now = await database_utc_now(session)
            for _ in range(MERGE_ATTEMPTS):
                artifact_id = await repository.ready_artifact(resolved)
                if artifact_id is not None:
                    if resolved.revision_immutability is RevisionImmutability.IMMUTABLE:
                        reusable = True
                    else:
                        comparison = compare_contents(
                            resolved.files, await repository.artifact_file_manifest(artifact_id)
                        )
                        reusable = isinstance(comparison, ContentsMatch)
                    if reusable:
                        await repository.complete_resolution(
                            task_id, resolved.resolved_revision, artifact_id, now
                        )
                        await session.commit()
                        return DownloadCompleted(task_id, artifact_id)
                winner_id = await repository.active_resolution_winner(task_id, resolved)
                if winner_id is not None:
                    await repository.merge_subscriptions(
                        task_id, winner_id, resolved.resolved_revision, now
                    )
                    await session.commit()
                    return DownloadMerged(task_id)
                acquired = await repository.acquire_resolved_identity(
                    task_id, resolved.resolved_revision, now
                )
                if isinstance(acquired, DownloadIdentityAcquired):
                    if not await repository.set_download_total(
                        task_id,
                        expected_attempt=int(task["attempt"]),
                        total_bytes=to_persisted_column(resolved.total_size_bytes),
                        now=now,
                    ):
                        raise DownloadMergeRejected("download total could not be persisted")
                    await session.commit()
                    return DownloadStarted(task_id)
            raise DownloadMergeContended()

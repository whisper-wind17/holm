"""Pre-resolution download creation; uniqueness is decided by PostgreSQL."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import cast
from uuid import UUID

import sqlalchemy as sa
from holm_common import as_utc, new_uuid7
from holm_domain import (
    ARTIFACT_MANIFEST_FILENAME,
    ARTIFACT_MANIFEST_SCHEMA_VERSION,
    DOWNLOAD_RESOLUTION_JOB_TYPE,
    EXTERNAL_REGISTRATION_JOB_TYPE,
    ArtifactManifest,
    ArtifactManifestError,
    ArtifactStorageType,
    DownloadSubscriberType,
    DownloadTaskStatus,
    Identified,
    JobStatus,
    ModelArtifactStatus,
    ModelOriginType,
    RemoteFile,
    artifact_manifest_hash,
    deserialize_artifact_manifest,
)
from holm_domain.download_merge import ArtifactFileManifestUnavailable
from holm_domain.model_acquisition import (
    EXTERNAL_REVISION,
    DownloadCancellationReason,
    DownloadFailureReason,
)
from holm_domain.model_resolution import ResolvedModel
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from holm_persistence.jobs import JOBS
from holm_persistence.model_domain import (
    DOWNLOAD_SUBSCRIPTIONS,
    DOWNLOAD_TASKS,
    MODEL_ARTIFACTS,
)

DOWNLOAD_CREATION_ATTEMPTS = 3
PRE_RESOLUTION_STATUSES = (DownloadTaskStatus.QUEUED.value, DownloadTaskStatus.RESOLVING.value)
RESOLVED_ACTIVE_STATUSES = (
    DownloadTaskStatus.DOWNLOADING.value,
    DownloadTaskStatus.PAUSED.value,
    DownloadTaskStatus.VERIFYING.value,
    DownloadTaskStatus.PUBLISHING.value,
)


@dataclass(frozen=True, slots=True)
class DownloadCreationContended:
    code: str = "download_creation_contended"


@dataclass(frozen=True, slots=True)
class DownloadIdentityAcquired:
    pass


@dataclass(frozen=True, slots=True)
class DownloadIdentityConflict:
    pass


@dataclass(frozen=True, slots=True)
class DownloadTaskRecord:
    id: UUID
    model_id: UUID
    status: DownloadTaskStatus
    attempt: int


@dataclass(frozen=True, slots=True)
class DownloadTaskCreation:
    task: DownloadTaskRecord
    created: bool


@dataclass(frozen=True, slots=True)
class DownloadSubscriptionRecord:
    id: UUID
    task_id: UUID
    subscriber_id: UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DownloadResolutionWorkRecord:
    """The operational fields a leased resolution handler is allowed to read."""

    id: UUID
    model_id: UUID
    source_type: ModelOriginType
    repository_id: str
    requested_revision_norm: str
    status: DownloadTaskStatus
    attempt: int
    error_code: str | None
    resolved_revision: str | None = None
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    staging_path: str | None = None


@dataclass(frozen=True, slots=True)
class DownloadCommandTaskRecord:
    """Fields required to decide and persist one user command."""

    id: UUID
    model_id: UUID
    source_type: ModelOriginType
    status: DownloadTaskStatus
    attempt: int
    resolved_revision: str | None
    staging_path: str | None


class DownloadCommandIdentityConflict(RuntimeError):
    """A command could not re-enter the pre-resolution identity slot."""


@dataclass(frozen=True, slots=True, repr=False)
class ArtifactInspectionRecord:
    id: UUID
    path: str
    manifest_hash: str
    storage_type: ArtifactStorageType
    status: ModelArtifactStatus
    manifest: ArtifactManifest | None = None

    def __repr__(self) -> str:
        return (
            "ArtifactInspectionRecord("
            f"id={self.id!r}, storage_type={self.storage_type!r}, "
            f"status={self.status!r}, path=<redacted>)"
        )


def _task(row: sa.RowMapping) -> DownloadTaskRecord:
    return DownloadTaskRecord(
        id=row["id"],
        model_id=row["model_id"],
        status=DownloadTaskStatus(row["status"]),
        attempt=int(row["attempt"]),
    )


def _resolution_work(row: sa.RowMapping) -> DownloadResolutionWorkRecord:
    return DownloadResolutionWorkRecord(
        id=row["id"],
        model_id=row["model_id"],
        source_type=ModelOriginType(row["source_type"]),
        repository_id=str(row["repository_id"]),
        requested_revision_norm=str(row["requested_revision_norm"]),
        status=DownloadTaskStatus(row["status"]),
        attempt=int(row["attempt"]),
        error_code=None if row["error_code"] is None else str(row["error_code"]),
        resolved_revision=(
            None if row["resolved_revision"] is None else str(row["resolved_revision"])
        ),
        downloaded_bytes=int(row["downloaded_bytes"]),
        total_bytes=None if row["total_bytes"] is None else int(row["total_bytes"]),
        staging_path=None if row["staging_path"] is None else str(row["staging_path"]),
    )


def _command_task(row: sa.RowMapping) -> DownloadCommandTaskRecord:
    return DownloadCommandTaskRecord(
        id=row["id"],
        model_id=row["model_id"],
        source_type=ModelOriginType(row["source_type"]),
        status=DownloadTaskStatus(row["status"]),
        attempt=int(row["attempt"]),
        resolved_revision=(
            None if row["resolved_revision"] is None else str(row["resolved_revision"])
        ),
        staging_path=None if row["staging_path"] is None else str(row["staging_path"]),
    )


def _unique(error: IntegrityError, constraint: str) -> bool:
    return (
        getattr(error.orig, "sqlstate", None) == "23505"
        and getattr(getattr(error.orig, "diag", None), "constraint_name", None) == constraint
    )


class DownloadTaskRepository:
    """Insert first, isolate conflicts with savepoints, then read the winner."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, task_id: UUID) -> DownloadTaskRecord | None:
        row = (
            (
                await self._session.execute(
                    sa.select(DOWNLOAD_TASKS).where(DOWNLOAD_TASKS.c.id == task_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _task(row)

    async def create_or_join_external(
        self, *, model_id: UUID, path: str, now: datetime
    ) -> DownloadTaskCreation:
        """The caller's Model upsert serializes registrations of one canonical path."""
        identity = (
            DOWNLOAD_TASKS.c.source_type == ModelOriginType.LOCAL_PATH.value,
            DOWNLOAD_TASKS.c.repository_id == path,
        )
        active = (
            (
                await self._session.execute(
                    sa.select(DOWNLOAD_TASKS).where(
                        *identity,
                        DOWNLOAD_TASKS.c.status.in_(RESOLVED_ACTIVE_STATUSES),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if active is not None:
            return DownloadTaskCreation(_task(active), created=False)
        artifact_id = (
            await self._session.execute(
                sa.select(MODEL_ARTIFACTS.c.id).where(
                    MODEL_ARTIFACTS.c.source_type == ModelOriginType.LOCAL_PATH.value,
                    MODEL_ARTIFACTS.c.repository_id == path,
                    MODEL_ARTIFACTS.c.status == ModelArtifactStatus.READY.value,
                )
            )
        ).scalar_one_or_none()
        if artifact_id is not None:
            completed = (
                (
                    await self._session.execute(
                        sa.select(DOWNLOAD_TASKS)
                        .where(
                            *identity,
                            DOWNLOAD_TASKS.c.artifact_id == artifact_id,
                        )
                        .order_by(DOWNLOAD_TASKS.c.created_at.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if completed is not None:
                return DownloadTaskCreation(_task(completed), created=False)
        row = (
            (
                await self._session.execute(
                    sa.insert(DOWNLOAD_TASKS)
                    .values(
                        id=new_uuid7(),
                        model_id=model_id,
                        source_type=ModelOriginType.LOCAL_PATH.value,
                        repository_id=path,
                        requested_revision_norm=EXTERNAL_REVISION,
                        resolved_revision=EXTERNAL_REVISION,
                        artifact_id=artifact_id,
                        status=(
                            DownloadTaskStatus.VERIFYING
                            if artifact_id is None
                            else DownloadTaskStatus.COMPLETED
                        ).value,
                        attempt=1,
                        downloaded_bytes=0,
                        created_at=as_utc(now),
                        updated_at=as_utc(now),
                        completed_at=as_utc(now) if artifact_id is not None else None,
                    )
                    .returning(*DOWNLOAD_TASKS.c)
                )
            )
            .mappings()
            .one()
        )
        return DownloadTaskCreation(_task(row), created=artifact_id is None)

    async def create_or_join(
        self,
        *,
        model_id: UUID,
        source_type: ModelOriginType,
        repository_id: str,
        requested_revision_norm: str,
        now: datetime,
    ) -> DownloadTaskCreation | DownloadCreationContended:
        statement = (
            sa.insert(DOWNLOAD_TASKS)
            .values(
                id=new_uuid7(),
                model_id=model_id,
                source_type=source_type.value,
                repository_id=repository_id,
                requested_revision_norm=requested_revision_norm,
                resolved_revision=None,
                status=DownloadTaskStatus.QUEUED.value,
                attempt=1,
                downloaded_bytes=0,
                created_at=as_utc(now),
                updated_at=as_utc(now),
            )
            .returning(*DOWNLOAD_TASKS.c)
        )
        for _ in range(DOWNLOAD_CREATION_ATTEMPTS):
            conflicting = False
            row: sa.RowMapping | None
            try:
                async with self._session.begin_nested():
                    row = (await self._session.execute(statement)).mappings().one()
                return DownloadTaskCreation(_task(row), created=True)
            except IntegrityError as error:
                conflicting = _unique(error, "uq_pre_resolution_download_request")
            if not conflicting:
                # No SQL parameters survive in an exception chain.
                raise RuntimeError("download task creation failed")
            row = await self._pre_resolution_winner(
                source_type, repository_id, requested_revision_norm
            )
            if row is not None:
                return DownloadTaskCreation(_task(row), created=False)
        return DownloadCreationContended()

    async def _pre_resolution_winner(
        self, source_type: ModelOriginType, repository_id: str, requested_revision_norm: str
    ) -> sa.RowMapping | None:
        row = (
            (
                await self._session.execute(
                    sa.select(DOWNLOAD_TASKS)
                    .where(
                        DOWNLOAD_TASKS.c.source_type == source_type.value,
                        DOWNLOAD_TASKS.c.repository_id == repository_id,
                        DOWNLOAD_TASKS.c.requested_revision_norm == requested_revision_norm,
                        DOWNLOAD_TASKS.c.status.in_(PRE_RESOLUTION_STATUSES),
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        # PostgreSQL READ COMMITTED rechecks predicates after a lock wait.
        # Also check explicitly so callers cannot join a departed winner.
        if row is None or row["status"] not in PRE_RESOLUTION_STATUSES:
            return None
        return row

    async def subscribe(
        self,
        *,
        task_id: UUID,
        actor_id: UUID,
        now: datetime,
    ) -> DownloadSubscriptionRecord:
        """A standalone subscription belongs to the requesting administrator."""
        statement = (
            sa.insert(DOWNLOAD_SUBSCRIPTIONS)
            .values(
                id=new_uuid7(),
                task_id=task_id,
                subscriber_type=DownloadSubscriberType.STANDALONE.value,
                subscriber_id=actor_id,
                actor_id=actor_id,
                created_at=as_utc(now),
            )
            .returning(*DOWNLOAD_SUBSCRIPTIONS.c)
        )
        conflicting = False
        row: sa.RowMapping | None = None
        try:
            async with self._session.begin_nested():
                row = (await self._session.execute(statement)).mappings().one()
        except IntegrityError as error:
            conflicting = _unique(error, "uq_download_subscription_identity")
        if row is None:
            if not conflicting:
                raise RuntimeError("download subscription creation failed")
            row = (
                (
                    await self._session.execute(
                        sa.select(DOWNLOAD_SUBSCRIPTIONS).where(
                            DOWNLOAD_SUBSCRIPTIONS.c.task_id == task_id,
                            DOWNLOAD_SUBSCRIPTIONS.c.subscriber_type
                            == DownloadSubscriberType.STANDALONE.value,
                            DOWNLOAD_SUBSCRIPTIONS.c.subscriber_id == actor_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise RuntimeError("download subscription conflict winner unavailable")
        return DownloadSubscriptionRecord(
            id=row["id"],
            task_id=row["task_id"],
            subscriber_id=row["subscriber_id"],
            created_at=as_utc(row["created_at"]),
        )

    async def lock_resolution_task(self, task_id: UUID) -> sa.RowMapping | None:
        return (
            (
                await self._session.execute(
                    sa.select(DOWNLOAD_TASKS)
                    .where(DOWNLOAD_TASKS.c.id == task_id)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )

    async def lock_command_task(self, task_id: UUID) -> DownloadCommandTaskRecord | None:
        """Serialize command decisions on the Download Task row."""

        row = await self.lock_resolution_task(task_id)
        return None if row is None else _command_task(row)

    async def apply_command_transition(
        self,
        task_id: UUID,
        *,
        expected_status: DownloadTaskStatus,
        status: DownloadTaskStatus,
        attempt: int,
        reset_progress: bool,
        now: datetime,
    ) -> DownloadCommandTaskRecord:
        """Apply a state-machine decision made while the row lock is held."""

        values: dict[str, object] = {
            "status": status.value,
            "attempt": attempt,
            "error_code": None,
            "updated_at": as_utc(now),
            "completed_at": None,
        }
        if reset_progress:
            values.update(
                {
                    "resolved_revision": None,
                    "downloaded_bytes": 0,
                    "total_bytes": None,
                }
            )
        statement = (
            sa.update(DOWNLOAD_TASKS)
            .where(
                DOWNLOAD_TASKS.c.id == task_id,
                DOWNLOAD_TASKS.c.status == expected_status.value,
            )
            .values(**values)
            .returning(*DOWNLOAD_TASKS.c)
        )
        conflict = False
        row: sa.RowMapping | None = None
        try:
            async with self._session.begin_nested():
                row = (await self._session.execute(statement)).mappings().one_or_none()
        except IntegrityError as error:
            conflict = _unique(error, "uq_pre_resolution_download_request")
        if conflict:
            raise DownloadCommandIdentityConflict()
        if row is None:
            raise RuntimeError("download command transition failed")
        return _command_task(row)

    async def lock_resolution_work(self, task_id: UUID) -> DownloadResolutionWorkRecord | None:
        """Lock and return only fields required by the Download Worker."""

        row = await self.lock_resolution_task(task_id)
        return None if row is None else _resolution_work(row)

    async def start_resolution(
        self,
        task_id: UUID,
        *,
        expected_attempt: int,
        now: datetime,
    ) -> bool:
        """Move the exact queued attempt to Resolving inside the caller transaction."""

        statement = (
            sa.update(DOWNLOAD_TASKS)
            .where(
                DOWNLOAD_TASKS.c.id == task_id,
                DOWNLOAD_TASKS.c.status == DownloadTaskStatus.QUEUED.value,
                DOWNLOAD_TASKS.c.attempt == expected_attempt,
            )
            .values(
                status=DownloadTaskStatus.RESOLVING.value,
                error_code=None,
                updated_at=as_utc(now),
            )
            .returning(DOWNLOAD_TASKS.c.id)
        )
        return (await self._session.execute(statement)).scalar_one_or_none() is not None

    async def set_download_total(
        self,
        task_id: UUID,
        *,
        expected_attempt: int,
        total_bytes: int | None,
        now: datetime,
    ) -> bool:
        """Persist source knowledge without collapsing an unknown total to zero."""

        statement = (
            sa.update(DOWNLOAD_TASKS)
            .where(
                DOWNLOAD_TASKS.c.id == task_id,
                DOWNLOAD_TASKS.c.status == DownloadTaskStatus.DOWNLOADING.value,
                DOWNLOAD_TASKS.c.attempt == expected_attempt,
            )
            .values(total_bytes=total_bytes, updated_at=as_utc(now))
            .returning(DOWNLOAD_TASKS.c.id)
        )
        return (await self._session.execute(statement)).scalar_one_or_none() is not None

    async def save_staging_path(
        self,
        task_id: UUID,
        *,
        expected_attempt: int,
        staging_path: str,
        now: datetime,
    ) -> bool:
        """Set or confirm the platform-derived staging path for one download."""

        statement = (
            sa.update(DOWNLOAD_TASKS)
            .where(
                DOWNLOAD_TASKS.c.id == task_id,
                DOWNLOAD_TASKS.c.status == DownloadTaskStatus.DOWNLOADING.value,
                DOWNLOAD_TASKS.c.attempt == expected_attempt,
                sa.or_(
                    DOWNLOAD_TASKS.c.staging_path.is_(None),
                    DOWNLOAD_TASKS.c.staging_path == staging_path,
                ),
            )
            .values(staging_path=staging_path, updated_at=as_utc(now))
            .returning(DOWNLOAD_TASKS.c.id)
        )
        return (await self._session.execute(statement)).scalar_one_or_none() is not None

    async def update_download_progress(
        self,
        task_id: UUID,
        *,
        expected_attempt: int,
        downloaded_bytes: int,
        now: datetime,
    ) -> bool:
        """Write one throttled progress sample selected by the Worker."""

        statement = (
            sa.update(DOWNLOAD_TASKS)
            .where(
                DOWNLOAD_TASKS.c.id == task_id,
                DOWNLOAD_TASKS.c.status == DownloadTaskStatus.DOWNLOADING.value,
                DOWNLOAD_TASKS.c.attempt == expected_attempt,
                DOWNLOAD_TASKS.c.downloaded_bytes <= downloaded_bytes,
            )
            .values(
                downloaded_bytes=downloaded_bytes,
                updated_at=as_utc(now),
            )
            .returning(DOWNLOAD_TASKS.c.id)
        )
        return (await self._session.execute(statement)).scalar_one_or_none() is not None

    async def finish_download(
        self,
        task_id: UUID,
        *,
        expected_attempt: int,
        downloaded_bytes: int,
        now: datetime,
    ) -> bool:
        """Move one fully transferred attempt to the verification handoff."""

        statement = (
            sa.update(DOWNLOAD_TASKS)
            .where(
                DOWNLOAD_TASKS.c.id == task_id,
                DOWNLOAD_TASKS.c.status == DownloadTaskStatus.DOWNLOADING.value,
                DOWNLOAD_TASKS.c.attempt == expected_attempt,
                DOWNLOAD_TASKS.c.resolved_revision.is_not(None),
                DOWNLOAD_TASKS.c.staging_path.is_not(None),
            )
            .values(
                status=DownloadTaskStatus.VERIFYING.value,
                downloaded_bytes=downloaded_bytes,
                error_code=None,
                updated_at=as_utc(now),
            )
            .returning(DOWNLOAD_TASKS.c.id)
        )
        return (await self._session.execute(statement)).scalar_one_or_none() is not None

    async def fail_tasks_with_failed_jobs(self, now: datetime) -> int:
        """Reconcile only the newest resolution job for the task's current attempt."""
        latest_status = (
            sa.select(JOBS.c.status)
            .where(
                JOBS.c.job_type.in_((DOWNLOAD_RESOLUTION_JOB_TYPE, EXTERNAL_REGISTRATION_JOB_TYPE)),
                JOBS.c.target_object_id == DOWNLOAD_TASKS.c.id,
                JOBS.c.target_object_version == DOWNLOAD_TASKS.c.attempt,
            )
            .order_by(JOBS.c.created_at.desc(), JOBS.c.id.desc())
            .limit(1)
            .correlate(DOWNLOAD_TASKS)
            .scalar_subquery()
        )
        statement = (
            sa.update(DOWNLOAD_TASKS)
            .where(
                DOWNLOAD_TASKS.c.status.in_(
                    (
                        DownloadTaskStatus.RESOLVING.value,
                        DownloadTaskStatus.DOWNLOADING.value,
                        DownloadTaskStatus.VERIFYING.value,
                    )
                ),
                latest_status == JobStatus.FAILED.value,
            )
            .values(
                status=DownloadTaskStatus.FAILED.value,
                error_code=DownloadFailureReason.DOWNLOAD_PROCESS_FAILED.value,
                updated_at=as_utc(now),
            )
            .returning(DOWNLOAD_TASKS.c.id)
        )
        return len((await self._session.execute(statement)).all())

    async def transition_staging_failure(
        self,
        task_id: UUID,
        *,
        expected_attempt: int,
        expected_status: DownloadTaskStatus,
        status: DownloadTaskStatus,
        reason: DownloadFailureReason,
        now: datetime,
    ) -> bool:
        """Persist a classified lifecycle failure without accepting raw codes."""

        statement = (
            sa.update(DOWNLOAD_TASKS)
            .where(
                DOWNLOAD_TASKS.c.id == task_id,
                DOWNLOAD_TASKS.c.status == expected_status.value,
                DOWNLOAD_TASKS.c.attempt == expected_attempt,
            )
            .values(status=status.value, error_code=reason.value, updated_at=as_utc(now))
            .returning(DOWNLOAD_TASKS.c.id)
        )
        return (await self._session.execute(statement)).scalar_one_or_none() is not None

    async def pause_for_storage(
        self,
        task_id: UUID,
        *,
        expected_attempt: int,
        now: datetime,
    ) -> bool:
        return await self.transition_staging_failure(
            task_id,
            expected_attempt=expected_attempt,
            expected_status=DownloadTaskStatus.DOWNLOADING,
            status=DownloadTaskStatus.PAUSED,
            reason=DownloadFailureReason.INSUFFICIENT_STORAGE,
            now=now,
        )

    async def clear_staging_path(
        self,
        task_id: UUID,
        *,
        expected_attempt: int,
        expected_status: DownloadTaskStatus,
        now: datetime,
    ) -> bool:
        """Forget a staging path only after the caller has deleted it."""

        statement = (
            sa.update(DOWNLOAD_TASKS)
            .where(
                DOWNLOAD_TASKS.c.id == task_id,
                DOWNLOAD_TASKS.c.status == expected_status.value,
                DOWNLOAD_TASKS.c.attempt == expected_attempt,
            )
            .values(
                staging_path=None,
                error_code=sa.case(
                    (
                        DOWNLOAD_TASKS.c.error_code
                        == DownloadFailureReason.STAGING_CLEANUP_FAILED.value,
                        None,
                    ),
                    else_=DOWNLOAD_TASKS.c.error_code,
                ),
                updated_at=as_utc(now),
            )
            .returning(DOWNLOAD_TASKS.c.id)
        )
        return (await self._session.execute(statement)).scalar_one_or_none() is not None

    async def ready_artifact(self, resolved: ResolvedModel) -> UUID | None:
        return (
            await self._session.execute(
                sa.select(MODEL_ARTIFACTS.c.id)
                .where(
                    MODEL_ARTIFACTS.c.source_type == resolved.source.value,
                    MODEL_ARTIFACTS.c.repository_id == resolved.repository_id,
                    MODEL_ARTIFACTS.c.resolved_revision == resolved.resolved_revision,
                    MODEL_ARTIFACTS.c.status == ModelArtifactStatus.READY.value,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

    async def artifact_file_manifest(
        self, artifact_id: UUID
    ) -> Identified[tuple[RemoteFile, ...]] | ArtifactFileManifestUnavailable:
        """Read per-file evidence only when canonical bytes match the database Hash."""

        row = (
            (
                await self._session.execute(
                    sa.select(MODEL_ARTIFACTS.c.path, MODEL_ARTIFACTS.c.manifest_hash).where(
                        MODEL_ARTIFACTS.c.id == artifact_id,
                        MODEL_ARTIFACTS.c.status == ModelArtifactStatus.READY.value,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return ArtifactFileManifestUnavailable()

        def read_manifest() -> Identified[tuple[RemoteFile, ...]] | None:
            try:
                raw = (Path(str(row["path"])) / ARTIFACT_MANIFEST_FILENAME).read_bytes()
                if artifact_manifest_hash(raw) != str(row["manifest_hash"]):
                    return None
                return deserialize_artifact_manifest(raw).as_remote_files()
            except (OSError, ArtifactManifestError):
                return None

        manifest = await asyncio.to_thread(read_manifest)
        return ArtifactFileManifestUnavailable() if manifest is None else manifest

    async def transition_to_publishing(
        self,
        task_id: UUID,
        *,
        expected_attempt: int,
        now: datetime,
    ) -> bool:
        """Enter Publishing before the first publication filesystem effect."""

        statement = (
            sa.update(DOWNLOAD_TASKS)
            .where(
                DOWNLOAD_TASKS.c.id == task_id,
                DOWNLOAD_TASKS.c.attempt == expected_attempt,
                DOWNLOAD_TASKS.c.status == DownloadTaskStatus.VERIFYING.value,
                DOWNLOAD_TASKS.c.resolved_revision.is_not(None),
                sa.or_(
                    DOWNLOAD_TASKS.c.staging_path.is_not(None),
                    DOWNLOAD_TASKS.c.source_type == ModelOriginType.LOCAL_PATH.value,
                ),
            )
            .values(
                status=DownloadTaskStatus.PUBLISHING.value,
                error_code=None,
                updated_at=as_utc(now),
            )
            .returning(DOWNLOAD_TASKS.c.id)
        )
        return (await self._session.execute(statement)).scalar_one_or_none() is not None

    async def delete_task(self, task_id: UUID) -> None:
        """Caller holds the task lock; published Artifacts are independent assets."""
        await self._session.execute(
            sa.delete(DOWNLOAD_SUBSCRIPTIONS).where(DOWNLOAD_SUBSCRIPTIONS.c.task_id == task_id)
        )
        await self._session.execute(sa.delete(DOWNLOAD_TASKS).where(DOWNLOAD_TASKS.c.id == task_id))

    async def complete_publication(
        self,
        task: DownloadResolutionWorkRecord,
        *,
        artifact_id: UUID,
        path: str,
        manifest_hash: str,
        manifest: ArtifactManifest,
        tokenizer_bundle_hash: str | None,
        tokenizer_bundle_path: str | None,
        attention_heads: int | None,
        kv_heads: int | None,
        size_bytes: int,
        now: datetime,
    ) -> bool:
        """Create Ready Artifact and complete its task in the caller's transaction."""

        if task.resolved_revision is None:
            raise ValueError("publication requires a resolved Revision")
        await self._session.execute(
            sa.insert(MODEL_ARTIFACTS).values(
                id=artifact_id,
                model_id=task.model_id,
                source_type=task.source_type.value,
                repository_id=task.repository_id,
                resolved_revision=task.resolved_revision,
                storage_type=(
                    ArtifactStorageType.EXTERNAL
                    if task.source_type is ModelOriginType.LOCAL_PATH
                    else ArtifactStorageType.MANAGED
                ).value,
                status=ModelArtifactStatus.READY.value,
                path=path,
                manifest_hash=manifest_hash,
                manifest_files=[asdict(entry) for entry in manifest.files],
                tokenizer_bundle_hash=tokenizer_bundle_hash,
                tokenizer_bundle_path=tokenizer_bundle_path,
                attention_heads=attention_heads,
                kv_heads=kv_heads,
                size_bytes=size_bytes,
                created_at=as_utc(now),
                updated_at=as_utc(now),
            )
        )
        completed_status = DownloadTaskStatus.COMPLETED.value
        statement = (
            sa.update(DOWNLOAD_TASKS)
            .where(
                DOWNLOAD_TASKS.c.id == task.id,
                DOWNLOAD_TASKS.c.attempt == task.attempt,
                DOWNLOAD_TASKS.c.status == DownloadTaskStatus.PUBLISHING.value,
                DOWNLOAD_TASKS.c.artifact_id.is_(None),
            )
            .values(
                artifact_id=artifact_id,
                status=completed_status,
                staging_path=None,
                error_code=None,
                completed_at=as_utc(now),
                updated_at=as_utc(now),
            )
            .returning(DOWNLOAD_TASKS.c.id)
        )
        return (await self._session.execute(statement)).scalar_one_or_none() is not None

    async def artifact_for_inspection(self, artifact_id: UUID) -> ArtifactInspectionRecord | None:
        row = (
            (
                await self._session.execute(
                    sa.select(
                        MODEL_ARTIFACTS.c.id,
                        MODEL_ARTIFACTS.c.path,
                        MODEL_ARTIFACTS.c.manifest_hash,
                        MODEL_ARTIFACTS.c.manifest_files,
                        MODEL_ARTIFACTS.c.storage_type,
                        MODEL_ARTIFACTS.c.status,
                    ).where(MODEL_ARTIFACTS.c.id == artifact_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return ArtifactInspectionRecord(
            id=row["id"],
            path=str(row["path"]),
            manifest_hash=str(row["manifest_hash"]),
            storage_type=ArtifactStorageType(row["storage_type"]),
            status=ModelArtifactStatus(row["status"]),
            manifest=(
                deserialize_artifact_manifest(
                    json.dumps(
                        {
                            "schema_version": ARTIFACT_MANIFEST_SCHEMA_VERSION,
                            "files": row["manifest_files"],
                        },
                        ensure_ascii=True,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode()
                    + b"\n"
                )
                if row["storage_type"] == ArtifactStorageType.EXTERNAL.value
                and row["manifest_files"] is not None
                else None
            ),
        )

    async def apply_artifact_health(
        self,
        artifact_id: UUID,
        *,
        expected_manifest_hash: str,
        status: ModelArtifactStatus,
        now: datetime,
        light_check: bool = False,
        manifest: ArtifactManifest | None = None,
    ) -> bool:
        """Persist judgement; a light check cannot restore a known unhealthy Artifact."""

        statement = (
            sa.update(MODEL_ARTIFACTS)
            .where(
                MODEL_ARTIFACTS.c.id == artifact_id,
                MODEL_ARTIFACTS.c.manifest_hash == expected_manifest_hash,
            )
            .values(status=status.value, updated_at=as_utc(now))
            .returning(MODEL_ARTIFACTS.c.id)
        )
        if manifest is not None:
            statement = statement.values(
                manifest_files=sa.func.coalesce(
                    MODEL_ARTIFACTS.c.manifest_files,
                    sa.literal(
                        [asdict(entry) for entry in manifest.files],
                        type_=MODEL_ARTIFACTS.c.manifest_files.type,
                    ),
                )
            )
        if light_check:
            statement = statement.where(MODEL_ARTIFACTS.c.status == "Ready").values(
                light_checked_at=as_utc(now)
            )
        return (await self._session.execute(statement)).scalar_one_or_none() is not None

    async def active_resolution_winner(self, task_id: UUID, resolved: ResolvedModel) -> UUID | None:
        row = (
            (
                await self._session.execute(
                    sa.select(DOWNLOAD_TASKS.c.id, DOWNLOAD_TASKS.c.status)
                    .where(
                        DOWNLOAD_TASKS.c.id != task_id,
                        DOWNLOAD_TASKS.c.source_type == resolved.source.value,
                        DOWNLOAD_TASKS.c.repository_id == resolved.repository_id,
                        DOWNLOAD_TASKS.c.resolved_revision == resolved.resolved_revision,
                        DOWNLOAD_TASKS.c.status.in_(RESOLVED_ACTIVE_STATUSES),
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None or row["status"] not in RESOLVED_ACTIVE_STATUSES:
            return None
        return cast(UUID, row["id"])

    async def complete_resolution(
        self, task_id: UUID, resolved_revision: str, artifact_id: UUID, now: datetime
    ) -> None:
        await self._session.execute(
            sa.update(DOWNLOAD_TASKS)
            .where(DOWNLOAD_TASKS.c.id == task_id)
            .values(
                status=DownloadTaskStatus.COMPLETED.value,
                resolved_revision=resolved_revision,
                artifact_id=artifact_id,
                error_code=None,
                completed_at=as_utc(now),
                updated_at=as_utc(now),
            )
        )

    async def merge_subscriptions(
        self, task_id: UUID, winner_id: UUID, resolved_revision: str, now: datetime
    ) -> None:
        # DELETE RETURNING feeds INSERT in one statement: no unbounded Python
        # buffer, and duplicate subscribers retain the winner's original row.
        moved = (
            sa.delete(DOWNLOAD_SUBSCRIPTIONS)
            .where(DOWNLOAD_SUBSCRIPTIONS.c.task_id == task_id)
            .returning(*DOWNLOAD_SUBSCRIPTIONS.c)
            .cte("moved_subscriptions")
        )
        columns = ("id", "task_id", "subscriber_type", "subscriber_id", "actor_id", "created_at")
        await self._session.execute(
            pg_insert(DOWNLOAD_SUBSCRIPTIONS)
            .from_select(
                columns,
                sa.select(
                    moved.c.id,
                    sa.literal(winner_id),
                    moved.c.subscriber_type,
                    moved.c.subscriber_id,
                    moved.c.actor_id,
                    moved.c.created_at,
                ),
            )
            .on_conflict_do_nothing(constraint="uq_download_subscription_identity")
        )
        await self._session.execute(
            sa.update(DOWNLOAD_TASKS)
            .where(DOWNLOAD_TASKS.c.id == task_id)
            .values(
                status=DownloadTaskStatus.CANCELLED.value,
                resolved_revision=resolved_revision,
                error_code=DownloadCancellationReason.DUPLICATE_RESOLVED_REVISION.value,
                updated_at=as_utc(now),
            )
        )

    async def acquire_resolved_identity(
        self, task_id: UUID, resolved_revision: str, now: datetime
    ) -> DownloadIdentityAcquired | DownloadIdentityConflict:
        conflict = False
        try:
            async with self._session.begin_nested():
                await self._session.execute(
                    sa.update(DOWNLOAD_TASKS)
                    .where(DOWNLOAD_TASKS.c.id == task_id)
                    .values(
                        status=DownloadTaskStatus.DOWNLOADING.value,
                        resolved_revision=resolved_revision,
                        error_code=None,
                        updated_at=as_utc(now),
                    )
                )
            return DownloadIdentityAcquired()
        except IntegrityError as error:
            conflict = _unique(error, "uq_active_download_identity")
        if conflict:
            return DownloadIdentityConflict()
        raise RuntimeError("download resolution update failed")

"""Transactional Artifact deletion guards and retained Download Task detachment."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import sqlalchemy as sa
from holm_domain import ArtifactStorageType, ModelArtifactStatus
from sqlalchemy.ext.asyncio import AsyncSession

from holm_persistence.artifact_lock import lock_artifact
from holm_persistence.job_repository import JobRecord, JobRepository
from holm_persistence.jobs import JOBS
from holm_persistence.model_domain import DOWNLOAD_TASKS, MODEL_ARTIFACTS
from holm_persistence.model_instances import MODEL_INSTANCES

ARTIFACT_DELETE_JOB_TYPE = "artifact.delete"
ARTIFACT_TRASH_JOB_TYPE = "artifact.trash_cleanup"


@dataclass(frozen=True, slots=True)
class ArtifactDeletionRecord:
    id: UUID
    storage_type: ArtifactStorageType
    status: ModelArtifactStatus
    path: str
    tokenizer_bundle_path: str | None


class ArtifactDeletionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock(self, artifact_id: UUID) -> ArtifactDeletionRecord | None:
        await lock_artifact(self._session, artifact_id)
        row = (
            (
                await self._session.execute(
                    sa.select(MODEL_ARTIFACTS).where(MODEL_ARTIFACTS.c.id == artifact_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return ArtifactDeletionRecord(
            id=row["id"],
            storage_type=ArtifactStorageType(row["storage_type"]),
            status=ModelArtifactStatus(row["status"]),
            path=str(row["path"]),
            tokenizer_bundle_path=row["tokenizer_bundle_path"],
        )

    async def referenced(self, artifact_id: UUID) -> bool:
        return (
            await self._session.execute(
                sa.select(
                    sa.exists(
                        sa.select(MODEL_INSTANCES.c.id).where(
                            MODEL_INSTANCES.c.artifact_id == artifact_id
                        )
                    )
                )
            )
        ).scalar_one()

    async def count_references(self, artifact_id: UUID) -> int:
        return int(
            (
                await self._session.execute(
                    sa.select(sa.func.count())
                    .select_from(MODEL_INSTANCES)
                    .where(MODEL_INSTANCES.c.artifact_id == artifact_id)
                )
            ).scalar_one()
        )

    async def pending(self, artifact_id: UUID) -> bool:
        return (
            await self._session.execute(
                sa.select(
                    sa.exists(
                        sa.select(JOBS.c.id).where(
                            JOBS.c.target_object_id == artifact_id,
                            JOBS.c.job_type == ARTIFACT_DELETE_JOB_TYPE,
                            JOBS.c.status.in_(("Queued", "Running")),
                        )
                    )
                )
            )
        ).scalar_one()

    async def latest_job(self, artifact_id: UUID) -> JobRecord | None:
        job_id = (
            await self._session.execute(
                sa.select(JOBS.c.id)
                .where(
                    JOBS.c.target_object_id == artifact_id,
                    JOBS.c.job_type == ARTIFACT_DELETE_JOB_TYPE,
                )
                .order_by(JOBS.c.created_at.desc(), JOBS.c.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return None if job_id is None else await JobRepository(self._session).get(job_id)

    async def remove(self, artifact_id: UUID) -> None:
        await self._session.execute(
            sa.update(DOWNLOAD_TASKS)
            .where(DOWNLOAD_TASKS.c.artifact_id == artifact_id)
            .values(artifact_id=None)
        )
        await self._session.execute(
            sa.delete(MODEL_ARTIFACTS).where(MODEL_ARTIFACTS.c.id == artifact_id)
        )


__all__ = ["ARTIFACT_DELETE_JOB_TYPE", "ARTIFACT_TRASH_JOB_TYPE", "ArtifactDeletionRepository"]

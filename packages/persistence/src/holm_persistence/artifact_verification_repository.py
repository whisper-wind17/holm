"""Queries for manual verification jobs; Artifact locks serialize commands."""

from datetime import datetime
from typing import cast
from uuid import UUID

import sqlalchemy as sa
from holm_domain import DomainEventType
from sqlalchemy.ext.asyncio import AsyncSession

from holm_persistence.domain_events import _DOMAIN_EVENTS
from holm_persistence.job_repository import JobRecord, JobRepository
from holm_persistence.jobs import JOBS
from holm_persistence.launch_verification_repository import LAUNCH_VERIFY_JOB_TYPE
from holm_persistence.model_domain import MODEL_ARTIFACTS

ARTIFACT_VERIFY_JOB_TYPE = "artifact.verify"


class ArtifactVerificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def latest_job(self, artifact_id: UUID) -> JobRecord | None:
        job_id = (
            await self._session.execute(
                sa.select(JOBS.c.id)
                .where(
                    JOBS.c.target_object_id == artifact_id,
                    JOBS.c.job_type == ARTIFACT_VERIFY_JOB_TYPE,
                )
                .order_by(JOBS.c.created_at.desc(), JOBS.c.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return None if job_id is None else await JobRepository(self._session).get(job_id)

    async def last_completed_at(self, artifact_id: UUID) -> datetime | None:
        manual = sa.select(JOBS.c.completed_at.label("verified_at")).where(
            JOBS.c.target_object_id == artifact_id,
            JOBS.c.job_type == ARTIFACT_VERIFY_JOB_TYPE,
            JOBS.c.status == "Succeeded",
        )
        automatic = (
            sa.select(_DOMAIN_EVENTS.c.occurred_at.label("verified_at"))
            .join(
                JOBS,
                _DOMAIN_EVENTS.c.aggregate_id
                == sa.cast(JOBS.c.payload["request_id"].astext, sa.Uuid),
            )
            .where(
                JOBS.c.target_object_id == artifact_id,
                JOBS.c.job_type == LAUNCH_VERIFY_JOB_TYPE,
                _DOMAIN_EVENTS.c.event == DomainEventType.INSTANCE_VERIFICATION_COMPLETED.value,
                _DOMAIN_EVENTS.c.payload["result"].astext.in_(("Ready", "Missing", "Corrupted")),
            )
        )
        # A canceled/superseded launch Job may finish without reading files. Only
        # the lease-fenced completion event proves actual full verification.
        light = sa.select(MODEL_ARTIFACTS.c.light_checked_at.label("verified_at")).where(
            MODEL_ARTIFACTS.c.id == artifact_id
        )
        completions = manual.union_all(automatic, light).subquery()
        result = await self._session.scalar(sa.select(sa.func.max(completions.c.verified_at)))

        return cast(datetime | None, result)

    async def pending(self, artifact_id: UUID) -> bool:
        job = await self.latest_job(artifact_id)
        return job is not None and job.status.value in {"Queued", "Running"}

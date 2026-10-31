"""Queue administrator verification without opening model files."""

from uuid import UUID

from holm_domain import ModelArtifactStatus
from holm_persistence import AsyncSessionFactory, JobDraft, JobRepository
from holm_persistence.artifact_deletion_repository import ArtifactDeletionRepository
from holm_persistence.artifact_verification_repository import (
    ARTIFACT_VERIFY_JOB_TYPE,
    ArtifactVerificationRepository,
)
from holm_persistence.job_repository import JobRecord


class ArtifactVerifyRejected(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("artifact verification rejected")


class ArtifactVerificationService:
    def __init__(self, session_factory: AsyncSessionFactory) -> None:
        self._session_factory = session_factory

    async def verify(self, artifact_id: UUID) -> JobRecord:
        async with self._session_factory() as session, session.begin():
            artifacts = ArtifactDeletionRepository(session)
            record = await artifacts.lock(artifact_id)
            if record is None:
                raise ArtifactVerifyRejected("artifact_not_found")
            # Missing is "可重新校验" — a restored file turns it Ready again.
            if record.status not in {
                ModelArtifactStatus.READY,
                ModelArtifactStatus.CORRUPTED,
                ModelArtifactStatus.MISSING,
            }:
                raise ArtifactVerifyRejected("artifact_not_ready")
            if await artifacts.pending(artifact_id):
                raise ArtifactVerifyRejected("artifact_delete_in_progress")
            previous = await ArtifactVerificationRepository(session).latest_job(artifact_id)
            if previous is not None and previous.status.value in {"Queued", "Running"}:
                return previous
            return await JobRepository(session).enqueue(
                JobDraft(
                    job_type=ARTIFACT_VERIFY_JOB_TYPE,
                    payload={},
                    target_object_id=artifact_id,
                    target_object_version=0,
                )
            )

    async def status(self, artifact_id: UUID) -> JobRecord | None:
        async with self._session_factory() as session:
            if await ArtifactDeletionRepository(session).lock(artifact_id) is None:
                raise ArtifactVerifyRejected("artifact_not_found")
            return await ArtifactVerificationRepository(session).latest_job(artifact_id)

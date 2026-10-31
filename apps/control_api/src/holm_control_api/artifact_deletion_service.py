"""Admin-only Artifact deletion request; file writes belong to Download Worker."""

from __future__ import annotations

from uuid import UUID

from holm_domain import (
    ArtifactStorageType,
    AuditAction,
    AuditActorType,
    AuditObjectType,
    AuditResult,
    ModelArtifactStatus,
)
from holm_persistence import (
    AsyncSessionFactory,
    AuditEventDraft,
    AuditRepository,
    JobDraft,
    JobRepository,
)
from holm_persistence.artifact_deletion_repository import (
    ARTIFACT_DELETE_JOB_TYPE,
    ArtifactDeletionRepository,
)
from holm_persistence.artifact_verification_repository import ArtifactVerificationRepository
from holm_persistence.job_repository import JobRecord
from holm_security import build_audit_details


class ArtifactDeleteRejected(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("artifact deletion rejected")


class ArtifactDeletionService:
    def __init__(self, session_factory: AsyncSessionFactory) -> None:
        self._session_factory = session_factory

    async def status(self, artifact_id: UUID) -> JobRecord | None:
        async with self._session_factory() as session:
            return await ArtifactDeletionRepository(session).latest_job(artifact_id)

    async def delete(self, *, artifact_id: UUID, actor_id: UUID, request_id: UUID) -> bool:
        async with self._session_factory() as session, session.begin():
            repository = ArtifactDeletionRepository(session)
            record = await repository.lock(artifact_id)
            if record is None:
                raise ArtifactDeleteRejected("artifact_not_found")
            if await ArtifactVerificationRepository(session).pending(artifact_id):
                raise ArtifactDeleteRejected("artifact_verify_in_progress")
            if await repository.referenced(artifact_id):
                raise ArtifactDeleteRejected("artifact_referenced")
            if (
                record.storage_type is ArtifactStorageType.EXTERNAL
                and record.status is not ModelArtifactStatus.READY
            ):
                raise ArtifactDeleteRejected("artifact_delete_not_allowed")
            if record.storage_type is ArtifactStorageType.MANAGED:
                if await repository.pending(artifact_id):
                    raise ArtifactDeleteRejected("artifact_delete_in_progress")
                await JobRepository(session).enqueue(
                    JobDraft(
                        job_type=ARTIFACT_DELETE_JOB_TYPE,
                        payload={"actor_id": str(actor_id), "request_id": str(request_id)},
                        target_object_id=artifact_id,
                        target_object_version=0,
                    )
                )
            else:
                await repository.remove(artifact_id)
                await AuditRepository(session).add(
                    AuditEventDraft(
                        actor_type=AuditActorType.USER,
                        actor_id=actor_id,
                        action=AuditAction.ARTIFACT_DELETE,
                        object_type=AuditObjectType.MODEL_ARTIFACT,
                        object_id=artifact_id,
                        result=AuditResult.SUCCEEDED,
                        request_id=request_id,
                        details=build_audit_details(AuditAction.ARTIFACT_DELETE.value, {}),
                    )
                )
            return record.storage_type is ArtifactStorageType.MANAGED

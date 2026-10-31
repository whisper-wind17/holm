"""Download creation after independently idempotent Model registration."""

from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from typing import Final, Literal
from uuid import UUID

from holm_domain import (
    DOWNLOAD_RESOLUTION_JOB_TYPE,
    EXTERNAL_REGISTRATION_JOB_TYPE,
    AuditAction,
    AuditActorType,
    AuditObjectType,
    AuditResult,
    ModelOriginType,
)
from holm_domain.model_acquisition import EXTERNAL_REVISION
from holm_persistence import (
    AsyncSessionFactory,
    AuditEventDraft,
    AuditRepository,
    IdempotencyRepository,
    JobDraft,
    JobRepository,
    ModelRepository,
    database_utc_now,
    idempotency_digests,
)
from holm_persistence.download_task_repository import (
    DownloadCreationContended,
    DownloadTaskRecord,
    DownloadTaskRepository,
)
from holm_security import build_audit_details

from holm_control_api.model_registration_service import (
    IDEMPOTENCY_LIFETIME,
    IdempotencyConflict,
    ModelRegistrationService,
)
from holm_control_api.model_resolution import ResolutionFailed

CREATE_DOWNLOAD_OPERATION: Final[str] = "download.create"
type DownloadDisposition = Literal["created", "joined_existing"]


class DownloadSourceRejected(RuntimeError):
    def __init__(self) -> None:
        super().__init__("download source is unsupported")


@dataclass(frozen=True, slots=True)
class DownloadCreationSuccess:
    task: DownloadTaskRecord
    replayed: bool
    disposition: DownloadDisposition


class DownloadCreationService:
    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory,
        registration: ModelRegistrationService,
        idempotency_hmac_secret: bytes,
    ) -> None:
        self._session_factory = session_factory
        self._registration = registration
        self._hmac_secret = idempotency_hmac_secret

    async def create(
        self,
        *,
        actor_id: UUID,
        request_id: UUID,
        idempotency_key: str,
        source: ModelOriginType,
        repository_id: str | None,
        external_path: str | None,
        revision: str | None,
    ) -> DownloadCreationSuccess | ResolutionFailed | DownloadCreationContended:
        if source is ModelOriginType.LOCAL_PATH:
            candidate = await self._registration.external_candidate(
                external_path, repository_id=repository_id, revision=revision
            )
            if isinstance(candidate, ResolutionFailed):
                return candidate
            async with self._session_factory() as session, session.begin():
                now = await database_utc_now(session)
                model = await ModelRepository(session).upsert(candidate, now=now)
                repository = DownloadTaskRepository(session)
                external_creation = await repository.create_or_join_external(
                    model_id=model.id, path=model.source_key, now=now
                )
                digests = idempotency_digests(
                    hmac_secret=self._hmac_secret,
                    idempotency_key=idempotency_key,
                    normalized_request=json.dumps(
                        {
                            "source": source.value,
                            "source_key": model.source_key,
                            "requested_revision": EXTERNAL_REVISION,
                        },
                        sort_keys=True,
                    ).encode(),
                )
                claim = await IdempotencyRepository(session).claim(
                    actor_id=actor_id,
                    operation=CREATE_DOWNLOAD_OPERATION,
                    digests=digests,
                    response_object_id=external_creation.task.id,
                    now=now,
                    expires_before=now - IDEMPOTENCY_LIFETIME,
                )
                if claim.claimed is False:
                    await session.rollback()
                    if not hmac.compare_digest(claim.record.request_hash, digests.request_hash):
                        raise IdempotencyConflict()
                    # Read after leaving the rolled-back transaction below.
                    replay_id = claim.record.response_object_id
                else:
                    if external_creation.created:
                        await JobRepository(session).enqueue(
                            JobDraft(
                                job_type=EXTERNAL_REGISTRATION_JOB_TYPE,
                                payload={},
                                target_object_id=external_creation.task.id,
                                target_object_version=external_creation.task.attempt,
                            )
                        )
                    await repository.subscribe(
                        task_id=external_creation.task.id, actor_id=actor_id, now=now
                    )
                    external_disposition: DownloadDisposition = (
                        "created" if external_creation.created else "joined_existing"
                    )
                    await AuditRepository(session).add(
                        AuditEventDraft(
                            actor_type=AuditActorType.USER,
                            actor_id=actor_id,
                            action=AuditAction.DOWNLOAD_CHANGE,
                            object_type=AuditObjectType.DOWNLOAD_TASK,
                            object_id=external_creation.task.id,
                            result=AuditResult.SUCCEEDED,
                            request_id=request_id,
                            details=build_audit_details(
                                AuditAction.DOWNLOAD_CHANGE.value,
                                {
                                    "operation": external_disposition,
                                    "reason_code": None,
                                    "source_type": source.value,
                                },
                            ),
                        )
                    )
                    return DownloadCreationSuccess(
                        external_creation.task, replayed=False, disposition=external_disposition
                    )
            async with self._session_factory() as session:
                task = await DownloadTaskRepository(session).get(replay_id)
                if task is None:
                    raise RuntimeError("download replay object unavailable")
                return DownloadCreationSuccess(task, replayed=True, disposition="joined_existing")
        registered = await self._registration.register(
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            source=source,
            repository_id=repository_id,
            external_path=external_path,
            revision=revision,
        )
        if isinstance(registered, ResolutionFailed):
            return registered
        model = registered.model
        requested_revision_norm = registered.requested_revision_norm
        if model.repository_id is None or requested_revision_norm is None:
            raise RuntimeError("download registration identity unavailable")
        digests = idempotency_digests(
            hmac_secret=self._hmac_secret,
            idempotency_key=idempotency_key,
            normalized_request=json.dumps(
                {
                    "source": model.source_type.value,
                    "source_key": model.source_key,
                    "requested_revision": requested_revision_norm,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8"),
        )
        async with self._session_factory() as session:
            now = await database_utc_now(session)
            repository = DownloadTaskRepository(session)
            creation = await repository.create_or_join(
                model_id=model.id,
                source_type=model.source_type,
                repository_id=model.repository_id,
                requested_revision_norm=requested_revision_norm,
                now=now,
            )
            if isinstance(creation, DownloadCreationContended):
                return creation
            if creation.created:
                await JobRepository(session).enqueue(
                    JobDraft(
                        job_type=DOWNLOAD_RESOLUTION_JOB_TYPE,
                        payload={},
                        target_object_id=creation.task.id,
                        target_object_version=creation.task.attempt,
                    )
                )
            await repository.subscribe(task_id=creation.task.id, actor_id=actor_id, now=now)
            claim = await IdempotencyRepository(session).claim(
                actor_id=actor_id,
                operation=CREATE_DOWNLOAD_OPERATION,
                digests=digests,
                response_object_id=creation.task.id,
                now=now,
                expires_before=now - IDEMPOTENCY_LIFETIME,
            )
            if not claim.claimed:
                await session.rollback()
                if not hmac.compare_digest(claim.record.request_hash, digests.request_hash):
                    raise IdempotencyConflict()
                task = await repository.get(claim.record.response_object_id)
                if task is None:
                    raise RuntimeError("download replay object unavailable")
                # This invocation reused an object; it did not create one.
                return DownloadCreationSuccess(task, replayed=True, disposition="joined_existing")
            disposition: DownloadDisposition = "created" if creation.created else "joined_existing"
            await AuditRepository(session).add(
                AuditEventDraft(
                    actor_type=AuditActorType.USER,
                    actor_id=actor_id,
                    action=AuditAction.DOWNLOAD_CHANGE,
                    object_type=AuditObjectType.DOWNLOAD_TASK,
                    object_id=creation.task.id,
                    result=AuditResult.SUCCEEDED,
                    request_id=request_id,
                    details=build_audit_details(
                        AuditAction.DOWNLOAD_CHANGE.value,
                        {
                            "operation": disposition,
                            "reason_code": None,
                            "source_type": model.source_type.value,
                        },
                    ),
                )
            )
            await session.commit()
        return DownloadCreationSuccess(creation.task, replayed=False, disposition=disposition)

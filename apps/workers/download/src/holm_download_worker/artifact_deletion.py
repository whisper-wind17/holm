"""Leased, recoverable Managed Artifact deletion and post-commit trash cleanup."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from uuid import UUID

from holm_common.config import StorageConfig
from holm_domain import (
    ArtifactStorageType,
    AuditAction,
    AuditActorType,
    AuditObjectType,
    AuditResult,
)
from holm_persistence import (
    AsyncSessionFactory,
    AuditEventDraft,
    AuditRepository,
    JobDraft,
    JobHandlerContext,
    JobRepository,
)
from holm_persistence.artifact_deletion_repository import (
    ARTIFACT_TRASH_JOB_TYPE,
    ArtifactDeletionRepository,
)
from holm_persistence.job_executor import JobFailure
from holm_security import build_audit_details


class ArtifactDeletionFailed(JobFailure):
    """Persist only the stable deletion failure code, never its source path."""


def _owned(path: Path, root: Path) -> Path:
    if (
        not path.is_absolute()
        or root.is_symlink()
        or root.resolve(strict=False) != root
        or path.is_symlink()
    ):
        raise ArtifactDeletionFailed("artifact_delete_unsafe_path")
    if path == root or not path.is_relative_to(root) or path.resolve(strict=False) != path:
        raise ArtifactDeletionFailed("artifact_delete_unsafe_path")
    return path


def _trash(root: Path, artifact_id: UUID) -> Path:
    trash_root = root / ".trash"
    _owned(trash_root, root)
    return _owned(trash_root / str(artifact_id), root)


def _move(source: Path, destination: Path) -> bool:
    if destination.exists() or destination.is_symlink():
        if source.exists() or source.is_symlink():
            raise ArtifactDeletionFailed("artifact_delete_trash_conflict")
        if not destination.is_dir() or destination.is_symlink():
            raise ArtifactDeletionFailed("artifact_delete_unsafe_path")
        return False
    if not source.exists() and not source.is_symlink():
        return False
    if not source.is_dir() or source.is_symlink():
        raise ArtifactDeletionFailed("artifact_delete_source_missing")
    destination.parent.mkdir(exist_ok=True)
    os.rename(source, destination)
    return True


class ArtifactDeletionHandler:
    def __init__(self, *, session_factory: AsyncSessionFactory, storage: StorageConfig) -> None:
        self._session_factory = session_factory
        self._model_root = storage.model_root
        self._tokenizer_root = storage.tokenizer_root

    async def delete(self, context: JobHandlerContext) -> None:
        artifact_id = context.job.target_object_id
        async with self._session_factory() as session:
            record = await ArtifactDeletionRepository(session).lock(artifact_id)
        if record is None:
            return
        if record.storage_type is not ArtifactStorageType.MANAGED:
            raise ArtifactDeletionFailed("artifact_delete_unsafe_path")
        source = _owned(Path(record.path), self._model_root / "managed")
        model_trash = _trash(self._model_root, artifact_id)
        bundle_source = None
        bundle_trash = None
        if record.tokenizer_bundle_path is not None:
            bundle_source = _owned(Path(record.tokenizer_bundle_path), self._tokenizer_root)
            if bundle_source != self._tokenizer_root / str(artifact_id):
                raise ArtifactDeletionFailed("artifact_delete_unsafe_path")
            bundle_trash = _trash(self._tokenizer_root, artifact_id)
        context.ensure_lease()
        try:
            await asyncio.to_thread(_move, source, model_trash)
            if bundle_source is not None and bundle_trash is not None:
                await asyncio.to_thread(_move, bundle_source, bundle_trash)
            context.ensure_lease()
            async with self._session_factory() as session, session.begin():
                repository = ArtifactDeletionRepository(session)
                locked = await repository.lock(artifact_id)
                if locked is None:
                    raise ArtifactDeletionFailed("artifact_delete_not_found")
                if await repository.referenced(artifact_id):
                    raise ArtifactDeletionFailed("artifact_referenced")
                await repository.remove(artifact_id)
                await JobRepository(session).enqueue(
                    JobDraft(
                        job_type=ARTIFACT_TRASH_JOB_TYPE,
                        payload={"bundle": bundle_trash is not None},
                        target_object_id=artifact_id,
                        target_object_version=0,
                    )
                )
                await AuditRepository(session).add(
                    AuditEventDraft(
                        actor_type=AuditActorType.USER,
                        actor_id=UUID(str(context.job.payload["actor_id"])),
                        action=AuditAction.ARTIFACT_DELETE,
                        object_type=AuditObjectType.MODEL_ARTIFACT,
                        object_id=artifact_id,
                        result=AuditResult.SUCCEEDED,
                        request_id=UUID(str(context.job.payload["request_id"])),
                        details=build_audit_details(AuditAction.ARTIFACT_DELETE.value, {}),
                    )
                )
        except Exception as error:
            if (
                bundle_source is not None
                and bundle_trash is not None
                and bundle_trash.exists()
                and not bundle_source.exists()
            ):
                await asyncio.to_thread(os.rename, bundle_trash, bundle_source)
            if model_trash.exists() and not source.exists():
                await asyncio.to_thread(os.rename, model_trash, source)
            if isinstance(error, OSError):
                raise ArtifactDeletionFailed("artifact_delete_move_failed") from None
            raise

    async def cleanup(self, context: JobHandlerContext) -> None:
        artifact_id = context.job.target_object_id
        roots = [self._model_root]
        if context.job.payload.get("bundle") is True:
            roots.append(self._tokenizer_root)
        for root in roots:
            context.ensure_lease()
            path = _trash(root, artifact_id)
            if path.is_symlink():
                raise ArtifactDeletionFailed("artifact_delete_unsafe_path")
            if path.is_dir():
                await asyncio.to_thread(shutil.rmtree, path)


__all__ = ["ArtifactDeletionHandler"]

"""Lease-safe user commands for persisted Download Tasks."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final
from uuid import UUID

from holm_domain import (
    DOWNLOAD_CLEANUP_JOB_TYPE,
    DOWNLOAD_RESOLUTION_JOB_TYPE,
    EXTERNAL_REGISTRATION_JOB_TYPE,
    AuditAction,
    AuditActorType,
    AuditObjectType,
    AuditResult,
    DownloadFailureReason,
    DownloadTaskStatus,
    ModelOriginType,
)
from holm_persistence import (
    AsyncSessionFactory,
    AuditEventDraft,
    AuditRepository,
    JobDraft,
    JobRepository,
    database_utc_now,
)
from holm_persistence.download_task_repository import (
    DownloadCommandIdentityConflict,
    DownloadCommandTaskRecord,
    DownloadTaskRepository,
)
from holm_security import build_audit_details

STAGING_CLEANUP_REQUESTED_REASON: Final[str] = "staging_cleanup_requested"


class DownloadCommand(StrEnum):
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"
    RETRY = "retry"


class DownloadCommandErrorCode(StrEnum):
    NOT_FOUND = "download_not_found"
    NOT_ALLOWED = "download_command_not_allowed"
    ALREADY_IN_PROGRESS = "download_already_in_progress"


# This matrix defines permitted download commands. Terminal and Publishing rows are
# explicit so callers cannot mistake an absent key for an unknown state.
DOWNLOAD_COMMAND_MATRIX: Final[
    MappingProxyType[tuple[DownloadTaskStatus, str | None], frozenset[DownloadCommand]]
] = MappingProxyType(
    {
        (DownloadTaskStatus.QUEUED, None): frozenset({DownloadCommand.CANCEL}),
        (DownloadTaskStatus.RESOLVING, None): frozenset({DownloadCommand.CANCEL}),
        (DownloadTaskStatus.DOWNLOADING, None): frozenset(
            {DownloadCommand.PAUSE, DownloadCommand.CANCEL}
        ),
        (DownloadTaskStatus.PAUSED, None): frozenset(
            {DownloadCommand.RESUME, DownloadCommand.CANCEL}
        ),
        (DownloadTaskStatus.VERIFYING, None): frozenset({DownloadCommand.CANCEL}),
        (DownloadTaskStatus.PUBLISHING, None): frozenset(),
        (DownloadTaskStatus.COMPLETED, None): frozenset(),
        (DownloadTaskStatus.FAILED, None): frozenset(
            {DownloadCommand.RESUME, DownloadCommand.RETRY}
        ),
        (DownloadTaskStatus.CANCELLED, None): frozenset(),
        (
            DownloadTaskStatus.FAILED,
            DownloadFailureReason.MODEL_LAYOUT_UNSUPPORTED.value,
        ): frozenset(),
        (
            DownloadTaskStatus.FAILED,
            DownloadFailureReason.ARTIFACT_REQUIRED_FILES_INVALID.value,
        ): frozenset(),
    }
)


def available_commands_for(
    status: DownloadTaskStatus,
    error_code: str | None = None,
    matrix: Mapping[
        tuple[DownloadTaskStatus, str | None], frozenset[DownloadCommand]
    ] = DOWNLOAD_COMMAND_MATRIX,
    *,
    source_type: ModelOriginType | None = None,
) -> tuple[DownloadCommand, ...]:
    """Project the single command table; None keys provide each state's default."""
    if source_type is ModelOriginType.LOCAL_PATH:
        if status is DownloadTaskStatus.FAILED:
            return (DownloadCommand.RESUME,)
        return (DownloadCommand.CANCEL,) if status is DownloadTaskStatus.VERIFYING else ()
    commands = matrix.get((status, error_code), matrix[(status, None)])
    return tuple(sorted(commands, key=lambda command: command.value))


class DownloadCommandNotFound(RuntimeError):
    def __init__(self) -> None:
        super().__init__("download task was not found")


class DownloadCommandNotAllowed(RuntimeError):
    def __init__(self, *, command: DownloadCommand | str, status: DownloadTaskStatus) -> None:
        self.command = command
        self.status = status
        super().__init__("download command is not allowed for the current state")


class DownloadAlreadyInProgress(RuntimeError):
    def __init__(self) -> None:
        super().__init__("the same model already has a download in progress")


@dataclass(frozen=True, slots=True)
class DownloadCommandResult:
    task: DownloadCommandTaskRecord
    command: DownloadCommand
    staging_cleanup_requested: bool


def _target_status(
    command: DownloadCommand,
    task: DownloadCommandTaskRecord,
) -> DownloadTaskStatus:
    if task.source_type is ModelOriginType.LOCAL_PATH and command is DownloadCommand.RESUME:
        return DownloadTaskStatus.VERIFYING
    if command is DownloadCommand.PAUSE:
        return DownloadTaskStatus.PAUSED
    if command is DownloadCommand.CANCEL:
        return DownloadTaskStatus.CANCELLED
    if command is DownloadCommand.RETRY:
        return DownloadTaskStatus.RESOLVING
    if command is DownloadCommand.RESUME:
        if task.status is DownloadTaskStatus.PAUSED or task.resolved_revision is not None:
            return DownloadTaskStatus.DOWNLOADING
        return DownloadTaskStatus.RESOLVING
    raise AssertionError("unhandled download command")


class DownloadCommandService:
    """Serialize state changes and their audit event in one transaction."""

    def __init__(self, *, session_factory: AsyncSessionFactory) -> None:
        self._session_factory = session_factory

    async def cancel(
        self, *, task_id: UUID, actor_id: UUID, request_id: UUID, cleanup_staging: bool = True
    ) -> DownloadCommandResult:
        return await self.execute(
            task_id=task_id,
            actor_id=actor_id,
            request_id=request_id,
            cleanup_staging=cleanup_staging,
            command=DownloadCommand.CANCEL,
        )

    async def delete(
        self, *, task_id: UUID, actor_id: UUID, request_id: UUID, cleanup_staging: bool = False
    ) -> None:
        async with self._session_factory() as session, session.begin():
            repository = DownloadTaskRepository(session)
            task = await repository.lock_command_task(task_id)
            if task is None:
                raise DownloadCommandNotFound()
            if task.status not in {
                DownloadTaskStatus.COMPLETED,
                DownloadTaskStatus.FAILED,
                DownloadTaskStatus.CANCELLED,
            }:
                raise DownloadCommandNotAllowed(command="delete", status=task.status)
            if cleanup_staging and task.source_type is not ModelOriginType.LOCAL_PATH:
                await JobRepository(session).enqueue(
                    JobDraft(
                        job_type=DOWNLOAD_CLEANUP_JOB_TYPE,
                        payload={},
                        target_object_id=task.id,
                        target_object_version=task.attempt,
                    )
                )
            await repository.delete_task(task.id)
            details: dict[str, object] = {
                "operation": "delete",
                "source_type": task.source_type.value,
            }
            if cleanup_staging:
                details["reason_code"] = STAGING_CLEANUP_REQUESTED_REASON
            await AuditRepository(session).add(
                AuditEventDraft(
                    actor_type=AuditActorType.USER,
                    actor_id=actor_id,
                    action=AuditAction.DOWNLOAD_CHANGE,
                    object_type=AuditObjectType.DOWNLOAD_TASK,
                    object_id=task.id,
                    result=AuditResult.SUCCEEDED,
                    request_id=request_id,
                    details=build_audit_details(AuditAction.DOWNLOAD_CHANGE.value, details),
                )
            )

    async def execute(
        self,
        *,
        task_id: UUID,
        command: DownloadCommand,
        actor_id: UUID,
        request_id: UUID,
        cleanup_staging: bool = True,
    ) -> DownloadCommandResult:
        async with self._session_factory() as session, session.begin():
            repository = DownloadTaskRepository(session)
            task = await repository.lock_command_task(task_id)
            if task is None:
                raise DownloadCommandNotFound()
            work = await repository.lock_resolution_work(task.id)
            if work is None:
                raise DownloadCommandNotFound()
            if command not in available_commands_for(
                task.status,
                work.error_code,
                matrix=DOWNLOAD_COMMAND_MATRIX,
                source_type=task.source_type,
            ):
                raise DownloadCommandNotAllowed(command=command, status=task.status)

            target_status = _target_status(command, task)
            next_attempt = task.attempt + (1 if command is DownloadCommand.RETRY else 0)
            try:
                updated = await repository.apply_command_transition(
                    task.id,
                    expected_status=task.status,
                    status=target_status,
                    attempt=next_attempt,
                    reset_progress=command is DownloadCommand.RETRY,
                    now=await database_utc_now(session),
                )
            except DownloadCommandIdentityConflict:
                raise DownloadAlreadyInProgress() from None
            cleanup_requested = command in {DownloadCommand.CANCEL, DownloadCommand.RETRY}
            if task.source_type is ModelOriginType.LOCAL_PATH:
                cleanup_requested = False
            if command is DownloadCommand.CANCEL and not cleanup_staging:
                cleanup_requested = False
            if target_status in {
                DownloadTaskStatus.RESOLVING,
                DownloadTaskStatus.DOWNLOADING,
                DownloadTaskStatus.VERIFYING,
            }:
                await JobRepository(session).enqueue(
                    JobDraft(
                        job_type=(
                            EXTERNAL_REGISTRATION_JOB_TYPE
                            if task.source_type is ModelOriginType.LOCAL_PATH
                            else DOWNLOAD_RESOLUTION_JOB_TYPE
                        ),
                        payload={"staging_cleanup_requested": cleanup_requested},
                        target_object_id=updated.id,
                        target_object_version=updated.attempt,
                    )
                )
            elif command is DownloadCommand.CANCEL and cleanup_requested:
                await JobRepository(session).enqueue(
                    JobDraft(
                        job_type=DOWNLOAD_CLEANUP_JOB_TYPE,
                        payload={},
                        target_object_id=updated.id,
                        target_object_version=updated.attempt,
                    )
                )
            detail_candidates: dict[str, object] = {
                "operation": command.value,
                "source_type": updated.source_type.value,
            }
            if cleanup_requested:
                detail_candidates["reason_code"] = STAGING_CLEANUP_REQUESTED_REASON
            await AuditRepository(session).add(
                AuditEventDraft(
                    actor_type=AuditActorType.USER,
                    actor_id=actor_id,
                    action=AuditAction.DOWNLOAD_CHANGE,
                    object_type=AuditObjectType.DOWNLOAD_TASK,
                    object_id=updated.id,
                    result=AuditResult.SUCCEEDED,
                    request_id=request_id,
                    details=build_audit_details(
                        AuditAction.DOWNLOAD_CHANGE.value,
                        detail_candidates,
                    ),
                )
            )
            return DownloadCommandResult(
                task=updated,
                command=command,
                staging_cleanup_requested=cleanup_requested,
            )


__all__ = [
    "DOWNLOAD_COMMAND_MATRIX",
    "STAGING_CLEANUP_REQUESTED_REASON",
    "DownloadAlreadyInProgress",
    "DownloadCommand",
    "DownloadCommandErrorCode",
    "DownloadCommandNotAllowed",
    "DownloadCommandNotFound",
    "DownloadCommandResult",
    "DownloadCommandService",
]

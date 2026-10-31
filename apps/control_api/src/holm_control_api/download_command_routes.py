"""Authenticated Pause, Resume, Cancel and Retry endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from holm_domain import Capability, DownloadTaskStatus
from pydantic import BaseModel, ConfigDict
from starlette.requests import Request
from starlette.responses import JSONResponse

from holm_control_api.auth_routes import json_response
from holm_control_api.authorization import (
    capability_route,
    current_authenticated_session,
    current_request_id,
)
from holm_control_api.download_command_service import (
    DownloadAlreadyInProgress,
    DownloadCommand,
    DownloadCommandErrorCode,
    DownloadCommandNotAllowed,
    DownloadCommandNotFound,
    DownloadCommandResult,
    DownloadCommandService,
)


class DownloadCommandResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    status: DownloadTaskStatus
    attempt: int
    command: DownloadCommand
    staging_cleanup_requested: bool


async def handle_download_command_error(request: Request, exception: Exception) -> JSONResponse:
    request_id = str(current_request_id(request))
    if isinstance(exception, DownloadCommandNotFound):
        status_code = 404
        code = DownloadCommandErrorCode.NOT_FOUND
        message = "下载任务不存在，未执行操作，请刷新列表后重试"
    elif isinstance(exception, DownloadCommandNotAllowed):
        status_code = 409
        code = DownloadCommandErrorCode.NOT_ALLOWED
        message = "当前状态不允许执行此操作，下载任务未改变，请刷新状态后重试"
    elif isinstance(exception, DownloadAlreadyInProgress):
        status_code = 409
        code = DownloadCommandErrorCode.ALREADY_IN_PROGRESS
        message = "同一模型已有进行中的下载任务，当前任务未改变，请等待该任务完成后重试"
    else:
        raise exception
    return json_response(
        status_code=status_code,
        request_id=request_id,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": {},
                "request_id": request_id,
            }
        },
    )


def _payload(result: DownloadCommandResult) -> dict[str, object]:
    return DownloadCommandResponse(
        id=result.task.id,
        status=result.task.status,
        attempt=result.task.attempt,
        command=result.command,
        staging_cleanup_requested=result.staging_cleanup_requested,
    ).model_dump(mode="json")


def create_download_command_router(service: DownloadCommandService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/downloads", tags=["downloads"])

    def register(command: DownloadCommand) -> None:
        async def execute(request: Request, id: UUID, cleanup_staging: bool = True) -> JSONResponse:
            result = await service.execute(
                task_id=id,
                command=command,
                cleanup_staging=cleanup_staging,
                actor_id=current_authenticated_session(request).user.id,
                request_id=current_request_id(request),
            )
            return json_response(
                content=_payload(result),
                request_id=str(current_request_id(request)),
            )

        execute.__name__ = f"{command.value}_download"
        router.add_api_route(
            f"/{{id}}/{command.value}",
            capability_route(Capability.MANAGE_LOCAL_MODELS)(execute),
            methods=["POST"],
        )

    for command in DownloadCommand:
        register(command)
    return router


__all__ = [
    "DownloadCommandResponse",
    "create_download_command_router",
    "handle_download_command_error",
]

"""The authenticated download creation surface; no source identifiers are returned."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Header
from holm_domain import Capability, DownloadTaskStatus
from holm_persistence.download_task_repository import DownloadCreationContended
from pydantic import BaseModel, ConfigDict
from starlette.requests import Request
from starlette.responses import JSONResponse

from holm_control_api.auth_routes import json_response
from holm_control_api.authorization import (
    capability_route,
    current_authenticated_session,
    current_request_id,
)
from holm_control_api.download_creation_service import (
    DownloadCreationService,
    DownloadCreationSuccess,
    DownloadDisposition,
    DownloadSourceRejected,
)
from holm_control_api.model_registration_routes import (
    IDEMPOTENCY_KEY_HEADER,
    ModelRegistrationRequest,
    RegistrationUnresolvedResponse,
)


class DownloadCreatedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    model_id: UUID
    status: DownloadTaskStatus
    replayed: bool
    disposition: DownloadDisposition


async def handle_download_source_rejected(request: Request, exception: Exception) -> JSONResponse:
    if not isinstance(exception, DownloadSourceRejected):
        raise exception
    request_id = str(current_request_id(request))
    return json_response(
        status_code=400,
        request_id=request_id,
        content={
            "error": {
                "code": "download_source_unsupported",
                "message": "本地目录不支持下载，未创建下载任务，请使用注册本地目录操作",
                "details": {},
                "request_id": request_id,
            }
        },
    )


def create_download_creation_router(service: DownloadCreationService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/downloads", tags=["downloads"])

    @router.post("")
    @capability_route(Capability.MANAGE_LOCAL_MODELS)
    async def create_download(
        request: Request,
        body: ModelRegistrationRequest,
        idempotency_key: str = Header(min_length=1, max_length=256, alias=IDEMPOTENCY_KEY_HEADER),
    ) -> JSONResponse:
        result = await service.create(
            actor_id=current_authenticated_session(request).user.id,
            request_id=current_request_id(request),
            idempotency_key=idempotency_key,
            source=body.source,
            repository_id=body.repository_id,
            external_path=body.external_path,
            revision=body.revision,
        )
        if isinstance(result, DownloadCreationContended):
            request_id = str(current_request_id(request))
            return json_response(
                status_code=503,
                request_id=request_id,
                content={
                    "error": {
                        "code": result.code,
                        "message": "下载请求暂时繁忙，本次未创建下载任务，请稍后重试",
                        "details": {},
                        "request_id": request_id,
                    }
                },
            )
        if isinstance(result, DownloadCreationSuccess):
            payload = DownloadCreatedResponse(
                id=result.task.id,
                model_id=result.task.model_id,
                status=result.task.status,
                replayed=result.replayed,
                disposition=result.disposition,
            ).model_dump(mode="json")
        else:
            conclusion = result.result
            if conclusion.next_action is None:
                raise RuntimeError("unresolved download must carry a direction")
            payload = RegistrationUnresolvedResponse(
                status=conclusion.status,
                reason_code=conclusion.reason_code,
                next_action=conclusion.next_action,
            ).model_dump(mode="json")
        return json_response(content=payload, request_id=str(current_request_id(request)))

    return router

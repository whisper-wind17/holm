"""Administrator HTTP API for publishing and updating AI Services."""

from __future__ import annotations

from typing import Annotated, Final, Self
from uuid import UUID

from fastapi import APIRouter
from holm_domain import AiServiceRuntimeStatus, Capability, ModelInstanceStatus
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from holm_control_api.ai_service_service import (
    AiServiceDeleteConflict,
    AiServiceDisplayNameConflict,
    AiServiceManagementService,
    AiServiceNameConflict,
    AiServiceNotFound,
    AiServicePrimaryInstanceNotFound,
    AiServiceRuntimeRecord,
    AiServiceVersionConflict,
    CreateAiService,
    UpdateAiService,
)
from holm_control_api.auth_routes import json_response
from holm_control_api.authorization import (
    capability_route,
    current_authenticated_session,
    current_request_id,
)
from holm_control_api.model_catalog_routes import _error_response
from holm_control_api.model_instance_service import ModelInstanceInvalidState

SERVICES_PATH: Final[str] = "/api/v1/services"


def _normalize_display_name(value: str) -> str:
    name = value.strip()
    if "\x00" in name or not 2 <= len(name) <= 40:
        raise ValueError("service display name must contain 2 to 40 characters")
    return name


class CreateAiServiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str | None = None
    public_model_name: Annotated[str, Field(min_length=1, max_length=255)]
    description: Annotated[str, Field(max_length=2000)] = ""
    enabled: bool = True
    primary_instance_id: UUID

    @model_validator(mode="after")
    def validate_normalized_name(self) -> Self:
        if "name" in self.model_fields_set:
            if self.name is None:
                raise ValueError("service display name must contain 2 to 40 characters")
            object.__setattr__(self, "name", _normalize_display_name(self.name))
        if self.public_model_name != self.public_model_name.strip():
            raise ValueError("public_model_name must be normalized")
        return self


class UpdateAiServiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    description: Annotated[str, Field(max_length=2000)] | None = None
    enabled: bool | None = None
    primary_instance_id: UUID | None = None

    @model_validator(mode="after")
    def require_change(self) -> Self:
        if all(
            value is None for value in (self.description, self.enabled, self.primary_instance_id)
        ):
            raise ValueError("at least one service field is required")
        return self


class RenameAiServiceRequest(BaseModel):
    """Explicit confirmation and observed version for a public route identity change."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    public_model_name: Annotated[
        str, Field(min_length=2, max_length=64, pattern=r"^[a-z0-9._-]{2,64}$")
    ]
    confirm_public_model_name: str
    expected_route_version: Annotated[int, Field(ge=1, strict=True)]

    @model_validator(mode="after")
    def require_confirmation(self) -> Self:
        if self.confirm_public_model_name != self.public_model_name:
            raise ValueError("public model name confirmation does not match")
        return self


class RenameAiServiceDisplayNameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    expected_route_version: Annotated[int, Field(ge=1, strict=True)]

    @model_validator(mode="after")
    def normalize_name(self) -> Self:
        object.__setattr__(self, "name", _normalize_display_name(self.name))
        return self


class AiServiceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    name: str
    public_model_name: str
    description: str
    enabled: bool
    primary_instance_id: UUID
    primary_instance_status: ModelInstanceStatus | None
    route_version: int
    runtime_status: AiServiceRuntimeStatus

    @classmethod
    def from_record(cls, record: AiServiceRuntimeRecord) -> Self:
        service = record.service
        return cls(
            id=service.id,
            name=service.name,
            public_model_name=service.public_model_name,
            description=service.description,
            enabled=service.enabled,
            primary_instance_id=service.primary_instance_id,
            primary_instance_status=record.primary_instance_status,
            route_version=service.route_version,
            runtime_status=record.runtime_status,
        )


async def handle_ai_service_error(request: Request, exception: Exception) -> JSONResponse:
    if isinstance(exception, AiServiceDisplayNameConflict):
        return _error_response(
            request,
            status_code=409,
            code="service_display_name_conflict",
            message="服务名称已被使用，AI 服务操作未执行，请更换服务名称",
        )
    if isinstance(exception, AiServiceNotFound):
        return _error_response(
            request,
            status_code=404,
            code="service_not_found",
            message="AI 服务不存在，操作未执行，请刷新服务列表",
        )
    if isinstance(exception, AiServicePrimaryInstanceNotFound):
        return _error_response(
            request,
            status_code=404,
            code="instance_not_found",
            message="模型实例不存在，AI 服务操作未执行，请刷新实例列表",
        )
    if isinstance(exception, ModelInstanceInvalidState):
        return _error_response(
            request,
            status_code=409,
            code="invalid_instance_state",
            message="模型实例未在运行，AI 服务操作未执行，请选择运行中的实例",
        )
    if isinstance(exception, AiServiceVersionConflict):
        return _error_response(
            request,
            status_code=409,
            code="version_conflict",
            message="配置已更新，本次修改未保存，请刷新后重新提交",
        )
    if isinstance(exception, AiServiceNameConflict):
        return _error_response(
            request,
            status_code=409,
            code="service_name_conflict",
            message="API 模型名已被使用，AI 服务操作未执行，请更换名称",
        )
    raise exception


def _response(
    request: Request,
    record: AiServiceRuntimeRecord,
    *,
    status_code: int = 200,
) -> JSONResponse:
    return json_response(
        status_code=status_code,
        request_id=str(current_request_id(request)),
        content=AiServiceResponse.from_record(record).model_dump(mode="json"),
    )


def create_ai_service_router(service: AiServiceManagementService) -> APIRouter:
    router = APIRouter(prefix=SERVICES_PATH, tags=["services"])

    @router.get("")
    @capability_route(Capability.MANAGE_AI_SERVICES)
    async def list_services(request: Request) -> JSONResponse:
        return json_response(
            request_id=str(current_request_id(request)),
            content={
                "items": [
                    AiServiceResponse.from_record(record).model_dump(mode="json")
                    for record in await service.list_with_runtime_status()
                ]
            },
        )

    @router.get("/{service_id}")
    @capability_route(Capability.MANAGE_AI_SERVICES)
    async def get_service(request: Request, service_id: UUID) -> JSONResponse:
        return _response(request, await service.get_with_runtime_status(service_id))

    @router.post("", status_code=201)
    @capability_route(Capability.MANAGE_AI_SERVICES)
    async def create_service(request: Request, body: CreateAiServiceRequest) -> JSONResponse:
        actor_id = current_authenticated_session(request).user.id
        record = await service.create(
            CreateAiService(
                name=body.name,
                public_model_name=body.public_model_name,
                description=body.description,
                enabled=body.enabled,
                primary_instance_id=body.primary_instance_id,
            ),
            actor_id=actor_id,
            request_id=current_request_id(request),
        )
        return _response(
            request,
            await service.get_with_runtime_status(record.id),
            status_code=201,
        )

    @router.delete("/{service_id}", status_code=204)
    @capability_route(Capability.MANAGE_AI_SERVICES)
    async def delete_service(request: Request, service_id: UUID) -> Response:
        try:
            await service.delete(
                service_id,
                actor_id=current_authenticated_session(request).user.id,
                request_id=current_request_id(request),
            )
        except AiServiceDeleteConflict:
            return _error_response(
                request,
                status_code=409,
                code="service_delete_conflict",
                message="服务仍在启用且后端实例未停止，服务没有被删除，请先停用服务后重试",
            )
        return Response(status_code=204)

    @router.patch("/{service_id}")
    @capability_route(Capability.MANAGE_AI_SERVICES)
    async def update_service(
        request: Request,
        service_id: UUID,
        body: UpdateAiServiceRequest,
    ) -> JSONResponse:
        record = await service.update(
            service_id,
            UpdateAiService(
                description=body.description,
                enabled=body.enabled,
                primary_instance_id=body.primary_instance_id,
            ),
            actor_id=current_authenticated_session(request).user.id,
            request_id=current_request_id(request),
        )
        return _response(request, await service.get_with_runtime_status(record.id))

    @router.post("/{service_id}:rename")
    @capability_route(Capability.MANAGE_AI_SERVICES)
    async def rename_service(
        request: Request, service_id: UUID, body: RenameAiServiceRequest
    ) -> JSONResponse:
        record = await service.rename(
            service_id,
            public_model_name=body.public_model_name,
            expected_route_version=body.expected_route_version,
            actor_id=current_authenticated_session(request).user.id,
            request_id=current_request_id(request),
        )
        return _response(request, await service.get_with_runtime_status(record.id))

    @router.post("/{service_id}:rename-display-name")
    @capability_route(Capability.MANAGE_AI_SERVICES)
    async def rename_display_name(
        request: Request, service_id: UUID, body: RenameAiServiceDisplayNameRequest
    ) -> JSONResponse:
        record = await service.rename_display_name(
            service_id,
            name=body.name,
            expected_route_version=body.expected_route_version,
            actor_id=current_authenticated_session(request).user.id,
            request_id=current_request_id(request),
        )
        return _response(request, await service.get_with_runtime_status(record.id))

    return router


__all__ = [
    "SERVICES_PATH",
    "AiServiceResponse",
    "CreateAiServiceRequest",
    "UpdateAiServiceRequest",
    "create_ai_service_router",
    "handle_ai_service_error",
]

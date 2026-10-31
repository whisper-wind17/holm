"""Authenticated user's own API Key HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Self
from uuid import UUID

from fastapi import APIRouter
from holm_common import as_utc
from holm_domain import ApiKeyStatus, Capability
from holm_persistence import ApiKeyRecord
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.requests import Request
from starlette.responses import JSONResponse

from holm_control_api.api_key_service import (
    ApiKeyActiveDeletion,
    ApiKeyExpirationInvalid,
    ApiKeyManagementService,
    ApiKeyNotFound,
    ApiKeyScopeNotAuthorized,
    ApiKeyScopeServiceNotFound,
    CreatePersonalApiKey,
)
from holm_control_api.auth_routes import json_response
from holm_control_api.authorization import (
    capability_route,
    current_authenticated_session,
    current_request_id,
)
from holm_control_api.model_catalog_routes import _error_response

API_KEYS_PATH: Final[str] = "/api/v1/me/api-keys"


class RenameApiKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Annotated[str, Field(min_length=2, max_length=40)]

    @field_validator("name")
    @classmethod
    def require_normalized_name(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("name must be normalized and nonblank")
        return value


class CreateApiKeyRequest(RenameApiKeyRequest):
    expires_at: datetime | None = None
    service_ids: tuple[UUID, ...] = ()

    @field_validator("expires_at")
    @classmethod
    def require_aware_expiry(cls, value: datetime | None) -> datetime | None:
        return None if value is None else as_utc(value)

    @model_validator(mode="after")
    def validate_normalized_fields(self) -> Self:
        if len(set(self.service_ids)) != len(self.service_ids):
            raise ValueError("service_ids must be unique")
        return self


class ApiKeyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    name: str
    display_prefix: str
    last4: str
    status: ApiKeyStatus
    expires_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime
    revoked_at: datetime | None
    service_ids: tuple[UUID, ...]

    @classmethod
    def from_record(cls, record: ApiKeyRecord, *, now: datetime) -> Self:
        return cls(
            id=record.id,
            name=record.name,
            display_prefix=record.display_prefix,
            last4=record.last4,
            status=record.effective_status_at(now),
            expires_at=record.expires_at,
            last_used_at=record.last_used_at,
            created_at=record.created_at,
            revoked_at=record.revoked_at,
            service_ids=record.service_ids,
        )


class EffectiveApiKeyServiceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    name: str
    public_model_name: str


class ListedApiKeyResponse(ApiKeyResponse):
    # This derived range never changes the stored Scope or intrinsic key status.
    effective_services: tuple[EffectiveApiKeyServiceResponse, ...]


class CreatedApiKeyResponse(ApiKeyResponse):
    api_key: str


async def handle_api_key_error(request: Request, exception: Exception) -> JSONResponse:
    if isinstance(exception, ApiKeyActiveDeletion):
        return _error_response(
            request,
            status_code=409,
            code="api_key_active",
            message="API 密钥仍然有效，显示记录未删除，请先撤销密钥",
        )
    if isinstance(exception, ApiKeyNotFound):
        return _error_response(
            request,
            status_code=404,
            code="api_key_not_found",
            message="API 密钥不存在，操作未执行，请刷新密钥列表",
        )
    if isinstance(exception, ApiKeyScopeServiceNotFound):
        return _error_response(
            request,
            status_code=400,
            code="invalid_request",
            message="API 密钥的服务范围无效，密钥未创建，请刷新服务列表",
        )
    if isinstance(exception, ApiKeyScopeNotAuthorized):
        return _error_response(
            request,
            status_code=403,
            code="service_not_authorized",
            message="未获得所选 AI 服务的权限，密钥未创建，请联系管理员授权",
        )
    if isinstance(exception, ApiKeyExpirationInvalid):
        return _error_response(
            request,
            status_code=400,
            code="invalid_request",
            message="API 密钥到期时间无效，密钥未创建，请选择未来时间",
        )
    raise exception


def create_api_key_router(service: ApiKeyManagementService) -> APIRouter:
    router = APIRouter(prefix=API_KEYS_PATH, tags=["api-keys"])

    @router.get("")
    @capability_route(Capability.MANAGE_API_KEYS)
    async def list_api_keys(request: Request) -> JSONResponse:
        user = current_authenticated_session(request).user
        now = await service.database_now()
        return json_response(
            request_id=str(current_request_id(request)),
            content={
                "items": [
                    ListedApiKeyResponse(
                        **ApiKeyResponse.from_record(item.record, now=now).model_dump(),
                        effective_services=tuple(
                            EffectiveApiKeyServiceResponse(
                                id=entry.id,
                                name=entry.name,
                                public_model_name=entry.public_model_name,
                            )
                            for entry in item.effective_services
                        ),
                    ).model_dump(mode="json")
                    for item in await service.list_with_effective_services(
                        user.id, user_role=user.role
                    )
                ]
            },
        )

    @router.post("", status_code=201)
    @capability_route(Capability.MANAGE_API_KEYS)
    async def create_api_key(request: Request, body: CreateApiKeyRequest) -> JSONResponse:
        user = current_authenticated_session(request).user
        created = await service.create(
            CreatePersonalApiKey(
                name=body.name,
                expires_at=body.expires_at,
                service_ids=body.service_ids,
            ),
            user_id=user.id,
            user_role=user.role,
            request_id=current_request_id(request),
        )
        payload = CreatedApiKeyResponse(
            **ApiKeyResponse.from_record(
                created.record,
                now=created.record.created_at,
            ).model_dump(),
            api_key=created.plaintext,
        )
        return json_response(
            status_code=201,
            request_id=str(current_request_id(request)),
            content=payload.model_dump(mode="json"),
        )

    @router.patch("/{key_id}")
    @capability_route(Capability.MANAGE_API_KEYS)
    async def rename_api_key(
        request: Request, key_id: UUID, body: RenameApiKeyRequest
    ) -> JSONResponse:
        record = await service.rename(
            key_id,
            name=body.name,
            user_id=current_authenticated_session(request).user.id,
            request_id=current_request_id(request),
        )
        return json_response(
            request_id=str(current_request_id(request)),
            content=ApiKeyResponse.from_record(record, now=await service.database_now()).model_dump(
                mode="json"
            ),
        )

    @router.delete("/{key_id}")
    @capability_route(Capability.MANAGE_API_KEYS)
    async def delete_api_key(request: Request, key_id: UUID) -> JSONResponse:
        record = await service.delete(
            key_id,
            user_id=current_authenticated_session(request).user.id,
            request_id=current_request_id(request),
        )
        return json_response(
            request_id=str(current_request_id(request)),
            content=ApiKeyResponse.from_record(record, now=await service.database_now()).model_dump(
                mode="json"
            ),
        )

    @router.post("/{key_id}:revoke")
    @capability_route(Capability.MANAGE_API_KEYS)
    async def revoke_api_key(request: Request, key_id: UUID) -> JSONResponse:
        record = await service.revoke(
            key_id,
            user_id=current_authenticated_session(request).user.id,
            request_id=current_request_id(request),
        )
        return json_response(
            request_id=str(current_request_id(request)),
            content=ApiKeyResponse.from_record(
                record,
                now=record.revoked_at or record.created_at,
            ).model_dump(mode="json"),
        )

    return router


__all__ = [
    "API_KEYS_PATH",
    "ApiKeyResponse",
    "CreateApiKeyRequest",
    "CreatedApiKeyResponse",
    "RenameApiKeyRequest",
    "create_api_key_router",
    "handle_api_key_error",
]

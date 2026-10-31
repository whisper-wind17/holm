"""HTTP contracts for administrator users and shared service grants."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Self
from uuid import UUID

from fastapi import APIRouter, Query
from holm_common.usernames import UsernameError
from holm_domain import ApiKeyStatus, Capability, UserRole, UserStatus
from holm_persistence import (
    AiServiceRecord,
    ApiKeyRecord,
    DuplicateUsernameError,
    UserOverviewRecord,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.requests import Request
from starlette.responses import JSONResponse

from holm_control_api.auth_routes import json_response
from holm_control_api.authorization import (
    authenticated_route,
    capability_route,
    current_authenticated_session,
    current_request_id,
)
from holm_control_api.model_catalog_routes import _error_response
from holm_control_api.user_management_service import (
    LastActiveAdministrator,
    PermissionTargetInvalid,
    PersonalAiService,
    ServicePermissionNotFound,
    UserDetail,
    UserInputInvalid,
    UserManagementService,
    UserNotFound,
    UserSelfProtection,
)

USERS_PATH: Final[str] = "/api/v1/users"
ME_SERVICES_PATH: Final[str] = "/api/v1/me/services"


class UserOverviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    username: str
    display_name: str
    role: UserRole
    status: UserStatus
    must_change_password: bool
    authorized_service_count: int
    api_key_count: int
    last_login_at: datetime | None

    @classmethod
    def from_record(cls, record: UserOverviewRecord) -> Self:
        return cls(
            id=record.user.id,
            username=record.user.username_norm,
            display_name=record.user.display_name,
            role=record.user.role,
            status=record.user.status,
            must_change_password=record.user.must_change_password,
            authorized_service_count=record.authorized_service_count,
            api_key_count=record.api_key_count,
            last_login_at=record.last_login_at,
        )


class CreateUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    username: Annotated[str, Field(min_length=3, max_length=32)]
    display_name: Annotated[str, Field(max_length=80)] = ""
    role: UserRole = UserRole.USER


class CreatedUserResponse(UserOverviewResponse):
    temporary_password: str


class ResetPasswordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    temporary_password: str
    must_change_password: bool


class SafeApiKeyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    name: str
    display_prefix: str
    last4: str
    status: ApiKeyStatus
    expires_at: datetime | None
    last_used_at: datetime | None

    @classmethod
    def from_record(cls, record: ApiKeyRecord, *, now: datetime) -> Self:
        return cls(
            id=record.id,
            name=record.name,
            expires_at=record.expires_at,
            last_used_at=record.last_used_at,
            display_prefix=record.display_prefix,
            last4=record.last4,
            status=record.effective_status_at(now),
        )


class PermissionLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_concurrent_requests: int | None = Field(default=None, ge=1, le=64, strict=True)
    max_requests_per_minute: int | None = Field(default=None, ge=1, le=600, strict=True)


class GrantedServiceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    name: str
    public_model_name: str
    description: str
    enabled: bool
    max_concurrent_requests: int | None = None
    max_requests_per_minute: int | None = None

    @classmethod
    def from_record(
        cls, record: AiServiceRecord, limits: tuple[int | None, int | None] = (None, None)
    ) -> Self:
        return cls(
            id=record.id,
            name=record.name,
            public_model_name=record.public_model_name,
            description=record.description,
            enabled=record.enabled,
            max_concurrent_requests=limits[0],
            max_requests_per_minute=limits[1],
        )


class UserDetailResponse(UserOverviewResponse):
    created_at: datetime
    authorized_services: tuple[GrantedServiceResponse, ...]
    api_keys: tuple[SafeApiKeyResponse, ...]

    @classmethod
    def from_detail(cls, detail: UserDetail) -> Self:
        return cls(
            **UserOverviewResponse.from_record(detail.overview).model_dump(),
            created_at=detail.overview.user.created_at,
            authorized_services=tuple(
                GrantedServiceResponse.from_record(
                    record, detail.limits.get(record.id, (None, None))
                )
                for record in detail.authorized_services
            ),
            api_keys=tuple(
                SafeApiKeyResponse.from_record(record, now=detail.database_now)
                for record in detail.api_keys
            ),
        )


class ReplaceUserPermissionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    service_ids: tuple[UUID, ...]
    limits: dict[UUID, PermissionLimits] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        if len(set(self.service_ids)) != len(self.service_ids):
            raise ValueError("service_ids must be unique")
        if not self.limits.keys() <= set(self.service_ids):
            raise ValueError("limits must reference granted IDs")
        return self


class ReplaceServicePermissionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_ids: tuple[UUID, ...]
    limits: dict[UUID, PermissionLimits] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        if len(set(self.user_ids)) != len(self.user_ids):
            raise ValueError("user_ids must be unique")
        if not self.limits.keys() <= set(self.user_ids):
            raise ValueError("limits must reference granted IDs")
        return self


class PersonalAiServiceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    public_model_name: str
    description: str
    available: bool
    enabled: bool
    id: UUID
    last_used_at: datetime | None
    last_succeeded_at: datetime | None

    @classmethod
    def from_record(cls, record: PersonalAiService) -> Self:
        return cls(
            name=record.name,
            public_model_name=record.public_model_name,
            description=record.description,
            available=record.available,
            enabled=record.enabled,
            id=record.id,
            last_used_at=record.last_used_at,
            last_succeeded_at=record.last_succeeded_at,
        )


class PersonalProfileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    username: str
    display_name: str
    role: UserRole
    current_login_at: datetime
    previous_login_at: datetime | None
    created_at: datetime
    authorized_service_count: int


class UpdatePersonalProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str


async def handle_user_management_error(request: Request, exception: Exception) -> JSONResponse:
    if isinstance(exception, UserNotFound):
        return _error_response(
            request,
            status_code=404,
            code="user_not_found",
            message="用户不存在，操作未执行，请刷新用户列表",
        )
    if isinstance(exception, DuplicateUsernameError):
        return _error_response(
            request,
            status_code=409,
            code="username_conflict",
            message="用户名已被使用，用户未创建，请更换用户名",
        )
    if isinstance(exception, UserSelfProtection):
        return _error_response(
            request,
            status_code=409,
            code="self_protection",
            message="不能停用或删除当前账号，本次操作未执行，请使用其他管理员账号",
        )
    if isinstance(exception, LastActiveAdministrator):
        return _error_response(
            request,
            status_code=409,
            code="last_active_administrator",
            message="系统必须保留一个正常管理员，本次操作未执行，请先创建或启用其他管理员",
        )
    if isinstance(exception, PermissionTargetInvalid):
        return _error_response(
            request,
            status_code=400,
            code="invalid_request",
            message="授权目标无效，权限未更新，请刷新用户和服务列表",
        )
    if isinstance(exception, ServicePermissionNotFound):
        return _error_response(
            request,
            status_code=404,
            code="service_not_found",
            message="AI 服务不存在，访问权限无法显示，请刷新服务列表",
        )
    if isinstance(exception, (UsernameError, UserInputInvalid)):
        return _error_response(
            request,
            status_code=400,
            code="invalid_request",
            message="用户信息无效，操作未执行，请检查后重试",
        )
    raise exception


def _overview_response(request: Request, record: UserOverviewRecord) -> JSONResponse:
    return json_response(
        request_id=str(current_request_id(request)),
        content=UserOverviewResponse.from_record(record).model_dump(mode="json"),
    )


def create_user_management_router(service: UserManagementService) -> APIRouter:
    router = APIRouter(tags=["users"])

    @router.get(USERS_PATH)
    @capability_route(Capability.MANAGE_USERS)
    async def list_users(
        request: Request,
        search: Annotated[str | None, Query(max_length=100)] = None,
        role: UserRole | None = None,
        status: UserStatus | None = None,
        include_deleted: bool = False,
    ) -> JSONResponse:
        records = await service.list_users(
            search=search,
            role=role,
            status=status,
            include_deleted=include_deleted,
        )
        return json_response(
            request_id=str(current_request_id(request)),
            content={
                "items": [
                    UserOverviewResponse.from_record(record).model_dump(mode="json")
                    for record in records
                ]
            },
        )

    @router.post(USERS_PATH, status_code=201)
    @capability_route(Capability.MANAGE_USERS)
    async def create_user(request: Request, body: CreateUserRequest) -> JSONResponse:
        actor_id = current_authenticated_session(request).user.id
        created = await service.create_user(
            username=body.username,
            display_name=body.display_name,
            role=body.role,
            actor_id=actor_id,
            request_id=current_request_id(request),
        )
        payload = CreatedUserResponse(
            **UserOverviewResponse.from_record(created.overview).model_dump(),
            temporary_password=created.temporary_password,
        )
        return json_response(
            status_code=201,
            request_id=str(current_request_id(request)),
            content=payload.model_dump(mode="json"),
        )

    @router.get(f"{USERS_PATH}/{{user_id}}")
    @capability_route(Capability.MANAGE_USERS)
    async def get_user(request: Request, user_id: UUID) -> JSONResponse:
        return json_response(
            request_id=str(current_request_id(request)),
            content=UserDetailResponse.from_detail(await service.get_user(user_id)).model_dump(
                mode="json"
            ),
        )

    @router.get(f"{USERS_PATH}/{{user_id}}/activity")
    @capability_route(Capability.MANAGE_USERS)
    async def get_user_activity(
        request: Request,
        user_id: UUID,
        action: Annotated[list[str] | None, Query()] = None,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> JSONResponse:
        return json_response(
            request_id=str(current_request_id(request)),
            content=await service.get_user_activity(
                user_id,
                actions=tuple(action or ()),
                cursor=cursor,
                limit=limit,
            ),
        )

    @router.post(f"{USERS_PATH}/{{user_id}}:disable")
    @capability_route(Capability.MANAGE_USERS)
    async def disable_user(request: Request, user_id: UUID) -> JSONResponse:
        return _overview_response(
            request,
            await service.disable_user(
                user_id,
                actor_id=current_authenticated_session(request).user.id,
                request_id=current_request_id(request),
            ),
        )

    @router.post(f"{USERS_PATH}/{{user_id}}:enable")
    @capability_route(Capability.MANAGE_USERS)
    async def enable_user(request: Request, user_id: UUID) -> JSONResponse:
        return _overview_response(
            request,
            await service.enable_user(
                user_id,
                actor_id=current_authenticated_session(request).user.id,
                request_id=current_request_id(request),
            ),
        )

    @router.post(f"{USERS_PATH}/{{user_id}}/api-keys/{{key_id}}:revoke")
    @capability_route(Capability.MANAGE_USERS)
    async def revoke_user_api_key(request: Request, user_id: UUID, key_id: UUID) -> JSONResponse:
        revoked = await service.revoke_user_api_key(
            user_id,
            key_id,
            actor_id=current_authenticated_session(request).user.id,
            request_id=current_request_id(request),
        )
        return json_response(
            request_id=str(current_request_id(request)),
            content=SafeApiKeyResponse.from_record(
                revoked.record, now=revoked.database_now
            ).model_dump(mode="json"),
        )

    @router.post(f"{USERS_PATH}/{{user_id}}:reset-password")
    @capability_route(Capability.MANAGE_USERS)
    async def reset_password(request: Request, user_id: UUID) -> JSONResponse:
        reset = await service.reset_password(
            user_id,
            actor_id=current_authenticated_session(request).user.id,
            request_id=current_request_id(request),
        )
        return json_response(
            request_id=str(current_request_id(request)),
            content=ResetPasswordResponse(
                temporary_password=reset.temporary_password,
                must_change_password=reset.overview.user.must_change_password,
            ).model_dump(mode="json"),
        )

    @router.post(f"{USERS_PATH}/{{user_id}}:delete")
    @capability_route(Capability.MANAGE_USERS)
    async def delete_user(request: Request, user_id: UUID) -> JSONResponse:
        return _overview_response(
            request,
            await service.delete_user(
                user_id,
                actor_id=current_authenticated_session(request).user.id,
                request_id=current_request_id(request),
            ),
        )

    @router.put(f"{USERS_PATH}/{{user_id}}/permissions")
    @capability_route(Capability.MANAGE_USERS)
    async def replace_user_permissions(
        request: Request,
        user_id: UUID,
        body: ReplaceUserPermissionsRequest,
    ) -> JSONResponse:
        records = await service.replace_user_permissions(
            user_id,
            body.service_ids,
            limits={
                key: value.model_dump(exclude_unset=True) for key, value in body.limits.items()
            },
            actor_id=current_authenticated_session(request).user.id,
            request_id=current_request_id(request),
        )
        return json_response(
            request_id=str(current_request_id(request)),
            content={
                "service_ids": [str(record.id) for record in records],
            },
        )

    @router.get("/api/v1/services/{service_id}/permissions")
    @capability_route(Capability.MANAGE_USERS)
    async def get_service_permissions(request: Request, service_id: UUID) -> JSONResponse:
        limits = await service.service_permission_limits(service_id)
        user_ids = tuple(limits)
        return json_response(
            request_id=str(current_request_id(request)),
            content={
                "user_ids": [str(user_id) for user_id in user_ids],
                "limits": {
                    str(key): {
                        "max_concurrent_requests": value[0],
                        "max_requests_per_minute": value[1],
                    }
                    for key, value in limits.items()
                },
            },
        )

    @router.put("/api/v1/services/{service_id}/permissions")
    @capability_route(Capability.MANAGE_USERS)
    async def replace_service_permissions(
        request: Request,
        service_id: UUID,
        body: ReplaceServicePermissionsRequest,
    ) -> JSONResponse:
        user_ids = await service.replace_service_permissions(
            service_id,
            body.user_ids,
            limits={
                key: value.model_dump(exclude_unset=True) for key, value in body.limits.items()
            },
            actor_id=current_authenticated_session(request).user.id,
            request_id=current_request_id(request),
        )
        limits = await service.service_permission_limits(service_id)
        return json_response(
            request_id=str(current_request_id(request)),
            content={
                "user_ids": [str(user_id) for user_id in user_ids],
                "limits": {
                    str(key): {
                        "max_concurrent_requests": value[0],
                        "max_requests_per_minute": value[1],
                    }
                    for key, value in limits.items()
                },
            },
        )

    @router.get(ME_SERVICES_PATH)
    @capability_route(Capability.VIEW_AI_SERVICES)
    async def list_my_services(request: Request) -> JSONResponse:
        user = current_authenticated_session(request).user
        records = await service.list_personal_services(user_id=user.id, role=user.role)
        return json_response(
            request_id=str(current_request_id(request)),
            content={
                "items": [
                    PersonalAiServiceResponse.from_record(record).model_dump(mode="json")
                    for record in records
                ]
            },
        )

    @router.get("/api/v1/me/profile", response_model=PersonalProfileResponse)
    @authenticated_route
    async def get_my_profile(request: Request) -> JSONResponse:
        authenticated = current_authenticated_session(request)
        profile = await service.personal_profile(
            user_id=authenticated.user.id,
            current_login_at=authenticated.session.created_at,
        )
        return json_response(
            request_id=str(current_request_id(request)),
            content=PersonalProfileResponse.model_validate(profile).model_dump(mode="json"),
        )

    @router.patch("/api/v1/me/profile", response_model=PersonalProfileResponse)
    @authenticated_route
    async def update_my_profile(
        request: Request, body: UpdatePersonalProfileRequest
    ) -> JSONResponse:
        authenticated = current_authenticated_session(request)
        profile = await service.personal_profile(
            user_id=authenticated.user.id,
            current_login_at=authenticated.session.created_at,
            display_name=body.display_name,
            request_id=current_request_id(request),
        )
        return json_response(
            request_id=str(current_request_id(request)),
            content=PersonalProfileResponse.model_validate(profile).model_dump(mode="json"),
        )

    return router


__all__ = [
    "ME_SERVICES_PATH",
    "USERS_PATH",
    "create_user_management_router",
    "handle_user_management_error",
]

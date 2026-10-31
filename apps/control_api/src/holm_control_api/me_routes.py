"""Current-subject contract and server-derived navigation."""

from __future__ import annotations

from typing import Final
from uuid import UUID

from fastapi import APIRouter
from holm_common import new_uuid7
from holm_domain import (
    Capability,
    NavigationId,
    PermissionScope,
    UserRole,
    granted_capabilities,
    navigation_for,
    permission_scope,
)
from pydantic import BaseModel, ConfigDict
from starlette.requests import Request
from starlette.responses import JSONResponse

from holm_control_api.authorization import (
    current_authenticated_session,
    password_change_exempt_route,
)
from holm_control_api.http_constants import CACHE_CONTROL_VALUE

ME_PATH: Final[str] = "/api/v1/me"


class _ClosedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CurrentUserResponse(_ClosedResponse):
    id: UUID
    username: str
    display_name: str


class CapabilityResponse(_ClosedResponse):
    capability: Capability
    scope: PermissionScope


class NavigationResponse(_ClosedResponse):
    id: NavigationId
    label: str
    required_capability: Capability


class MeResponse(_ClosedResponse):
    user: CurrentUserResponse
    role: UserRole
    must_change_password: bool
    capabilities: tuple[CapabilityResponse, ...]
    navigation: tuple[NavigationResponse, ...]


def create_me_router() -> APIRouter:
    router = APIRouter(tags=["identity"])

    @router.get(ME_PATH, response_model=MeResponse)
    @password_change_exempt_route
    async def read_me(request: Request) -> JSONResponse:
        authenticated = current_authenticated_session(request)
        user = authenticated.user
        payload = MeResponse(
            user=CurrentUserResponse(
                id=user.id,
                username=user.username_norm,
                display_name=user.display_name,
            ),
            role=user.role,
            must_change_password=user.must_change_password,
            capabilities=tuple(
                CapabilityResponse(
                    capability=capability,
                    scope=permission_scope(user.role, capability),
                )
                for capability in granted_capabilities(user.role)
            ),
            navigation=tuple(
                NavigationResponse(
                    id=item.id,
                    label=item.label,
                    required_capability=item.required_capability,
                )
                for item in navigation_for(user.role)
            ),
        )
        return JSONResponse(
            content=payload.model_dump(mode="json"),
            headers={
                "Cache-Control": CACHE_CONTROL_VALUE,
                "x-request-id": str(new_uuid7()),
            },
        )

    return router


__all__ = [
    "ME_PATH",
    "CapabilityResponse",
    "CurrentUserResponse",
    "MeResponse",
    "NavigationResponse",
    "create_me_router",
]

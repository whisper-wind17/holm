"""Administrator HTTP contract for Master Key credential rotation."""

from __future__ import annotations

from typing import Final, Self

from fastapi import APIRouter, Response
from holm_domain import Capability
from holm_security import MasterKeyRotationPhase
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette.requests import Request
from starlette.responses import JSONResponse

from holm_control_api.auth_routes import json_response
from holm_control_api.authorization import (
    capability_route,
    current_authenticated_session,
    current_request_id,
)
from holm_control_api.http_constants import (
    CACHE_CONTROL_VALUE,
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
)
from holm_control_api.master_key_rotation_service import (
    MasterKeyRotationAdvanceResult,
    MasterKeyRotationOperationService,
    MasterKeyRotationSnapshot,
    MasterKeyRotationStatus,
    MasterKeyRotationUnavailable,
)

MASTER_KEY_ROTATION_PATH: Final[str] = "/api/v1/system/master-key-rotation"
MASTER_KEY_ROTATION_ADVANCE_PATH: Final[str] = "/api/v1/system/master-key-rotation:advance"


class MasterKeyRotationAdvanceRequest(BaseModel):
    """Closed command Schema: it accepts a password, never Key configuration."""

    model_config = ConfigDict(extra="forbid")

    current_password: SecretStr | None = Field(default=None, min_length=1, max_length=4096)


class MasterKeyRotationResponse(BaseModel):
    """Safe derived state without Key material or persisted progress fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: MasterKeyRotationStatus
    phase: MasterKeyRotationPhase | None
    writable_key_id: str | None
    old_key_id: str | None
    referenced_rows: int
    scanned_rows: int

    @classmethod
    def from_snapshot(cls, value: MasterKeyRotationSnapshot) -> Self:
        return cls(
            status=value.status,
            phase=value.phase,
            writable_key_id=value.writable_key_id,
            old_key_id=value.old_key_id,
            referenced_rows=value.referenced_rows,
            scanned_rows=value.scanned_rows,
        )


class MasterKeyRotationAdvanceResponse(MasterKeyRotationResponse):
    reencrypted_rows: int

    @classmethod
    def from_result(cls, value: MasterKeyRotationAdvanceResult) -> Self:
        return cls(
            **MasterKeyRotationResponse.from_snapshot(value.state).model_dump(),
            reencrypted_rows=value.reencrypted_rows,
        )


async def handle_master_key_rotation_unavailable(
    request: Request,
    exception: Exception,
) -> JSONResponse:
    """Render only a stable classification and the request correlation ID."""

    if not isinstance(exception, MasterKeyRotationUnavailable):
        raise exception
    request_id = str(current_request_id(request))
    return json_response(
        status_code=503,
        request_id=request_id,
        content={
            "error": {
                "code": exception.code,
                "message": "Master Key 轮换状态当前不可用，本次操作未执行，请检查密钥配置后重试",
                "details": {},
                "request_id": request_id,
            }
        },
    )


def create_master_key_rotation_router(
    service: MasterKeyRotationOperationService,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/system", tags=["system"])

    @router.get("/master-key-rotation", response_model=MasterKeyRotationResponse)
    @capability_route(Capability.VIEW_SYSTEM)
    async def read_master_key_rotation(response: Response) -> MasterKeyRotationResponse:
        response.headers["Cache-Control"] = CACHE_CONTROL_VALUE
        return MasterKeyRotationResponse.from_snapshot(await service.inspect())

    @router.post(
        "/master-key-rotation:advance",
        response_model=MasterKeyRotationAdvanceResponse,
    )
    @capability_route(Capability.VIEW_SYSTEM)
    async def advance_master_key_rotation(
        request: Request,
        response: Response,
        body: MasterKeyRotationAdvanceRequest,
    ) -> MasterKeyRotationAdvanceResponse:
        response.headers["Cache-Control"] = CACHE_CONTROL_VALUE
        authenticated = current_authenticated_session(request)
        result = await service.advance(
            actor_id=authenticated.user.id,
            session_token=request.cookies.get(SESSION_COOKIE_NAME),
            csrf_token=request.headers.get(CSRF_HEADER_NAME),
            current_password=(
                None if body.current_password is None else body.current_password.get_secret_value()
            ),
            request_id=current_request_id(request),
        )
        return MasterKeyRotationAdvanceResponse.from_result(result)

    return router


__all__ = [
    "MASTER_KEY_ROTATION_ADVANCE_PATH",
    "MASTER_KEY_ROTATION_PATH",
    "MasterKeyRotationAdvanceRequest",
    "MasterKeyRotationAdvanceResponse",
    "MasterKeyRotationResponse",
    "create_master_key_rotation_router",
    "handle_master_key_rotation_unavailable",
]

"""Administrator Model Source credential HTTP contract."""

from __future__ import annotations

from datetime import datetime
from typing import Final, Self

from fastapi import APIRouter, Response
from holm_domain import (
    Capability,
    ModelSourceConfigurationState,
    ModelSourceType,
    ModelSourceValidationAction,
    ModelSourceValidationReason,
    ModelSourceValidationStatus,
)
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
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
from holm_control_api.model_source_service import (
    ModelSourceCredentialConflict,
    ModelSourceCredentialNotSaved,
    ModelSourceCredentialService,
    ModelSourcePresentation,
)

MODEL_SOURCES_PATH: Final[str] = "/api/v1/system/model-sources"


class ModelSourceCredentialRequest(BaseModel):
    """Closed write Schema with no address-like field."""

    model_config = ConfigDict(extra="forbid")

    source_type: ModelSourceType
    credential: SecretStr | None = Field(default=None, min_length=1, max_length=4096)
    clear: bool = False
    confirm_unverified: bool = Field(default=False, strict=True)
    current_password: SecretStr | None = Field(default=None, min_length=1, max_length=4096)

    @model_validator(mode="after")
    def require_exactly_one_credential_operation(self) -> Self:
        if self.clear == (self.credential is not None):
            raise ValueError("provide a credential or request clear")
        return self


class ModelSourceResponse(BaseModel):
    """Safe response vocabulary; IDs, Key fields, and Endpoints are absent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_type: ModelSourceType
    configuration_state: ModelSourceConfigurationState
    credential_last4: str | None
    verification_status: ModelSourceValidationStatus | None
    verified_at: datetime | None
    verification_reason_code: ModelSourceValidationReason | None
    next_action: ModelSourceValidationAction | None

    @classmethod
    def from_presentation(cls, value: ModelSourcePresentation) -> Self:
        return cls(
            source_type=value.source_type,
            configuration_state=value.configuration_state,
            credential_last4=value.credential_last4,
            verification_status=value.verification_status,
            verified_at=value.verified_at,
            verification_reason_code=value.verification_reason_code,
            next_action=value.next_action,
        )


async def handle_model_source_conflict(
    request: Request,
    exception: Exception,
) -> JSONResponse:
    """Render a stable conflict without exposing the competing envelope."""

    if not isinstance(exception, ModelSourceCredentialConflict):
        raise exception
    request_id = str(current_request_id(request))
    return json_response(
        status_code=409,
        request_id=request_id,
        content={
            "error": {
                "code": "credential_update_conflict",
                "message": "模型源凭证已被其他操作更新，本次操作未执行，请刷新后重试",
                "details": {},
                "request_id": request_id,
            }
        },
    )


def create_model_source_router(service: ModelSourceCredentialService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/system", tags=["system"])

    @router.get("/model-sources", response_model=list[ModelSourceResponse])
    @capability_route(Capability.VIEW_SYSTEM)
    async def read_model_sources(response: Response) -> list[ModelSourceResponse]:
        response.headers["Cache-Control"] = CACHE_CONTROL_VALUE
        return [ModelSourceResponse.from_presentation(item) for item in await service.read_all()]

    @router.put("/model-sources", response_model=ModelSourceResponse)
    @capability_route(Capability.VIEW_SYSTEM)
    async def update_model_source(
        request: Request,
        response: Response,
        body: ModelSourceCredentialRequest,
    ) -> ModelSourceResponse | JSONResponse:
        response.headers["Cache-Control"] = CACHE_CONTROL_VALUE
        authenticated = current_authenticated_session(request)
        try:
            presentation = await service.update(
                actor_id=authenticated.user.id,
                session_token=request.cookies.get(SESSION_COOKIE_NAME),
                csrf_token=request.headers.get(CSRF_HEADER_NAME),
                current_password=(
                    None
                    if body.current_password is None
                    else body.current_password.get_secret_value()
                ),
                source_type=body.source_type,
                credential=(
                    None if body.credential is None else body.credential.get_secret_value()
                ),
                clear=body.clear,
                confirm_unverified=body.confirm_unverified,
                request_id=current_request_id(request),
            )
        except ModelSourceCredentialNotSaved as failure:
            request_id = str(current_request_id(request))
            return json_response(
                status_code=422 if failure.rejected else 409,
                request_id=request_id,
                content={
                    "error": {
                        "code": (
                            "model_source_credential_rejected"
                            if failure.rejected
                            else "model_source_validation_unavailable"
                        ),
                        "message": (
                            "新凭证未生效，下载仍使用原凭证，请检查后重新输入"
                            if failure.rejected
                            else (
                                "暂时无法验证新凭证，尚未保存；"
                                "如需仍然保存（未验证），请重新输入管理员密码并确认"
                            )
                        ),
                        "details": {"reason_code": failure.reason.value},
                        "request_id": request_id,
                    }
                },
            )
        return ModelSourceResponse.from_presentation(presentation)

    return router


__all__ = [
    "MODEL_SOURCES_PATH",
    "ModelSourceCredentialRequest",
    "ModelSourceResponse",
    "create_model_source_router",
    "handle_model_source_conflict",
]

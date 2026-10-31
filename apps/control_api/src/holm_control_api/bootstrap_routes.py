"""HTTP boundary for the three Bootstrap endpoints.

The rendering rules that matter here:

*   an initialized platform answers **404** on all three paths (initialization
    names the creation endpoint; ``check`` and ``status`` are closed with it
    because ``check`` hands host CPU/memory/disk/GPU/driver facts to an
    unauthenticated caller and ``status`` would otherwise be a probe for
    "was this machine ever set up?");
*   every Token refusal renders one identical body, so the four internal reasons
    are invisible from outside;
*   a successful creation renders through the *existing* logged-in response
    helper, so its cookies and CSRF delivery cannot drift from login's.
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from holm_control_api.auth_routes import issued_session_response, json_response
from holm_control_api.authorization import current_request_id, public_route
from holm_control_api.bootstrap_service import (
    BOOTSTRAP_BLOCKED_CODE,
    BOOTSTRAP_BLOCKED_MESSAGE,
    BOOTSTRAP_DENIED_CODE,
    BOOTSTRAP_DENIED_MESSAGE,
    BOOTSTRAP_INVALID_PASSWORD_CODE,
    BOOTSTRAP_INVALID_PASSWORD_MESSAGE,
    BOOTSTRAP_INVALID_USERNAME_CODE,
    BOOTSTRAP_INVALID_USERNAME_MESSAGE,
    BOOTSTRAP_NOT_FOUND_CODE,
    BOOTSTRAP_NOT_FOUND_MESSAGE,
    BOOTSTRAP_TOKEN_HEADER,
    BOOTSTRAP_USERNAME_TAKEN_CODE,
    BOOTSTRAP_USERNAME_TAKEN_MESSAGE,
    BootstrapBlocked,
    BootstrapClosed,
    BootstrapCreated,
    BootstrapDenied,
    BootstrapInvalidPassword,
    BootstrapInvalidUsername,
    BootstrapService,
    BootstrapUsernameTaken,
)

BOOTSTRAP_STATUS_PATH: Final[str] = "/api/v1/bootstrap/status"
BOOTSTRAP_CHECK_PATH: Final[str] = "/api/v1/bootstrap/check"
BOOTSTRAP_ADMIN_PATH: Final[str] = "/api/v1/bootstrap/admin"
BOOTSTRAP_PATHS: Final[tuple[str, ...]] = (
    BOOTSTRAP_STATUS_PATH,
    BOOTSTRAP_CHECK_PATH,
    BOOTSTRAP_ADMIN_PATH,
)
BOOTSTRAP_NOT_FOUND_STATUS: Final[int] = 404
BOOTSTRAP_DENIED_STATUS: Final[int] = 401
BOOTSTRAP_BLOCKED_STATUS: Final[int] = 409
BOOTSTRAP_INVALID_INPUT_STATUS: Final[int] = 400
BOOTSTRAP_USERNAME_TAKEN_STATUS: Final[int] = 409


class BootstrapCheckRequest(BaseModel):
    """An empty, closed body.

    ``extra="forbid"`` is the enforcement point for "the server does not believe
    the client's detection result": there is no field to put one in, and adding
    one would be visible in the request contract rather than buried in a handler.
    """

    model_config = ConfigDict(extra="forbid")


class BootstrapAdminRequest(BaseModel):
    """Initialization configuration. Deliberately carries no detection result of any kind."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=640)
    display_name: str = Field(default="", max_length=1280)
    password: str = Field(min_length=1, max_length=4096)
    confirm_password: str = Field(min_length=1, max_length=4096)


def _error(
    *,
    status_code: int,
    code: str,
    message: str,
    request_id: str,
    details: dict[str, object] | None = None,
) -> JSONResponse:
    return json_response(
        status_code=status_code,
        request_id=request_id,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": {} if details is None else details,
                "request_id": request_id,
            }
        },
    )


def bootstrap_not_found(request_id: str) -> JSONResponse:
    """The response every Bootstrap path gives once the platform is initialized."""
    return _error(
        status_code=BOOTSTRAP_NOT_FOUND_STATUS,
        code=BOOTSTRAP_NOT_FOUND_CODE,
        message=BOOTSTRAP_NOT_FOUND_MESSAGE,
        request_id=request_id,
    )


def bootstrap_denied(request_id: str) -> JSONResponse:
    """The single refusal shared by missing, unknown, expired and spent Tokens."""
    return _error(
        status_code=BOOTSTRAP_DENIED_STATUS,
        code=BOOTSTRAP_DENIED_CODE,
        message=BOOTSTRAP_DENIED_MESSAGE,
        request_id=request_id,
    )


def create_bootstrap_router(service: BootstrapService) -> APIRouter:
    """Build the three Bootstrap endpoints."""
    router = APIRouter(tags=["bootstrap"])

    @router.get(BOOTSTRAP_STATUS_PATH)
    @public_route
    async def read_status(request: Request) -> JSONResponse:
        request_id = current_request_id(request)
        outcome = await service.read_status(
            token=request.headers.get(BOOTSTRAP_TOKEN_HEADER),
            request_id=request_id,
        )
        if isinstance(outcome, BootstrapClosed):
            return bootstrap_not_found(str(request_id))
        if isinstance(outcome, BootstrapDenied):
            return bootstrap_denied(str(request_id))
        return json_response(
            content=outcome.model_dump(mode="json"),
            request_id=str(request_id),
        )

    @router.post(BOOTSTRAP_CHECK_PATH)
    @public_route
    async def run_checks(request: Request, body: BootstrapCheckRequest) -> JSONResponse:
        del body
        request_id = current_request_id(request)
        outcome = await service.run_checks(
            token=request.headers.get(BOOTSTRAP_TOKEN_HEADER),
            request_id=request_id,
        )
        if isinstance(outcome, BootstrapClosed):
            return bootstrap_not_found(str(request_id))
        if isinstance(outcome, BootstrapDenied):
            return bootstrap_denied(str(request_id))
        return json_response(
            content=outcome.model_dump(mode="json"),
            request_id=str(request_id),
        )

    @router.post(BOOTSTRAP_ADMIN_PATH)
    @public_route
    async def create_administrator(
        request: Request,
        body: BootstrapAdminRequest,
    ) -> JSONResponse:
        request_id = current_request_id(request)
        if body.password != body.confirm_password:
            return _error(
                status_code=BOOTSTRAP_INVALID_INPUT_STATUS,
                code="password_confirmation_mismatch",
                message="两次输入的密码不一致，管理员未创建，请重新输入",
                request_id=str(request_id),
            )
        outcome = await service.create_first_administrator(
            token=request.headers.get(BOOTSTRAP_TOKEN_HEADER),
            username=body.username,
            display_name=body.display_name,
            password=body.password,
            request_id=request_id,
        )
        if isinstance(outcome, BootstrapClosed):
            return bootstrap_not_found(str(request_id))
        if isinstance(outcome, BootstrapDenied):
            return bootstrap_denied(str(request_id))
        if isinstance(outcome, BootstrapBlocked):
            return _error(
                status_code=BOOTSTRAP_BLOCKED_STATUS,
                code=BOOTSTRAP_BLOCKED_CODE,
                message=BOOTSTRAP_BLOCKED_MESSAGE,
                request_id=str(request_id),
                details={"blocking": [identifier.value for identifier in outcome.blocking]},
            )
        if isinstance(outcome, BootstrapInvalidUsername):
            return _error(
                status_code=BOOTSTRAP_INVALID_INPUT_STATUS,
                code=BOOTSTRAP_INVALID_USERNAME_CODE,
                message=BOOTSTRAP_INVALID_USERNAME_MESSAGE,
                request_id=str(request_id),
                details={"reason_code": outcome.code},
            )
        if isinstance(outcome, BootstrapUsernameTaken):
            return _error(
                status_code=BOOTSTRAP_USERNAME_TAKEN_STATUS,
                code=BOOTSTRAP_USERNAME_TAKEN_CODE,
                message=BOOTSTRAP_USERNAME_TAKEN_MESSAGE,
                request_id=str(request_id),
            )
        if isinstance(outcome, BootstrapInvalidPassword):
            return _error(
                status_code=BOOTSTRAP_INVALID_INPUT_STATUS,
                code=BOOTSTRAP_INVALID_PASSWORD_CODE,
                message=BOOTSTRAP_INVALID_PASSWORD_MESSAGE,
                request_id=str(request_id),
                details={
                    "password_policy": [violation.as_dict() for violation in outcome.violations]
                },
            )
        return _created_response(outcome, request_id=str(request_id))

    return router


def _created_response(outcome: BootstrapCreated, *, request_id: str) -> JSONResponse:
    """Reuse login's logged-in response, then add the one bootstrap-only field.

    ``issued_session_response`` owns the cookie attributes and the CSRF
    delivery, so the session handed out here is the same one login hands out.
    """
    return issued_session_response(
        outcome.issued,
        request_id=request_id,
        extra_fields={"system_initialized_at": outcome.system_initialized_at.isoformat()},
    )


__all__ = [
    "BOOTSTRAP_ADMIN_PATH",
    "BOOTSTRAP_BLOCKED_STATUS",
    "BOOTSTRAP_CHECK_PATH",
    "BOOTSTRAP_DENIED_STATUS",
    "BOOTSTRAP_INVALID_INPUT_STATUS",
    "BOOTSTRAP_NOT_FOUND_STATUS",
    "BOOTSTRAP_PATHS",
    "BOOTSTRAP_STATUS_PATH",
    "BOOTSTRAP_USERNAME_TAKEN_STATUS",
    "BootstrapAdminRequest",
    "BootstrapCheckRequest",
    "bootstrap_denied",
    "bootstrap_not_found",
    "create_bootstrap_router",
]

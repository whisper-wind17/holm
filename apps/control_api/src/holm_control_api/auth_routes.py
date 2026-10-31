"""HTTP boundary for login, logout, password changes, Cookie and CSRF."""

from __future__ import annotations

from typing import Any, Final

from fastapi import APIRouter
from fastapi.exceptions import RequestValidationError
from holm_common import new_uuid7
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from holm_control_api.authentication import (
    SESSION_ABSOLUTE_TIMEOUT,
    AuthenticationFailure,
    AuthenticationService,
    IssuedSession,
)
from holm_control_api.authorization import (
    current_request_id,
    password_change_exempt_route,
    public_route,
)
from holm_control_api.http_constants import (
    CACHE_CONTROL_VALUE,
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
)

LOGIN_PATH: Final[str] = "/api/v1/auth/login"
LOGOUT_PATH: Final[str] = "/api/v1/auth/logout"
PASSWORD_PATH: Final[str] = "/api/v1/auth/password"


class _ClosedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(_ClosedRequest):
    username: str = Field(min_length=1, max_length=640)
    password: str = Field(min_length=1, max_length=4096)


class PasswordChangeRequest(_ClosedRequest):
    current_password: str | None = Field(default=None, min_length=1, max_length=4096)
    new_password: str = Field(min_length=1, max_length=4096)
    confirm_password: str = Field(min_length=1, max_length=4096)


def json_response(
    *,
    content: dict[str, Any],
    status_code: int = 200,
    request_id: str | None = None,
) -> JSONResponse:
    """Render one JSON body with the fixed cache policy and a correlation ID."""
    correlation_id = str(new_uuid7()) if request_id is None else request_id
    return JSONResponse(
        status_code=status_code,
        content=content,
        headers={
            "Cache-Control": CACHE_CONTROL_VALUE,
            "x-request-id": correlation_id,
        },
    )


def set_session_cookies(response: JSONResponse, issued: IssuedSession) -> None:
    """The single definition of the two session cookies and their attributes.

    Every producer of a logged-in response goes through here, so a second
    producer cannot drift into a weaker cookie by copying five attributes and
    forgetting the sixth.
    """
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=issued.token_pair.session_token,
        max_age=int(SESSION_ABSOLUTE_TIMEOUT.total_seconds()),
        secure=True,
        httponly=True,
        samesite="strict",
        path="/",
        # Deliberately no Domain: __Host- cookies are rejected by browsers if
        # Domain is present, even if every other attribute is correct.
    )
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=issued.token_pair.csrf_token,
        max_age=int(SESSION_ABSOLUTE_TIMEOUT.total_seconds()),
        secure=True,
        # The browser client must read this delivery copy and echo it in the
        # header; server-side validation still uses the Session-bound hash.
        httponly=False,
        samesite="strict",
        path="/",
    )


def expire_session_cookies(response: JSONResponse) -> None:
    """Clear both session cookies with the attributes the browser matched on."""
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value="",
        max_age=0,
        expires=0,
        secure=True,
        httponly=True,
        samesite="strict",
        path="/",
    )
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value="",
        max_age=0,
        expires=0,
        secure=True,
        httponly=False,
        samesite="strict",
        path="/",
    )


def issued_session_response(
    issued: IssuedSession,
    *,
    request_id: str,
    extra_fields: dict[str, Any] | None = None,
) -> JSONResponse:
    """The one shape a successful, logged-in response has.

    ``extra_fields`` lets a second producer add its own facts (the authentication flow returns
    ``system_initialized_at``) without reproducing the CSRF delivery or the
    cookie attributes, which stay decided here.
    """
    content: dict[str, Any] = {
        "csrf_token": issued.token_pair.csrf_token,
        "must_change_password": issued.user.must_change_password,
    }
    if extra_fields:
        content.update(extra_fields)
    response = json_response(content=content, request_id=request_id)
    set_session_cookies(response, issued)
    return response


async def handle_authentication_failure(
    request: Request,
    exception: Exception,
) -> JSONResponse:
    """Render only stable fields; no credential or database detail is retained."""
    if not isinstance(exception, AuthenticationFailure):
        raise exception
    failure = exception
    request_id = str(current_request_id(request))
    details = dict(failure.details)
    if failure.retry_after_seconds is not None:
        details["retry_after_seconds"] = failure.retry_after_seconds
    response = json_response(
        status_code=failure.status_code,
        request_id=request_id,
        content={
            "error": {
                "code": failure.code,
                "message": failure.message,
                "details": details,
                "request_id": request_id,
            }
        },
    )
    if failure.retry_after_seconds is not None:
        response.headers["Retry-After"] = str(failure.retry_after_seconds)
    return response


async def handle_request_validation_error(
    request: Request,
    exception: Exception,
) -> JSONResponse:
    """Reject malformed auth input without echoing passwords or raw JSON."""
    if not isinstance(exception, RequestValidationError):
        raise exception
    request_id = str(current_request_id(request))
    return json_response(
        status_code=400,
        request_id=request_id,
        content={
            "error": {
                "code": "invalid_request",
                "message": "请求格式不正确，本次操作未执行，请检查后重试",
                "details": {},
                "request_id": request_id,
            }
        },
    )


def create_auth_router(authentication: AuthenticationService) -> APIRouter:
    """Build the three authentication endpoints."""
    router = APIRouter(tags=["authentication"])

    @router.post(LOGIN_PATH)
    @public_route
    async def login(request: Request, body: LoginRequest) -> JSONResponse:
        request_id = current_request_id(request)
        issued = await authentication.login(
            username=body.username,
            password=body.password,
            presented_session_token=request.cookies.get(SESSION_COOKIE_NAME),
            request_id=request_id,
        )
        return issued_session_response(issued, request_id=str(request_id))

    @router.post(LOGOUT_PATH)
    @password_change_exempt_route
    async def logout(request: Request) -> JSONResponse:
        request_id = current_request_id(request)
        await authentication.logout(
            session_token=request.cookies.get(SESSION_COOKIE_NAME),
            csrf_token=request.headers.get(CSRF_HEADER_NAME),
            request_id=request_id,
        )
        response = json_response(content={"status": "logged_out"}, request_id=str(request_id))
        expire_session_cookies(response)
        return response

    @router.post(PASSWORD_PATH)
    @password_change_exempt_route
    async def change_password(request: Request, body: PasswordChangeRequest) -> JSONResponse:
        request_id = current_request_id(request)
        if body.new_password != body.confirm_password:
            raise AuthenticationFailure(
                code="password_confirmation_mismatch",
                message="两次输入的新密码不一致，密码未修改，请重新输入",
                status_code=400,
            )
        issued = await authentication.change_password(
            session_token=request.cookies.get(SESSION_COOKIE_NAME),
            csrf_token=request.headers.get(CSRF_HEADER_NAME),
            current_password=body.current_password,
            new_password=body.new_password,
            request_id=request_id,
        )
        return issued_session_response(issued, request_id=str(request_id))

    return router


__all__ = [
    "CACHE_CONTROL_VALUE",
    "CSRF_COOKIE_NAME",
    "CSRF_HEADER_NAME",
    "LOGIN_PATH",
    "LOGOUT_PATH",
    "PASSWORD_PATH",
    "SESSION_COOKIE_NAME",
    "LoginRequest",
    "PasswordChangeRequest",
    "create_auth_router",
    "expire_session_cookies",
    "issued_session_response",
    "json_response",
    "set_session_cookies",
    "handle_authentication_failure",
    "handle_request_validation_error",
]

"""Registered, default-deny authorization for every Control API route."""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from enum import StrEnum
from re import Pattern
from typing import Any, Final, cast
from uuid import UUID

from fastapi.routing import APIRoute, APIRouter
from holm_common import new_uuid7
from holm_domain import (
    AuditAction,
    AuditActorType,
    AuditObjectType,
    AuditResult,
    AuthorizationSubject,
    Capability,
    permission_scope,
)
from holm_persistence import AuditEventDraft
from holm_security import build_audit_details
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from holm_control_api.authentication import (
    AuthenticatedSession,
    AuthenticationFailure,
    AuthenticationService,
)
from holm_control_api.http_constants import (
    CACHE_CONTROL_VALUE,
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
)

_POLICY_ATTRIBUTE: Final[str] = "__holm_route_access_policy__"
_SESSION_STATE_ATTRIBUTE: Final[str] = "holm_authenticated_session"
_REQUEST_ID_STATE_ATTRIBUTE: Final[str] = "holm_request_id"
_CSRF_SAFE_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "OPTIONS"})


class RouteAccessKind(StrEnum):
    PUBLIC = "public"
    AUTHENTICATED = "authenticated"
    CAPABILITY = "capability"


@dataclass(frozen=True, slots=True)
class RouteAccessPolicy:
    """The explicit access declaration attached while a route is registered."""

    kind: RouteAccessKind
    capability: Capability | None = None
    allow_during_password_change: bool = False

    def __post_init__(self) -> None:
        if (self.kind is RouteAccessKind.CAPABILITY) != (self.capability is not None):
            raise ValueError("only capability policies carry a capability")
        if self.kind is RouteAccessKind.PUBLIC and self.allow_during_password_change:
            raise ValueError("public routes are outside the temporary-password gate")


@dataclass(frozen=True, slots=True)
class _RegisteredApiRoute:
    route: APIRoute
    path: str
    path_regex: Pattern[str]
    methods: frozenset[str]


class _RouteDiscoveryError(AssertionError):
    """The route table contains a shape this authorization layer cannot prove safe."""


def _unrecognized_route(route: object) -> _RouteDiscoveryError:
    return _RouteDiscoveryError(
        "unrecognized route entry during authorization discovery: "
        f"{type(route).__module__}.{type(route).__qualname__}"
    )


def _declare[EndpointT: Callable[..., Any]](
    policy: RouteAccessPolicy,
    endpoint: EndpointT,
) -> EndpointT:
    if hasattr(endpoint, _POLICY_ATTRIBUTE):
        raise ValueError("route access policy already declared")
    setattr(endpoint, _POLICY_ATTRIBUTE, policy)
    return endpoint


def public_route[EndpointT: Callable[..., Any]](endpoint: EndpointT) -> EndpointT:
    """Declare an intentionally unauthenticated route."""
    return _declare(RouteAccessPolicy(RouteAccessKind.PUBLIC), endpoint)


def authenticated_route[EndpointT: Callable[..., Any]](endpoint: EndpointT) -> EndpointT:
    """Declare an authenticated route blocked by the temporary-password gate."""
    return _declare(RouteAccessPolicy(RouteAccessKind.AUTHENTICATED), endpoint)


def password_change_exempt_route[EndpointT: Callable[..., Any]](
    endpoint: EndpointT,
) -> EndpointT:
    """Declare an authenticated route needed to complete or leave the gate."""
    return _declare(
        RouteAccessPolicy(
            RouteAccessKind.AUTHENTICATED,
            allow_during_password_change=True,
        ),
        endpoint,
    )


@dataclass(frozen=True, slots=True)
class _CapabilityRouteDecorator:
    capability: Capability

    def __call__[EndpointT: Callable[..., Any]](self, endpoint: EndpointT) -> EndpointT:
        return _declare(
            RouteAccessPolicy(RouteAccessKind.CAPABILITY, self.capability),
            endpoint,
        )


def capability_route(capability: Capability) -> _CapabilityRouteDecorator:
    """Declare the capability required before an endpoint may run."""
    return _CapabilityRouteDecorator(Capability(capability))


def route_access_policy(route: APIRoute) -> RouteAccessPolicy | None:
    value = getattr(route.endpoint, _POLICY_ATTRIBUTE, None)
    return value if isinstance(value, RouteAccessPolicy) else None


def _registered_api_routes(routes: Sequence[BaseRoute]) -> tuple[_RegisteredApiRoute, ...]:
    """Flatten FastAPI's dynamic wrappers while retaining effective prefixes.

    An unknown shape is a discovery failure, never an ignorable non-API route:
    ignoring it would also hide it from runtime matching and silently bypass the
    default-deny middleware after a FastAPI routing-internals change.
    """
    found: list[_RegisteredApiRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append(
                _RegisteredApiRoute(
                    route=route,
                    path=route.path,
                    path_regex=route.path_regex,
                    methods=frozenset(route.methods or ()),
                )
            )
            continue
        included = getattr(route, "original_router", None)
        candidates = getattr(route, "effective_candidates", None)
        if not isinstance(included, APIRouter) or not callable(candidates):
            raise _unrecognized_route(route)
        candidate_factory = cast(Callable[[], Sequence[object]], candidates)
        try:
            contexts = tuple(candidate_factory())
        except Exception as failure:
            raise _unrecognized_route(route) from failure
        for context in contexts:
            original_route = getattr(context, "original_route", None)
            path = getattr(context, "path", None)
            path_regex = getattr(context, "path_regex", None)
            methods = getattr(context, "methods", None)
            if (
                not isinstance(original_route, APIRoute)
                or not isinstance(path, str)
                or not callable(getattr(path_regex, "match", None))
                or (methods is not None and not isinstance(methods, set))
            ):
                raise _unrecognized_route(context)
            found.append(
                _RegisteredApiRoute(
                    route=original_route,
                    path=path,
                    path_regex=cast(Pattern[str], path_regex),
                    methods=frozenset(methods or ()),
                )
            )
    return tuple(found)


def undeclared_routes(routes: Sequence[BaseRoute]) -> tuple[str, ...]:
    """Return deterministic method/path labels for every fail-closed route."""
    labels: list[str] = []
    for registered in _registered_api_routes(routes):
        if route_access_policy(registered.route) is not None:
            continue
        methods = ",".join(sorted(registered.methods or ("ANY",)))
        labels.append(f"{methods} {registered.path}")
    return tuple(sorted(labels))


def assert_route_authorization_registered(routes: Sequence[BaseRoute]) -> None:
    """Deterministic build/test assertion for the whole application route table."""
    registered = _registered_api_routes(routes)
    if not registered:
        raise AssertionError("authorization discovery found no API routes")
    missing = tuple(
        sorted(
            f"{','.join(sorted(item.methods or ('ANY',)))} {item.path}"
            for item in registered
            if route_access_policy(item.route) is None
        )
    )
    if missing:
        raise AssertionError("undeclared route access policy: " + "; ".join(missing))


def assert_password_change_gate_registered(
    routes: Sequence[BaseRoute],
    *,
    expected_exemptions: Collection[str],
) -> None:
    """Prove the complete protected route table has only reviewed exemptions."""
    registered = _registered_api_routes(routes)
    if not registered:
        raise AssertionError("password-change gate discovery found no API routes")
    missing = tuple(
        sorted(
            f"{','.join(sorted(item.methods or ('ANY',)))} {item.path}"
            for item in registered
            if route_access_policy(item.route) is None
        )
    )
    if missing:
        raise AssertionError(
            "password-change gate found undeclared access policy: " + "; ".join(missing)
        )
    observed = frozenset(
        f"{','.join(sorted(item.methods or ('ANY',)))} {item.path}"
        for item in registered
        if (policy := route_access_policy(item.route)) is not None
        and policy.kind is not RouteAccessKind.PUBLIC
        and policy.allow_during_password_change
    )
    expected = frozenset(expected_exemptions)
    if observed != expected:
        missing_exemptions = sorted(expected - observed)
        extra_exemptions = sorted(observed - expected)
        raise AssertionError(
            "password-change gate exemptions differ: "
            f"missing={missing_exemptions}; extra={extra_exemptions}"
        )


def current_authenticated_session(request: Request) -> AuthenticatedSession:
    """Read the identity installed by the default-deny middleware."""
    value = request.scope.get(_SESSION_STATE_ATTRIBUTE)
    if not isinstance(value, AuthenticatedSession):
        raise RuntimeError("authenticated route executed without an authenticated session")
    return value


def current_subject(request: Request) -> AuthorizationSubject:
    authenticated = current_authenticated_session(request)
    return AuthorizationSubject(
        user_id=authenticated.user.id,
        role=authenticated.user.role,
    )


def current_request_id(request: Request) -> UUID:
    """Return the correlation ID installed before authentication executes."""
    value = request.scope.get(_REQUEST_ID_STATE_ATTRIBUTE)
    if not isinstance(value, UUID):
        raise RuntimeError("request executed without a correlation ID")
    return value


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, int] | None = None,
    request_id: UUID | None = None,
) -> JSONResponse:
    correlation_id = new_uuid7() if request_id is None else request_id
    rendered_request_id = str(correlation_id)
    response = JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": {} if details is None else details,
                "request_id": rendered_request_id,
            }
        },
        headers={"Cache-Control": CACHE_CONTROL_VALUE, "x-request-id": rendered_request_id},
    )
    if details is not None and "retry_after_seconds" in details:
        response.headers["Retry-After"] = str(details["retry_after_seconds"])
    return response


def _authentication_failure_response(
    failure: AuthenticationFailure,
    *,
    request_id: UUID,
) -> JSONResponse:
    details = (
        None
        if failure.retry_after_seconds is None
        else {"retry_after_seconds": failure.retry_after_seconds}
    )
    return _error_response(
        status_code=failure.status_code,
        code=failure.code,
        message=failure.message,
        details=details,
        request_id=request_id,
    )


def _forbidden_response(*, request_id: UUID | None = None) -> JSONResponse:
    # One response covers both an undeclared route and a role-level denial.  It
    # never reports whether an object addressed by the URL exists.
    return _error_response(
        status_code=403,
        code="permission_denied",
        message="没有执行此操作的权限，本次操作未执行，请联系管理员",
        request_id=request_id,
    )


def _password_change_required_response(*, request_id: UUID) -> JSONResponse:
    return _error_response(
        status_code=403,
        code="password_change_required",
        message="需要先修改临时密码，其他功能暂不可用，请设置新密码后重试",
        request_id=request_id,
    )


def _matching_api_route(routes: Sequence[BaseRoute], scope: Scope) -> APIRoute | None:
    method = str(scope["method"])
    path = str(scope["path"])
    for registered in _registered_api_routes(routes):
        if registered.path_regex.match(path) is None:
            continue
        if registered.methods and method not in registered.methods:
            continue
        return registered.route
    return None


class RouteAuthorizationMiddleware:
    """Enforce declarations even for routes added after application creation."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        routes: Sequence[BaseRoute],
        authentication: AuthenticationService | None,
    ) -> None:
        self._app = app
        self._routes = routes
        self._authentication = authentication

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        request_id = new_uuid7()
        scope[_REQUEST_ID_STATE_ATTRIBUTE] = request_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", ()))
                if not any(name.lower() == b"x-request-id" for name, _ in headers):
                    headers.append((b"x-request-id", str(request_id).encode("ascii")))
                    message["headers"] = headers
            await send(message)

        try:
            route = _matching_api_route(self._routes, scope)
        except _RouteDiscoveryError:
            # If FastAPI changes its private included-router shape, no HTTP
            # route can be proven safe.  Return the same sanitized denial as an
            # undeclared policy instead of bypassing authorization or raising 500.
            await _forbidden_response(request_id=request_id)(scope, receive, send_with_request_id)
            return
        if route is None:
            await self._app(scope, receive, send_with_request_id)
            return
        policy = route_access_policy(route)
        if policy is None:
            await _forbidden_response(request_id=request_id)(scope, receive, send_with_request_id)
            return
        if policy.kind is RouteAccessKind.PUBLIC:
            await self._app(scope, receive, send_with_request_id)
            return
        if self._authentication is None:
            response = _error_response(
                status_code=401,
                code="authentication_required",
                message="登录会话无效或已过期，请重新登录",
                request_id=request_id,
            )
            await response(scope, receive, send_with_request_id)
            return

        request = Request(scope, receive=receive)
        require_csrf = str(scope["method"]).upper() not in _CSRF_SAFE_METHODS
        try:
            authenticated = await self._authentication.authenticate_session(
                session_token=request.cookies.get(SESSION_COOKIE_NAME),
                csrf_token=(request.headers.get(CSRF_HEADER_NAME) if require_csrf else None),
                require_csrf=require_csrf,
            )
        except AuthenticationFailure as failure:
            await _authentication_failure_response(failure, request_id=request_id)(
                scope, receive, send_with_request_id
            )
            return
        if authenticated.user.must_change_password and not policy.allow_during_password_change:
            await _password_change_required_response(request_id=request_id)(
                scope, receive, send_with_request_id
            )
            return
        if policy.kind is RouteAccessKind.CAPABILITY:
            capability = cast(Capability, policy.capability)
            if not permission_scope(authenticated.user.role, capability).permits_use:
                # Only the denial is audited.  Audit records track *operations*
                # (of which "account/permission" is already covered by
                # account.change / permission.change); a per-request record of
                # successful capability checks is not one of them, and
                # core.audit_events is retained long term, so writing
                # the success side would grow the table with traffic instead of
                # with security events -- and would put an extra database
                # transaction on every protected request.
                event = AuditEventDraft(
                    actor_type=AuditActorType.USER,
                    actor_id=authenticated.user.id,
                    action=AuditAction.AUTHORIZATION_CHECK,
                    object_type=AuditObjectType.PERMISSION,
                    object_id=None,
                    result=AuditResult.DENIED,
                    request_id=request_id,
                    details=build_audit_details(
                        AuditAction.AUTHORIZATION_CHECK.value,
                        {
                            "capability": capability.value,
                            "reason_code": "permission_denied",
                            "route_template": route.path,
                        },
                    ),
                )
                # Storage failure aborts the request and reaches the application's
                # stable unexpected-error boundary; it is never converted into a
                # successful/denied business result or silently swallowed here.
                await self._authentication.audit_writer.write(event)
                await _forbidden_response(request_id=request_id)(
                    scope, receive, send_with_request_id
                )
                return
        scope[_SESSION_STATE_ATTRIBUTE] = authenticated
        await self._app(scope, receive, send_with_request_id)


__all__ = [
    "RouteAccessKind",
    "RouteAccessPolicy",
    "RouteAuthorizationMiddleware",
    "assert_password_change_gate_registered",
    "assert_route_authorization_registered",
    "authenticated_route",
    "capability_route",
    "current_authenticated_session",
    "current_request_id",
    "current_subject",
    "password_change_exempt_route",
    "public_route",
    "route_access_policy",
    "undeclared_routes",
]

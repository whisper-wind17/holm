"""TC-SEC-11/12/13 route registration, IDOR and response-boundary tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Final, cast
from uuid import UUID

import holm_control_api.authorization as authorization
import pytest
from fastapi import APIRouter, FastAPI
from fixtures.asgi_client import (
    NO_CONFIGURED_CREDENTIAL_ROWS,
    AsgiResponse,
    AsgiTestClient,
)
from holm_contracts import VersionPayload
from holm_control_api import (
    EVENTS_PATH,
    LIVE_PATH,
    LOGOUT_PATH,
    ME_PATH,
    PASSWORD_PATH,
    READY_PATH,
    SESSION_COOKIE_NAME,
    VERSION_PATH,
    AuthenticatedSession,
    AuthenticationService,
    DependencyCheck,
    DiagnosticBundleService,
    EventStreamService,
    ReadinessReason,
    assert_password_change_gate_registered,
    assert_route_authorization_registered,
    authenticated_route,
    capability_route,
    create_app,
    current_subject,
    password_change_exempt_route,
    public_route,
)
from holm_domain import (
    AuditAction,
    AuditResult,
    AuthorizationSubject,
    Capability,
    UserRole,
    UserStatus,
    granted_capabilities,
    navigation_for,
    permission_scope,
)
from holm_persistence import AuditEventDraft, AuditEventRecord, SessionRecord, UserRecord
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

NOW: Final[datetime] = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)
USER_A: Final[UUID] = UUID("00000000-0000-7000-8000-00000000000a")
USER_B: Final[UUID] = UUID("00000000-0000-7000-8000-00000000000b")
MISSING: Final[UUID] = UUID("00000000-0000-7000-8000-00000000000c")


async def _readiness_ok() -> ReadinessReason:
    return ReadinessReason.OK


def _authenticated(
    role: UserRole,
    user_id: UUID = USER_A,
    *,
    must_change_password: bool = False,
) -> AuthenticatedSession:
    user = UserRecord(
        id=user_id,
        username_norm=f"{role.value}-subject",
        display_name=f"{role.value.title()} Subject",
        role=role,
        status=UserStatus.ACTIVE,
        must_change_password=must_change_password,
        created_at=NOW,
        updated_at=NOW,
        deleted_at=None,
        password_hash="$argon2id$not-returned",
    )
    session = SessionRecord(
        id=UUID("00000000-0000-7000-8000-000000000099"),
        user_id=user_id,
        session_hash="a" * 64,
        csrf_hash="b" * 64,
        created_at=NOW,
        last_seen_at=NOW,
        expires_at=NOW + timedelta(hours=8),
        revoked_at=None,
    )
    return AuthenticatedSession(user=user, session=session)


class _Authenticator:
    def __init__(self, authenticated: AuthenticatedSession) -> None:
        self.authenticated = authenticated
        self.calls = 0
        self.audit_writer = _AuditWriter()

    async def authenticate_session(
        self,
        *,
        session_token: str | None,
        csrf_token: str | None = None,
        require_csrf: bool = False,
    ) -> AuthenticatedSession:
        self.calls += 1
        return self.authenticated


class _AuditWriter:
    def __init__(self) -> None:
        self.events: list[AuditEventDraft] = []

    async def add(
        self,
        session: AsyncSession,
        event: AuditEventDraft,
    ) -> AuditEventRecord:
        raise AssertionError("test authenticator has no business transaction")

    async def write(self, event: AuditEventDraft) -> AuditEventRecord:
        self.events.append(event)
        return cast(AuditEventRecord, object())


def _app_with_authenticator(role: UserRole) -> tuple[FastAPI, _Authenticator]:
    authenticator = _Authenticator(_authenticated(role))
    return _app_for(authenticator), authenticator


def _app(role: UserRole) -> FastAPI:
    return _app_for(_Authenticator(_authenticated(role)))


def _app_for(authenticator: _Authenticator) -> FastAPI:
    authentication = cast(AuthenticationService, authenticator)
    return create_app(
        version_payload=VersionPayload(
            build_version="1.4.0+gm102000001",
            commit="1" * 40,
            schema_version=1,
            runtime_compat_hash="2" * 64,
        ),
        credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
        readiness_checks=[DependencyCheck(name="database", probe=_readiness_ok)],
        authentication=authentication,
    )


def _app_with_all_current_routes(authenticator: _Authenticator) -> FastAPI:
    authentication = cast(AuthenticationService, authenticator)
    return create_app(
        version_payload=VersionPayload(
            build_version="1.4.0+gm1010e0001",
            commit="e" * 40,
            schema_version=1,
            runtime_compat_hash="f" * 64,
        ),
        credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
        readiness_checks=[DependencyCheck(name="database", probe=_readiness_ok)],
        authentication=authentication,
        # Route discovery does not enter lifespan or invoke the captured service.
        event_stream=cast(EventStreamService, object()),
        diagnostics=cast(DiagnosticBundleService, object()),
    )


def _responses(app: FastAPI, paths: Sequence[str]) -> tuple[AsgiResponse, ...]:
    async def scenario() -> tuple[AsgiResponse, ...]:
        client = AsgiTestClient(app)
        async with client.lifespan():
            return tuple([await client.get(path) for path in paths])

    return asyncio.run(scenario())


def _response_without_request_id(response: AsgiResponse) -> dict[str, object]:
    payload = response.json()
    error = dict(payload["error"])
    error.pop("request_id")
    return {"error": error}


def _header(response: AsgiResponse, name: str) -> str:
    values = [value for key, value in response.headers if key.casefold() == name.casefold()]
    assert len(values) == 1
    return values[0]


def test_TC_SEC_11_every_application_route_has_an_explicit_access_policy() -> None:
    app = _app(UserRole.ADMIN)
    assert_route_authorization_registered(app.routes)
    discovered_paths = {
        route.path
        for route in authorization._registered_api_routes(app.routes)  # noqa: SLF001
    }
    assert discovered_paths
    assert {ME_PATH, LIVE_PATH, READY_PATH, VERSION_PATH} <= discovered_paths


def test_TC_SEC_10_password_change_exemptions_are_exact_and_discovery_is_nonempty() -> None:
    app = _app_with_all_current_routes(_Authenticator(_authenticated(UserRole.ADMIN)))
    expected = {
        f"POST {LOGOUT_PATH}",
        f"POST {PASSWORD_PATH}",
        f"GET {ME_PATH}",
    }
    assert_password_change_gate_registered(app.routes, expected_exemptions=expected)
    discovered_paths = {
        route.path
        for route in authorization._registered_api_routes(app.routes)  # noqa: SLF001
    }
    assert discovered_paths
    assert EVENTS_PATH in discovered_paths

    with pytest.raises(
        AssertionError,
        match="password-change gate discovery found no API routes",
    ):
        assert_password_change_gate_registered((), expected_exemptions=expected)


def test_TC_SEC_10_extra_password_change_exemption_fails_complete_route_assertion() -> None:
    app = _app_with_all_current_routes(_Authenticator(_authenticated(UserRole.ADMIN)))
    expected = {
        f"POST {LOGOUT_PATH}",
        f"POST {PASSWORD_PATH}",
        f"GET {ME_PATH}",
    }

    @app.get("/test/extra-password-change-exemption")
    @password_change_exempt_route
    async def extra_exemption() -> dict[str, bool]:
        return {"reached": True}

    with pytest.raises(AssertionError, match="extra=.*extra-password-change-exemption"):
        assert_password_change_gate_registered(app.routes, expected_exemptions=expected)


def test_TC_SEC_10_undeclared_password_change_exemption_defaults_to_runtime_denial() -> None:
    authenticator = _Authenticator(_authenticated(UserRole.USER, must_change_password=True))
    app = _app_for(authenticator)
    reached = False

    @app.get("/test/password-change-default-deny")
    @authenticated_route
    async def blocked_by_default() -> dict[str, bool]:
        nonlocal reached
        reached = True
        return {"reached": True}

    response = _responses(app, ("/test/password-change-default-deny",))[0]
    error = response.json()["error"]
    assert response.status == 403
    assert error == {
        "code": "password_change_required",
        "message": "需要先修改临时密码，其他功能暂不可用，请设置新密码后重试",
        "details": {},
        "request_id": _header(response, "x-request-id"),
    }
    assert _header(response, "cache-control") == "no-store"
    assert reached is False
    assert authenticator.audit_writer.events == []


def test_TC_SEC_10_password_change_gate_precedes_capability_denial() -> None:
    authenticator = _Authenticator(_authenticated(UserRole.USER, must_change_password=True))
    app = _app_for(authenticator)

    @app.get("/test/password-change-before-capability")
    @capability_route(Capability.MANAGE_USERS)
    async def capability_denied() -> dict[str, bool]:
        raise AssertionError("temporary-password gate must run before the endpoint")

    response = _responses(app, ("/test/password-change-before-capability",))[0]
    assert response.status == 403
    assert response.json()["error"]["code"] == "password_change_required"
    assert authenticator.audit_writer.events == []


def test_TC_SEC_11_undeclared_route_is_detected_and_runtime_denied() -> None:
    app = _app(UserRole.ADMIN)

    @app.get("/test/undeclared")
    async def undeclared() -> dict[str, bool]:
        return {"should_never_be_reachable": True}

    try:
        assert_route_authorization_registered(app.routes)
    except AssertionError as failure:
        assert str(failure) == "undeclared route access policy: GET /test/undeclared"
    else:
        raise AssertionError("positive control did not detect the undeclared route")

    response = _responses(app, ("/test/undeclared",))[0]
    assert response.status == 403
    assert "should_never_be_reachable" not in response.text


def test_M1_05_registered_success_route_gets_request_id_from_middleware() -> None:
    app = _app(UserRole.ADMIN)

    @app.get("/test/request-id-default")
    @public_route
    async def route_without_header() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/test/request-id-preserved")
    @public_route
    async def route_with_header() -> JSONResponse:
        return JSONResponse({"ok": True}, headers={"x-request-id": str(USER_A)})

    generated, preserved = _responses(
        app,
        ("/test/request-id-default", "/test/request-id-preserved"),
    )
    assert generated.status == preserved.status == 200
    UUID(_header(generated, "x-request-id"))
    assert _header(preserved, "x-request-id") == str(USER_A)


def test_TC_SEC_11_unrecognized_route_shape_fails_discovery_and_runtime_denies() -> None:
    app = _app(UserRole.ADMIN)
    reached = False

    async def unrecognized(_: Request) -> JSONResponse:
        nonlocal reached
        reached = True
        return JSONResponse({"should_never_be_reachable": True})

    app.router.routes.append(Route("/test/unrecognized", endpoint=unrecognized))

    try:
        assert_route_authorization_registered(app.routes)
    except AssertionError as failure:
        assert (
            str(failure)
            == "unrecognized route entry during authorization discovery: starlette.routing.Route"
        )
    else:
        raise AssertionError("positive control silently accepted an unrecognized route")

    response = _responses(app, ("/test/unrecognized",))[0]
    assert response.status == 403
    assert reached is False
    assert "should_never_be_reachable" not in response.text


def test_TC_SEC_11_role_denial_is_403_before_any_object_lookup() -> None:
    app = _app(UserRole.USER)
    looked_up: list[UUID] = []

    @app.get("/test/admin/users/{user_id}")
    @capability_route(Capability.MANAGE_USERS)
    async def admin_user(user_id: UUID) -> dict[str, str]:
        looked_up.append(user_id)
        return {"user_id": str(user_id)}

    existing, missing = _responses(
        app,
        (f"/test/admin/users/{USER_B}", f"/test/admin/users/{MISSING}"),
    )
    assert existing.status == missing.status == 403
    assert _response_without_request_id(existing) == _response_without_request_id(missing)
    assert str(USER_B) not in existing.text
    assert str(MISSING) not in missing.text
    assert looked_up == []


def test_TC_SEC_11_prefixed_included_router_keeps_the_registered_guard() -> None:
    app = _app(UserRole.USER)
    router = APIRouter()
    reached = False

    @router.get("/users")
    @capability_route(Capability.MANAGE_USERS)
    async def prefixed_users() -> dict[str, bool]:
        nonlocal reached
        reached = True
        return {"reached": True}

    app.include_router(router, prefix="/test/prefixed")
    assert_route_authorization_registered(app.routes)
    response = _responses(app, ("/test/prefixed/users",))[0]
    assert response.status == 403
    assert reached is False


def test_TC_SEC_20_granted_capability_check_produces_no_audit_event() -> None:
    assert permission_scope(UserRole.USER, Capability.VIEW_USAGE).permits_use
    assert not permission_scope(UserRole.USER, Capability.MANAGE_USERS).permits_use

    granted_app, granted_authenticator = _app_with_authenticator(UserRole.USER)
    reached = False

    @granted_app.get("/test/audit/granted")
    @capability_route(Capability.VIEW_USAGE)
    async def granted() -> dict[str, bool]:
        nonlocal reached
        reached = True
        return {"reached": True}

    granted_response = _responses(granted_app, ("/test/audit/granted",))[0]
    assert granted_response.status == 200
    assert reached is True
    # A passing capability check is not an audited operation, so the request
    # never reaches the audit writer and runs no extra database transaction.
    assert granted_authenticator.audit_writer.events == []

    denied_app, denied_authenticator = _app_with_authenticator(UserRole.USER)

    @denied_app.get("/test/audit/denied")
    @capability_route(Capability.MANAGE_USERS)
    async def denied() -> dict[str, bool]:
        raise AssertionError("denied capability route must not run")

    denied_response = _responses(denied_app, ("/test/audit/denied",))[0]
    assert denied_response.status == 403
    assert [
        (event.action, event.result, event.request_id)
        for event in denied_authenticator.audit_writer.events
    ] == [
        (
            AuditAction.AUTHORIZATION_CHECK,
            AuditResult.DENIED,
            UUID(_header(denied_response, "x-request-id")),
        )
    ]


class _OwnedObjectRepository:
    """Test-only protected resource used because M2-M5 resources do not exist."""

    def __init__(self) -> None:
        self._owners = {USER_A: USER_A, USER_B: USER_B}

    def get_visible(self, subject: AuthorizationSubject, object_id: UUID) -> UUID | None:
        owner_id = self._owners.get(object_id)
        if subject.role is UserRole.ADMIN or owner_id == subject.user_id:
            return object_id if owner_id is not None else None
        return None


def test_TC_SEC_12_object_denial_is_indistinguishable_from_absence() -> None:
    app = _app(UserRole.USER)
    repository = _OwnedObjectRepository()

    @app.get("/test/owned/{object_id}")
    @capability_route(Capability.VIEW_USAGE)
    async def owned_object(request: Request, object_id: UUID) -> JSONResponse:
        visible = repository.get_visible(current_subject(request), object_id)
        if visible is None:
            return JSONResponse(status_code=404, content={"error": {"code": "not_found"}})
        return JSONResponse(status_code=200, content={"id": str(visible)})

    other_user, absent = _responses(
        app,
        (f"/test/owned/{USER_B}", f"/test/owned/{MISSING}"),
    )
    assert other_user.status == absent.status == 404
    assert other_user.body == absent.body


def test_TC_SEC_11_me_returns_admin_permissions_and_six_navigation_items() -> None:
    response = _responses(_app(UserRole.ADMIN), (ME_PATH,))[0]
    payload = response.json()

    assert response.status == 200
    assert _header(response, "cache-control") == "no-store"
    assert _header(response, "x-request-id")
    assert payload["role"] == UserRole.ADMIN.value
    assert payload["must_change_password"] is False
    assert payload["capabilities"] == [
        {
            "capability": capability.value,
            "scope": permission_scope(UserRole.ADMIN, capability).value,
        }
        for capability in granted_capabilities(UserRole.ADMIN)
    ]
    assert [item["id"] for item in payload["navigation"]] == [
        item.id.value for item in navigation_for(UserRole.ADMIN)
    ]
    assert len(payload["navigation"]) == 6


def test_TC_SEC_13_me_returns_user_permissions_and_no_infrastructure_fields() -> None:
    response = _responses(_app(UserRole.USER), (ME_PATH,))[0]
    payload = response.json()

    assert response.status == 200
    assert payload["role"] == UserRole.USER.value
    assert payload["must_change_password"] is False
    assert payload["capabilities"] == [
        {
            "capability": capability.value,
            "scope": permission_scope(UserRole.USER, capability).value,
        }
        for capability in granted_capabilities(UserRole.USER)
    ]
    assert [item["id"] for item in payload["navigation"]] == [
        item.id.value for item in navigation_for(UserRole.USER)
    ]
    assert len(payload["navigation"]) == 3

    serialized = json.dumps(payload, ensure_ascii=False).casefold()
    forbidden_sentinels = (
        "gpu",
        "model_instance",
        "runtime",
        "tensor_parallel",
        "container",
        "internal_endpoint",
        "runtime_waiting",
        "kv_cache",
        "other_user",
        "platform_latency",
    )
    assert all(sentinel not in serialized for sentinel in forbidden_sentinels)
    assert SESSION_COOKIE_NAME not in serialized


def test_M7_11B_diagnostic_resource_get_and_legacy_preview_have_admin_policies() -> None:
    app = _app_with_all_current_routes(_Authenticator(_authenticated(UserRole.ADMIN)))
    assert_route_authorization_registered(app.routes)
    routes = [
        route
        for route in authorization._registered_api_routes(app.routes)  # noqa: SLF001
        if route.path.startswith("/api/v1/diagnostics")
    ]
    assert {(route.path, method) for route in routes for method in route.methods} == {
        ("/api/v1/diagnostics", "GET"),
        ("/api/v1/diagnostics", "POST"),
        ("/api/v1/diagnostics/preview", "GET"),
    }
    for route in routes:
        policy = authorization.route_access_policy(route.route)
        assert policy is not None
        assert policy.kind is authorization.RouteAccessKind.CAPABILITY
        assert policy.capability is Capability.VIEW_INFRASTRUCTURE

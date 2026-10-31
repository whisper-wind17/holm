"""M4-16 integration coverage for Pepper rotation, startup, and read API."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Final, cast
from uuid import UUID

import httpx
import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from fastapi import FastAPI
from fixtures.asgi_client import (
    NO_CONFIGURED_CREDENTIAL_ROWS,
    AsgiTestClient,
    LifespanError,
)
from holm_contracts import VersionPayload
from holm_control_api import (
    API_KEY_PEPPERS_PATH,
    SESSION_COOKIE_NAME,
    AuthenticatedSession,
    AuthenticationService,
    DependencyCheck,
    ReadinessReason,
)
from holm_control_api import create_app as create_control_app
from holm_control_api.api_key_pepper_service import ApiKeyPepperService
from holm_control_api.api_key_service import (
    ApiKeyManagementService,
    CreatePersonalApiKey,
)
from holm_control_api.authentication import AuthenticationFailure
from holm_domain import ApiKeyStatus, UserRole, UserStatus
from holm_gateway.api_key_pepper_startup import (  # type: ignore[import-untyped]
    check_api_key_pepper_references,
)
from holm_gateway.application import (  # type: ignore[import-untyped]
    create_app as create_gateway_app,
)
from holm_gateway.authentication import ApiKeyAuthenticator  # type: ignore[import-untyped]
from holm_gateway.route_snapshot import (  # type: ignore[import-untyped]
    PostgresRouteSnapshotSource,
    RouteSnapshotManager,
    SnapshotApiKeyValidationLookup,
)
from holm_persistence import (
    API_KEYS,
    ApiKeyPepperReferenceRepository,
    AsyncSessionFactory,
    AuditEventDraft,
    AuditEventRecord,
    SessionRecord,
    UserRecord,
    create_database_engine,
    create_session_factory,
)
from holm_runtimes import RuntimeRequestCapabilities
from holm_security import (
    API_KEY_PEPPER_MISSING_ERROR_CODE,
    ApiKeyPepperKeyring,
    ApiKeyPepperStartupError,
)
from migration_test_support import upgrade_head
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

NOW: Final = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
USER_ID: Final = UUID("76000000-0000-4000-8000-000000000001")
SESSION_ID: Final = UUID("76000000-0000-4000-8000-000000000002")


def _async_url(fixture: EphemeralPostgres) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username="ubuntu",
        host=str(fixture.socket_directory),
        port=fixture.port,
        database="postgres",
    )


def _reset(fixture: EphemeralPostgres) -> None:
    upgrade_head(fixture.engine)
    with fixture.engine.begin() as connection:
        connection.execute(sa.text("TRUNCATE core.users, core.domain_events CASCADE"))
        connection.execute(
            sa.text(
                "INSERT INTO core.users "
                "(id, username_norm, display_name, role, status, password_hash, "
                "must_change_password, created_at, updated_at, deleted_at) VALUES "
                "(:id, 'm4-16-admin', 'M4-16 Admin', 'admin', 'active', :password_hash, "
                "false, clock_timestamp(), clock_timestamp(), NULL)"
            ),
            {"id": USER_ID, "password_hash": "$argon2id$m4-16-not-used"},
        )


async def _database(fixture: EphemeralPostgres) -> tuple[AsyncEngine, AsyncSessionFactory]:
    engine = create_database_engine(_async_url(fixture), connect_timeout_seconds=2)
    return engine, create_session_factory(engine)


async def _readiness_ok() -> ReadinessReason:
    return ReadinessReason.OK


def _version() -> VersionPayload:
    return VersionPayload(
        build_version="1.0.0+gm4160000001",
        commit="4" * 40,
        schema_version=1,
        runtime_compat_hash="6" * 64,
    )


class _AuditWriter:
    async def add(self, session: AsyncSession, event: AuditEventDraft) -> AuditEventRecord:
        raise AssertionError("M4-16 read route does not open a business transaction")

    async def write(self, event: AuditEventDraft) -> AuditEventRecord:
        return cast(AuditEventRecord, object())


class _Authenticator:
    def __init__(self, role: UserRole) -> None:
        user = UserRecord(
            id=USER_ID,
            username_norm="m4-16-user",
            display_name="M4-16 User",
            role=role,
            status=UserStatus.ACTIVE,
            must_change_password=False,
            created_at=NOW,
            updated_at=NOW,
            deleted_at=None,
            password_hash="$argon2id$not-returned",
        )
        self.authenticated = AuthenticatedSession(
            user=user,
            session=SessionRecord(
                id=SESSION_ID,
                user_id=user.id,
                session_hash="a" * 64,
                csrf_hash="b" * 64,
                created_at=NOW,
                last_seen_at=NOW,
                expires_at=NOW + timedelta(hours=1),
                revoked_at=None,
            ),
        )
        self.audit_writer = _AuditWriter()

    async def authenticate_session(
        self,
        *,
        session_token: str | None,
        csrf_token: str | None = None,
        require_csrf: bool = False,
    ) -> AuthenticatedSession:
        if session_token is None:
            raise AuthenticationFailure(
                code="authentication_required",
                message="登录会话无效或已过期，请重新登录",
                status_code=401,
            )
        return self.authenticated


def _control_app(
    service: ApiKeyPepperService,
    *,
    role: UserRole | None,
) -> FastAPI:
    authentication = None if role is None else cast(AuthenticationService, _Authenticator(role))
    return create_control_app(
        version_payload=_version(),
        readiness_checks=[DependencyCheck(name="database", probe=_readiness_ok)],
        credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
        authentication=authentication,
        api_key_peppers=service,
    )


def _insert_key(
    fixture: EphemeralPostgres,
    *,
    offset: int,
    pepper_key_id: str,
    status: ApiKeyStatus,
    expires_at: datetime | None,
) -> None:
    created_at = NOW - timedelta(days=2)
    revoked_at = NOW - timedelta(days=1) if status is ApiKeyStatus.REVOKED else None
    lookup_id = chr(ord("A") + offset) * 26
    with fixture.engine.begin() as connection:
        connection.execute(
            sa.insert(API_KEYS).values(
                id=UUID(int=USER_ID.int + 100 + offset),
                user_id=USER_ID,
                name=f"M4-16 key {offset}",
                lookup_id=lookup_id,
                display_prefix="holm_sk_",
                last4="ABCD",
                pepper_key_id=pepper_key_id,
                secret_hmac=bytes([offset + 1]) * 32,
                status=status.value,
                expires_at=expires_at,
                last_used_at=None,
                created_at=created_at,
                revoked_at=revoked_at,
            )
        )


def test_M4_16_reference_counts_match_gateway_active_and_expiry_rules(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _reset(ephemeral_postgres)
    _insert_key(
        ephemeral_postgres,
        offset=0,
        pepper_key_id="pepper-a",
        status=ApiKeyStatus.ACTIVE,
        expires_at=None,
    )
    _insert_key(
        ephemeral_postgres,
        offset=1,
        pepper_key_id="pepper-a",
        status=ApiKeyStatus.ACTIVE,
        expires_at=NOW + timedelta(days=365),
    )
    _insert_key(
        ephemeral_postgres,
        offset=2,
        pepper_key_id="expired-by-time",
        status=ApiKeyStatus.ACTIVE,
        expires_at=NOW - timedelta(days=1),
    )
    _insert_key(
        ephemeral_postgres,
        offset=3,
        pepper_key_id="expired-by-status",
        status=ApiKeyStatus.EXPIRED,
        expires_at=NOW + timedelta(days=365),
    )
    _insert_key(
        ephemeral_postgres,
        offset=4,
        pepper_key_id="revoked",
        status=ApiKeyStatus.REVOKED,
        expires_at=None,
    )

    async def scenario() -> None:
        engine, factory = await _database(ephemeral_postgres)
        try:
            async with factory() as session:
                counts = await ApiKeyPepperReferenceRepository(session).active_reference_counts()
            assert counts == {"pepper-a": 2}
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_M4_16_real_issuance_and_gateway_snapshot_support_old_and_current_peppers(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _reset(ephemeral_postgres)

    async def scenario() -> None:
        engine, factory = await _database(ephemeral_postgres)
        manager: RouteSnapshotManager | None = None
        try:
            old_keyring = ApiKeyPepperKeyring(
                current_key_id="pepper-a", keys={"pepper-a": b"A" * 32}
            )
            rotated_keyring = ApiKeyPepperKeyring(
                current_key_id="pepper-b",
                keys={"pepper-a": b"A" * 32, "pepper-b": b"B" * 32},
            )
            old = await ApiKeyManagementService(
                session_factory=factory, keyring=old_keyring
            ).create(
                CreatePersonalApiKey("old", None, ()),
                user_id=USER_ID,
                user_role=UserRole.ADMIN,
                request_id=UUID(int=USER_ID.int + 10),
            )
            current = await ApiKeyManagementService(
                session_factory=factory, keyring=rotated_keyring
            ).create(
                CreatePersonalApiKey("current", None, ()),
                user_id=USER_ID,
                user_role=UserRole.ADMIN,
                request_id=UUID(int=USER_ID.int + 11),
            )
            assert old.record.pepper_key_id == "pepper-a"
            assert current.record.pepper_key_id == "pepper-b"

            manager = RouteSnapshotManager(PostgresRouteSnapshotSource(factory), listener=None)
            await manager.refresh()
            gateway = create_gateway_app(
                authenticator=ApiKeyAuthenticator(
                    lookup=SnapshotApiKeyValidationLookup(manager, utc_now=lambda: NOW),
                    keyring=rotated_keyring,
                    availability_check=manager.ensure_available,
                ),
                routes=manager,
                runtime_capabilities={
                    "vllm": RuntimeRequestCapabilities(
                        openai_chat_completions=frozenset({"model", "messages"})
                    )
                },
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=gateway), base_url="http://gateway"
            ) as client:
                responses = []
                for plaintext in (old.plaintext, current.plaintext):
                    responses.append(
                        await client.post(
                            "/v1/chat/completions",
                            headers={"Authorization": f"Bearer {plaintext}"},
                            json={"model": "missing-m4-16", "messages": []},
                        )
                    )
            assert [response.status_code for response in responses] == [404, 404]
            assert [response.json()["error"]["code"] for response in responses] == [
                "model_not_found",
                "model_not_found",
            ]
        finally:
            if manager is not None:
                await manager.stop()
            await engine.dispose()

    asyncio.run(scenario())


def test_M4_16_control_and_gateway_startup_reject_missing_active_pepper_safely(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _reset(ephemeral_postgres)
    _insert_key(
        ephemeral_postgres,
        offset=0,
        pepper_key_id="pepper-a",
        status=ApiKeyStatus.ACTIVE,
        expires_at=None,
    )

    async def scenario() -> None:
        engine, factory = await _database(ephemeral_postgres)
        keyring = ApiKeyPepperKeyring(current_key_id="pepper-b", keys={"pepper-b": b"B" * 32})
        service = ApiKeyPepperService(session_factory=factory, keyring=keyring)
        try:
            for check in (
                service.check_startup(),
                check_api_key_pepper_references(factory, keyring),
            ):
                with pytest.raises(ApiKeyPepperStartupError) as raised:
                    await check
                assert raised.value.code == API_KEY_PEPPER_MISSING_ERROR_CODE
                assert str(raised.value) == "api_key_pepper_missing:pepper-a"

            client = AsgiTestClient(_control_app(service, role=UserRole.ADMIN))
            with pytest.raises(LifespanError, match="lifespan.startup.failed"):
                async with client.lifespan():
                    raise AssertionError("Control API started with a missing Pepper")
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_M4_16_startup_allows_removed_pepper_after_references_expire_or_revoke(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _reset(ephemeral_postgres)
    _insert_key(
        ephemeral_postgres,
        offset=0,
        pepper_key_id="pepper-a",
        status=ApiKeyStatus.ACTIVE,
        expires_at=NOW - timedelta(days=1),
    )
    _insert_key(
        ephemeral_postgres,
        offset=1,
        pepper_key_id="pepper-a",
        status=ApiKeyStatus.REVOKED,
        expires_at=None,
    )

    async def scenario() -> None:
        engine, factory = await _database(ephemeral_postgres)
        keyring = ApiKeyPepperKeyring(current_key_id="pepper-b", keys={"pepper-b": b"B" * 32})
        try:
            await ApiKeyPepperService(session_factory=factory, keyring=keyring).check_startup()
            await check_api_key_pepper_references(factory, keyring)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_M4_16_read_api_is_admin_only_and_never_returns_pepper_material(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _reset(ephemeral_postgres)
    _insert_key(
        ephemeral_postgres,
        offset=0,
        pepper_key_id="pepper-a",
        status=ApiKeyStatus.ACTIVE,
        expires_at=None,
    )

    async def scenario() -> None:
        engine, factory = await _database(ephemeral_postgres)
        material_sentinel = b"PWSSNTL-MASTER-TC-GW-23-A416B416"[:32]
        keyring = ApiKeyPepperKeyring(
            current_key_id="pepper-b",
            keys={"pepper-a": b"A" * 32, "pepper-b": material_sentinel},
        )
        service = ApiKeyPepperService(session_factory=factory, keyring=keyring)
        try:
            responses = []
            for role, cookies in (
                (None, None),
                (UserRole.USER, {SESSION_COOKIE_NAME: "present"}),
                (UserRole.ADMIN, {SESSION_COOKIE_NAME: "present"}),
            ):
                client = AsgiTestClient(_control_app(service, role=role))
                async with client.lifespan():
                    responses.append(await client.get(API_KEY_PEPPERS_PATH, cookies=cookies))

            unauthenticated, ordinary_user, admin = responses
            assert unauthenticated.status == 401
            assert ordinary_user.status == 403
            assert admin.status == 200
            assert admin.json() == {
                "items": [
                    {
                        "pepper_key_id": "pepper-a",
                        "is_current": False,
                        "active_reference_count": 1,
                        "can_remove": False,
                        "reason_code": "active_key_references",
                    },
                    {
                        "pepper_key_id": "pepper-b",
                        "is_current": True,
                        "active_reference_count": 0,
                        "can_remove": False,
                        "reason_code": "current_pepper",
                    },
                ]
            }
            serialized = json.dumps(admin.json(), sort_keys=True)
            assert material_sentinel.decode("ascii") not in serialized
            assert "lookup_id" not in serialized
            assert "secret_hmac" not in serialized
            assert "user_id" not in serialized
        finally:
            await engine.dispose()

    asyncio.run(scenario())

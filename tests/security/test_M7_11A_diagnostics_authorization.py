"""M7-11A / TC-SEC-11 diagnostic route authorization boundary."""

from __future__ import annotations

import asyncio
import io
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Final, cast
from uuid import UUID

from fastapi import FastAPI
from fixtures.asgi_client import NO_CONFIGURED_CREDENTIAL_ROWS, AsgiResponse, AsgiTestClient
from holm_contracts import VersionPayload
from holm_control_api import (
    AuthenticatedSession,
    AuthenticationService,
    DependencyCheck,
    DiagnosticBundleService,
    ReadinessReason,
    create_app,
)
from holm_control_api.diagnostics_service import (
    DiagnosticEvent,
    DiagnosticLogEntry,
    DiagnosticRequestRecord,
    SourcePage,
)
from holm_domain import UserRole, UserStatus
from holm_persistence import (
    AuditEventDraft,
    AuditEventRecord,
    SessionRecord,
    UserRecord,
)
from sqlalchemy.ext.asyncio import AsyncSession

NOW: Final = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
USER_ID: Final = UUID("01999a00-0000-7000-8000-000000000721")


async def _readiness_ok() -> ReadinessReason:
    return ReadinessReason.OK


class _AuditWriter:
    async def add(self, session: AsyncSession, event: AuditEventDraft) -> AuditEventRecord:
        raise AssertionError("authorization test has no business transaction")

    async def write(self, event: AuditEventDraft) -> AuditEventRecord:
        return cast(AuditEventRecord, object())


class _Authenticator:
    def __init__(self, role: UserRole) -> None:
        user = UserRecord(
            id=USER_ID,
            username_norm=f"diagnostic-{role.value}",
            display_name="Diagnostic User",
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
                id=UUID("01999a00-0000-7000-8000-000000000722"),
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
        return self.authenticated


class _MappingSource:
    async def read(self) -> dict[str, object]:
        return {"status": "available"}


class _Events:
    async def read(self, **_: object) -> SourcePage[DiagnosticEvent]:
        return SourcePage(())


class _Requests:
    async def read(self, **_: object) -> SourcePage[DiagnosticRequestRecord]:
        return SourcePage(())


class _Logs:
    async def read(self, **_: object) -> SourcePage[DiagnosticLogEntry]:
        return SourcePage(())


def _diagnostics() -> DiagnosticBundleService:
    return DiagnosticBundleService(
        version=VersionPayload(
            build_version="1.0.0+gm711a",
            commit="7" * 40,
            schema_version=1,
            runtime_compat_hash="8" * 64,
        ),
        config={},
        health=_MappingSource(),
        events=_Events(),
        metrics=_MappingSource(),
        request_records=_Requests(),
        logs=_Logs(),
        clock=lambda: NOW,
    )


def _app(role: UserRole) -> FastAPI:
    return create_app(
        version_payload=VersionPayload(
            build_version="1.0.0+gm711a",
            commit="7" * 40,
            schema_version=1,
            runtime_compat_hash="8" * 64,
        ),
        readiness_checks=[DependencyCheck(name="database", probe=_readiness_ok)],
        credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
        authentication=cast(AuthenticationService, _Authenticator(role)),
        diagnostics=_diagnostics(),
    )


def test_M7_11A_TC_SEC_11_normal_user_is_denied_before_object_lookup() -> None:
    async def scenario() -> tuple[AsgiResponse, AsgiResponse]:
        client = AsgiTestClient(_app(UserRole.USER))
        async with client.lifespan():
            existing = await client.get(f"/api/v1/diagnostics/preview?instance_id={USER_ID}")
            missing = await client.get(
                "/api/v1/diagnostics/preview?instance_id=01999a00-0000-7000-8000-000000000799"
            )
        return existing, missing

    existing, missing = asyncio.run(scenario())
    assert existing.status == missing.status == 403
    for response in (existing, missing):
        body = response.json()
        body["error"].pop("request_id")
        assert body == {
            "error": {
                "code": "permission_denied",
                "message": "没有执行此操作的权限，本次操作未执行，请联系管理员",
                "details": {},
            }
        }


def test_M7_11A_admin_can_preview_and_stream_the_same_manifest_shape() -> None:
    async def scenario() -> tuple[AsgiResponse, AsgiResponse]:
        client = AsgiTestClient(_app(UserRole.ADMIN))
        async with client.lifespan():
            preview = await client.get(
                f"/api/v1/diagnostics/preview?end_at={NOW.isoformat().replace('+00:00', 'Z')}"
            )
            bundle = await client.request(
                "POST",
                "/api/v1/diagnostics",
                headers={"X-CSRF-Token": "test-token"},
                json_body={"end_at": NOW.isoformat()},
            )
        return preview, bundle

    preview, bundle = asyncio.run(scenario())
    assert preview.status == 200
    assert bundle.status == 200
    with zipfile.ZipFile(io.BytesIO(bundle.body)) as archive:
        manifest = archive.read("manifest.json")
        assert set(archive.namelist()) == set(preview.json()["archive_files"])
    assert b'"items"' in manifest

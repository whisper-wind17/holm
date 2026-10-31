from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Final, cast
from uuid import UUID

from fixtures.asgi_client import NO_CONFIGURED_CREDENTIAL_ROWS, AsgiTestClient
from holm_contracts import VersionPayload
from holm_control_api import (
    AuthenticatedSession,
    AuthenticationService,
    DependencyCheck,
    ReadinessReason,
    create_app,
)
from holm_control_api.telemetry_service import TelemetryService
from holm_domain import UserRole, UserStatus
from holm_persistence import (
    AuditEventDraft,
    AuditEventRecord,
    SessionRecord,
    UserRecord,
)
from sqlalchemy.ext.asyncio import AsyncSession

NOW: Final = datetime(2026, 9, 15, tzinfo=UTC)
USER_ID: Final = UUID("00000000-0000-7000-8000-000000001414")


async def _readiness_ok() -> ReadinessReason:
    return ReadinessReason.OK


class _AuditWriter:
    async def add(self, session: AsyncSession, event: AuditEventDraft) -> AuditEventRecord:
        raise AssertionError("authorization test has no business transaction")

    async def write(self, event: AuditEventDraft) -> AuditEventRecord:
        return cast(AuditEventRecord, object())


class _Authenticator:
    def __init__(self) -> None:
        user = UserRecord(
            id=USER_ID,
            username_norm="telemetry-user",
            display_name="Telemetry User",
            role=UserRole.USER,
            status=UserStatus.ACTIVE,
            must_change_password=False,
            created_at=NOW,
            updated_at=NOW,
            deleted_at=None,
            password_hash="$argon2id$not-returned",
        )
        session = SessionRecord(
            id=UUID("00000000-0000-7000-8000-000000001415"),
            user_id=user.id,
            session_hash="a" * 64,
            csrf_hash="b" * 64,
            created_at=NOW,
            last_seen_at=NOW,
            expires_at=NOW + timedelta(hours=1),
            revoked_at=None,
        )
        self.authenticated = AuthenticatedSession(user=user, session=session)
        self.audit_writer = _AuditWriter()

    async def authenticate_session(
        self,
        *,
        session_token: str | None,
        csrf_token: str | None = None,
        require_csrf: bool = False,
    ) -> AuthenticatedSession:
        return self.authenticated


class _Telemetry:
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


def test_PROTO_14_telemetry_api_returns_403_for_normal_users() -> None:
    version = VersionPayload(
        build_version="1.0.0+gproto14",
        commit="1" * 40,
        schema_version=1,
        runtime_compat_hash="2" * 64,
    )
    app = create_app(
        version_payload=version,
        readiness_checks=[DependencyCheck(name="database", probe=_readiness_ok)],
        credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
        authentication=cast(AuthenticationService, _Authenticator()),
        telemetry=cast(TelemetryService, _Telemetry()),
    )

    async def scenario() -> list[int]:
        client = AsgiTestClient(app)
        async with client.lifespan():
            paths = (
                f"/api/v1/telemetry/instances/{USER_ID}",
                f"/api/v1/telemetry/services/{USER_ID}",
                "/api/v1/telemetry/gpus",
                "/api/v1/telemetry/overview",
            )
            return [(await client.get(path)).status for path in paths]

    assert asyncio.run(scenario()) == [403, 403, 403, 403]

"""PROTO-13 Usage aggregation and role scoping on real PostgreSQL."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, time, timedelta
from http.cookies import SimpleCookie
from typing import Final, cast
from urllib.parse import urlencode
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from fixtures.asgi_client import (
    NO_CONFIGURED_CREDENTIAL_ROWS,
    AsgiResponse,
    AsgiTestClient,
    SessionCookies,
)
from holm_common import new_uuid7
from holm_contracts import VersionPayload
from holm_control_api import usage_service as usage_service_module
from holm_control_api.application import create_app
from holm_control_api.authentication import AuthenticationService
from holm_control_api.http_constants import SESSION_COOKIE_NAME
from holm_control_api.readiness import DependencyCheck, ReadinessReason
from holm_control_api.usage_service import UsageQueryService
from holm_domain import (
    ApiKeyStatus,
    ArtifactStorageType,
    MetadataStatus,
    ModelArtifactStatus,
    ModelInstanceStatus,
    ModelOriginType,
    RequestRecordStatus,
    RuntimeType,
    UserRole,
)
from holm_persistence import (
    LEDGER_REQUEST_RECORDS,
    USER_SERVICE_PERMISSIONS,
    AiServiceCreate,
    AiServiceRepository,
    ApiKeyCreate,
    ApiKeyRepository,
    AsyncSessionFactory,
    UsageRepository,
    UserRepository,
    create_database_engine,
    create_session_factory,
    database_utc_now,
    ensure_request_record_partitions,
)
from holm_security import (
    OWASP_MINIMUM_MEMORY_COST_KIB,
    OWASP_MINIMUM_TIME_COST,
    ApiKeyPepperKeyring,
    Argon2idParameters,
    PasswordHasher,
    issue_api_key,
)
from migration_test_support import upgrade_head
from sqlalchemy.engine import URL, Engine
from sqlalchemy.ext.asyncio import AsyncEngine

PASSWORD: Final[str] = "correct-password-for-proto-13-tests"
# M5-08: keep the seeded records inside the still-open bucket that "Today" aggregates live.
USAGE_CLOCK_OFFSET: Final[timedelta] = timedelta(minutes=6)
MODEL_ID: Final[UUID] = UUID("13000000-0000-4000-8000-000000000001")
ARTIFACT_ID: Final[UUID] = UUID("13000000-0000-4000-8000-000000000002")
INSTANCE_ID: Final[UUID] = UUID("13000000-0000-4000-8000-000000000003")
UNAUTHORIZED_SERVICE_ID: Final[UUID] = UUID("13000000-0000-4000-8000-000000000004")
CHEAP: Final = Argon2idParameters(
    memory_cost_kib=OWASP_MINIMUM_MEMORY_COST_KIB,
    time_cost=OWASP_MINIMUM_TIME_COST,
    parallelism=1,
)


async def _readiness_ok() -> ReadinessReason:
    return ReadinessReason.OK


def _async_url(fixture: EphemeralPostgres) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username="ubuntu",
        host=str(fixture.socket_directory),
        port=fixture.port,
        database="postgres",
    )


def _seed_model_instance(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO core.models "
                "(id, source_type, source_key, display_name, external_path, metadata_status, "
                "created_at, updated_at) VALUES "
                "(:id, :source_type, :source_key, 'PROTO-13 Model', '/models/proto-13', "
                ":metadata_status, clock_timestamp(), clock_timestamp())"
            ),
            {
                "id": MODEL_ID,
                "source_type": ModelOriginType.LOCAL_PATH.value,
                "source_key": "/models/proto-13",
                "metadata_status": MetadataStatus.CONFIRMED.value,
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO core.model_artifacts "
                "(id, model_id, source_type, repository_id, resolved_revision, storage_type, "
                "status, path, manifest_hash, size_bytes, created_at, updated_at) VALUES "
                "(:id, :model_id, :source_type, 'proto-13', :revision, :storage_type, "
                ":status, '/models/proto-13', :manifest_hash, 1024, "
                "clock_timestamp(), clock_timestamp())"
            ),
            {
                "id": ARTIFACT_ID,
                "model_id": MODEL_ID,
                "source_type": ModelOriginType.LOCAL_PATH.value,
                "revision": "1" * 40,
                "storage_type": ArtifactStorageType.EXTERNAL.value,
                "status": ModelArtifactStatus.READY.value,
                "manifest_hash": "2" * 40,
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO core.model_instances "
                "(id, artifact_id, runtime_type, expected_gpu_uuids, tensor_parallel_size, "
                "context_length, gpu_memory_utilization, name, internal_model_name, status, "
                "error_code, internal_endpoint, config_version, created_at, updated_at) "
                "VALUES (:id, :artifact_id, :runtime_type, CAST(:gpus AS jsonb), 1, 4096, "
                "0.9, 'FX instance 13', 'mi_000000000013', :status, NULL, NULL, 1, "
                "clock_timestamp(), clock_timestamp())"
            ),
            {
                "id": INSTANCE_ID,
                "artifact_id": ARTIFACT_ID,
                "runtime_type": RuntimeType.VLLM.value,
                "gpus": '["GPU-proto-13"]',
                "status": ModelInstanceStatus.STOPPED.value,
            },
        )


def _record(
    *,
    at: datetime,
    user_id: UUID,
    api_key_id: UUID,
    service_id: UUID,
    status: RequestRecordStatus,
    input_tokens: int | None,
    output_tokens: int | None,
) -> dict[str, object]:
    terminal_at = at + timedelta(seconds=1)
    return {
        "request_id": new_uuid7(),
        "received_at": at,
        "event_seq": 3,
        "record_state": status.value,
        "user_id": user_id,
        "api_key_id": api_key_id,
        "service_id": service_id,
        "instance_id": INSTANCE_ID,
        "stream": False,
        "http_status": 500 if status is RequestRecordStatus.FAILED else 200,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "duration_ms": 1_000,
        "terminal_at": terminal_at,
        "updated_at": terminal_at,
    }


async def _seed_usage(
    factory: AsyncSessionFactory,
    engine: AsyncEngine,
) -> tuple[UUID, UUID, UUID, UUID, datetime]:
    password_hash = await asyncio.to_thread(PasswordHasher(CHEAP).hash, PASSWORD)
    keyring = ApiKeyPepperKeyring(current_key_id="proto-13", keys={"proto-13": b"U" * 32})
    async with factory() as session:
        now = await database_utc_now(session)
        users = UserRepository(session)
        admin = await users.create(
            username="proto-13-admin",
            display_name="PROTO-13 Admin",
            role=UserRole.ADMIN,
            password_hash=password_hash,
            now=now,
            must_change_password=False,
        )
        user = await users.create(
            username="proto-13-user",
            display_name="PROTO-13 User",
            role=UserRole.USER,
            password_hash=password_hash,
            now=now,
            must_change_password=False,
        )
        service = await AiServiceRepository(session).create(
            AiServiceCreate(
                public_model_name="qwen-usage",
                description="PROTO-13 service",
                enabled=True,
                primary_instance_id=INSTANCE_ID,
            ),
            now=now,
        )
        keys = ApiKeyRepository(session)
        issued_admin = issue_api_key(keyring)
        admin_key = await keys.create(
            ApiKeyCreate(
                user_id=admin.id,
                name="admin-key",
                lookup_id=issued_admin.lookup_id,
                display_prefix=issued_admin.display_prefix,
                last4=issued_admin.last4,
                pepper_key_id=issued_admin.pepper_key_id,
                secret_hmac=issued_admin.secret_hmac,
                expires_at=None,
                service_ids=(),
            ),
            now=now,
        )
        issued_user = issue_api_key(keyring)
        user_key = await keys.create(
            ApiKeyCreate(
                user_id=user.id,
                name="user-key",
                lookup_id=issued_user.lookup_id,
                display_prefix=issued_user.display_prefix,
                last4=issued_user.last4,
                pepper_key_id=issued_user.pepper_key_id,
                secret_hmac=issued_user.secret_hmac,
                expires_at=None,
                service_ids=(),
            ),
            now=now,
        )
        await session.execute(
            sa.insert(USER_SERVICE_PERMISSIONS).values(
                user_id=user.id,
                service_id=service.id,
                created_at=now,
                created_by=admin.id,
            )
        )

        # PROTO-13 pins the time-zone boundary: 00:30 Asia/Shanghai is 16:30 UTC on the
        # previous day, so "Today" only contains these records under Asia/Shanghai.
        local_date = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
        boundary = datetime.combine(
            local_date,
            time(hour=0, minute=30),
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ).astimezone(UTC)
        usage_now = boundary + USAGE_CLOCK_OFFSET
        records = (
            _record(
                at=boundary,
                user_id=admin.id,
                api_key_id=admin_key.id,
                service_id=service.id,
                status=RequestRecordStatus.COMPLETED,
                input_tokens=10,
                output_tokens=5,
            ),
            _record(
                at=boundary + timedelta(minutes=1),
                user_id=admin.id,
                api_key_id=admin_key.id,
                service_id=service.id,
                status=RequestRecordStatus.FAILED,
                input_tokens=None,
                output_tokens=2,
            ),
            _record(
                at=boundary + timedelta(minutes=2),
                user_id=admin.id,
                api_key_id=admin_key.id,
                service_id=service.id,
                status=RequestRecordStatus.ABORTED,
                input_tokens=3,
                output_tokens=None,
            ),
            _record(
                at=boundary + timedelta(minutes=3),
                user_id=admin.id,
                api_key_id=admin_key.id,
                service_id=service.id,
                status=RequestRecordStatus.REJECTED,
                input_tokens=99,
                output_tokens=99,
            ),
            _record(
                at=boundary + timedelta(minutes=4),
                user_id=user.id,
                api_key_id=user_key.id,
                service_id=service.id,
                status=RequestRecordStatus.COMPLETED,
                input_tokens=4,
                output_tokens=1,
            ),
            _record(
                at=boundary + timedelta(minutes=5),
                user_id=user.id,
                api_key_id=user_key.id,
                service_id=service.id,
                status=RequestRecordStatus.COMPLETED,
                input_tokens=None,
                output_tokens=None,
            ),
        )
        await ensure_request_record_partitions(
            engine,
            (cast(datetime, record["received_at"]) for record in records),
        )
        await session.execute(sa.insert(LEDGER_REQUEST_RECORDS), records)
        await keys.revoke(user_key.id, now=now + timedelta(seconds=1))
        await session.commit()
        async with session.begin():
            await UsageRepository(session).recompute_recent_buckets(now=usage_now)
        return admin.id, user.id, service.id, user_key.id, boundary


def _cookie(response: AsgiResponse) -> str:
    values: list[str] = []
    for key, raw in response.headers:
        if key.casefold() != "set-cookie":
            continue
        parsed = SimpleCookie()
        parsed.load(raw)
        if SESSION_COOKIE_NAME in parsed:
            values.append(parsed[SESSION_COOKIE_NAME].value)
    assert len(values) == 1
    return values[0]


async def _login(client: AsgiTestClient, username: str) -> Mapping[str, str]:
    response = await client.request(
        "POST",
        "/api/v1/auth/login",
        json_body={"username": username, "password": PASSWORD},
    )
    assert response.status == 200
    return SessionCookies(
        {SESSION_COOKIE_NAME: _cookie(response)},
        csrf_token=str(response.json()["csrf_token"]),
    )


@pytest.fixture(autouse=True)
def _migrated_database(ephemeral_postgres: EphemeralPostgres) -> Iterator[None]:
    upgrade_head(ephemeral_postgres.engine)
    yield


def test_PROTO_13_usage_metrics_timezone_and_ordinary_user_scope(
    ephemeral_postgres: EphemeralPostgres,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_model_instance(ephemeral_postgres.engine)

    async def scenario() -> None:
        engine = create_database_engine(_async_url(ephemeral_postgres))
        factory = create_session_factory(engine)
        admin_id, user_id, service_id, revoked_key_id, boundary = await _seed_usage(factory, engine)

        async def fixed_database_now(session: object) -> datetime:
            del session
            return boundary + USAGE_CLOCK_OFFSET

        monkeypatch.setattr(usage_service_module, "database_utc_now", fixed_database_now)
        client = AsgiTestClient(
            create_app(
                version_payload=VersionPayload(
                    build_version="0.1.0+proto13",
                    commit="1" * 40,
                    schema_version=1,
                    runtime_compat_hash="2" * 64,
                ),
                readiness_checks=[DependencyCheck(name="database", probe=_readiness_ok)],
                credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
                authentication=AuthenticationService(
                    session_factory=factory,
                    password_hasher=PasswordHasher(CHEAP),
                    minimum_login_failure_seconds=0,
                ),
                usage=UsageQueryService(session_factory=factory),
            )
        )
        admin_cookies = await _login(client, "proto-13-admin")
        user_cookies = await _login(client, "proto-13-user")
        from_time = boundary - timedelta(days=1)
        to_time = boundary + timedelta(days=2)

        def query(**values: object) -> str:
            return "/api/v1/usage?" + urlencode(
                {
                    "from": from_time.isoformat(),
                    "to": to_time.isoformat(),
                    **values,
                }
            )

        admin_report = await client.request(
            "GET",
            query(tz="UTC", group_by="user"),
            cookies=admin_cookies,
        )
        assert admin_report.status == 200
        payload = admin_report.json()
        assert payload["summary"] == {
            "requests": 5,
            "input_tokens": 17,
            "output_tokens": 8,
            "total_tokens": 25,
            "error_requests": 1,
            "partial_token_requests": 0,
            "error_rate": 0.2,
        }
        assert {row["display_name"] for row in payload["details"]} == {
            "proto-13-admin",
            "proto-13-user",
        }

        shanghai_report = await client.request(
            "GET",
            query(tz="Asia/Shanghai", group_by="service"),
            cookies=admin_cookies,
        )
        assert shanghai_report.status == 200
        assert (
            payload["trend"][0]["bucket_start"]
            != shanghai_report.json()["trend"][0]["bucket_start"]
        )

        admin_summary = await client.request(
            "GET", "/api/v1/usage/summary?tz=Asia%2FShanghai", cookies=admin_cookies
        )
        user_summary = await client.request(
            "GET", "/api/v1/usage/summary?tz=Asia%2FShanghai", cookies=user_cookies
        )
        assert admin_summary.status == 200
        assert admin_summary.json()["today"]["requests"] == 5
        assert admin_summary.json()["this_month"]["requests"] == 5
        assert admin_summary.json()["data_updated_at"] is not None
        assert user_summary.status == 200
        assert user_summary.json()["today"]["requests"] == 2

        own_revoked_key = await client.request(
            "GET",
            query(api_key_id=revoked_key_id, group_by="api_key"),
            cookies=user_cookies,
        )
        assert own_revoked_key.status == 200
        assert own_revoked_key.json()["summary"]["requests"] == 2
        assert own_revoked_key.json()["details"][0]["status"] == ApiKeyStatus.REVOKED.value

        other_user = await client.request("GET", query(user_id=admin_id), cookies=user_cookies)
        assert other_user.status == 403
        grouped_users = await client.request("GET", query(group_by="user"), cookies=user_cookies)
        assert grouped_users.status == 400
        unauthorized_service = await client.request(
            "GET", query(service_id=UNAUTHORIZED_SERVICE_ID), cookies=user_cookies
        )
        assert unauthorized_service.status == 403
        invalid_time_zone = await client.request(
            "GET", query(tz="Not/A_Timezone"), cookies=admin_cookies
        )
        assert invalid_time_zone.status == 400
        too_long = await client.request(
            "GET",
            "/api/v1/usage?"
            + urlencode(
                {
                    "from": boundary.isoformat(),
                    "to": (boundary + timedelta(days=31, seconds=1)).isoformat(),
                }
            ),
            cookies=admin_cookies,
        )
        assert too_long.status == 400
        await engine.dispose()

    asyncio.run(scenario())

"""PROTO-05: AI Service and personal API Key API on real PostgreSQL."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Final
from uuid import UUID

import httpx
import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from fixtures.asgi_client import (
    NO_CONFIGURED_CREDENTIAL_ROWS,
    AsgiResponse,
    AsgiTestClient,
    SessionCookies,
)
from holm_contracts import VersionPayload
from holm_contracts.agent.v1 import agent_pb2
from holm_control_api.agent_client import AgentSnapshot
from holm_control_api.ai_service_service import AiServiceManagementService
from holm_control_api.api_key_service import ApiKeyManagementService
from holm_control_api.application import create_app
from holm_control_api.authentication import AuthenticationService
from holm_control_api.http_constants import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
)
from holm_control_api.model_instance_service import ModelInstanceService
from holm_control_api.readiness import DependencyCheck, ReadinessReason
from holm_control_api.user_management_routes import ME_SERVICES_PATH
from holm_control_api.user_management_service import (
    LastActiveAdministrator,
    UserManagementService,
)
from holm_domain import (
    ArtifactStorageType,
    MetadataStatus,
    ModelArtifactStatus,
    ModelInstanceStatus,
    ModelOriginType,
    RuntimeType,
    UserRole,
    UserStatus,
)
from holm_gateway.application import (  # type: ignore[import-untyped]
    create_app as create_gateway_app,
)
from holm_gateway.authentication import ApiKeyAuthenticator  # type: ignore[import-untyped]
from holm_gateway.routing import (  # type: ignore[import-untyped]
    CachedRouteDirectory,
    GatewayRoute,
)
from holm_persistence import (
    AsyncSessionFactory,
    PostgresApiKeyValidationLookup,
    UserRepository,
    create_database_engine,
    create_session_factory,
    database_utc_now,
)
from holm_runtimes import RuntimeRequestCapabilities, load_runtime_compatibility
from holm_security import (
    OWASP_MINIMUM_MEMORY_COST_KIB,
    OWASP_MINIMUM_TIME_COST,
    ApiKeyPepperKeyring,
    ApiKeyValidationError,
    Argon2idParameters,
    PasswordHasher,
    validate_api_key,
)
from migration_test_support import upgrade_head
from sqlalchemy.engine import URL, Engine

ADMIN_USERNAME: Final[str] = "proto-05-admin"
MEMBER_USERNAME: Final[str] = "proto-05-member"
PASSWORD: Final[str] = "correct-password-for-proto-05-tests"
CHEAP: Final = Argon2idParameters(
    memory_cost_kib=OWASP_MINIMUM_MEMORY_COST_KIB,
    time_cost=OWASP_MINIMUM_TIME_COST,
    parallelism=1,
)
REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]


def _vllm_request_capabilities() -> RuntimeRequestCapabilities:
    runtime = load_runtime_compatibility(REPOSITORY_ROOT / "runtime-compat.yaml").runtime("vllm")
    if runtime is None:
        raise RuntimeError("vllm compatibility entry is required")
    return runtime.request_capabilities


VLLM_CAPABILITIES: Final = _vllm_request_capabilities()
MODEL_ID: Final[UUID] = UUID("51000000-0000-4000-8000-000000000001")
ARTIFACT_ID: Final[UUID] = UUID("52000000-0000-4000-8000-000000000001")
INSTANCE_1: Final[UUID] = UUID("53000000-0000-4000-8000-000000000001")
INSTANCE_2: Final[UUID] = UUID("53000000-0000-4000-8000-000000000002")


class _UnusedAgent:
    async def observe(self) -> AgentSnapshot:
        return AgentSnapshot(
            host_info=agent_pb2.GetHostInfoResponse(),
            gpu_inventory=agent_pb2.ListGPUsResponse(
                discovery_state=agent_pb2.GPU_DISCOVERY_STATE_READY,
                devices=[],
            ),
        )


@dataclass(frozen=True, slots=True)
class _Environment:
    client: AsgiTestClient
    session_factory: AsyncSessionFactory
    keyring: ApiKeyPepperKeyring
    users: UserManagementService


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


@asynccontextmanager
async def _environment(fixture: EphemeralPostgres) -> AsyncIterator[_Environment]:
    engine = create_database_engine(_async_url(fixture), connect_timeout_seconds=5)
    try:
        session_factory = create_session_factory(engine)
        authentication = AuthenticationService(
            session_factory=session_factory,
            password_hasher=PasswordHasher(CHEAP),
            minimum_login_failure_seconds=0,
        )
        keyring = ApiKeyPepperKeyring(
            current_key_id="proto-05-pepper",
            keys={"proto-05-pepper": b"K" * 32},
        )
        users = UserManagementService(
            session_factory=session_factory,
            password_hasher=PasswordHasher(CHEAP),
        )
        app = create_app(
            version_payload=VersionPayload(
                build_version="0.1.0+proto05",
                commit="5" * 40,
                schema_version=1,
                runtime_compat_hash="6" * 64,
            ),
            readiness_checks=[DependencyCheck(name="database", probe=_readiness_ok)],
            credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
            authentication=authentication,
            model_instances=ModelInstanceService(
                session_factory=session_factory,
                agent=_UnusedAgent(),
            ),
            ai_services=AiServiceManagementService(session_factory=session_factory),
            api_keys=ApiKeyManagementService(
                session_factory=session_factory,
                keyring=keyring,
            ),
            users=users,
        )
        yield _Environment(
            client=AsgiTestClient(app),
            session_factory=session_factory,
            keyring=keyring,
            users=users,
        )
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def _migrated_database(ephemeral_postgres: EphemeralPostgres) -> Iterator[None]:
    upgrade_head(ephemeral_postgres.engine)
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(
            sa.text("TRUNCATE core.ai_services, core.models, core.users, core.audit_events CASCADE")
        )
    _seed_instances(ephemeral_postgres.engine)
    yield
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(
            sa.text("TRUNCATE core.ai_services, core.models, core.users, core.audit_events CASCADE")
        )


def _seed_instances(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO core.models "
                "(id, source_type, source_key, display_name, external_path, metadata_status, "
                "created_at, updated_at) VALUES "
                "(:id, :source_type, :source_key, :display_name, :path, :metadata_status, "
                "clock_timestamp(), clock_timestamp())"
            ),
            {
                "id": MODEL_ID,
                "source_type": ModelOriginType.LOCAL_PATH.value,
                "source_key": "/models/proto-05",
                "display_name": "PROTO-05 Model",
                "path": "/models/proto-05",
                "metadata_status": MetadataStatus.CONFIRMED.value,
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO core.model_artifacts "
                "(id, model_id, source_type, repository_id, resolved_revision, storage_type, "
                "status, path, manifest_hash, size_bytes, created_at, updated_at) VALUES "
                "(:id, :model_id, :source_type, :repository_id, :revision, :storage_type, "
                ":status, :path, :manifest_hash, 1024, clock_timestamp(), clock_timestamp())"
            ),
            {
                "id": ARTIFACT_ID,
                "model_id": MODEL_ID,
                "source_type": ModelOriginType.LOCAL_PATH.value,
                "repository_id": "proto-05",
                "revision": "a" * 40,
                "storage_type": ArtifactStorageType.EXTERNAL.value,
                "status": ModelArtifactStatus.READY.value,
                "path": "/models/proto-05",
                "manifest_hash": "b" * 40,
            },
        )
        for number, instance_id in enumerate((INSTANCE_1, INSTANCE_2), start=1):
            connection.execute(
                sa.text(
                    "INSERT INTO core.model_instances "
                    "(id, artifact_id, runtime_type, expected_gpu_uuids, tensor_parallel_size, "
                    "context_length, gpu_memory_utilization, name, internal_model_name, status, "
                    "error_code, internal_endpoint, config_version, created_at, updated_at) "
                    "VALUES (:id, :artifact_id, :runtime_type, CAST(:gpus AS jsonb), 1, 4096, "
                    "0.9, :display_name, :internal_name, :status, NULL, NULL, 1, "
                    "clock_timestamp(), clock_timestamp())"
                ),
                {
                    "id": instance_id,
                    "artifact_id": ARTIFACT_ID,
                    "runtime_type": RuntimeType.VLLM.value,
                    "gpus": f'["GPU-proto-05-{number}"]',
                    "display_name": f"FX instance {number}",
                    "internal_name": f"mi_{number:012x}",
                    "status": ModelInstanceStatus.STOPPED.value,
                },
            )


async def _create_accounts(environment: _Environment) -> dict[str, UUID]:
    async with environment.session_factory() as session:
        password_hash = await asyncio.to_thread(PasswordHasher(CHEAP).hash, PASSWORD)
        now = await database_utc_now(session)
        repository = UserRepository(session)
        account_ids: dict[str, UUID] = {}
        for username, role in (
            (ADMIN_USERNAME, UserRole.ADMIN),
            (MEMBER_USERNAME, UserRole.USER),
        ):
            record = await repository.create(
                username=username,
                display_name=username,
                role=role,
                password_hash=password_hash,
                now=now,
                must_change_password=False,
            )
            account_ids[username] = record.id
        await session.commit()
        return account_ids


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


async def _login(environment: _Environment, username: str) -> SessionCookies:
    response = await environment.client.request(
        "POST",
        "/api/v1/auth/login",
        json_body={"username": username, "password": PASSWORD},
    )
    assert response.status == 200
    return SessionCookies(
        {SESSION_COOKIE_NAME: _cookie(response)},
        csrf_token=str(response.json()["csrf_token"]),
    )


def _all_cookies(response: AsgiResponse) -> SessionCookies:
    values: dict[str, str] = {}
    for key, raw in response.headers:
        if key.casefold() != "set-cookie":
            continue
        parsed = SimpleCookie()
        parsed.load(raw)
        for name, morsel in parsed.items():
            values[name] = morsel.value
    return SessionCookies(values, csrf_token=values[CSRF_COOKIE_NAME])


async def _login_response(
    environment: _Environment,
    username: str,
    password: str,
) -> AsgiResponse:
    return await environment.client.request(
        "POST",
        "/api/v1/auth/login",
        json_body={"username": username, "password": password},
    )


async def _create_service(
    environment: _Environment,
    admin_cookies: Mapping[str, str],
    *,
    name: str,
    instance_id: UUID,
) -> UUID:
    await _set_instance_status(environment, instance_id, ModelInstanceStatus.RUNNING)
    response = await environment.client.request(
        "POST",
        "/api/v1/services",
        cookies=admin_cookies,
        json_body={
            "public_model_name": name,
            "description": f"{name} description",
            "primary_instance_id": str(instance_id),
        },
    )
    await _set_instance_status(environment, instance_id, ModelInstanceStatus.STOPPED)
    assert response.status == 201
    return UUID(response.json()["id"])


async def _set_instance_status(
    environment: _Environment,
    instance_id: UUID,
    status: ModelInstanceStatus,
) -> None:
    async with environment.session_factory() as session, session.begin():
        await session.execute(
            sa.text(
                "UPDATE core.model_instances SET status = :status, "
                "updated_at = clock_timestamp() WHERE id = :id"
            ),
            {"id": instance_id, "status": status.value},
        )


class _GatewayRoutes:
    def __init__(self, routes: tuple[GatewayRoute, ...]) -> None:
        self._routes = routes

    async def list_routes(self) -> tuple[GatewayRoute, ...]:
        return self._routes


def _gateway_route(service_id: UUID, name: str) -> GatewayRoute:
    return GatewayRoute(
        service_id=service_id,
        instance_id=INSTANCE_1,
        public_model_name=name,
        enabled=True,
        instance_status=ModelInstanceStatus.RUNNING.value,
        runtime_name="vllm",
        internal_endpoint="fake-upstream:8000",
        internal_model_name=f"mi_{service_id.hex[:12]}",
    )


def test_PROTO_16_session_writes_require_the_bound_csrf_token(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    wrong_csrf = "PROTO-16-wrong-csrf-token"
    new_password = "Copper-Forest!9274-Lantern"

    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as environment:
            await _create_accounts(environment)
            admin_login = await _login_response(environment, ADMIN_USERNAME, PASSWORD)
            member_login = await _login_response(environment, MEMBER_USERNAME, PASSWORD)
            assert admin_login.status == member_login.status == 200

            admin_cookies = {SESSION_COOKIE_NAME: _cookie(admin_login)}
            member_cookies = {SESSION_COOKIE_NAME: _cookie(member_login)}
            admin_csrf = str(admin_login.json()["csrf_token"])
            member_csrf = str(member_login.json()["csrf_token"])

            async def create_user(csrf_token: str | None) -> AsgiResponse:
                headers = None if csrf_token is None else {CSRF_HEADER_NAME: csrf_token}
                return await environment.client.request(
                    "POST",
                    "/api/v1/users",
                    cookies=admin_cookies,
                    headers=headers,
                    json_body={"username": "csrf-user", "display_name": "CSRF User"},
                )

            capability_rejections = (
                await create_user(None),
                await create_user(wrong_csrf),
                await create_user(member_csrf),
            )
            assert [response.status for response in capability_rejections] == [403, 403, 403]
            assert {response.json()["error"]["code"] for response in capability_rejections} == {
                "csrf_validation_failed"
            }
            created_user = await create_user(admin_csrf)
            assert created_user.status == 201

            temporary_login = await _login_response(
                environment,
                "csrf-user",
                str(created_user.json()["temporary_password"]),
            )
            assert temporary_login.status == 200
            temporary_cookies = {SESSION_COOKIE_NAME: _cookie(temporary_login)}
            csrf_before_other_gates = await environment.client.request(
                "POST",
                "/api/v1/users",
                cookies=temporary_cookies,
                json_body={"username": "not-created", "display_name": "Not Created"},
            )
            assert csrf_before_other_gates.status == 403
            assert csrf_before_other_gates.json()["error"]["code"] == "csrf_validation_failed"

            async def create_key(csrf_token: str | None) -> AsgiResponse:
                headers = None if csrf_token is None else {CSRF_HEADER_NAME: csrf_token}
                return await environment.client.request(
                    "POST",
                    "/api/v1/me/api-keys",
                    cookies=member_cookies,
                    headers=headers,
                    json_body={"name": "csrf key", "service_ids": []},
                )

            authenticated_rejections = (
                await create_key(None),
                await create_key(wrong_csrf),
                await create_key(admin_csrf),
            )
            assert [response.status for response in authenticated_rejections] == [403, 403, 403]
            assert {response.json()["error"]["code"] for response in authenticated_rejections} == {
                "csrf_validation_failed"
            }
            created_key = await create_key(member_csrf)
            assert created_key.status == 201

            safe_read = await environment.client.get(
                "/api/v1/me/api-keys",
                cookies=member_cookies,
            )
            assert safe_read.status == 200

            for response in (
                *capability_rejections,
                *authenticated_rejections,
                csrf_before_other_gates,
            ):
                assert wrong_csrf not in response.text
                assert admin_csrf not in response.text
                assert member_csrf not in response.text

            changed = await environment.client.request(
                "POST",
                "/api/v1/auth/password",
                cookies=member_cookies,
                headers={CSRF_HEADER_NAME: member_csrf},
                json_body={
                    "current_password": PASSWORD,
                    "new_password": new_password,
                    "confirm_password": new_password,
                },
            )
            assert changed.status == 200
            changed_cookies = {SESSION_COOKIE_NAME: _cookie(changed)}
            changed_csrf = str(changed.json()["csrf_token"])
            logged_out = await environment.client.request(
                "POST",
                "/api/v1/auth/logout",
                cookies=changed_cookies,
                headers={CSRF_HEADER_NAME: changed_csrf},
            )
            assert logged_out.status == 200
            assert logged_out.json() == {"status": "logged_out"}
            assert (
                await environment.client.get("/api/v1/me", cookies=changed_cookies)
            ).status == 401

            async with environment.session_factory() as session:
                account_changes = await session.scalar(
                    sa.text(
                        "SELECT count(*) FROM core.audit_events WHERE action = 'account.change'"
                    )
                )
                api_key_changes = await session.scalar(
                    sa.text(
                        "SELECT count(*) FROM core.audit_events WHERE action = 'api_key.change'"
                    )
                )
            assert account_changes == 1
            assert api_key_changes == 1

    asyncio.run(scenario())


def test_PROTO_18_service_permission_read_is_batched_authorized_and_not_found(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as environment:
            account_ids = await _create_accounts(environment)
            admin = await _login(environment, ADMIN_USERNAME)
            member = await _login(environment, MEMBER_USERNAME)
            service_id = await _create_service(
                environment,
                admin,
                name="proto-18-permissions",
                instance_id=INSTANCE_1,
            )

            updated = await environment.client.request(
                "PUT",
                f"/api/v1/services/{service_id}/permissions",
                cookies=admin,
                json_body={"user_ids": [str(account_ids[MEMBER_USERNAME])]},
            )
            assert updated.status == 200

            fetched = await environment.client.get(
                f"/api/v1/services/{service_id}/permissions",
                cookies=admin,
            )
            assert fetched.status == 200
            assert fetched.json() == updated.json()

            denied = await environment.client.get(
                f"/api/v1/services/{service_id}/permissions",
                cookies=member,
            )
            assert denied.status == 403
            assert denied.json()["error"]["code"] == "permission_denied"

            missing = await environment.client.get(
                "/api/v1/services/00000000-0000-4000-8000-000000000018/permissions",
                cookies=admin,
            )
            assert missing.status == 404
            assert missing.json()["error"]["code"] == "service_not_found"

    asyncio.run(scenario())


def test_M4_11_admin_four_state_status_and_personal_two_state_privacy(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    endpoint_sentinel = "M4-11-INTERNAL-ENDPOINT-SENTINEL"

    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as environment:
            account_ids = await _create_accounts(environment)
            admin = await _login(environment, ADMIN_USERNAME)
            member = await _login(environment, MEMBER_USERNAME)
            await _set_instance_status(
                environment,
                INSTANCE_1,
                ModelInstanceStatus.RUNNING,
            )
            async with environment.session_factory() as session, session.begin():
                await session.execute(
                    sa.text(
                        "UPDATE core.model_instances SET internal_endpoint = :endpoint "
                        "WHERE id = :id"
                    ),
                    {"endpoint": endpoint_sentinel, "id": INSTANCE_1},
                )

            created = await environment.client.request(
                "POST",
                "/api/v1/services",
                cookies=admin,
                json_body={
                    "public_model_name": "m4-11-service",
                    "description": "M4-11 status fixture",
                    "primary_instance_id": str(INSTANCE_1),
                },
            )
            assert created.status == 201
            service_id = UUID(created.json()["id"])
            assert created.json()["runtime_status"] == "Available"
            assert created.json()["primary_instance_status"] == "Running"
            assert set(created.json()) == {
                "name",
                "description",
                "enabled",
                "id",
                "primary_instance_id",
                "primary_instance_status",
                "public_model_name",
                "route_version",
                "runtime_status",
            }

            listed = await environment.client.get("/api/v1/services", cookies=admin)
            detail = await environment.client.get(
                f"/api/v1/services/{service_id}",
                cookies=admin,
            )
            assert listed.json()["items"] == [detail.json()] == [created.json()]

            granted = await environment.client.request(
                "PUT",
                f"/api/v1/users/{account_ids[MEMBER_USERNAME]}/permissions",
                cookies=admin,
                json_body={"service_ids": [str(service_id)]},
            )
            assert granted.status == 200
            personal_available = await environment.client.get(
                ME_SERVICES_PATH,
                cookies=member,
            )
            assert personal_available.status == 200
            assert personal_available.json()["items"][0]["available"] is True
            assert set(personal_available.json()["items"][0]) == {
                "name",
                "available",
                "enabled",
                "id",
                "last_used_at",
                "last_succeeded_at",
                "description",
                "public_model_name",
            }
            assert endpoint_sentinel not in personal_available.text
            assert "busy" not in personal_available.json()["items"][0]
            assert "GPU-proto-05" not in personal_available.text

            await _set_instance_status(
                environment,
                INSTANCE_1,
                ModelInstanceStatus.STARTING,
            )
            degraded = await environment.client.get(
                f"/api/v1/services/{service_id}",
                cookies=admin,
            )
            assert degraded.json()["runtime_status"] == "Degraded"
            assert degraded.json()["primary_instance_status"] == "Starting"
            personal_unavailable = await environment.client.get(
                ME_SERVICES_PATH,
                cookies=member,
            )
            assert personal_unavailable.json()["items"] == [
                {
                    "available": False,
                    "enabled": True,
                    "id": str(service_id),
                    "last_used_at": None,
                    "last_succeeded_at": None,
                    "description": "M4-11 status fixture",
                    "name": created.json()["name"],
                    "public_model_name": "m4-11-service",
                }
            ]

            await _set_instance_status(
                environment,
                INSTANCE_1,
                ModelInstanceStatus.STOPPED,
            )
            unavailable = await environment.client.get(
                f"/api/v1/services/{service_id}",
                cookies=admin,
            )
            assert unavailable.json()["runtime_status"] == "Unavailable"
            disabled = await environment.client.request(
                "PATCH",
                f"/api/v1/services/{service_id}",
                cookies=admin,
                json_body={"enabled": False},
            )
            assert disabled.status == 200
            assert disabled.json()["runtime_status"] == "Disabled"
            assert disabled.json()["primary_instance_status"] == "Stopped"

    asyncio.run(scenario())


def test_PROTO_05_services_keys_audit_validation_and_instance_guard(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> tuple[str, UUID]:
        async with _environment(ephemeral_postgres) as environment:
            account_ids = await _create_accounts(environment)
            admin = await _login(environment, ADMIN_USERNAME)
            member = await _login(environment, MEMBER_USERNAME)

            await _set_instance_status(
                environment,
                INSTANCE_1,
                ModelInstanceStatus.RUNNING,
            )

            created_service = await environment.client.request(
                "POST",
                "/api/v1/services",
                cookies=admin,
                json_body={
                    "public_model_name": "qwen-mini",
                    "description": "prototype service",
                    "primary_instance_id": str(INSTANCE_1),
                },
            )
            assert created_service.status == 201
            service_id = UUID(created_service.json()["id"])
            duplicate = await environment.client.request(
                "POST",
                "/api/v1/services",
                cookies=admin,
                json_body={
                    "public_model_name": "qwen-mini",
                    "primary_instance_id": str(INSTANCE_1),
                },
            )
            assert duplicate.status == 409
            assert duplicate.json()["error"]["code"] == "service_name_conflict"

            denied = await environment.client.get("/api/v1/services", cookies=member)
            assert denied.status == 403
            await _set_instance_status(
                environment,
                INSTANCE_2,
                ModelInstanceStatus.RUNNING,
            )
            updated = await environment.client.request(
                "PATCH",
                f"/api/v1/services/{service_id}",
                cookies=admin,
                json_body={
                    "enabled": False,
                    "primary_instance_id": str(INSTANCE_2),
                },
            )
            assert updated.status == 200
            assert updated.json()["route_version"] == 2
            assert not updated.json()["enabled"]
            fetched = await environment.client.get(f"/api/v1/services/{service_id}", cookies=admin)
            listed_services = await environment.client.get("/api/v1/services", cookies=admin)
            assert fetched.json() == updated.json()
            assert listed_services.json()["items"] == [updated.json()]

            granted = await environment.client.request(
                "PUT",
                f"/api/v1/users/{account_ids[MEMBER_USERNAME]}/permissions",
                cookies=admin,
                json_body={"service_ids": [str(service_id)]},
            )
            assert granted.status == 200

            created_key = await environment.client.request(
                "POST",
                "/api/v1/me/api-keys",
                cookies=member,
                json_body={"name": "CLI", "service_ids": [str(service_id)]},
            )
            assert created_key.status == 201
            plaintext = created_key.json()["api_key"]
            key_id = UUID(created_key.json()["id"])
            assert plaintext.startswith("holm_sk_")

            listed_keys = await environment.client.get("/api/v1/me/api-keys", cookies=member)
            assert listed_keys.status == 200
            assert len(listed_keys.json()["items"]) == 1
            item = listed_keys.json()["items"][0]
            assert set(item) == {
                "id",
                "name",
                "display_prefix",
                "last4",
                "status",
                "expires_at",
                "last_used_at",
                "created_at",
                "revoked_at",
                "service_ids",
                "effective_services",
            }
            assert item["display_prefix"] == "holm_sk_"
            assert item["last4"] == plaintext[-4:]
            admin_keys = await environment.client.get("/api/v1/me/api-keys", cookies=admin)
            assert admin_keys.json()["items"] == []

            lookup = PostgresApiKeyValidationLookup(environment.session_factory)
            validated = await validate_api_key(
                plaintext,
                lookup=lookup,
                keyring=environment.keyring,
            )
            assert validated.scoped_service_ids == frozenset({service_id})
            assert not validated.unrestricted

            async with environment.session_factory() as session, session.begin():
                await session.execute(
                    sa.text(
                        "UPDATE core.model_instances SET status = :status, "
                        "internal_endpoint = NULL, updated_at = clock_timestamp() WHERE id = :id"
                    ),
                    {"id": INSTANCE_2, "status": ModelInstanceStatus.STOPPED.value},
                )

            delete_referenced = await environment.client.request(
                "DELETE",
                f"/api/v1/instances/{INSTANCE_2}",
                cookies=admin,
            )
            assert delete_referenced.status == 409
            assert delete_referenced.json()["error"]["code"] == "instance_referenced"

            revoked = await environment.client.request(
                "POST",
                f"/api/v1/me/api-keys/{key_id}:revoke",
                cookies=member,
            )
            assert revoked.status == 200
            assert revoked.json()["status"] == "Revoked"
            with pytest.raises(ApiKeyValidationError) as failure:
                await validate_api_key(
                    plaintext,
                    lookup=lookup,
                    keyring=environment.keyring,
                )
            assert failure.value.code == "invalid_api_key"
            return plaintext, key_id

    plaintext, key_id = asyncio.run(scenario())
    _, separator, encoded_secret = plaintext.removeprefix("holm_sk_").partition("_")
    assert separator
    with ephemeral_postgres.engine.connect() as connection:
        key_row = (
            connection.execute(
                sa.text(
                    "SELECT lookup_id, display_prefix, last4, pepper_key_id, secret_hmac, status "
                    "FROM core.api_keys WHERE id = :id"
                ),
                {"id": key_id},
            )
            .mappings()
            .one()
        )
        audits = tuple(
            connection.execute(
                sa.text(
                    "SELECT action, details::text AS details FROM core.audit_events "
                    "WHERE action IN ('ai_service.change', 'api_key.change') ORDER BY created_at"
                )
            ).mappings()
        )
    assert key_row["display_prefix"] == "holm_sk_"
    assert key_row["last4"] == plaintext[-4:]
    assert len(bytes(key_row["secret_hmac"])) == 32
    assert plaintext not in " ".join(str(value) for value in key_row.values())
    assert encoded_secret not in " ".join(str(value) for value in key_row.values())
    assert [row["action"] for row in audits] == [
        "ai_service.change",
        "ai_service.change",
        "api_key.change",
        "api_key.change",
    ]
    assert [row["details"] for row in audits] == [
        "{}",
        "{}",
        '{"operation": "create"}',
        '{"operation": "revoke"}',
    ]


def test_PROTO_11_temporary_password_lifecycle_revokes_sessions_keys_and_permissions(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    permanent_password = "Violet-River!6429-Quartz"

    async def scenario() -> tuple[str, str, str, UUID]:
        async with _environment(ephemeral_postgres) as environment:
            account_ids = await _create_accounts(environment)
            admin = await _login(environment, ADMIN_USERNAME)
            service_id = await _create_service(
                environment,
                admin,
                name="qwen-mini",
                instance_id=INSTANCE_1,
            )
            created = await environment.client.request(
                "POST",
                "/api/v1/users",
                cookies=admin,
                json_body={"username": "Alice", "display_name": "Alice Example"},
            )
            assert created.status == 201
            created_body = created.json()
            alice_id = UUID(created_body["id"])
            temporary_password = str(created_body["temporary_password"])
            assert created_body["username"] == "alice"
            assert created_body["must_change_password"] is True
            searched = await environment.client.get(
                "/api/v1/users?search=ali&role=user&status=active",
                cookies=admin,
            )
            assert [item["id"] for item in searched.json()["items"]] == [str(alice_id)]
            initial_detail = await environment.client.get(
                f"/api/v1/users/{alice_id}",
                cookies=admin,
            )
            assert initial_detail.status == 200
            assert "temporary_password" not in initial_detail.json()

            async with environment.session_factory() as session:
                stored = (
                    (
                        await session.execute(
                            sa.text(
                                "SELECT password_hash, must_change_password FROM core.users "
                                "WHERE id = :user_id"
                            ),
                            {"user_id": alice_id},
                        )
                    )
                    .mappings()
                    .one()
                )
            assert str(stored["password_hash"]).startswith("$argon2id$")
            assert temporary_password not in str(dict(stored))
            assert stored["must_change_password"] is True

            first_login = await _login_response(environment, "alice", temporary_password)
            assert first_login.status == 200
            assert first_login.json()["must_change_password"] is True
            first_cookies = _all_cookies(first_login)
            gated = await environment.client.get(ME_SERVICES_PATH, cookies=first_cookies)
            assert gated.status == 403
            assert gated.json()["error"]["code"] == "password_change_required"

            changed = await environment.client.request(
                "POST",
                "/api/v1/auth/password",
                cookies=first_cookies,
                headers={CSRF_HEADER_NAME: str(first_login.json()["csrf_token"])},
                json_body={
                    "new_password": permanent_password,
                    "confirm_password": permanent_password,
                },
            )
            assert changed.status == 200
            alice_cookies = _all_cookies(changed)
            granted = await environment.client.request(
                "PUT",
                f"/api/v1/users/{alice_id}/permissions",
                cookies=admin,
                json_body={"service_ids": [str(service_id)]},
            )
            assert granted.status == 200
            created_key = await environment.client.request(
                "POST",
                "/api/v1/me/api-keys",
                cookies=alice_cookies,
                json_body={"name": "Alice CLI", "service_ids": []},
            )
            assert created_key.status == 201
            plaintext_key = str(created_key.json()["api_key"])

            disabled = await environment.client.request(
                "POST",
                f"/api/v1/users/{alice_id}:disable",
                cookies=admin,
            )
            assert disabled.status == 200
            assert disabled.json()["status"] == UserStatus.DISABLED.value
            assert (await environment.client.get("/api/v1/me", cookies=alice_cookies)).status == 401
            assert (await _login_response(environment, "alice", permanent_password)).status == 401
            with pytest.raises(ApiKeyValidationError) as disabled_key:
                await validate_api_key(
                    plaintext_key,
                    lookup=PostgresApiKeyValidationLookup(environment.session_factory),
                    keyring=environment.keyring,
                )
            assert disabled_key.value.code == "invalid_api_key"
            async with environment.session_factory() as session:
                stored_key_status = await session.scalar(
                    sa.text("SELECT status FROM core.api_keys WHERE user_id = :user_id"),
                    {"user_id": alice_id},
                )
            assert stored_key_status == "Active"

            enabled = await environment.client.request(
                "POST",
                f"/api/v1/users/{alice_id}:enable",
                cookies=admin,
            )
            assert enabled.status == 200
            restored = await validate_api_key(
                plaintext_key,
                lookup=PostgresApiKeyValidationLookup(environment.session_factory),
                keyring=environment.keyring,
            )
            assert restored.user_id == alice_id
            restored_login = await _login_response(environment, "alice", permanent_password)
            assert restored_login.status == 200
            restored_cookies = _all_cookies(restored_login)

            reset = await environment.client.request(
                "POST",
                f"/api/v1/users/{alice_id}:reset-password",
                cookies=admin,
            )
            assert reset.status == 200
            reset_password = str(reset.json()["temporary_password"])
            assert reset.json()["must_change_password"] is True
            assert (
                await environment.client.get("/api/v1/me", cookies=restored_cookies)
            ).status == 401
            reset_did_not_revoke_key = await validate_api_key(
                plaintext_key,
                lookup=PostgresApiKeyValidationLookup(environment.session_factory),
                keyring=environment.keyring,
            )
            assert reset_did_not_revoke_key.user_id == alice_id

            deleted = await environment.client.request(
                "POST",
                f"/api/v1/users/{alice_id}:delete",
                cookies=admin,
            )
            assert deleted.status == 200
            assert deleted.json()["status"] == UserStatus.DELETED.value
            default_list = await environment.client.get("/api/v1/users", cookies=admin)
            assert str(alice_id) not in {item["id"] for item in default_list.json()["items"]}
            deleted_list = await environment.client.get(
                "/api/v1/users?include_deleted=true&status=deleted",
                cookies=admin,
            )
            assert [item["id"] for item in deleted_list.json()["items"]] == [str(alice_id)]
            with pytest.raises(ApiKeyValidationError):
                await validate_api_key(
                    plaintext_key,
                    lookup=PostgresApiKeyValidationLookup(environment.session_factory),
                    keyring=environment.keyring,
                )
            assert account_ids[ADMIN_USERNAME] != alice_id
            return temporary_password, reset_password, plaintext_key, alice_id

    temporary_password, reset_password, plaintext_key, alice_id = asyncio.run(scenario())
    with ephemeral_postgres.engine.connect() as connection:
        row = (
            (
                connection.execute(
                    sa.text(
                        "SELECT status, deleted_at, password_hash FROM core.users "
                        "WHERE id = :user_id"
                    ),
                    {"user_id": alice_id},
                )
            )
            .mappings()
            .one()
        )
        key_states = tuple(
            connection.execute(
                sa.text("SELECT status FROM core.api_keys WHERE user_id = :user_id"),
                {"user_id": alice_id},
            ).scalars()
        )
        permission_count = int(
            connection.execute(
                sa.text(
                    "SELECT count(*) FROM core.user_service_permissions WHERE user_id = :user_id"
                ),
                {"user_id": alice_id},
            ).scalar_one()
        )
        audit_details = tuple(
            connection.execute(
                sa.text(
                    "SELECT details::text FROM core.audit_events "
                    "WHERE object_id = :user_id ORDER BY created_at"
                ),
                {"user_id": alice_id},
            ).scalars()
        )
    assert row["status"] == UserStatus.DELETED.value
    assert row["deleted_at"] is not None
    assert temporary_password not in str(row["password_hash"])
    assert reset_password not in str(row["password_hash"])
    assert key_states == ("Revoked",)
    assert permission_count == 0
    rendered_audit = " ".join(audit_details)
    assert temporary_password not in rendered_audit
    assert reset_password not in rendered_audit
    assert plaintext_key not in rendered_audit


def test_PROTO_11_shared_permissions_filter_me_keys_and_gateway(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def upstream(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            content=b"data: "
            + json.dumps(
                {
                    "id": "chatcmpl-proto11",
                    "object": "chat.completion.chunk",
                    "model": payload["model"],
                    "choices": [{"delta": {"role": "assistant", "content": "ok"}}],
                }
            ).encode()
            + b"\n\ndata: [DONE]\n\n",
        )

    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as environment:
            account_ids = await _create_accounts(environment)
            admin = await _login(environment, ADMIN_USERNAME)
            member = await _login(environment, MEMBER_USERNAME)
            service_a = await _create_service(
                environment,
                admin,
                name="qwen-mini",
                instance_id=INSTANCE_1,
            )
            service_b = await _create_service(
                environment,
                admin,
                name="other-mini",
                instance_id=INSTANCE_2,
            )
            unauthorized_scope = await environment.client.request(
                "POST",
                "/api/v1/me/api-keys",
                cookies=member,
                json_body={"name": "bad scope", "service_ids": [str(service_a)]},
            )
            assert unauthorized_scope.status == 403
            assert unauthorized_scope.json()["error"]["code"] == "service_not_authorized"

            empty_scope = await environment.client.request(
                "POST",
                "/api/v1/me/api-keys",
                cookies=member,
                json_body={"name": "all authorized", "service_ids": []},
            )
            assert empty_scope.status == 201
            member_key = str(empty_scope.json()["api_key"])
            admin_key_response = await environment.client.request(
                "POST",
                "/api/v1/me/api-keys",
                cookies=admin,
                json_body={"name": "admin", "service_ids": []},
            )
            assert admin_key_response.status == 201
            admin_key = str(admin_key_response.json()["api_key"])

            routes = (
                _gateway_route(service_a, "qwen-mini"),
                _gateway_route(service_b, "other-mini"),
            )
            upstream_client = httpx.AsyncClient(
                transport=httpx.MockTransport(upstream),
                timeout=None,
            )
            gateway = create_gateway_app(
                authenticator=ApiKeyAuthenticator(
                    lookup=PostgresApiKeyValidationLookup(environment.session_factory),
                    keyring=environment.keyring,
                ),
                routes=CachedRouteDirectory(_GatewayRoutes(routes), ttl_seconds=2),
                runtime_capabilities={"vllm": VLLM_CAPABILITIES},
                upstream_client=upstream_client,
            )
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=gateway),
                    base_url="http://gateway",
                ) as client:
                    member_headers = {"Authorization": f"Bearer {member_key}"}
                    initial_models = await client.get("/v1/models", headers=member_headers)
                    assert initial_models.status_code == 200
                    assert initial_models.json()["data"] == []
                    denied = await client.post(
                        "/v1/chat/completions",
                        headers=member_headers,
                        json={"model": "qwen-mini", "messages": []},
                    )
                    assert denied.status_code == 403
                    assert denied.json()["error"]["code"] == "service_not_authorized"

                    by_user = await environment.client.request(
                        "PUT",
                        f"/api/v1/users/{account_ids[MEMBER_USERNAME]}/permissions",
                        cookies=admin,
                        json_body={"service_ids": [str(service_a)]},
                    )
                    assert by_user.status == 200
                    by_service = await environment.client.request(
                        "PUT",
                        f"/api/v1/services/{service_b}/permissions",
                        cookies=admin,
                        json_body={"user_ids": [str(account_ids[MEMBER_USERNAME])]},
                    )
                    assert by_service.status == 200
                    detail = await environment.client.get(
                        f"/api/v1/users/{account_ids[MEMBER_USERNAME]}",
                        cookies=admin,
                    )
                    assert {item["id"] for item in detail.json()["authorized_services"]} == {
                        str(service_a),
                        str(service_b),
                    }
                    assert detail.json()["authorized_service_count"] == 2
                    assert detail.json()["api_key_count"] == 1
                    assert all(
                        set(item)
                        == {
                            "id",
                            "name",
                            "display_prefix",
                            "last4",
                            "status",
                            "expires_at",
                            "last_used_at",
                        }
                        for item in detail.json()["api_keys"]
                    )

                    by_user_again = await environment.client.request(
                        "PUT",
                        f"/api/v1/users/{account_ids[MEMBER_USERNAME]}/permissions",
                        cookies=admin,
                        json_body={"service_ids": [str(service_b)]},
                    )
                    assert by_user_again.status == 200
                    validated_member = await validate_api_key(
                        member_key,
                        lookup=PostgresApiKeyValidationLookup(environment.session_factory),
                        keyring=environment.keyring,
                    )
                    assert not validated_member.user_is_admin
                    assert validated_member.authorized_service_ids == frozenset({service_b})
                    personal = await environment.client.get(ME_SERVICES_PATH, cookies=member)
                    assert personal.status == 200
                    assert personal.json()["items"] == [
                        {
                            "name": "other-mini",
                            "public_model_name": "other-mini",
                            "description": "other-mini description",
                            "available": False,
                            "enabled": True,
                            "id": str(service_b),
                            "last_used_at": None,
                            "last_succeeded_at": None,
                        }
                    ]
                    filtered_models = await client.get("/v1/models", headers=member_headers)
                    assert [item["id"] for item in filtered_models.json()["data"]] == ["other-mini"]
                    allowed = await client.post(
                        "/v1/chat/completions",
                        headers=member_headers,
                        json={"model": "other-mini", "messages": []},
                    )
                    assert allowed.status_code == 200
                    denied_after_replace = await client.post(
                        "/v1/chat/completions",
                        headers=member_headers,
                        json={"model": "qwen-mini", "messages": []},
                    )
                    assert denied_after_replace.status_code == 403

                    grant_admin = await environment.client.request(
                        "PUT",
                        f"/api/v1/users/{account_ids[ADMIN_USERNAME]}/permissions",
                        cookies=admin,
                        json_body={"service_ids": [str(service_a), str(service_b)]},
                    )
                    assert grant_admin.status == 200
                    admin_models = await client.get(
                        "/v1/models",
                        headers={"Authorization": f"Bearer {admin_key}"},
                    )
                    assert {item["id"] for item in admin_models.json()["data"]} == {
                        "qwen-mini",
                        "other-mini",
                    }
            finally:
                await upstream_client.aclose()

    asyncio.run(scenario())


def test_PROTO_11_self_and_last_active_administrator_protection(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as environment:
            account_ids = await _create_accounts(environment)
            admin = await _login(environment, ADMIN_USERNAME)
            admin_id = account_ids[ADMIN_USERNAME]
            self_disable = await environment.client.request(
                "POST",
                f"/api/v1/users/{admin_id}:disable",
                cookies=admin,
            )
            self_delete = await environment.client.request(
                "POST",
                f"/api/v1/users/{admin_id}:delete",
                cookies=admin,
            )
            assert self_disable.status == 409
            assert self_delete.status == 409
            assert self_disable.json()["error"]["code"] == "self_protection"
            assert self_delete.json()["error"]["code"] == "self_protection"

            with pytest.raises(LastActiveAdministrator):
                await environment.users.disable_user(
                    admin_id,
                    actor_id=account_ids[MEMBER_USERNAME],
                    request_id=UUID("00000000-0000-7000-8000-000000001111"),
                )

    asyncio.run(scenario())

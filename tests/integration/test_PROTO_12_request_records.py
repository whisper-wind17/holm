"""PROTO-12 Request Records on real PostgreSQL."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator, Mapping
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Final
from urllib.parse import urlencode
from uuid import UUID

import httpx
import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from fixtures.asgi_client import NO_CONFIGURED_CREDENTIAL_ROWS, AsgiResponse, AsgiTestClient
from holm_common import new_uuid7, utc_now
from holm_contracts import VersionPayload
from holm_control_api.application import create_app
from holm_control_api.authentication import AuthenticationService
from holm_control_api.http_constants import SESSION_COOKIE_NAME
from holm_control_api.readiness import DependencyCheck, ReadinessReason
from holm_control_api.request_record_service import (
    RequestRecordQuery,
    RequestRecordQueryService,
)
from holm_domain import (
    ArtifactStorageType,
    MetadataStatus,
    ModelArtifactStatus,
    ModelInstanceStatus,
    ModelOriginType,
    RequestRecordState,
    RuntimeType,
    UserRole,
)
from holm_gateway.application import (  # type: ignore[import-untyped]
    create_app as create_gateway_app,
)
from holm_gateway.authentication import ApiKeyAuthenticator  # type: ignore[import-untyped]
from holm_gateway.routing import (  # type: ignore[import-untyped]
    CachedRouteDirectory,
    GatewayRoute,
)
from holm_ledger import ConnectionRole, EventType, SpoolWriter, open_ledger
from holm_persistence import (
    LEDGER_REQUEST_RECORDS,
    REQUEST_RECORDS,
    AiServiceCreate,
    AiServiceRepository,
    ApiKeyCreate,
    ApiKeyRepository,
    AsyncSessionFactory,
    PostgresApiKeyValidationLookup,
    RequestRecordLedgerRepository,
    UserManagementRepository,
    UserRepository,
    create_database_engine,
    create_session_factory,
    database_utc_now,
    ensure_request_record_partitions,
)
from holm_runtimes import RuntimeRequestCapabilities, load_runtime_compatibility
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
from starlette.types import ASGIApp, Message, Scope

ADMIN_USERNAME: Final[str] = "proto-12-admin"
USER_USERNAME: Final[str] = "proto-12-user"
PASSWORD: Final[str] = "correct-password-for-proto-12-tests"
SENTINEL: Final[str] = "PROTO_12_SENTINEL_PROMPT_3f7d0906"
MODEL_ID: Final[UUID] = UUID("12000000-0000-4000-8000-000000000001")
ARTIFACT_ID: Final[UUID] = UUID("12000000-0000-4000-8000-000000000002")
INSTANCE_ID: Final[UUID] = UUID("12000000-0000-4000-8000-000000000003")
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


class _Routes:
    def __init__(self, route: GatewayRoute) -> None:
        self._route = route

    async def list_routes(self) -> tuple[GatewayRoute, ...]:
        return (self._route,)


class _BlockingStream(httpx.AsyncByteStream):
    def __init__(self, first: bytes) -> None:
        self._first = first
        self._release = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._first
        await self._release.wait()

    async def aclose(self) -> None:
        self._release.set()


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
                "(:id, :source_type, :source_key, :display_name, :path, :metadata_status, "
                "clock_timestamp(), clock_timestamp())"
            ),
            {
                "id": MODEL_ID,
                "source_type": ModelOriginType.LOCAL_PATH.value,
                "source_key": "/models/proto-12",
                "display_name": "PROTO-12 Model",
                "path": "/models/proto-12",
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
                "repository_id": "proto-12",
                "revision": "1" * 40,
                "storage_type": ArtifactStorageType.EXTERNAL.value,
                "status": ModelArtifactStatus.READY.value,
                "path": "/models/proto-12",
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
                "0.9, 'FX instance 12', 'mi_000000000012', :status, NULL, NULL, 1, "
                "clock_timestamp(), clock_timestamp())"
            ),
            {
                "id": INSTANCE_ID,
                "artifact_id": ARTIFACT_ID,
                "runtime_type": RuntimeType.VLLM.value,
                "gpus": '["GPU-proto-12"]',
                "status": ModelInstanceStatus.STOPPED.value,
            },
        )


async def _seed_identities(
    factory: AsyncSessionFactory,
) -> tuple[UUID, UUID, UUID, UUID, str, str, ApiKeyPepperKeyring]:
    keyring = ApiKeyPepperKeyring(
        current_key_id="proto-12",
        keys={"proto-12": b"R" * 32},
    )
    password_hash = await asyncio.to_thread(PasswordHasher(CHEAP).hash, PASSWORD)
    async with factory() as session:
        now = await database_utc_now(session)
        users = UserRepository(session)
        admin = await users.create(
            username=ADMIN_USERNAME,
            display_name="PROTO-12 Admin",
            role=UserRole.ADMIN,
            password_hash=password_hash,
            now=now,
            must_change_password=False,
        )
        user = await users.create(
            username=USER_USERNAME,
            display_name="PROTO-12 User",
            role=UserRole.USER,
            password_hash=password_hash,
            now=now,
            must_change_password=False,
        )
        service = await AiServiceRepository(session).create(
            AiServiceCreate(
                public_model_name="qwen-mini",
                description="PROTO-12 service",
                enabled=True,
                primary_instance_id=INSTANCE_ID,
            ),
            now=now,
        )
        await UserManagementRepository(session).replace_for_user(
            admin.id, (service.id,), actor_id=admin.id, now=now
        )
        issued_admin = issue_api_key(keyring)
        admin_key = await ApiKeyRepository(session).create(
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
        await ApiKeyRepository(session).create(
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
        await session.commit()
    return (
        admin.id,
        user.id,
        service.id,
        admin_key.id,
        issued_admin.plaintext,
        issued_user.plaintext,
        keyring,
    )


def _gateway_scope(body: bytes, key: str) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"authorization", f"Bearer {key}".encode("ascii")),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("gateway", 80),
    }


async def _gateway_request(
    app: ASGIApp,
    *,
    key: str,
    payload: dict[str, object],
) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://gateway",
    ) as client:
        return await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json=payload,
        )


async def _disconnect_stream(app: ASGIApp, key: str) -> None:
    body = json.dumps({"model": "qwen-mini", "messages": [], "stream": True}).encode()
    disconnect = asyncio.Event()
    delivered_request = False

    async def receive() -> Message:
        nonlocal delivered_request
        if not delivered_request:
            delivered_request = True
            return {"type": "http.request", "body": body, "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            disconnect.set()

    await asyncio.wait_for(app(_gateway_scope(body, key), receive, send), timeout=1)


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
    return {SESSION_COOKIE_NAME: _cookie(response)}


@pytest.fixture(autouse=True)
def _migrated_database(ephemeral_postgres: EphemeralPostgres) -> Iterator[None]:
    upgrade_head(ephemeral_postgres.engine)
    yield


def test_PROTO_12_M5_11_gateway_records_only_allowlisted_metadata_and_admin_api_filters(
    ephemeral_postgres: EphemeralPostgres,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    _seed_model_instance(ephemeral_postgres.engine)

    async def scenario() -> tuple[set[str], UUID]:
        engine = create_database_engine(_async_url(ephemeral_postgres))
        factory = create_session_factory(engine)
        (
            admin_id,
            user_id,
            service_id,
            admin_key_id,
            admin_plaintext,
            user_plaintext,
            keyring,
        ) = await _seed_identities(factory)
        route = GatewayRoute(
            service_id=service_id,
            instance_id=INSTANCE_ID,
            public_model_name="qwen-mini",
            enabled=True,
            instance_status=ModelInstanceStatus.RUNNING.value,
            runtime_name="vllm",
            internal_endpoint="fake-upstream:8000",
            internal_model_name="mi_000000000012",
        )
        ledger_path = tmp_path / "spool" / "requests.sqlite3"
        ledger_path.parent.mkdir(mode=0o700)
        writer = SpoolWriter(ledger_path)
        writer.start()
        captured_stream_options: list[object] = []

        async def successful_upstream(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            assert payload["stream"] is True
            # FX-PROTO-12: the two external streaming calls have no messages;
            # retain their distinct usage values with streaming upstream for all.
            if not payload["messages"]:
                captured_stream_options.append(payload.get("stream_options"))
                stream_body = (
                    b'data: {"model":"mi_000000000012","choices":'
                    b'[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
                    b'data: {"model":"mi_000000000012","choices":[],"usage":'
                    b'{"prompt_tokens":11,"completion_tokens":13,"total_tokens":24}}\n\n'
                    b"data: [DONE]\n\n"
                )
                return httpx.Response(
                    200,
                    content=stream_body,
                    headers={"content-type": "text/event-stream"},
                )
            return httpx.Response(
                200,
                content=b"data: "
                + json.dumps(
                    {
                        "model": "mi_000000000012",
                        "choices": [{"finish_reason": "stop", "delta": {"content": "ok"}}],
                        "usage": {"prompt_tokens": 7, "completion_tokens": 9},
                    }
                ).encode()
                + b"\n\ndata: [DONE]\n\n",
            )

        async def failed_upstream(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, content=b"upstream detail must not escape")

        disconnect_stream = _BlockingStream(
            b'data: {"model":"mi_000000000012","choices":[{"delta":{"content":"first"}}]}\n\n'
        )

        async def blocking_upstream(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                stream=disconnect_stream,
                headers={"content-type": "text/event-stream"},
            )

        authenticator = ApiKeyAuthenticator(
            lookup=PostgresApiKeyValidationLookup(factory),
            keyring=keyring,
        )

        def gateway(
            handler: Callable[[httpx.Request], Coroutine[None, None, httpx.Response]],
        ) -> ASGIApp:
            application: ASGIApp = create_gateway_app(
                authenticator=authenticator,
                routes=CachedRouteDirectory(_Routes(route), ttl_seconds=2),
                runtime_capabilities={"vllm": VLLM_CAPABILITIES},
                upstream_client=httpx.AsyncClient(
                    transport=httpx.MockTransport(handler),
                    timeout=None,
                ),
                request_ledger=writer,
            )
            return application

        success_app = gateway(successful_upstream)
        failed_app = gateway(failed_upstream)
        disconnect_app = gateway(blocking_upstream)
        responses: list[httpx.Response] = []
        try:
            with caplog.at_level("INFO"):
                responses.append(
                    await _gateway_request(
                        success_app,
                        key=admin_plaintext,
                        payload={
                            "model": "qwen-mini",
                            "messages": [{"role": "user", "content": SENTINEL}],
                        },
                    )
                )
                injected = await _gateway_request(
                    success_app,
                    key=admin_plaintext,
                    payload={"model": "qwen-mini", "messages": [], "stream": True},
                )
                responses.append(injected)
                included = await _gateway_request(
                    success_app,
                    key=admin_plaintext,
                    payload={
                        "model": "qwen-mini",
                        "messages": [],
                        "stream": True,
                        "stream_options": {"include_usage": True},
                    },
                )
                responses.append(included)
                responses.append(
                    await _gateway_request(
                        failed_app,
                        key=admin_plaintext,
                        payload={"model": "qwen-mini", "messages": []},
                    )
                )
                responses.append(
                    await _gateway_request(
                        success_app,
                        key="holm_sk_invalid",
                        payload={"model": "qwen-mini", "messages": []},
                    )
                )
                responses.append(
                    await _gateway_request(
                        success_app,
                        key=admin_plaintext,
                        payload={"model": "unknown", "messages": []},
                    )
                )
                responses.append(
                    await _gateway_request(
                        success_app,
                        key=user_plaintext,
                        payload={"model": "qwen-mini", "messages": []},
                    )
                )
                await _disconnect_stream(disconnect_app, admin_plaintext)
            assert [response.status_code for response in responses] == [
                200,
                200,
                200,
                503,
                401,
                404,
                403,
            ]
            assert '"usage"' not in injected.text
            assert '"usage"' in included.text
            assert captured_stream_options == [
                {"include_usage": True},
                {"include_usage": True},
            ]
        finally:
            await writer.close()
        with open_ledger(ledger_path, ConnectionRole.READER) as ledger:
            events = tuple(stored.event for stored in ledger.list_unacked(limit=500))
        async with factory() as session:
            await RequestRecordLedgerRepository(session, engine).upsert_batch(events)
            await session.commit()
            assert await session.scalar(sa.text("SELECT count(*) FROM core.request_records")) == 0
        for path in (ledger_path, Path(f"{ledger_path}-wal")):
            if path.exists():
                assert SENTINEL.encode() not in path.read_bytes()

        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        sa.text(
                            "SELECT request_id, received_at, terminal_at AS completed_at, "
                            "user_id, api_key_id, "
                            "service_id, instance_id, stream, record_state AS status, "
                            "http_status, error_code, "
                            "input_tokens, output_tokens, ttft_ms, duration_ms, finish_reason, "
                            "metering_source, metrics_capability "
                            "FROM usage.request_records ORDER BY received_at"
                        )
                    )
                )
                .mappings()
                .all()
            )
            leaked = await session.scalar(
                sa.text(
                    "SELECT EXISTS (SELECT 1 FROM usage.request_records AS record "
                    "WHERE row_to_json(record)::text LIKE :needle)"
                ),
                {"needle": f"%{SENTINEL}%"},
            )
        assert len(rows) == 8
        assert {str(row["status"]) for row in rows} == {
            "completed",
            "failed",
            "aborted",
            "rejected",
        }
        assert sum(row["status"] == "completed" for row in rows) == 3
        assert sum(row["status"] == "rejected" for row in rows) == 3
        assert all(
            row["input_tokens"] is not None
            and row["output_tokens"] is not None
            and row["ttft_ms"] is not None
            for row in rows
            if row["status"] == "completed"
        )
        assert any(
            row["status"] == "failed" and row["error_code"] == "backend_unavailable" for row in rows
        )
        assert any(row["status"] == "aborted" for row in rows)
        assert any(
            row["status"] == "rejected"
            and row["error_code"] == "invalid_api_key"
            and row["user_id"] is None
            for row in rows
        )
        assert leaked is False
        assert all(
            row["api_key_id"] in {None, admin_key_id}
            for row in rows
            if row["user_id"] in {None, admin_id}
        )

        ledger_only_ids = {
            RequestRecordState.RECEIVED: new_uuid7(),
            RequestRecordState.IN_PROGRESS: new_uuid7(),
            RequestRecordState.UNKNOWN: new_uuid7(),
        }
        latest_received_at = max(row["received_at"] for row in rows)
        ledger_only_received_at = {
            state: latest_received_at.replace(microsecond=index)
            for index, state in enumerate(ledger_only_ids, start=1)
        }
        await ensure_request_record_partitions(
            engine,
            tuple(row["received_at"] for row in rows) + tuple(ledger_only_received_at.values()),
        )
        projected_rows = (
            {
                "received_at": ledger_only_received_at[RequestRecordState.RECEIVED],
                "request_id": ledger_only_ids[RequestRecordState.RECEIVED],
                "event_seq": 1,
                "record_state": RequestRecordState.RECEIVED.value,
                "protocol": "openai",
                "operation": "chat.completions",
                "endpoint": "/v1/chat/completions",
                "stream": False,
                "updated_at": ledger_only_received_at[RequestRecordState.RECEIVED],
            },
            {
                "received_at": ledger_only_received_at[RequestRecordState.IN_PROGRESS],
                "request_id": ledger_only_ids[RequestRecordState.IN_PROGRESS],
                "event_seq": 2,
                "record_state": RequestRecordState.IN_PROGRESS.value,
                "protocol": "openai",
                "operation": "chat.completions",
                "endpoint": "/v1/chat/completions",
                "stream": False,
                "user_id": admin_id,
                "api_key_id": admin_key_id,
                "service_id": service_id,
                "instance_id": INSTANCE_ID,
                "updated_at": ledger_only_received_at[RequestRecordState.IN_PROGRESS],
            },
            {
                "received_at": ledger_only_received_at[RequestRecordState.UNKNOWN],
                "request_id": ledger_only_ids[RequestRecordState.UNKNOWN],
                "event_seq": 3,
                "record_state": RequestRecordState.UNKNOWN.value,
                "protocol": "openai",
                "operation": "chat.completions",
                "endpoint": "/v1/chat/completions",
                "stream": False,
                "user_id": admin_id,
                "api_key_id": admin_key_id,
                "service_id": service_id,
                "instance_id": INSTANCE_ID,
                "last_confirmed_event_type": EventType.IN_PROGRESS.value,
                "last_confirmed_event_seq": 2,
                "terminal_at": ledger_only_received_at[RequestRecordState.UNKNOWN],
                "updated_at": ledger_only_received_at[RequestRecordState.UNKNOWN],
            },
        )
        async with factory() as session:
            for projected_row in projected_rows:
                await session.execute(sa.insert(LEDGER_REQUEST_RECORDS).values(**projected_row))
            await session.commit()

        authentication = AuthenticationService(
            session_factory=factory,
            password_hasher=PasswordHasher(CHEAP),
            minimum_login_failure_seconds=0,
        )
        control = AsgiTestClient(
            create_app(
                version_payload=VersionPayload(
                    build_version="0.1.0+proto12",
                    commit="1" * 40,
                    schema_version=1,
                    runtime_compat_hash="2" * 64,
                ),
                readiness_checks=[DependencyCheck(name="database", probe=_readiness_ok)],
                credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
                authentication=authentication,
                request_records=RequestRecordQueryService(session_factory=factory),
            )
        )
        admin_cookies = await _login(control, ADMIN_USERNAME)
        user_cookies = await _login(control, USER_USERNAME)
        page_one = await control.request(
            "GET",
            "/api/v1/requests?limit=2",
            cookies=admin_cookies,
        )
        assert page_one.status == 200
        first_payload = page_one.json()
        assert len(first_payload["items"]) == 2
        assert first_payload["next_cursor"] is not None
        page_two = await control.request(
            "GET",
            "/api/v1/requests?" + urlencode({"limit": 2, "cursor": first_payload["next_cursor"]}),
            cookies=admin_cookies,
        )
        assert page_two.status == 200
        assert {item["request_id"] for item in first_payload["items"]}.isdisjoint(
            {item["request_id"] for item in page_two.json()["items"]}
        )

        failed_record = next(row for row in rows if row["status"] == "failed")
        exact = await control.request(
            "GET",
            "/api/v1/requests?"
            + urlencode(
                {
                    "service_id": service_id,
                    "user_id": admin_id,
                    "status": "failed",
                    "error_code": "backend_unavailable",
                    "request_id": failed_record["request_id"],
                    "from": "2020-01-01T00:00:00Z",
                    "to": "2030-01-01T00:00:00Z",
                }
            ),
            cookies=admin_cookies,
        )
        assert exact.status == 200
        assert len(exact.json()["items"]) == 1
        item = exact.json()["items"][0]
        assert item["username"] == ADMIN_USERNAME
        assert item["public_model_name"] == "qwen-mini"
        assert item["api_key_display_prefix"] == "holm_sk_"
        assert item["api_key_last4"] is not None
        assert item["error_code"] == "backend_unavailable"
        assert "upstream detail must not escape" not in json.dumps(item)

        for state in RequestRecordState:
            filtered = await control.request(
                "GET",
                "/api/v1/requests?" + urlencode({"status": state.value, "limit": 200}),
                cookies=admin_cookies,
            )
            assert filtered.status == 200
            filtered_items = filtered.json()["items"]
            assert filtered_items
            assert {entry["status"] for entry in filtered_items} == {state.value}

        ledger_only_request_id = ledger_only_ids[RequestRecordState.UNKNOWN]
        async with factory() as session:
            assert not bool(
                await session.scalar(
                    sa.text(
                        "SELECT EXISTS (SELECT 1 FROM core.request_records "
                        "WHERE request_id = :request_id)"
                    ),
                    {"request_id": ledger_only_request_id},
                )
            )
        ledger_only = await control.request(
            "GET",
            "/api/v1/requests?" + urlencode({"request_id": ledger_only_request_id}),
            cookies=admin_cookies,
        )
        assert ledger_only.status == 200
        ledger_only_items = ledger_only.json()["items"]
        assert len(ledger_only_items) == 1
        ledger_only_item = ledger_only_items[0]
        assert set(ledger_only_item) == {
            "api_key_display_prefix",
            "api_key_id",
            "api_key_name",
            "api_key_last4",
            "completed_at",
            "duration_ms",
            "error_code",
            "finish_reason",
            "http_status",
            "input_tokens",
            "instance_id",
            "output_tokens",
            "public_model_name",
            "received_at",
            "request_id",
            "service_id",
            "status",
            "stream",
            "ttft_ms",
            "user_id",
            "username",
        }
        assert ledger_only_item["request_id"] == str(ledger_only_request_id)
        assert ledger_only_item["status"] == RequestRecordState.UNKNOWN.value
        assert ledger_only_item["duration_ms"] is None
        assert ledger_only_item["input_tokens"] is None
        assert ledger_only_item["output_tokens"] is None
        assert SENTINEL not in json.dumps(ledger_only_item)
        denied = await control.request(
            "GET",
            "/api/v1/requests",
            cookies=user_cookies,
        )
        assert denied.status == 403
        await engine.dispose()
        return ({item["request_id"] for item in first_payload["items"]}, user_id)

    page_ids, user_id = asyncio.run(scenario())
    assert len(page_ids) == 2
    assert isinstance(user_id, UUID)
    rendered_logs = " ".join(
        f"{record.getMessage()} {record.__dict__}" for record in caplog.records
    )
    assert SENTINEL not in rendered_logs
    columns = {
        column["name"]
        for column in sa.inspect(ephemeral_postgres.engine).get_columns(
            "request_records",
            schema="core",
        )
    }
    assert columns == {
        "request_id",
        "received_at",
        "completed_at",
        "user_id",
        "api_key_id",
        "service_id",
        "instance_id",
        "stream",
        "status",
        "http_status",
        "error_code",
        "input_tokens",
        "output_tokens",
        "ttft_ms",
        "duration_ms",
        "finish_reason",
        # M5-07 added these two to the metadata Allowlist: they record which of
        # the three TDD 12.5 metering levels produced the Token counts and how
        # complete that measurement was.  Both are closed vocabularies.
        "metering_source",
        "metrics_capability",
    }


def test_CLEAN_02_PROTO_12_gateway_insert_is_denied_and_old_table_stays_empty(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    from sqlalchemy.exc import DBAPIError

    with ephemeral_postgres.engine.connect() as connection:
        count_before = connection.scalar(sa.text("SELECT count(*) FROM core.request_records"))
    with pytest.raises(DBAPIError) as error, ephemeral_postgres.engine.begin() as connection:
        connection.execute(sa.text("SET LOCAL ROLE holm_gateway"))
        connection.execute(
            sa.text(
                "INSERT INTO core.request_records "
                "(request_id, received_at, completed_at, stream, status, http_status, duration_ms) "
                "VALUES (:id, clock_timestamp(), clock_timestamp(), false, 'rejected', 401, 0)"
            ),
            {"id": new_uuid7()},
        )
    assert getattr(error.value.orig, "sqlstate", None) == "42501"
    with ephemeral_postgres.engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT count(*) FROM core.request_records")) == count_before
        )


def test_M5_11_reader_uses_usage_projection_and_ignores_core_only_rows(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> None:
        engine = create_database_engine(_async_url(ephemeral_postgres))
        factory = create_session_factory(engine)
        now = utc_now()
        core_only_id = new_uuid7()
        usage_only_id = new_uuid7()
        await ensure_request_record_partitions(engine, (now,))
        async with factory() as session:
            # Historical row fixture: CLEAN-02 retains the table and its data.
            await session.execute(
                sa.insert(REQUEST_RECORDS).values(
                    request_id=core_only_id,
                    received_at=now,
                    completed_at=now,
                    stream=False,
                    status="completed",
                    http_status=200,
                    input_tokens=1,
                    output_tokens=1,
                    ttft_ms=1,
                    duration_ms=1,
                )
            )
            await session.execute(
                sa.insert(LEDGER_REQUEST_RECORDS).values(
                    received_at=now,
                    request_id=usage_only_id,
                    event_seq=1,
                    record_state=RequestRecordState.RECEIVED.value,
                    protocol="openai",
                    operation="chat.completions",
                    endpoint="/v1/chat/completions",
                    stream=False,
                    updated_at=now,
                )
            )
            await session.commit()

        service = RequestRecordQueryService(session_factory=factory)
        usage_page = await service.list(RequestRecordQuery(request_id=usage_only_id, limit=50))
        core_page = await service.list(RequestRecordQuery(request_id=core_only_id, limit=50))
        assert [item.request_id for item in usage_page.items] == [usage_only_id]
        assert usage_page.items[0].status is RequestRecordState.RECEIVED
        assert usage_page.items[0].duration_ms is None
        assert core_page.items == ()
        async with factory() as session:
            assert bool(
                await session.scalar(
                    sa.text(
                        "SELECT EXISTS (SELECT 1 FROM core.request_records "
                        "WHERE request_id = :request_id)"
                    ),
                    {"request_id": core_only_id},
                )
            )
            assert not bool(
                await session.scalar(
                    sa.text(
                        "SELECT EXISTS (SELECT 1 FROM core.request_records "
                        "WHERE request_id = :request_id)"
                    ),
                    {"request_id": usage_only_id},
                )
            )
        await engine.dispose()

    asyncio.run(scenario())

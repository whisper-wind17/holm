"""M1-10A Bootstrap HTTP, transaction, concurrency, and secret-lifecycle tests."""

from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing
import os
import stat
import threading
from collections.abc import AsyncIterator, Callable, Iterator, MutableMapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from multiprocessing.connection import Connection as PipeConnection
from multiprocessing.context import SpawnProcess
from pathlib import Path
from typing import Any, Final, Protocol, cast
from uuid import UUID

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fixtures.asgi_client import (
    NO_CONFIGURED_CREDENTIAL_ROWS,
    AsgiResponse,
    AsgiTestClient,
)
from holm_contracts import VersionPayload
from holm_contracts.agent.v1 import agent_pb2
from holm_contracts.release import HostRequirements
from holm_control_api import (
    BOOTSTRAP_ADMIN_PATH,
    BOOTSTRAP_CHECK_PATH,
    BOOTSTRAP_STATUS_PATH,
    BOOTSTRAP_TOKEN_FILE_MODE,
    BOOTSTRAP_TOKEN_HEADER,
    CSRF_COOKIE_NAME,
    LOGIN_PATH,
    READY_PATH,
    SESSION_ABSOLUTE_TIMEOUT,
    SESSION_COOKIE_NAME,
    AgentSnapshot,
    BootstrapCreated,
    BootstrapDenialReason,
    BootstrapDenied,
    BootstrapService,
    BootstrapTokenIssuer,
    DependencyCheck,
    ReadinessReason,
    SystemStatusService,
    create_app,
)
from holm_control_api.authentication import AuthenticationService
from holm_persistence import (
    AsyncSessionFactory,
    BootstrapTokenRepository,
    SystemInitializationRecord,
    SystemInitializationRepository,
    SystemStatusSnapshot,
    create_database_engine,
    create_session_factory,
    database_utc_now,
)
from holm_security import (
    OWASP_MINIMUM_MEMORY_COST_KIB,
    OWASP_MINIMUM_TIME_COST,
    Argon2idParameters,
    PasswordHasher,
    hash_token,
)
from migration_test_support import (
    database_structure,
    head_revision,
    upgrade_head,
)
from sqlalchemy.engine import URL, Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

CLUSTER_USER: Final[str] = "ubuntu"
CLUSTER_DATABASE: Final[str] = "postgres"
PROCESS_TIMEOUT_SECONDS: Final[float] = 20.0
WEAK_READ_BARRIER_TIMEOUT_SECONDS: Final[float] = 1.0
ADMIN_USERNAME: Final[str] = "bootstrap-admin"
ADMIN_DISPLAY_NAME: Final[str] = "Bootstrap Administrator"
ADMIN_PASSWORD: Final[str] = "N7!qZ4@vL2#rT9$x"
TOKEN_SENTINEL: Final[str] = "PWSSNTL-MASTER-TCINIT10-A19C3E5F7B2D"
UNKNOWN_TOKEN_SENTINEL: Final[str] = "PWSSNTL-DENIED-UNKNOWN-7D40CBE2916A"
EXPIRED_TOKEN_SENTINEL: Final[str] = "PWSSNTL-DENIED-EXPIRED-84A36FB20C71"
CONSUMED_TOKEN_SENTINEL: Final[str] = "PWSSNTL-DENIED-CONSUMED-5E19A4D783B2"
CHEAP: Final[Argon2idParameters] = Argon2idParameters(
    memory_cost_kib=OWASP_MINIMUM_MEMORY_COST_KIB,
    time_cost=OWASP_MINIMUM_TIME_COST,
    parallelism=1,
)
VERSION: Final[VersionPayload] = VersionPayload(
    build_version="1.0.0+gm110a000001",
    commit="a" * 40,
    schema_version=1,
    runtime_compat_hash="b" * 64,
)

type Message = MutableMapping[str, Any]
type HeaderMapping = dict[str, str]
type InitializationRepositoryFactory = Callable[[AsyncSession], SystemInitializationRepository]


class PostgresFixture(Protocol):
    @property
    def engine(self) -> Engine: ...

    @property
    def socket_directory(self) -> Path: ...

    @property
    def port(self) -> int: ...


class _Agent:
    def __init__(self, observation: AgentSnapshot) -> None:
        self.observation = observation
        self.calls = 0

    async def observe(self) -> AgentSnapshot:
        self.calls += 1
        return self.observation


class _SystemDatabase:
    async def read(self) -> SystemStatusSnapshot:
        return SystemStatusSnapshot(
            postgres_version="16.15",
            heartbeats={},
            oldest_executable_job_age_seconds=None,
            expired_lease_count=0,
            running_job_count=0,
        )


class _InjectedInitializationFailure(RuntimeError):
    pass


class _FailingInitializationRepository(SystemInitializationRepository):
    async def mark_initialized(
        self,
        *,
        admin_user_id: UUID,
        now: datetime,
    ) -> SystemInitializationRecord:
        del admin_user_id, now
        raise _InjectedInitializationFailure("injected before system_initialized_at")


@dataclass(frozen=True, slots=True)
class _Environment:
    engine: AsyncEngine
    session_factory: AsyncSessionFactory
    agent: _Agent
    authentication: AuthenticationService
    bootstrap: BootstrapService
    system_status: SystemStatusService
    app: FastAPI


@pytest.fixture(scope="module")
def migrated_postgres(ephemeral_postgres: PostgresFixture) -> PostgresFixture:
    upgrade_head(ephemeral_postgres.engine)
    return ephemeral_postgres


@pytest.fixture
def bootstrap_database(migrated_postgres: PostgresFixture) -> Iterator[PostgresFixture]:
    _truncate_bootstrap_state(migrated_postgres.engine)
    yield migrated_postgres
    _truncate_bootstrap_state(migrated_postgres.engine)


def _truncate_bootstrap_state(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "TRUNCATE core.request_records, core.user_service_permissions, "
            "core.api_key_service_scopes, "
            "core.api_keys, core.idempotency_records, "
            "core.system_initialization, core.audit_events, core.login_protection, "
            "core.sessions, core.bootstrap_tokens, core.users"
        )


def _async_url(fixture: PostgresFixture) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username=CLUSTER_USER,
        host=str(fixture.socket_directory),
        port=fixture.port,
        database=CLUSTER_DATABASE,
    )


async def _readiness_ok() -> ReadinessReason:
    return ReadinessReason.OK


def _known_string(value: str) -> agent_pb2.ObservedString:
    return agent_pb2.ObservedString(state=agent_pb2.KNOWLEDGE_STATE_KNOWN, value=value)


def _known_uint(value: int) -> agent_pb2.ObservedUInt64:
    return agent_pb2.ObservedUInt64(state=agent_pb2.KNOWLEDGE_STATE_KNOWN, value=value)


def _host_info() -> agent_pb2.GetHostInfoResponse:
    return agent_pb2.GetHostInfoResponse(
        logical_cpu_count=_known_uint(32),
        total_memory_bytes=_known_uint(128 * 1024**3),
        total_disk_bytes=_known_uint(2 * 1024**4),
        available_disk_bytes=_known_uint(1024**4),
        driver_version=_known_string("595.45.04"),
        cuda_driver_version=_known_string("13.0"),
        docker_version=_known_string("27.3.1"),
        toolkit_version=_known_string("1.16.2"),
    )


def _agent_snapshot(*, gpu_ready: bool = True) -> AgentSnapshot:
    if gpu_ready:
        inventory = agent_pb2.ListGPUsResponse(
            discovery_state=agent_pb2.GPU_DISCOVERY_STATE_READY,
            devices=(
                agent_pb2.GpuDevice(
                    uuid="GPU-0001",
                    display_index=0,
                    model="L20",
                    memory_bytes=48 * 1024**3,
                    health=agent_pb2.GPU_HEALTH_STATE_HEALTHY,
                ),
            ),
        )
    else:
        inventory = agent_pb2.ListGPUsResponse(
            discovery_state=agent_pb2.GPU_DISCOVERY_STATE_NO_DEVICES,
            failure_reason=agent_pb2.GPU_DISCOVERY_FAILURE_REASON_NO_DEVICES,
        )
    return AgentSnapshot(host_info=_host_info(), gpu_inventory=inventory)


def _authentication(session_factory: AsyncSessionFactory) -> AuthenticationService:
    return AuthenticationService(
        session_factory=session_factory,
        password_hasher=PasswordHasher(CHEAP),
        minimum_login_failure_seconds=0,
    )


@asynccontextmanager
async def _environment(
    fixture: PostgresFixture,
    *,
    observation: AgentSnapshot | None = None,
    initialization_repository: InitializationRepositoryFactory = SystemInitializationRepository,
) -> AsyncIterator[_Environment]:
    engine = create_database_engine(_async_url(fixture), connect_timeout_seconds=5)
    try:
        session_factory = create_session_factory(engine)
        agent = _Agent(_agent_snapshot() if observation is None else observation)
        authentication = _authentication(session_factory)
        system_status = SystemStatusService(
            agent=agent,
            database=_SystemDatabase(),
            version=VERSION,
            host_requirements=HostRequirements(
                nvidia_driver="580.65.06",
                docker="27.0",
                compose="2.20",
                nvidia_container_toolkit="1.16",
            ),
        )
        bootstrap = BootstrapService(
            session_factory=session_factory,
            agent=agent,
            system_status=system_status,
            authentication=authentication,
            initialization_repository=initialization_repository,
        )
        app = create_app(
            version_payload=VERSION,
            readiness_checks=[DependencyCheck(name="database", probe=_readiness_ok)],
            credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
            authentication=authentication,
            system_status=system_status,
            bootstrap=bootstrap,
        )
        yield _Environment(
            engine=engine,
            session_factory=session_factory,
            agent=agent,
            authentication=authentication,
            bootstrap=bootstrap,
            system_status=system_status,
            app=app,
        )
    finally:
        await engine.dispose()


async def _create_token(
    session_factory: AsyncSessionFactory,
    plaintext: str,
    *,
    state: str = "valid",
) -> None:
    async with session_factory() as session:
        now = await database_utc_now(session)
        created_at = now if state != "expired" else now - timedelta(hours=2)
        expires_at = now + timedelta(minutes=30)
        if state == "expired":
            expires_at = now - timedelta(hours=1)
        tokens = BootstrapTokenRepository(session)
        await tokens.create(
            token_hash=hash_token(plaintext),
            expires_at=expires_at,
            now=created_at,
        )
        if state == "consumed":
            consumed = await tokens.consume(hash_token(plaintext))
            assert consumed is not None
        await session.commit()


async def _request(
    app: FastAPI,
    method: str,
    path: str,
    *,
    body: dict[str, object] | None = None,
    headers: HeaderMapping | None = None,
    cookies: HeaderMapping | None = None,
) -> AsgiResponse:
    encoded = b"" if body is None else json.dumps(body).encode("utf-8")
    raw_headers: list[tuple[bytes, bytes]] = [(b"host", b"asgi-test-client")]
    if body is not None:
        raw_headers.append((b"content-type", b"application/json"))
    if headers:
        raw_headers.extend(
            (name.lower().encode("ascii"), value.encode("latin-1"))
            for name, value in headers.items()
        )
    if cookies:
        raw_headers.append(
            (
                b"cookie",
                "; ".join(f"{name}={value}" for name, value in cookies.items()).encode("latin-1"),
            )
        )
    scope: Message = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "root_path": "",
        "headers": raw_headers,
        "client": ("127.0.0.1", 50000),
        "server": ("asgi-test-client", 443),
        "state": {},
    }
    messages: list[Message] = []
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": encoded, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        messages.append(message)

    server_error: BaseException | None = None
    try:
        await app(scope, receive, send)
    except Exception as error:  # noqa: BLE001 - asserted by rollback tests
        server_error = error
    start = next(message for message in messages if message["type"] == "http.response.start")
    response_body = b"".join(
        bytes(message.get("body", b""))
        for message in messages
        if message["type"] == "http.response.body"
    )
    response_headers = tuple(
        (name.decode("latin-1"), value.decode("latin-1"))
        for name, value in start.get("headers", ())
    )
    return AsgiResponse(
        status=int(start["status"]),
        headers=response_headers,
        body=response_body,
        server_error=server_error,
    )


def _admin_body(**extra: object) -> dict[str, object]:
    return {
        "username": ADMIN_USERNAME,
        "display_name": ADMIN_DISPLAY_NAME,
        "password": ADMIN_PASSWORD,
        "confirm_password": ADMIN_PASSWORD,
        **extra,
    }


def _token_headers(token: str) -> HeaderMapping:
    return {BOOTSTRAP_TOKEN_HEADER: token}


def _set_cookie_headers(response: AsgiResponse) -> tuple[str, ...]:
    return tuple(value for name, value in response.headers if name.casefold() == "set-cookie")


def _cookies(response: AsgiResponse) -> dict[str, SimpleCookie]:
    parsed: dict[str, SimpleCookie] = {}
    for raw in _set_cookie_headers(response):
        cookie = SimpleCookie()
        cookie.load(raw)
        assert len(cookie) == 1
        [name] = cookie.keys()
        parsed[name] = cookie
    return parsed


def _cookie_contract(response: AsgiResponse) -> dict[str, tuple[bool, bool, str, str, str]]:
    result: dict[str, tuple[bool, bool, str, str, str]] = {}
    for name, parsed in _cookies(response).items():
        morsel = parsed[name]
        result[name] = (
            bool(morsel["secure"]),
            bool(morsel["httponly"]),
            morsel["samesite"].casefold(),
            morsel["path"],
            morsel["max-age"],
        )
        assert morsel["domain"] == ""
    return result


def _cookie_value(response: AsgiResponse, name: str) -> str:
    parsed = _cookies(response)
    assert name in parsed
    return parsed[name][name].value


def _normalized_error(response: AsgiResponse) -> dict[str, object]:
    payload = dict(response.json()["error"])
    request_id = payload.pop("request_id")
    assert isinstance(request_id, str)
    return payload


def _database_counts(engine: Engine) -> dict[str, int]:
    tables = (
        "users",
        "sessions",
        "system_initialization",
        "bootstrap_tokens",
        "audit_events",
    )
    with engine.connect() as connection:
        return {
            table: int(
                connection.execute(sa.text(f"SELECT count(*) FROM core.{table}")).scalar_one()
            )
            for table in tables
        }


def _database_text(engine: Engine) -> str:
    """Serialize every row of every non-system table without echoing a match."""
    chunks: list[str] = []
    with engine.connect() as connection:
        tables = connection.execute(
            sa.text(
                """
                SELECT table_schema, table_name
                FROM information_schema.tables
                WHERE table_type = 'BASE TABLE'
                  AND table_schema IN ('core', 'usage', 'telemetry', 'public')
                ORDER BY table_schema, table_name
                """
            )
        ).tuples()
        for schema_name, table_name in tables:
            if not isinstance(schema_name, str) or not isinstance(table_name, str):
                raise AssertionError("catalog returned a non-text table identifier")
            quoted_schema = connection.dialect.identifier_preparer.quote(schema_name)
            quoted_table = connection.dialect.identifier_preparer.quote(table_name)
            rows = connection.exec_driver_sql(
                f"SELECT row_to_json(scanned_row)::text "
                f"FROM {quoted_schema}.{quoted_table} AS scanned_row"
            ).scalars()
            chunks.extend(str(row) for row in rows)
    return "\n".join(chunks)


def _process_result(receiver: PipeConnection) -> tuple[Any, ...]:
    if not receiver.poll(PROCESS_TIMEOUT_SECONDS):
        pytest.fail("bootstrap worker process did not report before timeout")
    result = receiver.recv()
    if not isinstance(result, tuple):
        pytest.fail("bootstrap worker returned an invalid result")
    return result


def _stop_processes(processes: list[SpawnProcess]) -> None:
    for process in processes:
        process.join(timeout=0)
        if process.is_alive():
            process.kill()
            process.join(timeout=5)


async def _bootstrap_in_process(
    url: URL,
    token: str,
    first_classification_barrier: Any,
    weak_read_barrier: Any,
) -> tuple[str, str | None, int]:
    engine = create_database_engine(url, connect_timeout_seconds=5)
    select_count = 0

    @sa.event.listens_for(engine.sync_engine, "after_cursor_execute")
    def synchronize_bootstrap_reads(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del connection, cursor, parameters, context, executemany
        nonlocal select_count
        normalized = " ".join(statement.upper().split())
        if not normalized.startswith("SELECT") or "BOOTSTRAP_TOKENS" not in normalized:
            return
        select_count += 1
        if select_count == 1:
            first_classification_barrier.wait(timeout=PROCESS_TIMEOUT_SECONDS)
        elif select_count == 2:
            # The production atomic UPDATE has no pre-write SELECT. If it is
            # changed to read-then-write, this synchronizes both weak reads so
            # both observe the unspent row. The atomic loser alone reaches a
            # second SELECT only to classify its denial, times out here, and
            # continues; no production function is replaced by the test.
            with suppress(threading.BrokenBarrierError):
                weak_read_barrier.wait(timeout=WEAK_READ_BARRIER_TIMEOUT_SECONDS)

    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            database_pid = int(
                (await session.execute(sa.text("SELECT pg_backend_pid()"))).scalar_one()
            )
        agent = _Agent(_agent_snapshot())
        authentication = _authentication(factory)
        service = BootstrapService(
            session_factory=factory,
            agent=agent,
            system_status=SystemStatusService(
                agent=agent,
                database=_SystemDatabase(),
                version=VERSION,
            ),
            authentication=authentication,
        )
        outcome = await service.create_first_administrator(
            token=token,
            username=ADMIN_USERNAME,
            display_name=ADMIN_DISPLAY_NAME,
            password=ADMIN_PASSWORD,
            request_id=UUID(int=os.getpid()),
        )
        if isinstance(outcome, BootstrapCreated):
            return type(outcome).__name__, None, database_pid
        if isinstance(outcome, BootstrapDenied):
            return type(outcome).__name__, outcome.reason.value, database_pid
        return type(outcome).__name__, None, database_pid
    finally:
        await engine.dispose()


def _bootstrap_process(
    url: URL,
    token: str,
    first_classification_barrier: Any,
    weak_read_barrier: Any,
    sender: PipeConnection,
) -> None:
    try:
        outcome, reason, database_pid = asyncio.run(
            _bootstrap_in_process(
                url,
                token,
                first_classification_barrier,
                weak_read_barrier,
            )
        )
        sender.send(("result", os.getpid(), database_pid, outcome, reason))
    except BaseException as error:
        sender.send(("error", os.getpid(), type(error).__name__))
        raise
    finally:
        sender.close()


def test_M1_10A_TC_INIT_09_two_processes_exchange_one_token_exactly_once(
    bootstrap_database: PostgresFixture,
) -> None:
    async def arrange() -> None:
        async with _environment(bootstrap_database) as environment:
            await _create_token(environment.session_factory, TOKEN_SENTINEL)

    asyncio.run(arrange())
    context = multiprocessing.get_context("spawn")
    first_classification_barrier = context.Barrier(2)
    weak_read_barrier = context.Barrier(2)
    processes: list[SpawnProcess] = []
    receivers: list[PipeConnection] = []
    try:
        for _ in range(2):
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(
                target=_bootstrap_process,
                args=(
                    _async_url(bootstrap_database),
                    TOKEN_SENTINEL,
                    first_classification_barrier,
                    weak_read_barrier,
                    sender,
                ),
            )
            process.start()
            sender.close()
            processes.append(process)
            receivers.append(receiver)

        results = [_process_result(receiver) for receiver in receivers]
        assert all(result[0] == "result" for result in results), results
        assert len({result[1] for result in results}) == 2
        assert len({result[2] for result in results}) == 2
        assert sorted(result[3] for result in results) == ["BootstrapCreated", "BootstrapDenied"]
        denied = next(result for result in results if result[3] == "BootstrapDenied")
        assert denied[4] == BootstrapDenialReason.TOKEN_CONSUMED.value
        for process in processes:
            process.join(timeout=PROCESS_TIMEOUT_SECONDS)
            assert process.exitcode == 0

        counts = _database_counts(bootstrap_database.engine)
        assert counts["users"] == 1
        assert counts["sessions"] == 1
        assert counts["system_initialization"] == 1
    finally:
        _stop_processes(processes)
        for receiver in receivers:
            receiver.close()


def test_M1_10A_TC_INIT_10_token_refusals_are_identical_outward_and_distinct_in_audit(
    bootstrap_database: PostgresFixture,
) -> None:
    async def scenario() -> None:
        async with _environment(bootstrap_database) as environment:
            expired = "expired-bootstrap-token"
            consumed = "consumed-bootstrap-token"
            await _create_token(environment.session_factory, expired, state="expired")
            await _create_token(environment.session_factory, consumed, state="consumed")

            captured: list[str] | None = None
            statement_batches: list[tuple[str, ...]] = []

            @sa.event.listens_for(environment.engine.sync_engine, "before_cursor_execute")
            def capture_statement_shapes(
                connection: object,
                cursor: object,
                statement: str,
                parameters: object,
                context: object,
                executemany: bool,
            ) -> None:
                del connection, cursor, parameters, context, executemany
                if captured is not None:
                    captured.append(" ".join(statement.split()))

            responses: list[AsgiResponse] = []
            for headers in (None, _token_headers(expired), _token_headers(consumed)):
                captured = []
                responses.append(
                    await _request(
                        environment.app,
                        "GET",
                        BOOTSTRAP_STATUS_PATH,
                        headers=headers,
                    )
                )
                statement_batches.append(tuple(captured))
            captured = None

            assert [response.status for response in responses] == [401, 401, 401]
            normalized = [_normalized_error(response) for response in responses]
            assert normalized[0] == normalized[1] == normalized[2]
            assert statement_batches[0] == statement_batches[1] == statement_batches[2]

            async with environment.session_factory() as session:
                reasons = tuple(
                    (
                        await session.execute(
                            sa.text(
                                """
                                SELECT details ->> 'reason_code'
                                FROM core.audit_events
                                WHERE action = 'system_config.change'
                                  AND result = 'denied'
                                ORDER BY created_at
                                """
                            )
                        )
                    ).scalars()
                )
            assert reasons == (
                BootstrapDenialReason.TOKEN_MISSING.value,
                BootstrapDenialReason.TOKEN_EXPIRED.value,
                BootstrapDenialReason.TOKEN_CONSUMED.value,
            )

    asyncio.run(scenario())


def test_M1_10A_TC_UI_14_server_rechecks_failures_allows_warnings_and_rejects_claims(
    bootstrap_database: PostgresFixture,
) -> None:
    async def scenario() -> None:
        async with _environment(
            bootstrap_database,
            observation=_agent_snapshot(gpu_ready=False),
        ) as environment:
            await _create_token(environment.session_factory, TOKEN_SENTINEL)
            headers = _token_headers(TOKEN_SENTINEL)

            failed_report = await _request(
                environment.app,
                "POST",
                BOOTSTRAP_CHECK_PATH,
                body={},
                headers=headers,
            )
            assert failed_report.status == 200
            assert "gpu" in failed_report.json()["blocking"]

            blocked = await _request(
                environment.app,
                "POST",
                BOOTSTRAP_ADMIN_PATH,
                body=_admin_body(),
                headers=headers,
            )
            assert blocked.status == 409
            assert blocked.json()["error"]["code"] == "bootstrap_checks_failed"
            assert _database_counts(bootstrap_database.engine)["users"] == 0

            claimed_pass = await _request(
                environment.app,
                "POST",
                BOOTSTRAP_ADMIN_PATH,
                body=_admin_body(
                    can_complete=True,
                    checks=[{"id": "gpu", "outcome": "passed"}],
                ),
                headers=headers,
            )
            assert claimed_pass.status == 400
            assert claimed_pass.json()["error"]["code"] == "invalid_request"
            assert _database_counts(bootstrap_database.engine)["users"] == 0

            environment.agent.observation = _agent_snapshot(gpu_ready=True)
            warning_report = await _request(
                environment.app,
                "POST",
                BOOTSTRAP_CHECK_PATH,
                body={},
                headers=headers,
            )
            assert warning_report.status == 200
            outcomes = {item["outcome"] for item in warning_report.json()["checks"]}
            assert "warning" in outcomes
            assert "failed" not in outcomes
            assert warning_report.json()["can_complete"] is True

            created = await _request(
                environment.app,
                "POST",
                BOOTSTRAP_ADMIN_PATH,
                body=_admin_body(),
                headers=headers,
            )
            assert created.status == 200
            assert created.json()["must_change_password"] is False

    asyncio.run(scenario())


def test_M1_10A_TC_INIT_11_transaction_failure_rolls_back_admin_and_emits_no_cookie(
    bootstrap_database: PostgresFixture,
) -> None:
    async def scenario() -> None:
        async with _environment(
            bootstrap_database,
            initialization_repository=_FailingInitializationRepository,
        ) as environment:
            await _create_token(environment.session_factory, TOKEN_SENTINEL)
            response = await _request(
                environment.app,
                "POST",
                BOOTSTRAP_ADMIN_PATH,
                body=_admin_body(),
                headers=_token_headers(TOKEN_SENTINEL),
            )
            assert response.status == 500
            assert isinstance(response.server_error, _InjectedInitializationFailure)
            assert _set_cookie_headers(response) == ()
            counts = _database_counts(bootstrap_database.engine)
            assert counts == {
                "users": 0,
                "sessions": 0,
                "system_initialization": 0,
                "bootstrap_tokens": 1,
                "audit_events": 0,
            }
            with bootstrap_database.engine.connect() as connection:
                consumed_at = connection.execute(
                    sa.text("SELECT consumed_at FROM core.bootstrap_tokens")
                ).scalar_one()
            assert consumed_at is None

    asyncio.run(scenario())


def test_M1_10A_TC_UI_14_PROTO_16_public_bootstrap_and_login_do_not_require_csrf(
    bootstrap_database: PostgresFixture,
) -> None:
    async def scenario() -> None:
        async with _environment(bootstrap_database) as environment:
            await _create_token(environment.session_factory, TOKEN_SENTINEL)
            created = await _request(
                environment.app,
                "POST",
                BOOTSTRAP_ADMIN_PATH,
                body=_admin_body(),
                headers=_token_headers(TOKEN_SENTINEL),
            )
            assert created.status == 200
            assert created.json()["must_change_password"] is False
            assert set(_cookies(created)) == {SESSION_COOKIE_NAME, CSRF_COOKIE_NAME}

            logged_in = await _request(
                environment.app,
                "POST",
                LOGIN_PATH,
                body={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
            )
            assert logged_in.status == 200
            assert logged_in.json()["must_change_password"] is False
            assert _cookie_contract(created) == _cookie_contract(logged_in)
            assert _cookie_value(created, SESSION_COOKIE_NAME) != _cookie_value(
                logged_in, SESSION_COOKIE_NAME
            )
            assert created.json()["csrf_token"] == _cookie_value(created, CSRF_COOKIE_NAME)
            assert logged_in.json()["csrf_token"] == _cookie_value(logged_in, CSRF_COOKIE_NAME)

            with bootstrap_database.engine.connect() as connection:
                sessions = tuple(
                    connection.execute(
                        sa.text(
                            """
                            SELECT session_hash, csrf_hash, created_at, expires_at
                            FROM core.sessions
                            ORDER BY created_at
                            """
                        )
                    ).mappings()
                )
            assert len(sessions) == 2
            response_pairs = (created, logged_in)
            for row, response in zip(sessions, response_pairs, strict=True):
                assert row["session_hash"] == hash_token(
                    _cookie_value(response, SESSION_COOKIE_NAME)
                )
                assert row["csrf_hash"] == hash_token(response.json()["csrf_token"])
                assert row["expires_at"] - row["created_at"] == SESSION_ABSOLUTE_TIMEOUT

            closed = (
                await _request(environment.app, "GET", BOOTSTRAP_STATUS_PATH),
                await _request(environment.app, "POST", BOOTSTRAP_CHECK_PATH, body={}),
                await _request(
                    environment.app,
                    "POST",
                    BOOTSTRAP_ADMIN_PATH,
                    body=_admin_body(),
                ),
            )
            assert [response.status for response in closed] == [404, 404, 404]
            assert {response.json()["error"]["code"] for response in closed} == {"not_found"}

    asyncio.run(scenario())


def test_M1_10A_TC_INIT_12_initialization_fact_is_database_immutable(
    bootstrap_database: PostgresFixture,
) -> None:
    async def scenario() -> None:
        async with _environment(bootstrap_database) as environment:
            await _create_token(environment.session_factory, TOKEN_SENTINEL)
            response = await _request(
                environment.app,
                "POST",
                BOOTSTRAP_ADMIN_PATH,
                body=_admin_body(),
                headers=_token_headers(TOKEN_SENTINEL),
            )
            assert response.status == 200

    asyncio.run(scenario())
    for statement in (
        "UPDATE core.system_initialization SET system_initialized_at = clock_timestamp()",
        "DELETE FROM core.system_initialization",
    ):
        with pytest.raises(sa.exc.DBAPIError), bootstrap_database.engine.begin() as connection:
            connection.execute(sa.text(statement))
    assert _database_counts(bootstrap_database.engine)["system_initialization"] == 1


def test_M1_10A_TC_INIT_13_sentinel_stays_out_of_database_audit_and_request_logs(
    bootstrap_database: PostgresFixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        async with _environment(bootstrap_database) as environment:
            await _create_token(
                environment.session_factory,
                EXPIRED_TOKEN_SENTINEL,
                state="expired",
            )
            await _create_token(
                environment.session_factory,
                CONSUMED_TOKEN_SENTINEL,
                state="consumed",
            )
            denied_responses = [
                await _request(
                    environment.app,
                    "GET",
                    BOOTSTRAP_STATUS_PATH,
                    headers=_token_headers(token),
                )
                for token in (
                    UNKNOWN_TOKEN_SENTINEL,
                    EXPIRED_TOKEN_SENTINEL,
                    CONSUMED_TOKEN_SENTINEL,
                )
            ]
            assert [response.status for response in denied_responses] == [401, 401, 401]

            await _create_token(environment.session_factory, TOKEN_SENTINEL)
            headers = _token_headers(TOKEN_SENTINEL)
            status_response = await _request(
                environment.app,
                "GET",
                BOOTSTRAP_STATUS_PATH,
                headers=headers,
            )
            check_response = await _request(
                environment.app,
                "POST",
                BOOTSTRAP_CHECK_PATH,
                body={},
                headers=headers,
            )
            admin_response = await _request(
                environment.app,
                "POST",
                BOOTSTRAP_ADMIN_PATH,
                body=_admin_body(),
                headers=headers,
            )
            assert [status_response.status, check_response.status, admin_response.status] == [
                200,
                200,
                200,
            ]

    with caplog.at_level(logging.DEBUG):
        asyncio.run(scenario())
    database_text = _database_text(bootstrap_database.engine)
    with bootstrap_database.engine.connect() as connection:
        audit_details = "\n".join(
            connection.execute(
                sa.text("SELECT details::text FROM core.audit_events ORDER BY created_at")
            ).scalars()
        )
    for sentinel in (
        TOKEN_SENTINEL,
        UNKNOWN_TOKEN_SENTINEL,
        EXPIRED_TOKEN_SENTINEL,
        CONSUMED_TOKEN_SENTINEL,
    ):
        assert sentinel not in database_text
        assert sentinel not in audit_details
        assert sentinel not in caplog.text


def test_M1_10A_TC_INIT_14_token_issuer_uses_digest_and_root_0400_under_umask_zero(
    bootstrap_database: PostgresFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.geteuid() != 0:
        pytest.fail("M1-10A token ownership test must run as root in the remote container")
    token_path = tmp_path / "secrets" / "bootstrap-token"
    records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.Logger("m1-10a-token-test")
    logger.addHandler(_Handler())
    monkeypatch.setattr(
        "holm_control_api.bootstrap_token.generate_token",
        lambda *, entropy_bits: TOKEN_SENTINEL,
    )

    async def scenario() -> None:
        async with _environment(bootstrap_database) as environment:
            issuer = BootstrapTokenIssuer(
                session_factory=environment.session_factory,
                token_path=token_path,
                logger=logger,
            )
            previous_umask = os.umask(0)
            try:
                issued = await issuer.ensure_token()
            finally:
                os.umask(previous_umask)
            assert issued is not None
            assert await issuer.ensure_token() is None

    asyncio.run(scenario())
    metadata = token_path.stat()
    assert stat.S_IMODE(metadata.st_mode) == BOOTSTRAP_TOKEN_FILE_MODE == 0o400
    assert metadata.st_uid == 0
    assert token_path.read_text(encoding="utf-8") == f"{TOKEN_SENTINEL}\n"
    assert len(records) == 1
    assert records[0].__dict__["bootstrap_token"] == TOKEN_SENTINEL
    with bootstrap_database.engine.connect() as connection:
        stored = connection.execute(
            sa.text("SELECT token_hash FROM core.bootstrap_tokens")
        ).scalar_one()
    assert stored == hash_token(TOKEN_SENTINEL)
    assert TOKEN_SENTINEL not in _database_text(bootstrap_database.engine)


def test_M1_10A_TC_INIT_15_uninitialized_platform_remains_ready(
    bootstrap_database: PostgresFixture,
) -> None:
    async def scenario() -> None:
        async with _environment(bootstrap_database) as environment:
            client = AsgiTestClient(cast(Any, environment.app))
            async with client.lifespan():
                response = await client.get(READY_PATH)
            assert response.status == 200
            assert response.json()["status"] == "ready"
            assert _database_counts(bootstrap_database.engine)["system_initialization"] == 0

    asyncio.run(scenario())


def test_M1_10A_TC_INIT_16_migration_repeat_is_stable_and_has_one_head(
    migrated_postgres: PostgresFixture,
) -> None:
    before = database_structure(migrated_postgres.engine)
    upgrade_head(migrated_postgres.engine)
    after_first_repeat = database_structure(migrated_postgres.engine)
    upgrade_head(migrated_postgres.engine)
    after_second_repeat = database_structure(migrated_postgres.engine)
    assert before == after_first_repeat == after_second_repeat
    assert before.alembic_versions == (head_revision(),)
    assert ("core", "bootstrap_tokens", "r") in before.relations
    assert ("core", "system_initialization", "r") in before.relations


@pytest.mark.parametrize("old_version", [False, True])
def test_SYS_06_TC_INIT_04_05_single_observation_host_response_and_completion(
    bootstrap_database: PostgresFixture,
    old_version: bool,
) -> None:
    async def scenario() -> None:
        observation = _agent_snapshot()
        # FX-SYS-06: development preview has no container tools; a known old
        # Toolkit must still block. The protobuf contains only allowlisted facts.
        observation.host_info.ClearField("docker_version")
        observation.host_info.ClearField("toolkit_version")
        if old_version:
            observation.host_info.toolkit_version.CopyFrom(_known_string("1.14.6"))
        async with _environment(bootstrap_database, observation=observation) as environment:
            await _create_token(environment.session_factory, TOKEN_SENTINEL)
            report = await _request(
                environment.app,
                "POST",
                BOOTSTRAP_CHECK_PATH,
                body={},
                headers=_token_headers(TOKEN_SENTINEL),
            )
            assert report.status == 200
            assert environment.agent.calls == 1
            components = await environment.system_status.read_components(observation=observation)
            assert environment.agent.calls == 1
            payload = report.json()
            assert set(payload) == {
                "observed_at",
                "checks",
                "blocking",
                "can_complete",
                "agent",
                "host_environment",
            }
            assert payload["host_environment"] == components.host_environment.model_dump(
                mode="json"
            )
            assert [check["id"] for check in payload["checks"]] == [
                "gpu",
                "nvidia_driver",
                "docker_compose",
                "nvidia_container_toolkit",
                "disk",
                "memory",
                "holm_components",
            ]
            assert payload["blocking"] == (["nvidia_container_toolkit"] if old_version else [])
            assert payload["can_complete"] is not old_version
            assert payload["checks"][2] == {
                "id": "docker_compose",
                "outcome": "warning",
                "reasons": ["docker_version_undetermined", "compose_version_undetermined"],
            }
            assert payload["checks"][3] == {
                "id": "nvidia_container_toolkit",
                "outcome": "failed" if old_version else "warning",
                "reasons": [
                    "toolkit_below_minimum" if old_version else "toolkit_version_undetermined"
                ],
            }
            serialized = json.dumps(payload)
            for forbidden in ("/workspace/", "/var/run/", "socket", "stdout", "stderr"):
                assert forbidden not in serialized
            created = await _request(
                environment.app,
                "POST",
                BOOTSTRAP_ADMIN_PATH,
                body=_admin_body(),
                headers=_token_headers(TOKEN_SENTINEL),
            )
            assert created.status == (409 if old_version else 200)
            assert environment.agent.calls == 2

    asyncio.run(scenario())

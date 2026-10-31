"""TC-SRC-11..13 and 15..17 Master Key rotation operation tests."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
from collections.abc import Mapping
from dataclasses import dataclass
from http.cookies import SimpleCookie
from multiprocessing.connection import Connection as PipeConnection
from multiprocessing.context import SpawnProcess
from pathlib import Path
from typing import Any, Final, Protocol
from uuid import UUID

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from credential_scan import database_bytes as _database_bytes
from fixtures.asgi_client import (
    NO_CONFIGURED_CREDENTIAL_ROWS,
    AsgiResponse,
    AsgiTestClient,
    LifespanError,
)
from holm_contracts import VersionPayload
from holm_control_api.application import create_app
from holm_control_api.authentication import AuthenticationService
from holm_control_api.credential_rotation import (
    CredentialRotationService,
    DatabaseCredentialStartupCheck,
    RotationObserver,
)
from holm_control_api.http_constants import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
)
from holm_control_api.master_key_rotation_routes import (
    MASTER_KEY_ROTATION_ADVANCE_PATH,
    MASTER_KEY_ROTATION_PATH,
)
from holm_control_api.master_key_rotation_service import (
    MasterKeyRotationOperationService,
)
from holm_control_api.readiness import DependencyCheck, ReadinessReason
from holm_domain import (
    AuditAction,
    AuditActorType,
    AuditObjectType,
    AuditResult,
    CredentialType,
    UserRole,
)
from holm_persistence import (
    AsyncSessionFactory,
    AuditEventDraft,
    UserRepository,
    create_database_engine,
    create_session_factory,
    database_utc_now,
)
from holm_security import (
    OWASP_MINIMUM_MEMORY_COST_KIB,
    OWASP_MINIMUM_TIME_COST,
    Argon2idParameters,
    MasterKey,
    MasterKeyring,
    MasterKeyState,
    PasswordHasher,
    build_audit_details,
)
from sqlalchemy import event
from sqlalchemy.engine import URL, Engine
from sqlalchemy.ext.asyncio import AsyncEngine
from test_TC_SEC_19_credential_rotation import (
    PROCESS_TIMEOUT_SECONDS,
    _assert_sentinels_absent,
    _async_url,
    _keyring,
    _prepare_database,
    _read_records,
    _seed_sources,
    _stop_process,
    _wait_message,
    _write_envelope,
)

USERNAME: Final[str] = "rotation-admin"
PASSWORD: Final[str] = "correct-password-for-rotation-tests"
TOKEN_TYPE: Final[str] = CredentialType.ACCESS_TOKEN.value
CHEAP: Final = Argon2idParameters(
    memory_cost_kib=OWASP_MINIMUM_MEMORY_COST_KIB,
    time_cost=OWASP_MINIMUM_TIME_COST,
    parallelism=1,
)


class PostgresFixture(Protocol):
    @property
    def engine(self) -> Engine: ...

    @property
    def socket_directory(self) -> Path: ...

    @property
    def port(self) -> int: ...


@dataclass(frozen=True, slots=True)
class _Environment:
    authentication: AuthenticationService
    session_factory: AsyncSessionFactory


async def _readiness_ok() -> ReadinessReason:
    return ReadinessReason.OK


def _fixture_async_url(fixture: PostgresFixture) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username="ubuntu",
        host=str(fixture.socket_directory),
        port=fixture.port,
        database="postgres",
    )


def _client(
    environment: _Environment,
    keyring: MasterKeyring | None,
    *,
    startup_check: DatabaseCredentialStartupCheck | None = None,
) -> AsgiTestClient:
    operation = MasterKeyRotationOperationService(
        session_factory=environment.session_factory,
        authentication=environment.authentication,
        keyring=keyring,
    )
    app = create_app(
        version_payload=VersionPayload(
            build_version="2.1.0+gsrc01b0001",
            commit="b" * 40,
            schema_version=1,
            runtime_compat_hash="c" * 64,
        ),
        readiness_checks=[DependencyCheck(name="database", probe=_readiness_ok)],
        credential_startup_check=(
            NO_CONFIGURED_CREDENTIAL_ROWS if startup_check is None else startup_check
        ),
        authentication=environment.authentication,
        master_key_rotation=operation,
    )
    return AsgiTestClient(app)


async def _environment(fixture: PostgresFixture) -> tuple[_Environment, AsyncEngine]:
    engine = create_database_engine(_fixture_async_url(fixture), connect_timeout_seconds=5)
    session_factory = create_session_factory(engine)
    authentication = AuthenticationService(
        session_factory=session_factory,
        password_hasher=PasswordHasher(CHEAP),
        minimum_login_failure_seconds=0,
    )
    return _Environment(authentication, session_factory), engine


async def _reset_and_create_admin(environment: _Environment) -> None:
    async with environment.session_factory() as session:
        await session.execute(
            sa.text(
                "TRUNCATE core.audit_events, core.login_protection, core.sessions, "
                "core.models, core.model_sources, core.users CASCADE"
            )
        )
        password_hash = await asyncio.to_thread(PasswordHasher(CHEAP).hash, PASSWORD)
        now = await database_utc_now(session)
        await UserRepository(session).create(
            username=USERNAME,
            display_name="Rotation Admin",
            role=UserRole.ADMIN,
            password_hash=password_hash,
            now=now,
            must_change_password=False,
        )
        await session.commit()


def _cookie(response: AsgiResponse, name: str) -> str:
    values: list[str] = []
    for key, raw in response.headers:
        if key.casefold() != "set-cookie":
            continue
        parsed = SimpleCookie()
        parsed.load(raw)
        if name in parsed:
            values.append(parsed[name].value)
    assert len(values) == 1
    return values[0]


def _header(response: AsgiResponse, name: str) -> str:
    values = [value for key, value in response.headers if key.casefold() == name.casefold()]
    assert len(values) == 1
    return values[0]


async def _login(
    client: AsgiTestClient,
) -> tuple[Mapping[str, str], Mapping[str, str]]:
    response = await client.request(
        "POST",
        "/api/v1/auth/login",
        json_body={"username": USERNAME, "password": PASSWORD},
    )
    assert response.status == 200
    return (
        {SESSION_COOKIE_NAME: _cookie(response, SESSION_COOKIE_NAME)},
        {CSRF_HEADER_NAME: _cookie(response, CSRF_COOKIE_NAME)},
    )


async def _advance(
    client: AsgiTestClient,
    *,
    cookies: Mapping[str, str],
    headers: Mapping[str, str],
    password: str | None,
) -> AsgiResponse:
    body = {} if password is None else {"current_password": password}
    return await client.request(
        "POST",
        MASTER_KEY_ROTATION_ADVANCE_PATH,
        cookies=cookies,
        headers=headers,
        json_body=body,
    )


def _assert_zero_scan_is_not_complete(payload: Mapping[str, object]) -> None:
    if payload["scanned_rows"] == 0:
        assert payload["status"] == "not_required"
        assert payload["phase"] is None


def test_TC_SRC_11_TC_SRC_16_phase_query_empty_and_scan_failure_are_distinct(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _prepare_database(ephemeral_postgres.engine)
    old_material = os.urandom(32)
    new_material = os.urandom(32)
    plaintexts = (os.urandom(30), os.urandom(31))

    async def scenario() -> None:
        environment, engine = await _environment(ephemeral_postgres)
        try:
            await _reset_and_create_admin(environment)
            source_ids = await _seed_sources(
                ephemeral_postgres.engine.url,
                old_material=old_material,
                plaintexts=plaintexts,
            )
            ring = _keyring(new_material, old_material)
            client = _client(environment, ring)
            cookies, _headers = await _login(client)

            initial = await client.get(MASTER_KEY_ROTATION_PATH, cookies=cookies)
            assert initial.status == 200
            assert _header(initial, "cache-control") == "no-store"
            assert initial.json() == {
                "status": "rotation",
                "phase": "new_key_writes",
                "writable_key_id": "new-key",
                "old_key_id": "old-key",
                "referenced_rows": 2,
                "scanned_rows": 2,
            }

            first_new = ring.encrypt(
                plaintexts[0], source_id=source_ids[0], credential_type=TOKEN_TYPE
            )
            _write_envelope(ephemeral_postgres.engine, source_ids[0], first_new)
            partial = await client.get(MASTER_KEY_ROTATION_PATH, cookies=cookies)
            assert partial.json()["phase"] == "reencrypting"
            assert partial.json()["referenced_rows"] == 1

            second_new = ring.encrypt(
                plaintexts[1], source_id=source_ids[1], credential_type=TOKEN_TYPE
            )
            _write_envelope(ephemeral_postgres.engine, source_ids[1], second_new)
            read_only = await client.get(MASTER_KEY_ROTATION_PATH, cookies=cookies)
            assert read_only.json()["phase"] == "old_key_read_only"
            assert read_only.json()["referenced_rows"] == 0

            disabled_ring = MasterKeyring(
                (
                    MasterKey("new-key", MasterKeyState.WRITABLE, new_material),
                    MasterKey("old-key", MasterKeyState.DISABLED, old_material),
                )
            )
            complete = await _client(environment, disabled_ring).get(
                MASTER_KEY_ROTATION_PATH,
                cookies=cookies,
            )
            assert complete.json()["phase"] == "complete"
            assert complete.json()["status"] == "rotation"

            ambiguous_ring = MasterKeyring(
                (
                    MasterKey("new-key", MasterKeyState.WRITABLE, new_material),
                    MasterKey("old-key", MasterKeyState.DECRYPT_ONLY, old_material),
                    MasterKey("another-old-key", MasterKeyState.DECRYPT_ONLY, os.urandom(32)),
                )
            )
            ambiguous = await _client(environment, ambiguous_ring).get(
                MASTER_KEY_ROTATION_PATH,
                cookies=cookies,
            )
            assert ambiguous.status == 503
            assert ambiguous.json()["error"]["code"] == "rotation_key_ambiguous"

            with ephemeral_postgres.engine.begin() as connection:
                connection.execute(sa.text("DELETE FROM core.model_sources"))
            no_keyring_client = _client(environment, None)
            empty = await no_keyring_client.get(MASTER_KEY_ROTATION_PATH, cookies=cookies)
            assert empty.status == 200
            assert empty.json()["status"] == "not_required"
            _assert_zero_scan_is_not_complete(empty.json())

            empty_startup = DatabaseCredentialStartupCheck(environment.session_factory, None)
            async with _client(
                environment,
                None,
                startup_check=empty_startup,
            ).lifespan():
                pass

            await _seed_sources(
                ephemeral_postgres.engine.url,
                old_material=old_material,
                plaintexts=plaintexts,
            )
            missing_key_app = _client(
                environment,
                None,
                startup_check=DatabaseCredentialStartupCheck(
                    environment.session_factory,
                    None,
                ),
            )
            with pytest.raises(LifespanError):
                async with missing_key_app.lifespan():
                    pass

            with ephemeral_postgres.engine.begin() as connection:
                connection.execute(
                    sa.text("ALTER TABLE core.model_sources RENAME TO model_sources_unavailable")
                )
            try:
                unavailable = await client.get(MASTER_KEY_ROTATION_PATH, cookies=cookies)
                assert unavailable.status == 503
                assert unavailable.json()["error"]["code"] == "reference_scan_unavailable"
                assert unavailable.json()["error"]["request_id"] == _header(
                    unavailable, "x-request-id"
                )
            finally:
                with ephemeral_postgres.engine.begin() as connection:
                    connection.execute(
                        sa.text(
                            "ALTER TABLE core.model_sources_unavailable RENAME TO model_sources"
                        )
                    )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


class _ConcurrentCredentialDeletion:
    """Commit a real deletion inside the production window between the two rotation reads.

    ``inspect()`` reads the configured rows and then scans Key references in two separate
    statements. Under READ COMMITTED the second statement takes a fresh snapshot, so a
    deletion another connection commits in between is exactly how production reaches
    ``scanned_rows == 0`` while still holding rows — the only way the empty-scan guard is
    reachable at all. Firing on the scan statement itself is what makes that window
    deterministic; the injection sits at the database boundary and nothing in ``inspect()``
    or in the HTTP path is stubbed or replaced.
    """

    __slots__ = ("_engine", "deleted_rows")

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self.deleted_rows: int | None = None

    def __call__(
        self,
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        del connection, cursor, parameters, context, executemany
        if self.deleted_rows is not None:
            return
        if "core.model_sources" not in statement or "count(*) FILTER (WHERE" not in statement:
            return
        with self._engine.begin() as target:
            self.deleted_rows = target.execute(sa.text("DELETE FROM core.model_sources")).rowcount


def _verdict(response: AsgiResponse) -> tuple[int, object, object, object]:
    payload = response.json()
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    return (
        response.status,
        payload.get("status") if isinstance(payload, dict) else None,
        payload.get("phase") if isinstance(payload, dict) else None,
        error.get("code"),
    )


def test_TC_SRC_11_TC_SRC_16_empty_scan_guard_runs_in_production_and_is_not_complete(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    """Drive the production empty-scan guard over HTTP and separate all three verdicts.

    The positive control for bounded rotation lives on the production statement,
    not on a checker: making
    ``inspect()`` treat ``scanned_rows <= 0`` as a completed rotation turns this 503 into a
    200 ``phase=complete`` and the test fails.
    """

    _prepare_database(ephemeral_postgres.engine)
    old_material = os.urandom(32)
    new_material = os.urandom(32)
    plaintexts = (os.urandom(32), os.urandom(33))

    async def scenario() -> None:
        environment, engine = await _environment(ephemeral_postgres)
        try:
            await _reset_and_create_admin(environment)
            source_ids = await _seed_sources(
                ephemeral_postgres.engine.url,
                old_material=old_material,
                plaintexts=plaintexts,
            )
            ring = _keyring(new_material, old_material)
            client = _client(environment, ring)
            cookies, _headers = await _login(client)

            race = _ConcurrentCredentialDeletion(ephemeral_postgres.engine)
            event.listen(engine.sync_engine, "before_cursor_execute", race)
            try:
                empty_scan = await client.get(MASTER_KEY_ROTATION_PATH, cookies=cookies)
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", race)
            # Without this the request never reached the scan and the 503 below would be
            # some other failure; the guard must have been executed, not merely defined.
            assert race.deleted_rows == len(source_ids)

            assert empty_scan.status == 503
            empty_payload = empty_scan.json()
            assert empty_payload["error"]["code"] == "reference_scan_empty"
            assert empty_payload["error"]["request_id"] == _header(empty_scan, "x-request-id")
            assert set(empty_payload) == {"error"}
            assert "complete" not in empty_scan.text
            assert "not_required" not in empty_scan.text

            not_required = await client.get(MASTER_KEY_ROTATION_PATH, cookies=cookies)
            assert not_required.status == 200
            assert not_required.json()["status"] == "not_required"
            assert not_required.json()["scanned_rows"] == 0
            _assert_zero_scan_is_not_complete(not_required.json())

            reseeded = await _seed_sources(
                ephemeral_postgres.engine.url,
                old_material=old_material,
                plaintexts=plaintexts,
            )
            for source_id, plaintext in zip(reseeded, plaintexts, strict=True):
                _write_envelope(
                    ephemeral_postgres.engine,
                    source_id,
                    ring.encrypt(plaintext, source_id=source_id, credential_type=TOKEN_TYPE),
                )
            disabled_ring = MasterKeyring(
                (
                    MasterKey("new-key", MasterKeyState.WRITABLE, new_material),
                    MasterKey("old-key", MasterKeyState.DISABLED, old_material),
                )
            )
            complete = await _client(environment, disabled_ring).get(
                MASTER_KEY_ROTATION_PATH,
                cookies=cookies,
            )
            assert complete.status == 200
            assert complete.json()["status"] == "rotation"
            assert complete.json()["phase"] == "complete"
            assert complete.json()["scanned_rows"] == len(reseeded)

            verdicts = tuple(_verdict(item) for item in (empty_scan, not_required, complete))
            assert len(set(verdicts)) == 3
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_TC_SRC_15_TC_SRC_17_advance_reauth_audit_and_zero_leakage(
    ephemeral_postgres: EphemeralPostgres,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _prepare_database(ephemeral_postgres.engine)
    source_sentinel = f"PWSSNTL-SRCTOK-TC-SRC-15-{os.urandom(4).hex().upper()}"
    master_sentinel = b"PWSSNTL-MASTER-TC-SRC-15".ljust(32, b"X")
    password_sentinel = f"PWSSNTL-PASSWD-TC-SRC-17-{os.urandom(4).hex().upper()}"
    assert len(master_sentinel) == 32
    plaintexts = (source_sentinel.encode(), os.urandom(31))

    async def scenario() -> tuple[AsgiResponse, ...]:
        environment, engine = await _environment(ephemeral_postgres)
        try:
            await _reset_and_create_admin(environment)
            await _seed_sources(
                ephemeral_postgres.engine.url,
                old_material=master_sentinel,
                plaintexts=plaintexts,
            )
            ring = _keyring(os.urandom(32), master_sentinel)
            client = _client(environment, ring)
            cookies, headers = await _login(client)

            read_without_password = await client.get(
                MASTER_KEY_ROTATION_PATH,
                cookies=cookies,
            )
            assert read_without_password.status == 200

            async def envelope_snapshot() -> tuple[tuple[object, ...], ...]:
                async with environment.session_factory() as session:
                    return tuple(
                        tuple(row)
                        for row in (
                            await session.execute(
                                sa.text(
                                    "SELECT credential_key_id, credential_nonce, "
                                    "credential_ciphertext, credential_tag "
                                    "FROM core.model_sources ORDER BY id"
                                )
                            )
                        ).all()
                    )

            before = await envelope_snapshot()
            responses: list[AsgiResponse] = []
            responses.append(
                await _advance(
                    client,
                    cookies=cookies,
                    headers=headers,
                    password=None,
                )
            )
            for _ in range(4):
                responses.append(
                    await _advance(
                        client,
                        cookies=cookies,
                        headers=headers,
                        password=password_sentinel,
                    )
                )
            responses.append(
                await _advance(
                    client,
                    cookies=cookies,
                    headers=headers,
                    password=PASSWORD,
                )
            )
            assert responses[0].status == 401
            assert responses[0].json()["error"]["code"] == "invalid_credentials"
            assert responses[-2].status == 429
            assert responses[-1].status == 429
            assert responses[-1].json()["error"]["code"] == "login_locked"
            assert await envelope_snapshot() == before

            async with environment.session_factory() as session:
                await session.execute(sa.text("DELETE FROM core.login_protection"))
                await session.commit()
            succeeded = await _advance(
                client,
                cookies=cookies,
                headers=headers,
                password=PASSWORD,
            )
            responses.append(succeeded)
            assert succeeded.status == 200
            assert _header(succeeded, "cache-control") == "no-store"
            assert succeeded.json()["reencrypted_rows"] == 2
            assert succeeded.json()["phase"] == "old_key_read_only"

            async with environment.session_factory() as session:
                audit_rows = (
                    (
                        await session.execute(
                            sa.text(
                                "SELECT actor_id, request_id, object_id, result, details "
                                "FROM core.audit_events WHERE action = :action "
                                "ORDER BY created_at"
                            ),
                            {"action": AuditAction.MODEL_SOURCE_CREDENTIAL_CHANGE.value},
                        )
                    )
                    .mappings()
                    .all()
                )
            assert len(audit_rows) == len(responses)
            assert [row["result"] for row in audit_rows[:-1]] == [AuditResult.DENIED.value] * (
                len(responses) - 1
            )
            assert audit_rows[-1]["result"] == AuditResult.SUCCEEDED.value
            assert audit_rows[-1]["object_id"] is None
            assert audit_rows[-1]["actor_id"] is not None
            assert str(audit_rows[-1]["request_id"]) == _header(succeeded, "x-request-id")
            assert set(audit_rows[-1]["details"]) == {
                "old_key_id",
                "reencrypted_rows",
                "referenced_rows",
                "scanned_rows",
                "writable_key_id",
            }
            assert audit_rows[-1]["details"]["reencrypted_rows"] == 2

            attempted_secret_details = build_audit_details(
                AuditAction.MODEL_SOURCE_CREDENTIAL_CHANGE.value,
                {"master_key_material": master_sentinel.decode()},
            )
            assert dict(attempted_secret_details) == {}

            with pytest.raises(ValueError):
                AuditEventDraft(
                    actor_type=AuditActorType.USER,
                    actor_id=audit_rows[-1]["actor_id"],
                    action=AuditAction.MODEL_SOURCE_CREDENTIAL_CHANGE,
                    object_type=AuditObjectType.MODEL_SOURCE,
                    object_id=None,
                    result=AuditResult.SUCCEEDED,
                    request_id=audit_rows[-1]["request_id"],
                    details={"master_key_material": master_sentinel.decode()},
                )

            _assert_sentinels_absent(
                _database_bytes(ephemeral_postgres.engine),
                (source_sentinel.encode(), master_sentinel, password_sentinel.encode()),
            )
            return tuple(responses)
        finally:
            await engine.dispose()

    responses = asyncio.run(scenario())
    response_surface = "\n".join(response.text for response in responses)
    log_surface = "\n".join(
        f"{record.getMessage()} {record.__dict__!r}" for record in caplog.records
    )
    process_surface = b"\n".join(
        (Path("/proc/self/cmdline").read_bytes(), json.dumps(dict(os.environ)).encode())
    ).decode(errors="replace")
    for sentinel in (
        source_sentinel,
        master_sentinel.decode(),
        password_sentinel,
    ):
        assert sentinel not in response_surface
        assert sentinel not in log_surface
        assert sentinel not in process_surface


class _KillAfterCommittedBatchObserver(RotationObserver):
    def __init__(self, sender: PipeConnection) -> None:
        self._sender = sender

    async def row_decrypted(self, source_id: UUID, row_number: int) -> None:
        if row_number == 2:
            self._sender.send(("paused", str(source_id)))
            await asyncio.Event().wait()


def _killable_rotation_worker(
    url: URL,
    new_material: bytes,
    old_material: bytes,
    sender: PipeConnection,
) -> None:
    async def scenario() -> None:
        engine = create_database_engine(_async_url(url))
        try:
            service = CredentialRotationService(
                create_session_factory(engine),
                _keyring(new_material, old_material),
                _KillAfterCommittedBatchObserver(sender),
            )
            await service.rotate(batch_size=1)
        finally:
            await engine.dispose()

    try:
        asyncio.run(scenario())
    finally:
        sender.close()


def test_TC_SRC_12_real_process_kill_resumes_from_rows_and_is_idempotent(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _prepare_database(ephemeral_postgres.engine)
    old_material = os.urandom(32)
    new_material = os.urandom(32)
    plaintexts = (os.urandom(35), os.urandom(36))
    source_ids = asyncio.run(
        _seed_sources(
            ephemeral_postgres.engine.url,
            old_material=old_material,
            plaintexts=plaintexts,
        )
    )
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    worker = context.Process(
        target=_killable_rotation_worker,
        args=(ephemeral_postgres.engine.url, new_material, old_material, sender),
    )
    worker.start()
    try:
        assert _wait_message(receiver)[0] == "paused"
        worker.kill()
        worker.join(timeout=5)
        assert worker.exitcode is not None and worker.exitcode != 0

        interrupted = asyncio.run(_read_records(ephemeral_postgres.engine.url))
        assert {record.credential.key_id for record in interrupted if record.credential} == {
            "new-key",
            "old-key",
        }
        ring = _keyring(new_material, old_material)
        originals = dict(zip(source_ids, plaintexts, strict=True))
        for record in interrupted:
            assert record.credential is not None
            assert (
                ring.decrypt(
                    record.credential,
                    source_id=record.id,
                    credential_type=TOKEN_TYPE,
                )
                == originals[record.id]
            )

        async def resume_twice() -> tuple[int, tuple[tuple[object, ...], ...]]:
            engine = create_database_engine(_async_url(ephemeral_postgres.engine.url))
            try:
                service = CredentialRotationService(create_session_factory(engine), ring)
                first = await service.rotate(batch_size=1)
                records_after_first = await _read_records(ephemeral_postgres.engine.url)
                snapshot = tuple(
                    (
                        record.id,
                        record.credential.key_id if record.credential else None,
                        record.credential.nonce if record.credential else None,
                        record.credential.ciphertext if record.credential else None,
                        record.credential.tag if record.credential else None,
                    )
                    for record in records_after_first
                )
                second = await service.rotate(batch_size=1)
                assert second.reencrypted_rows == 0
                return first.reencrypted_rows, snapshot
            finally:
                await engine.dispose()

        resumed_rows, final_snapshot = asyncio.run(resume_twice())
        assert resumed_rows == 1
        final_records = asyncio.run(_read_records(ephemeral_postgres.engine.url))
        assert (
            tuple(
                (
                    record.id,
                    record.credential.key_id if record.credential else None,
                    record.credential.nonce if record.credential else None,
                    record.credential.ciphertext if record.credential else None,
                    record.credential.tag if record.credential else None,
                )
                for record in final_records
            )
            == final_snapshot
        )
        assert {record.credential.key_id for record in final_records if record.credential} == {
            "new-key"
        }
    finally:
        _stop_process(worker)
        receiver.close()


class _CollisionObserver(RotationObserver):
    def __init__(self, collision: Any) -> None:
        self._collision = collision

    async def row_decrypted(self, source_id: UUID, row_number: int) -> None:
        del source_id
        if row_number == 1:
            await asyncio.to_thread(self._collision.wait)


def _concurrent_rotation_worker(
    url: URL,
    new_material: bytes,
    old_material: bytes,
    start: Any,
    collision: Any,
    sender: PipeConnection,
) -> None:
    async def scenario() -> tuple[str, tuple[str, ...]]:
        await asyncio.to_thread(start.wait)
        engine = create_database_engine(_async_url(url))
        try:
            service = CredentialRotationService(
                create_session_factory(engine),
                _keyring(new_material, old_material),
                _CollisionObserver(collision),
            )
            result = await service.rotate()
            return "ok", tuple(str(source_id) for source_id in result.reencrypted_source_ids)
        finally:
            await engine.dispose()

    try:
        sender.send(asyncio.run(scenario()))
    except Exception as error:
        sender.send(("error", (type(error).__name__,)))
    finally:
        sender.close()


def test_TC_SRC_13_two_spawned_processes_report_disjoint_successful_rows(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _prepare_database(ephemeral_postgres.engine)
    old_material = os.urandom(32)
    new_material = os.urandom(32)
    plaintexts = (os.urandom(40), os.urandom(41))
    source_ids = asyncio.run(
        _seed_sources(
            ephemeral_postgres.engine.url,
            old_material=old_material,
            plaintexts=plaintexts,
        )
    )
    context = multiprocessing.get_context("spawn")
    start = context.Barrier(2)
    collision = context.Barrier(2)
    channels = [context.Pipe(duplex=False) for _ in range(2)]
    processes: list[SpawnProcess] = []
    for _receiver, sender in channels:
        process = context.Process(
            target=_concurrent_rotation_worker,
            args=(
                ephemeral_postgres.engine.url,
                new_material,
                old_material,
                start,
                collision,
                sender,
            ),
        )
        processes.append(process)
        process.start()
    try:
        reports = [_wait_message(receiver) for receiver, _sender in channels]
        for process in processes:
            process.join(timeout=PROCESS_TIMEOUT_SECONDS)
            assert process.exitcode == 0
        assert all(report[0] == "ok" for report in reports)
        successful_sets = [set(report[1]) for report in reports]
        assert successful_sets[0].isdisjoint(successful_sets[1])
        assert successful_sets[0] | successful_sets[1] == {
            str(source_id) for source_id in source_ids
        }

        ring = _keyring(new_material, old_material)
        originals = dict(zip(source_ids, plaintexts, strict=True))
        records = asyncio.run(_read_records(ephemeral_postgres.engine.url))
        assert {record.credential.key_id for record in records if record.credential} == {"new-key"}
        for record in records:
            assert record.credential is not None
            assert (
                ring.decrypt(
                    record.credential,
                    source_id=record.id,
                    credential_type=TOKEN_TYPE,
                )
                == originals[record.id]
            )
    finally:
        for process in processes:
            _stop_process(process)
        for receiver, _sender in channels:
            receiver.close()

"""TC-SRC-01..09: real PostgreSQL Model Source credential HTTP workflow."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Final, Protocol

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from credential_scan import database_bytes
from fixtures.asgi_client import (
    NO_CONFIGURED_CREDENTIAL_ROWS,
    AsgiResponse,
    AsgiTestClient,
)
from holm_contracts import VersionPayload
from holm_control_api.application import create_app
from holm_control_api.authentication import AuthenticationService
from holm_control_api.http_constants import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
)
from holm_control_api.model_source_routes import MODEL_SOURCES_PATH
from holm_control_api.model_source_service import ModelSourceCredentialService
from holm_control_api.model_source_validation import (
    ModelSourceCredentialValidator,
    ModelSourceValidationResult,
    ModelSourceValidatorRegistry,
)
from holm_control_api.readiness import DependencyCheck, ReadinessReason
from holm_domain import (
    AuditAction,
    AuditResult,
    ModelSourceConfigurationState,
    ModelSourceType,
    ModelSourceValidationAction,
    ModelSourceValidationReason,
    ModelSourceValidationStatus,
    UserRole,
)
from holm_persistence import (
    AsyncSessionFactory,
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
)
from migration_test_support import upgrade_head
from sqlalchemy.engine import URL, Engine
from test_TC_SEC_19_credential_rotation import (
    _assert_sentinels_absent as assert_sentinels_absent,
)

USERNAME: Final[str] = "model-source-admin"
PASSWORD: Final[str] = "correct-password-for-model-source-tests"
CHEAP: Final = Argon2idParameters(
    memory_cost_kib=OWASP_MINIMUM_MEMORY_COST_KIB,
    time_cost=OWASP_MINIMUM_TIME_COST,
    parallelism=1,
)
KEYRING: Final = MasterKeyring((MasterKey("source-key", MasterKeyState.WRITABLE, b"S" * 32),))


class PostgresFixture(Protocol):
    @property
    def engine(self) -> Engine: ...

    @property
    def socket_directory(self) -> Path: ...

    @property
    def port(self) -> int: ...


@dataclass(frozen=True, slots=True)
class _CredentialAwareValidator:
    async def validate(self, *, credential: bytes | None) -> ModelSourceValidationResult:
        if credential is not None and credential.endswith(b"-invalid"):
            return ModelSourceValidationResult(
                ModelSourceValidationStatus.INVALID_CREDENTIAL,
                ModelSourceValidationReason.CREDENTIAL_INVALID,
                ModelSourceValidationAction.UPDATE_CREDENTIAL,
            )
        return ModelSourceValidationResult(
            ModelSourceValidationStatus.ACCESSIBLE,
            ModelSourceValidationReason.CREDENTIAL_VALID,
            None,
        )


@dataclass(frozen=True, slots=True)
class _ExplodingValidator:
    detail: str

    async def validate(self, *, credential: bytes | None) -> ModelSourceValidationResult:
        del credential
        raise RuntimeError(self.detail)


@dataclass(frozen=True, slots=True)
class _Environment:
    client: AsgiTestClient
    session_factory: AsyncSessionFactory


async def _readiness_ok() -> ReadinessReason:
    return ReadinessReason.OK


def _async_url(fixture: PostgresFixture) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username="ubuntu",
        host=str(fixture.socket_directory),
        port=fixture.port,
        database="postgres",
    )


@asynccontextmanager
async def _environment(
    fixture: PostgresFixture,
    *,
    validators_available: bool = True,
    validator: ModelSourceCredentialValidator | None = None,
) -> AsyncIterator[_Environment]:
    engine = create_database_engine(_async_url(fixture), connect_timeout_seconds=5)
    try:
        session_factory = create_session_factory(engine)
        authentication = AuthenticationService(
            session_factory=session_factory,
            password_hasher=PasswordHasher(CHEAP),
            minimum_login_failure_seconds=0,
        )
        validators = ModelSourceValidatorRegistry(
            {
                source_type: (_CredentialAwareValidator() if validator is None else validator)
                for source_type in ModelSourceType
            }
            if validators_available
            else {}
        )
        model_sources = ModelSourceCredentialService(
            session_factory=session_factory,
            authentication=authentication,
            keyring=KEYRING,
            validators=validators,
        )
        app = create_app(
            version_payload=VersionPayload(
                build_version="2.1.0+gsrc01a0001",
                commit="a" * 40,
                schema_version=1,
                runtime_compat_hash="b" * 64,
            ),
            readiness_checks=[DependencyCheck(name="database", probe=_readiness_ok)],
            credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
            authentication=authentication,
            model_sources=model_sources,
        )
        yield _Environment(client=AsgiTestClient(app), session_factory=session_factory)
    finally:
        await engine.dispose()


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
            display_name="Model Source Admin",
            role=UserRole.ADMIN,
            password_hash=password_hash,
            now=now,
            must_change_password=False,
        )
        await session.commit()


def _header(response: AsgiResponse, name: str) -> str:
    values = [value for key, value in response.headers if key.casefold() == name.casefold()]
    assert len(values) == 1
    return values[0]


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


async def _login(environment: _Environment) -> tuple[Mapping[str, str], Mapping[str, str]]:
    response = await environment.client.request(
        "POST",
        "/api/v1/auth/login",
        json_body={"username": USERNAME, "password": PASSWORD},
    )
    assert response.status == 200
    session = _cookie(response, SESSION_COOKIE_NAME)
    csrf = _cookie(response, CSRF_COOKIE_NAME)
    return {SESSION_COOKIE_NAME: session}, {CSRF_HEADER_NAME: csrf}


async def _put(
    environment: _Environment,
    *,
    cookies: Mapping[str, str],
    headers: Mapping[str, str],
    source_type: ModelSourceType,
    credential: str | None = None,
    clear: bool = False,
    password: str | None = PASSWORD,
    confirm_unverified: bool = False,
) -> AsgiResponse:
    body: dict[str, object] = {
        "source_type": source_type.value,
        "clear": clear,
        "current_password": password,
        "confirm_unverified": confirm_unverified,
    }
    if credential is not None:
        body["credential"] = credential
    return await environment.client.request(
        "PUT",
        MODEL_SOURCES_PATH,
        cookies=cookies,
        headers=headers,
        json_body=body,
    )


def test_TC_SRC_01_TC_SRC_02_three_states_tail_and_no_store(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    upgrade_head(ephemeral_postgres.engine)

    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as environment:
            await _reset_and_create_admin(environment)
            cookies, headers = await _login(environment)
            states: list[str] = []

            initial = await environment.client.get(MODEL_SOURCES_PATH, cookies=cookies)
            assert initial.status == 200
            assert _header(initial, "cache-control") == "no-store"
            payload = initial.json()
            assert len(payload) == len(ModelSourceType)
            assert all(item["configuration_state"] == "unconfigured" for item in payload)
            assert all(item["credential_last4"] is None for item in payload)
            states.append(payload[0]["configuration_state"])
            assert not ({"id", "key_id", "endpoint", "credential"} & set(payload[0]))

            configured = await _put(
                environment,
                cookies=cookies,
                headers=headers,
                source_type=ModelSourceType.HUGGING_FACE,
                credential="credential-value-1234",
            )
            assert configured.status == 200
            assert _header(configured, "cache-control") == "no-store"
            assert configured.json()["configuration_state"] == "configured"
            assert configured.json()["credential_last4"] == "1234"
            states.append(configured.json()["configuration_state"])

            short = await _put(
                environment,
                cookies=cookies,
                headers=headers,
                source_type=ModelSourceType.MODELSCOPE,
                credential="short7",
            )
            assert short.status == 200
            assert short.json()["credential_last4"] is None

            async with environment.session_factory() as session:
                await session.execute(
                    sa.text(
                        "UPDATE core.model_sources SET credential_key_id = 'missing-key' "
                        "WHERE type = 'Hugging Face'"
                    )
                )
                await session.commit()
            unavailable = await environment.client.get(MODEL_SOURCES_PATH, cookies=cookies)
            unavailable_item = next(
                item
                for item in unavailable.json()
                if item["source_type"] == ModelSourceType.HUGGING_FACE.value
            )
            assert unavailable_item["configuration_state"] == "key_unavailable"
            assert unavailable_item["credential_last4"] is None
            states.append(unavailable_item["configuration_state"])
            assert set(states) == {item.value for item in ModelSourceConfigurationState}

            def assert_three_states(values: list[str]) -> None:
                assert len(set(values)) == 3

            assert_three_states(states)
            for collapsed in ("unconfigured", "configured"):
                mutated = [states[0], states[1], collapsed]
                with pytest.raises(AssertionError):
                    assert_three_states(mutated)

    asyncio.run(scenario())


def test_TC_SRC_03_write_replace_clear_and_replay_are_idempotent(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    upgrade_head(ephemeral_postgres.engine)

    async def envelope(environment: _Environment) -> tuple[object, ...]:
        async with environment.session_factory() as session:
            row = (
                await session.execute(
                    sa.text(
                        "SELECT credential_key_id, credential_nonce, credential_ciphertext, "
                        "credential_tag FROM core.model_sources WHERE type = 'Hugging Face'"
                    )
                )
            ).one()
            return tuple(row)

    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as environment:
            await _reset_and_create_admin(environment)
            cookies, headers = await _login(environment)
            first = await _put(
                environment,
                cookies=cookies,
                headers=headers,
                source_type=ModelSourceType.HUGGING_FACE,
                credential="first-credential-value",
            )
            assert first.status == 200
            first_envelope = await envelope(environment)
            assert all(value is not None for value in first_envelope)

            replay = await _put(
                environment,
                cookies=cookies,
                headers=headers,
                source_type=ModelSourceType.HUGGING_FACE,
                credential="first-credential-value",
            )
            assert replay.status == 200
            assert await envelope(environment) == first_envelope

            replaced = await _put(
                environment,
                cookies=cookies,
                headers=headers,
                source_type=ModelSourceType.HUGGING_FACE,
                credential="replacement-value-9999",
            )
            assert replaced.status == 200
            assert replaced.json()["credential_last4"] == "9999"
            assert await envelope(environment) != first_envelope

            cleared = await _put(
                environment,
                cookies=cookies,
                headers=headers,
                source_type=ModelSourceType.HUGGING_FACE,
                clear=True,
            )
            assert cleared.status == 200
            assert cleared.json()["configuration_state"] == "unconfigured"
            assert cleared.json()["credential_last4"] is None
            assert await envelope(environment) == (None, None, None, None)
            replay_clear = await _put(
                environment,
                cookies=cookies,
                headers=headers,
                source_type=ModelSourceType.HUGGING_FACE,
                clear=True,
            )
            assert replay_clear.status == 200
            assert await envelope(environment) == (None, None, None, None)

    asyncio.run(scenario())


def test_TC_SRC_05_password_recheck_missing_wrong_and_locked_never_write(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    upgrade_head(ephemeral_postgres.engine)

    async def snapshot(environment: _Environment) -> tuple[tuple[object, ...], ...]:
        async with environment.session_factory() as session:
            return tuple(
                tuple(row)
                for row in (
                    await session.execute(sa.text("SELECT * FROM core.model_sources ORDER BY type"))
                ).all()
            )

    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as environment:
            await _reset_and_create_admin(environment)
            cookies, headers = await _login(environment)
            before = await snapshot(environment)
            missing = await _put(
                environment,
                cookies=cookies,
                headers=headers,
                source_type=ModelSourceType.HUGGING_FACE,
                credential="never-written-missing",
                password=None,
            )
            assert missing.status == 401
            assert missing.json()["error"]["code"] == "invalid_credentials"
            assert await snapshot(environment) == before

            wrong_responses = []
            for _ in range(4):
                wrong_responses.append(
                    await _put(
                        environment,
                        cookies=cookies,
                        headers=headers,
                        source_type=ModelSourceType.HUGGING_FACE,
                        credential="never-written-wrong",
                        password="wrong-password",
                    )
                )
            assert wrong_responses[-1].status == 429
            locked_correct = await _put(
                environment,
                cookies=cookies,
                headers=headers,
                source_type=ModelSourceType.HUGGING_FACE,
                credential="never-written-locked",
                password=PASSWORD,
            )
            assert locked_correct.status == 429
            assert locked_correct.json()["error"]["code"] == "login_locked"
            assert await snapshot(environment) == before

    asyncio.run(scenario())


def test_TC_SRC_07_missing_validator_saves_but_never_presents_as_verified(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    upgrade_head(ephemeral_postgres.engine)

    async def scenario() -> None:
        async with _environment(
            ephemeral_postgres,
            validators_available=False,
        ) as environment:
            await _reset_and_create_admin(environment)
            cookies, headers = await _login(environment)
            rejected = await _put(
                environment,
                cookies=cookies,
                headers=headers,
                source_type=ModelSourceType.HUGGING_FACE,
                credential="saved-without-an-adapter",
            )
            assert rejected.status == 409
            assert rejected.json()["error"]["details"]["reason_code"] == "validator_unavailable"
            with ephemeral_postgres.engine.connect() as connection:
                assert connection.execute(sa.text("SELECT * FROM core.model_sources")).all() == []
            response = await _put(
                environment,
                cookies=cookies,
                headers=headers,
                source_type=ModelSourceType.HUGGING_FACE,
                credential="saved-without-an-adapter",
                confirm_unverified=True,
            )
            assert response.status == 200
            assert response.json()["configuration_state"] == "configured"
            assert response.json()["verification_status"] is None
            assert response.json()["verification_reason_code"] == "validator_unavailable"
            assert response.json()["next_action"] == "install_validator"

            persisted = await environment.client.get(MODEL_SOURCES_PATH, cookies=cookies)
            item = next(
                item
                for item in persisted.json()
                if item["source_type"] == ModelSourceType.HUGGING_FACE.value
            )
            assert item == response.json()

    asyncio.run(scenario())


def test_TC_SRC_09_TC_SEC_09_validator_exception_text_and_repr_are_sanitized(
    ephemeral_postgres: EphemeralPostgres,
    caplog: pytest.LogCaptureFixture,
) -> None:
    upgrade_head(ephemeral_postgres.engine)
    sentinel = f"PWSSNTL-SRCTOK-TC-SRC-09-EXC-{os.urandom(6).hex().upper()}"
    injected_exception_text = f"adapter failure included {sentinel}"
    assert sentinel in injected_exception_text

    async def scenario() -> AsgiResponse:
        async with _environment(
            ephemeral_postgres,
            validator=_ExplodingValidator(injected_exception_text),
        ) as environment:
            await _reset_and_create_admin(environment)
            cookies, headers = await _login(environment)
            rejected = await _put(
                environment,
                cookies=cookies,
                headers=headers,
                source_type=ModelSourceType.HUGGING_FACE,
                credential=sentinel,
            )
            assert rejected.status == 409
            assert rejected.json()["error"]["details"]["reason_code"] == "validation_failed"
            assert sentinel not in rejected.text
            with ephemeral_postgres.engine.connect() as connection:
                assert connection.execute(sa.text("SELECT * FROM core.model_sources")).all() == []
            return await _put(
                environment,
                cookies=cookies,
                headers=headers,
                source_type=ModelSourceType.HUGGING_FACE,
                credential=sentinel,
                confirm_unverified=True,
            )

    response = asyncio.run(scenario())
    assert response.status == 200
    assert response.server_error is None
    assert response.json()["verification_status"] is None
    assert response.json()["verification_reason_code"] == "validation_failed"
    assert response.json()["next_action"] == "retry_later"
    exposed_exception_surface = f"{response.server_error!s}\n{response.server_error!r}"
    rendered_logs = "\n".join(
        f"{record.getMessage()} {record.__dict__!r}" for record in caplog.records
    )
    assert sentinel not in response.text
    assert sentinel not in exposed_exception_surface
    assert sentinel not in rendered_logs


def test_TC_SRC_09_TC_SEC_09_audit_allowlist_and_sentinels_have_zero_leakage(
    ephemeral_postgres: EphemeralPostgres,
    caplog: pytest.LogCaptureFixture,
) -> None:
    upgrade_head(ephemeral_postgres.engine)
    source_sentinel = f"PWSSNTL-SRCTOK-TC-SRC-09-{os.urandom(6).hex().upper()}"
    password_sentinel = f"PWSSNTL-PASSWD-TC-SRC-09-{os.urandom(6).hex().upper()}"

    async def scenario() -> tuple[AsgiResponse, AsgiResponse, AsgiResponse]:
        async with _environment(ephemeral_postgres) as environment:
            await _reset_and_create_admin(environment)
            cookies, headers = await _login(environment)
            succeeded = await _put(
                environment,
                cookies=cookies,
                headers=headers,
                source_type=ModelSourceType.HUGGING_FACE,
                credential=source_sentinel,
            )
            with ephemeral_postgres.engine.connect() as connection:
                before = connection.execute(sa.text("SELECT * FROM core.model_sources")).all()
            failed = await _put(
                environment,
                cookies=cookies,
                headers=headers,
                source_type=ModelSourceType.MODELSCOPE,
                credential=f"{source_sentinel}-invalid",
            )
            assert failed.json()["error"]["code"] == "model_source_credential_rejected"
            with ephemeral_postgres.engine.connect() as connection:
                assert (
                    connection.execute(sa.text("SELECT * FROM core.model_sources")).all() == before
                )
            denied = await _put(
                environment,
                cookies=cookies,
                headers=headers,
                source_type=ModelSourceType.HUGGING_FACE,
                credential=f"{source_sentinel}-denied",
                password=password_sentinel,
            )
            assert (succeeded.status, failed.status, denied.status) == (200, 422, 401)

            async with environment.session_factory() as session:
                rows = (
                    (
                        await session.execute(
                            sa.text(
                                "SELECT result, request_id, actor_id, object_type, details "
                                "FROM core.audit_events "
                                "WHERE action = :action ORDER BY created_at"
                            ),
                            {"action": AuditAction.MODEL_SOURCE_CREDENTIAL_CHANGE.value},
                        )
                    )
                    .mappings()
                    .all()
                )
            assert [row["result"] for row in rows] == [
                AuditResult.SUCCEEDED.value,
                AuditResult.FAILED.value,
                AuditResult.DENIED.value,
            ]
            assert all(
                row["actor_id"] is not None and row["request_id"] is not None for row in rows
            )
            assert all(row["object_type"] == "model_source" for row in rows)
            expected_detail_fields = {
                "operation",
                "reason_code",
                "source_type",
                "validation_status",
            }
            assert all(set(row["details"]) == expected_detail_fields for row in rows)
            rendered_details = json.dumps([dict(row["details"]) for row in rows])
            assert source_sentinel not in rendered_details
            assert password_sentinel not in rendered_details

            scanned = database_bytes(ephemeral_postgres.engine)
            assert_sentinels_absent(
                scanned,
                (source_sentinel.encode(), password_sentinel.encode()),
            )
            with ephemeral_postgres.engine.connect() as connection:
                original = connection.execute(
                    sa.text(
                        "SELECT credential_ciphertext FROM core.model_sources "
                        "WHERE type = 'Hugging Face'"
                    )
                ).scalar_one()
                connection.commit()
            try:
                with ephemeral_postgres.engine.begin() as connection:
                    connection.execute(
                        sa.text(
                            "UPDATE core.model_sources SET credential_ciphertext = :sentinel "
                            "WHERE type = 'Hugging Face'"
                        ),
                        {"sentinel": source_sentinel.encode()},
                    )
                assert source_sentinel.encode() in database_bytes(ephemeral_postgres.engine)
            finally:
                with ephemeral_postgres.engine.begin() as connection:
                    connection.execute(
                        sa.text(
                            "UPDATE core.model_sources SET credential_ciphertext = :original "
                            "WHERE type = 'Hugging Face'"
                        ),
                        {"original": original},
                    )
            return succeeded, failed, denied

    responses = asyncio.run(scenario())
    rendered_responses = "\n".join(response.text for response in responses)
    rendered_logs = "\n".join(
        f"{record.getMessage()} {record.__dict__!r}" for record in caplog.records
    )
    process_surface = b"\n".join(
        (Path("/proc/self/cmdline").read_bytes(), json.dumps(dict(os.environ)).encode())
    ).decode("utf-8", errors="replace")
    for sentinel in (source_sentinel, password_sentinel):
        assert sentinel not in rendered_responses
        assert sentinel not in rendered_logs
        assert sentinel not in process_surface

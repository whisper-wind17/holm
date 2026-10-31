"""TC-SEC-10/16: real HTTP authentication against real PostgreSQL 16."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator, MutableMapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from functools import partial
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Final, Protocol, cast
from uuid import UUID

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fixtures.asgi_client import NO_CONFIGURED_CREDENTIAL_ROWS, AsgiResponse
from holm_common import new_uuid7
from holm_contracts import VersionPayload
from holm_control_api import (
    CACHE_CONTROL_VALUE,
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    LOGIN_PATH,
    LOGOUT_PATH,
    ME_PATH,
    PASSWORD_PATH,
    SESSION_ABSOLUTE_TIMEOUT,
    SESSION_COOKIE_NAME,
    SESSION_IDLE_TIMEOUT,
    AuthenticationFailure,
    AuthenticationService,
    DependencyCheck,
    ReadinessReason,
    capability_route,
    create_app,
)
from holm_control_api.authentication import PasswordHasherPort
from holm_domain import (
    AuditAction,
    AuditActorType,
    AuditObjectType,
    AuditResult,
    Capability,
)
from holm_persistence import (
    ACCOUNT_TABLES,
    AUDIT_EVENTS,
    LOGIN_PROTECTION,
    SESSIONS,
    USERS,
    AsyncSessionFactory,
    AuditEventDraft,
    AuditEventRecord,
    AuditWriter,
    LoginProtectionRepository,
    UserRepository,
    UserRole,
    create_database_engine,
    create_session_factory,
    database_utc_now,
)
from holm_security import (
    OWASP_MINIMUM_MEMORY_COST_KIB,
    OWASP_MINIMUM_TIME_COST,
    Argon2idParameters,
    PasswordHasher,
    SessionTokenPair,
    build_audit_details,
    generate_session_token_pair,
    hash_token,
    login_principal_hash,
    parse_argon2id_parameters,
)
from migration_test_support import upgrade_head
from sqlalchemy.engine import URL, Engine
from sqlalchemy.ext.asyncio import AsyncSession

CLUSTER_USER: Final[str] = "ubuntu"
CLUSTER_DATABASE: Final[str] = "postgres"
CHEAP: Final[Argon2idParameters] = Argon2idParameters(
    memory_cost_kib=OWASP_MINIMUM_MEMORY_COST_KIB,
    time_cost=OWASP_MINIMUM_TIME_COST,
    parallelism=1,
)
USERNAME: Final[str] = "security-user"
PASSWORD: Final[str] = "correct-password-for-security-tests"
NEW_PASSWORD: Final[str] = "new-password-for-security-tests"

type Message = MutableMapping[str, Any]
type HeaderMapping = dict[str, str]
type TokenFactory = Callable[[], SessionTokenPair]


class PostgresFixture(Protocol):
    @property
    def engine(self) -> Engine: ...

    @property
    def socket_directory(self) -> Path: ...

    @property
    def data_directory(self) -> Path: ...

    @property
    def port(self) -> int: ...


class RecordingPasswordHasher:
    """Real Argon2id with a call trace that contains no candidate passwords."""

    def __init__(self) -> None:
        self._delegate = PasswordHasher(CHEAP)
        self.verified_hashes: list[str] = []

    def hash_user_password(
        self,
        password: str,
        *,
        username: str,
        display_name: str,
    ) -> str:
        return self._delegate.hash_user_password(
            password,
            username=username,
            display_name=display_name,
        )

    def hash_authentication_sentinel(self) -> str:
        return self._delegate.hash_authentication_sentinel()

    def verify(self, encoded_hash: str, password: str) -> bool:
        self.verified_hashes.append(encoded_hash)
        return self._delegate.verify(encoded_hash, password)


class BlockingPasswordHasher:
    """Expose worker concurrency without retaining any submitted password."""

    def __init__(self, expected_concurrency: int) -> None:
        self._delegate = PasswordHasher(CHEAP)
        self._expected_concurrency = expected_concurrency
        self._lock = threading.Lock()
        self.release = threading.Event()
        self.expected_workers_entered = threading.Event()
        self.active = 0
        self.calls = 0
        self.maximum_active = 0

    def hash_user_password(
        self,
        password: str,
        *,
        username: str,
        display_name: str,
    ) -> str:
        return self._delegate.hash_user_password(
            password,
            username=username,
            display_name=display_name,
        )

    def hash_authentication_sentinel(self) -> str:
        return self._delegate.hash_authentication_sentinel()

    def verify(self, encoded_hash: str, password: str) -> bool:
        with self._lock:
            self.active += 1
            self.calls += 1
            self.maximum_active = max(self.maximum_active, self.active)
            if self.active == self._expected_concurrency:
                self.expected_workers_entered.set()
        try:
            if not self.release.wait(timeout=5):
                raise RuntimeError("test password worker was not released")
            return self._delegate.verify(encoded_hash, password)
        finally:
            with self._lock:
                self.active -= 1


class PausingLoginProtectionSession:
    """Pause immediately after the protection insert/upsert reaches PostgreSQL."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self.statement_completed = asyncio.Event()
        self.resume = asyncio.Event()
        self._paused = False

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        result = await self._session.execute(statement, *args, **kwargs)
        if (
            not self._paused
            and bool(getattr(statement, "is_insert", False))
            and getattr(statement, "table", None) is LOGIN_PROTECTION
        ):
            self._paused = True
            self.statement_completed.set()
            await self.resume.wait()
        return result


class TokenSequence:
    """Return explicit test sentinels first, then production CSPRNG tokens."""

    def __init__(self, *pairs: SessionTokenPair) -> None:
        self._pairs = list(pairs)

    def __call__(self) -> SessionTokenPair:
        if self._pairs:
            return self._pairs.pop(0)
        return generate_session_token_pair()


@dataclass(frozen=True, slots=True)
class AuthEnvironment:
    app: FastAPI
    authentication: AuthenticationService
    session_factory: AsyncSessionFactory


@pytest.fixture(scope="module")
def migrated_postgres(ephemeral_postgres: PostgresFixture) -> PostgresFixture:
    upgrade_head(ephemeral_postgres.engine)
    return ephemeral_postgres


@pytest.fixture
def auth_database(migrated_postgres: PostgresFixture) -> Iterator[PostgresFixture]:
    with migrated_postgres.engine.begin() as connection:
        connection.exec_driver_sql(
            "TRUNCATE core.request_records, core.user_service_permissions, "
            "core.api_key_service_scopes, "
            "core.api_keys, core.idempotency_records, "
            "core.system_initialization, core.audit_events, core.login_protection, "
            "core.sessions, core.bootstrap_tokens, core.users"
        )
    yield migrated_postgres


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


def _build_app(authentication: AuthenticationService) -> FastAPI:
    return create_app(
        version_payload=VersionPayload(
            build_version="1.4.0+gm1010c0001",
            commit="c" * 40,
            schema_version=1,
            runtime_compat_hash="d" * 64,
        ),
        credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
        readiness_checks=[DependencyCheck(name="database", probe=_readiness_ok)],
        authentication=authentication,
    )


@asynccontextmanager
async def _environment(
    fixture: PostgresFixture,
    *,
    hasher: PasswordHasherPort | None = None,
    token_factory: TokenFactory = generate_session_token_pair,
    minimum_login_failure_seconds: float = 0,
    max_concurrent_password_operations: int = 4,
) -> AsyncIterator[AuthEnvironment]:
    engine = create_database_engine(_async_url(fixture), connect_timeout_seconds=5)
    try:
        session_factory = create_session_factory(engine)
        authentication = AuthenticationService(
            session_factory=session_factory,
            password_hasher=PasswordHasher(CHEAP) if hasher is None else hasher,
            token_factory=token_factory,
            minimum_login_failure_seconds=minimum_login_failure_seconds,
            max_concurrent_password_operations=max_concurrent_password_operations,
        )
        yield AuthEnvironment(
            app=_build_app(authentication),
            authentication=authentication,
            session_factory=session_factory,
        )
    finally:
        await engine.dispose()


async def _create_user(
    session_factory: AsyncSessionFactory,
    *,
    username: str = USERNAME,
    password: str = PASSWORD,
    must_change_password: bool = True,
) -> None:
    password_hash = await asyncio.to_thread(PasswordHasher(CHEAP).hash, password)
    async with session_factory() as session:
        now = await database_utc_now(session)
        await UserRepository(session).create(
            username=username,
            display_name="Security User",
            role=UserRole.USER,
            password_hash=password_hash,
            now=now,
            must_change_password=must_change_password,
        )
        await session.commit()


async def _request(
    app: FastAPI,
    method: str,
    path: str,
    *,
    body: dict[str, str] | None = None,
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
        cookie_value = "; ".join(f"{name}={value}" for name, value in cookies.items())
        raw_headers.append((b"cookie", cookie_value.encode("latin-1")))

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

    await app(scope, receive, send)
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
    )


def _header(response: AsgiResponse, name: str) -> str:
    values = [value for key, value in response.headers if key.casefold() == name.casefold()]
    assert len(values) == 1, (name, values)
    return values[0]


def _named_cookie(response: AsgiResponse, name: str) -> tuple[str, str, SimpleCookie]:
    matches: list[tuple[str, SimpleCookie]] = []
    for key, raw in response.headers:
        if key.casefold() != "set-cookie":
            continue
        parsed = SimpleCookie()
        parsed.load(raw)
        if name in parsed:
            matches.append((raw, parsed))
    assert len(matches) == 1, (name, [raw for raw, _ in matches])
    raw, parsed = matches[0]
    return parsed[name].value, raw, parsed


def _session_cookie(response: AsgiResponse) -> tuple[str, str, SimpleCookie]:
    return _named_cookie(response, SESSION_COOKIE_NAME)


def _csrf_cookie(response: AsgiResponse) -> tuple[str, str, SimpleCookie]:
    return _named_cookie(response, CSRF_COOKIE_NAME)


def _set_cookie_headers(response: AsgiResponse) -> tuple[str, ...]:
    return tuple(value for key, value in response.headers if key.casefold() == "set-cookie")


def _assert_exact_cookie_names(response: AsgiResponse) -> None:
    names: list[str] = []
    for raw in _set_cookie_headers(response):
        parsed = SimpleCookie()
        parsed.load(raw)
        assert len(parsed) == 1, raw
        names.extend(parsed.keys())
    assert names and len(names) == 2
    assert set(names) == {SESSION_COOKIE_NAME, CSRF_COOKIE_NAME}


def _assert_issued_cookie_contract(response: AsgiResponse) -> None:
    _assert_exact_cookie_names(response)
    csrf_token, raw, parsed = _csrf_cookie(response)
    morsel = parsed[CSRF_COOKIE_NAME]
    assert morsel.key == CSRF_COOKIE_NAME == "__Host-pws_csrf"
    assert bool(morsel["secure"]) is True
    assert bool(morsel["httponly"]) is False
    assert morsel["samesite"].casefold() == "strict"
    assert morsel["path"] == "/"
    assert morsel["domain"] == ""
    assert "domain=" not in raw.casefold()
    assert morsel["max-age"] == str(int(SESSION_ABSOLUTE_TIMEOUT.total_seconds()))
    assert csrf_token == _csrf_token(response)


def _assert_expired_cookie_contract(response: AsgiResponse) -> None:
    _assert_exact_cookie_names(response)
    for name in (SESSION_COOKIE_NAME, CSRF_COOKIE_NAME):
        value, raw, parsed = _named_cookie(response, name)
        morsel = parsed[name]
        assert value == ""
        assert morsel["max-age"] == "0"
        assert bool(morsel["secure"]) is True
        assert morsel["samesite"].casefold() == "strict"
        assert morsel["path"] == "/"
        assert morsel["domain"] == ""
        assert "domain=" not in raw.casefold()
    assert bool(_named_cookie(response, SESSION_COOKIE_NAME)[2][SESSION_COOKIE_NAME]["httponly"])
    assert not bool(_named_cookie(response, CSRF_COOKIE_NAME)[2][CSRF_COOKIE_NAME]["httponly"])


def _response_with_headers(
    response: AsgiResponse, headers: tuple[tuple[str, str], ...]
) -> AsgiResponse:
    return AsgiResponse(
        status=response.status,
        headers=headers,
        body=response.body,
        server_error=response.server_error,
    )


def _without_named_cookie(response: AsgiResponse, name: str) -> AsgiResponse:
    kept: list[tuple[str, str]] = []
    removed = 0
    for key, raw in response.headers:
        parsed = SimpleCookie()
        if key.casefold() == "set-cookie":
            parsed.load(raw)
        if name in parsed:
            removed += 1
        else:
            kept.append((key, raw))
    assert removed == 1
    return _response_with_headers(response, tuple(kept))


def _mutate_named_cookie(
    response: AsgiResponse, name: str, mutation: Callable[[str], str]
) -> AsgiResponse:
    changed: list[tuple[str, str]] = []
    mutated = 0
    for key, raw in response.headers:
        parsed = SimpleCookie()
        if key.casefold() == "set-cookie":
            parsed.load(raw)
        if name in parsed:
            changed.append((key, mutation(raw)))
            mutated += 1
        else:
            changed.append((key, raw))
    assert mutated == 1
    return _response_with_headers(response, tuple(changed))


def _replace_cookie_part(raw: str, old: str, new: str = "") -> str:
    assert old in raw, (old, raw)
    return raw.replace(old, new, 1)


def _assert_guard_rejects(callback: Callable[[], object], label: str) -> None:
    try:
        callback()
    except AssertionError:
        return
    raise AssertionError(f"positive control was silently accepted: {label}")


def _csrf_token(response: AsgiResponse) -> str:
    value = response.json()["csrf_token"]
    assert isinstance(value, str)
    return value


async def _login(
    environment: AuthEnvironment,
    *,
    username: str = USERNAME,
    password: str = PASSWORD,
    existing_session: str | None = None,
) -> AsgiResponse:
    cookies = None if existing_session is None else {SESSION_COOKIE_NAME: existing_session}
    return await _request(
        environment.app,
        "POST",
        LOGIN_PATH,
        body={"username": username, "password": password},
        cookies=cookies,
    )


def _error_without_request_id(response: AsgiResponse) -> dict[str, Any]:
    error = dict(response.json()["error"])
    request_id = error.pop("request_id")
    assert request_id == _header(response, "x-request-id")
    return error


def test_TC_SEC_10_login_sets_every_required_host_cookie_attribute(
    auth_database: PostgresFixture,
) -> None:
    async def scenario() -> None:
        async with _environment(auth_database) as environment:
            await _create_user(environment.session_factory)
            response = await _login(environment)

            assert response.status == 200
            assert response.json()["must_change_password"] is True
            assert _header(response, "cache-control") == CACHE_CONTROL_VALUE
            session_token, raw_cookie, parsed = _session_cookie(response)
            morsel = parsed[SESSION_COOKIE_NAME]
            assert morsel.key == "__Host-pws_session"
            assert bool(morsel["secure"]) is True
            assert bool(morsel["httponly"]) is True
            assert morsel["samesite"].casefold() == "strict"
            assert morsel["path"] == "/"
            assert morsel["domain"] == ""
            assert "domain=" not in raw_cookie.casefold()
            assert session_token not in response.text
            assert _csrf_token(response) != session_token

    asyncio.run(scenario())


def test_TC_SEC_14_m1_01f_all_session_responses_pair_the_exact_cookie_set(
    auth_database: PostgresFixture,
) -> None:
    async def scenario() -> None:
        async with _environment(auth_database) as environment:
            await _create_user(environment.session_factory)
            login = await _login(environment)
            _assert_issued_cookie_contract(login)
            session_token, _, _ = _session_cookie(login)
            csrf_token, _, _ = _csrf_cookie(login)
            browser_cookies = {
                SESSION_COOKIE_NAME: session_token,
                CSRF_COOKIE_NAME: csrf_token,
            }

            for headers in (None, {CSRF_HEADER_NAME: "wrong-csrf-token"}):
                rejected = await _request(
                    environment.app,
                    "POST",
                    LOGOUT_PATH,
                    cookies=browser_cookies,
                    headers=headers,
                )
                assert rejected.status == 403
                assert _error_without_request_id(rejected)["code"] == "csrf_validation_failed"

            changed = await _request(
                environment.app,
                "POST",
                PASSWORD_PATH,
                cookies=browser_cookies,
                headers={CSRF_HEADER_NAME: csrf_token},
                body={
                    "new_password": NEW_PASSWORD,
                    "confirm_password": NEW_PASSWORD,
                },
            )
            assert changed.status == 200
            _assert_issued_cookie_contract(changed)
            rotated_session, _, _ = _session_cookie(changed)
            rotated_csrf, _, _ = _csrf_cookie(changed)
            assert rotated_csrf != csrf_token

            rotated_cookies = {
                SESSION_COOKIE_NAME: rotated_session,
                CSRF_COOKIE_NAME: rotated_csrf,
            }
            stale = await _request(
                environment.app,
                "POST",
                LOGOUT_PATH,
                cookies=rotated_cookies,
                headers={CSRF_HEADER_NAME: csrf_token},
            )
            assert stale.status == 403
            assert _error_without_request_id(stale)["code"] == "csrf_validation_failed"

            logout = await _request(
                environment.app,
                "POST",
                LOGOUT_PATH,
                cookies=rotated_cookies,
                headers={CSRF_HEADER_NAME: rotated_csrf},
            )
            assert logout.status == 200
            _assert_expired_cookie_contract(logout)

    asyncio.run(scenario())


def test_TC_SEC_14_m1_01f_cookie_guards_have_every_required_positive_control(
    auth_database: PostgresFixture,
) -> None:
    async def scenario() -> None:
        async with _environment(auth_database) as environment:
            await _create_user(environment.session_factory)
            login = await _login(environment)
            _assert_issued_cookie_contract(login)
            session_token, _, _ = _session_cookie(login)
            csrf_token, _, _ = _csrf_cookie(login)
            changed = await _request(
                environment.app,
                "POST",
                PASSWORD_PATH,
                cookies={
                    SESSION_COOKIE_NAME: session_token,
                    CSRF_COOKIE_NAME: csrf_token,
                },
                headers={CSRF_HEADER_NAME: csrf_token},
                body={"new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD},
            )
            _assert_issued_cookie_contract(changed)
            rotated_session, _, _ = _session_cookie(changed)
            rotated_csrf, _, _ = _csrf_cookie(changed)
            logout = await _request(
                environment.app,
                "POST",
                LOGOUT_PATH,
                cookies={
                    SESSION_COOKIE_NAME: rotated_session,
                    CSRF_COOKIE_NAME: rotated_csrf,
                },
                headers={CSRF_HEADER_NAME: rotated_csrf},
            )
            _assert_expired_cookie_contract(logout)

            mutations: tuple[tuple[str, Callable[[str], str]], ...] = (
                (
                    "__Host- prefix",
                    lambda raw: _replace_cookie_part(raw, CSRF_COOKIE_NAME, "pws_csrf"),
                ),
                ("Secure", lambda raw: _replace_cookie_part(raw, "; Secure")),
                ("SameSite", lambda raw: _replace_cookie_part(raw, "; SameSite=strict")),
                ("Path", lambda raw: _replace_cookie_part(raw, "; Path=/")),
                ("no Domain", lambda raw: f"{raw}; Domain=example.test"),
                ("Max-Age", lambda raw: _replace_cookie_part(raw, "; Max-Age=28800")),
                ("non-HttpOnly", lambda raw: f"{raw}; HttpOnly"),
                (
                    "body-cookie equality",
                    lambda raw: _replace_cookie_part(raw, csrf_token, "different-csrf-token"),
                ),
            )
            for label, mutation in mutations:
                mutated = _mutate_named_cookie(login, CSRF_COOKIE_NAME, mutation)
                _assert_guard_rejects(partial(_assert_issued_cookie_contract, mutated), label)

            extra = _response_with_headers(
                login,
                (*login.headers, ("set-cookie", "third=value; Path=/; Secure")),
            )
            _assert_guard_rejects(lambda: _assert_exact_cookie_names(extra), "third Set-Cookie")
            _assert_guard_rejects(
                lambda: _assert_exact_cookie_names(_without_named_cookie(login, CSRF_COOKIE_NAME)),
                "missing Set-Cookie",
            )
            _assert_guard_rejects(
                lambda: _assert_issued_cookie_contract(
                    _without_named_cookie(changed, CSRF_COOKIE_NAME)
                ),
                "password-change CSRF setting",
            )
            _assert_guard_rejects(
                lambda: _assert_expired_cookie_contract(
                    _without_named_cookie(logout, CSRF_COOKIE_NAME)
                ),
                "logout CSRF expiry",
            )

            request_id = _header(login, "x-request-id")
            duplicate_request_id = _response_with_headers(
                login, (*login.headers, ("x-request-id", request_id))
            )
            _assert_guard_rejects(
                lambda: _header(duplicate_request_id, "x-request-id"),
                "duplicate x-request-id",
            )

    asyncio.run(scenario())


def test_TC_SEC_10_csrf_is_bound_to_the_server_side_session_and_logout_revokes_it(
    auth_database: PostgresFixture,
) -> None:
    async def scenario() -> None:
        async with _environment(auth_database) as environment:
            await _create_user(environment.session_factory)
            first = await _login(environment)
            second = await _login(environment)
            first_session, _, _ = _session_cookie(first)
            second_session, _, _ = _session_cookie(second)
            first_csrf = _csrf_token(first)
            second_csrf = _csrf_token(second)

            common = {SESSION_COOKIE_NAME: first_session}
            missing = await _request(environment.app, "POST", LOGOUT_PATH, cookies=common)
            wrong = await _request(
                environment.app,
                "POST",
                LOGOUT_PATH,
                cookies=common,
                headers={CSRF_HEADER_NAME: "wrong-csrf-token"},
            )
            other_session = await _request(
                environment.app,
                "POST",
                LOGOUT_PATH,
                cookies=common,
                headers={CSRF_HEADER_NAME: second_csrf},
            )
            for rejected in (missing, wrong, other_session):
                assert rejected.status == 403
                assert _error_without_request_id(rejected)["code"] == "csrf_validation_failed"

            accepted = await _request(
                environment.app,
                "POST",
                LOGOUT_PATH,
                cookies=common,
                headers={CSRF_HEADER_NAME: first_csrf},
            )
            assert accepted.status == 200
            _, raw_cookie, parsed = _session_cookie(accepted)
            deleted = parsed[SESSION_COOKIE_NAME]
            assert deleted["max-age"] == "0"
            assert bool(deleted["secure"]) is True
            assert deleted["path"] == "/"
            assert deleted["domain"] == ""
            assert "domain=" not in raw_cookie.casefold()
            with pytest.raises(AuthenticationFailure):
                await environment.authentication.authenticate_session(session_token=first_session)
            assert (
                await environment.authentication.authenticate_session(session_token=second_session)
            ).user.username_norm == USERNAME

    asyncio.run(scenario())


def test_TC_SEC_10_password_change_rotates_current_and_revokes_other_sessions(
    auth_database: PostgresFixture,
) -> None:
    async def scenario() -> None:
        async with _environment(auth_database) as environment:
            await _create_user(environment.session_factory)
            first = await _login(environment)
            second = await _login(environment)
            first_session, _, _ = _session_cookie(first)
            second_session, _, _ = _session_cookie(second)

            changed = await _request(
                environment.app,
                "POST",
                PASSWORD_PATH,
                cookies={SESSION_COOKIE_NAME: first_session},
                headers={CSRF_HEADER_NAME: _csrf_token(first)},
                body={
                    "current_password": PASSWORD,
                    "new_password": NEW_PASSWORD,
                    "confirm_password": NEW_PASSWORD,
                },
            )
            assert changed.status == 200
            current_session, _, _ = _session_cookie(changed)
            assert current_session not in {first_session, second_session}
            assert changed.json()["must_change_password"] is False

            for revoked in (first_session, second_session):
                with pytest.raises(AuthenticationFailure):
                    await environment.authentication.authenticate_session(session_token=revoked)
            # "Keep the current session" means the same browser immediately has
            # a usable rotated credential; the fixed pre-change ID is dead.
            authenticated = await environment.authentication.authenticate_session(
                session_token=current_session,
                csrf_token=_csrf_token(changed),
                require_csrf=True,
            )
            assert authenticated.user.username_norm == USERNAME

            old_password = await _login(environment, password=PASSWORD)
            new_password = await _login(environment, password=NEW_PASSWORD)
            assert old_password.status == 401
            assert new_password.status == 200

    asyncio.run(scenario())


def test_TC_SEC_10_me_and_gate_reread_password_flag_from_database(
    auth_database: PostgresFixture,
) -> None:
    async def scenario() -> None:
        async with _environment(auth_database) as environment:
            await _create_user(environment.session_factory)
            reached = False

            @environment.app.get("/test/password-change-database-gate")
            @capability_route(Capability.VIEW_USAGE)
            async def protected_route() -> dict[str, bool]:
                nonlocal reached
                reached = True
                return {"reached": True}

            unauthenticated = await _request(
                environment.app,
                "GET",
                "/test/password-change-database-gate",
            )
            assert unauthenticated.status == 401
            assert unauthenticated.json()["error"]["code"] == "authentication_required"

            login = await _login(environment)
            session_token, _, _ = _session_cookie(login)
            cookies = {SESSION_COOKIE_NAME: session_token}

            first_me = await _request(environment.app, "GET", ME_PATH, cookies=cookies)
            blocked = await _request(
                environment.app,
                "GET",
                "/test/password-change-database-gate",
                cookies=cookies,
            )
            assert first_me.status == 200
            assert first_me.json()["must_change_password"] is True
            assert blocked.status == 403
            assert blocked.json()["error"]["code"] == "password_change_required"
            assert reached is False

            async with environment.session_factory() as session:
                audit_count = int(
                    (
                        await session.execute(sa.select(sa.func.count()).select_from(AUDIT_EVENTS))
                    ).scalar_one()
                )
                assert audit_count == 1
                await session.execute(
                    sa.update(USERS)
                    .where(USERS.c.username_norm == USERNAME)
                    .values(must_change_password=False)
                )
                await session.commit()

            second_me = await _request(environment.app, "GET", ME_PATH, cookies=cookies)
            allowed = await _request(
                environment.app,
                "GET",
                "/test/password-change-database-gate",
                cookies=cookies,
            )
            assert second_me.status == 200
            assert second_me.json()["must_change_password"] is False
            assert allowed.status == 200
            assert allowed.json() == {"reached": True}
            assert reached is True

    asyncio.run(scenario())


def test_TC_SEC_10_TC_SEC_15_first_password_change_needs_no_old_password_and_lifts_gate(
    auth_database: PostgresFixture,
) -> None:
    async def scenario() -> None:
        async with _environment(auth_database) as environment:
            await _create_user(environment.session_factory)

            @environment.app.get("/test/available-after-first-password-change")
            @capability_route(Capability.VIEW_USAGE)
            async def protected_route() -> dict[str, bool]:
                return {"reached": True}

            login = await _login(environment)
            old_session, _, _ = _session_cookie(login)
            old_cookies = {SESSION_COOKIE_NAME: old_session}
            csrf = _csrf_token(login)

            blocked = await _request(
                environment.app,
                "GET",
                "/test/available-after-first-password-change",
                cookies=old_cookies,
            )
            assert blocked.status == 403
            assert _error_without_request_id(blocked) == {
                "code": "password_change_required",
                "message": "需要先修改临时密码，其他功能暂不可用，请设置新密码后重试",
                "details": {},
            }
            assert PASSWORD not in blocked.text

            wrong_submitted_password = await _request(
                environment.app,
                "POST",
                PASSWORD_PATH,
                cookies=old_cookies,
                headers={CSRF_HEADER_NAME: csrf},
                body={
                    "current_password": "wrong-temporary-password",
                    "new_password": NEW_PASSWORD,
                    "confirm_password": NEW_PASSWORD,
                },
            )
            assert wrong_submitted_password.status == 401
            assert (
                _error_without_request_id(wrong_submitted_password)["code"]
                == "current_password_incorrect"
            )

            changed = await _request(
                environment.app,
                "POST",
                PASSWORD_PATH,
                cookies=old_cookies,
                headers={CSRF_HEADER_NAME: csrf},
                body={
                    "new_password": NEW_PASSWORD,
                    "confirm_password": NEW_PASSWORD,
                },
            )
            assert changed.status == 200
            new_session, _, _ = _session_cookie(changed)
            assert new_session != old_session
            assert changed.json()["must_change_password"] is False

            async with environment.session_factory() as session:
                stored_flag = (
                    await session.execute(
                        sa.select(USERS.c.must_change_password).where(
                            USERS.c.username_norm == USERNAME
                        )
                    )
                ).scalar_one()
                assert stored_flag is False

            with pytest.raises(AuthenticationFailure):
                await environment.authentication.authenticate_session(session_token=old_session)

            new_cookies = {SESSION_COOKIE_NAME: new_session}
            allowed = await _request(
                environment.app,
                "GET",
                "/test/available-after-first-password-change",
                cookies=new_cookies,
            )
            me = await _request(environment.app, "GET", ME_PATH, cookies=new_cookies)
            assert allowed.status == 200
            assert allowed.json() == {"reached": True}
            assert me.status == 200
            assert me.json()["must_change_password"] is False

    asyncio.run(scenario())


def test_TC_SEC_10_TC_PWD_10_first_password_change_exemption_still_rejects_weak_password(
    auth_database: PostgresFixture,
) -> None:
    async def scenario() -> None:
        async with _environment(auth_database) as environment:
            await _create_user(environment.session_factory)
            login = await _login(environment)
            session_token, _, _ = _session_cookie(login)
            rejected = await _request(
                environment.app,
                "POST",
                PASSWORD_PATH,
                cookies={SESSION_COOKIE_NAME: session_token},
                headers={CSRF_HEADER_NAME: _csrf_token(login)},
                body={
                    "new_password": "letmein",
                    "confirm_password": "letmein",
                },
            )
            assert rejected.status == 400
            error = _error_without_request_id(rejected)
            assert error["code"] == "invalid_new_password"
            assert error["details"]["password_policy"] == [
                {"code": "too_short", "current_length": 7},
                {"code": "common_password"},
            ]
            async with environment.session_factory() as session:
                must_change = (
                    await session.execute(
                        sa.select(USERS.c.must_change_password).where(
                            USERS.c.username_norm == USERNAME
                        )
                    )
                ).scalar_one()
                assert must_change is True

    asyncio.run(scenario())


def test_TC_SEC_10_TC_PWD_11_complete_policy_error_response_has_no_password_fragment(
    auth_database: PostgresFixture,
) -> None:
    async def scenario() -> None:
        rejected_password = "security-user-PWSSNTL-PASSWD-TC-PWD-11-A1B2C3D4E5F6"
        async with _environment(auth_database) as environment:
            await _create_user(environment.session_factory, must_change_password=False)
            login = await _login(environment)
            session_token, _, _ = _session_cookie(login)
            rejected = await _request(
                environment.app,
                "POST",
                PASSWORD_PATH,
                cookies={SESSION_COOKIE_NAME: session_token},
                headers={CSRF_HEADER_NAME: _csrf_token(login)},
                body={
                    "current_password": PASSWORD,
                    "new_password": rejected_password,
                    "confirm_password": rejected_password,
                },
            )
            assert rejected.status == 400
            assert _error_without_request_id(rejected)["details"]["password_policy"] == [
                {"code": "similar_to_username"},
                {"code": "similar_to_display_name"},
            ]
            serialized = rejected.text
            assert rejected_password not in serialized
            assert all(
                rejected_password[index : index + 12] not in serialized
                for index in range(len(rejected_password) - 11)
            )

    asyncio.run(scenario())


def test_TC_SEC_10_regular_password_change_still_requires_server_verified_old_password(
    auth_database: PostgresFixture,
) -> None:
    async def scenario() -> None:
        async with _environment(auth_database) as environment:
            await _create_user(environment.session_factory, must_change_password=False)
            login = await _login(environment)
            session_token, _, _ = _session_cookie(login)
            cookies = {SESSION_COOKIE_NAME: session_token}
            headers = {CSRF_HEADER_NAME: _csrf_token(login)}
            new_fields = {
                "new_password": NEW_PASSWORD,
                "confirm_password": NEW_PASSWORD,
            }

            missing = await _request(
                environment.app,
                "POST",
                PASSWORD_PATH,
                cookies=cookies,
                headers=headers,
                body=new_fields,
            )
            wrong = await _request(
                environment.app,
                "POST",
                PASSWORD_PATH,
                cookies=cookies,
                headers=headers,
                body={"current_password": "wrong-password", **new_fields},
            )
            client_claimed_exemption = await _request(
                environment.app,
                "POST",
                PASSWORD_PATH,
                cookies=cookies,
                headers=headers,
                body={"must_change_password": "true", **new_fields},
            )

            assert missing.status == wrong.status == 401
            assert _error_without_request_id(missing)["code"] == "current_password_incorrect"
            assert _error_without_request_id(wrong)["code"] == "current_password_incorrect"
            assert client_claimed_exemption.status == 400
            assert _error_without_request_id(client_claimed_exemption)["code"] == "invalid_request"

            accepted = await _request(
                environment.app,
                "POST",
                PASSWORD_PATH,
                cookies=cookies,
                headers=headers,
                body={"current_password": PASSWORD, **new_fields},
            )
            assert accepted.status == 200

    asyncio.run(scenario())


def test_TC_SEC_10_login_and_privilege_elevation_each_rotate_the_session_id(
    auth_database: PostgresFixture,
) -> None:
    async def scenario() -> None:
        async with _environment(auth_database) as environment:
            await _create_user(environment.session_factory)
            first = await _login(environment)
            first_session, _, _ = _session_cookie(first)

            relogin = await _login(environment, existing_session=first_session)
            relogin_session, _, _ = _session_cookie(relogin)
            assert relogin_session != first_session
            with pytest.raises(AuthenticationFailure):
                await environment.authentication.authenticate_session(session_token=first_session)

            elevated = await environment.authentication.rotate_for_privilege_elevation(
                session_token=relogin_session,
                csrf_token=_csrf_token(relogin),
            )
            assert elevated.token_pair.session_token != relogin_session
            with pytest.raises(AuthenticationFailure):
                await environment.authentication.authenticate_session(session_token=relogin_session)
            assert (
                await environment.authentication.authenticate_session(
                    session_token=elevated.token_pair.session_token,
                    csrf_token=elevated.token_pair.csrf_token,
                    require_csrf=True,
                )
            ).user.username_norm == USERNAME

    asyncio.run(scenario())


def test_TC_SEC_10_concurrent_password_changes_serialize_without_deadlock(
    auth_database: PostgresFixture,
) -> None:
    async def scenario() -> None:
        async with _environment(auth_database) as environment:
            await _create_user(environment.session_factory)
            first = await _login(environment)
            second = await _login(environment)
            first_session, _, _ = _session_cookie(first)
            second_session, _, _ = _session_cookie(second)

            outcomes = await asyncio.gather(
                environment.authentication.change_password(
                    session_token=first_session,
                    csrf_token=_csrf_token(first),
                    current_password=PASSWORD,
                    new_password="concurrent-new-password-one",
                ),
                environment.authentication.change_password(
                    session_token=second_session,
                    csrf_token=_csrf_token(second),
                    current_password=PASSWORD,
                    new_password="concurrent-new-password-two",
                ),
                return_exceptions=True,
            )
            failures = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
            successes = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
            assert len(successes) == 1
            assert len(failures) == 1
            assert isinstance(failures[0], AuthenticationFailure)
            assert failures[0].code == "authentication_required"

    asyncio.run(scenario())


def test_TC_SEC_10_idle_and_absolute_session_timeouts_use_database_time(
    auth_database: PostgresFixture,
) -> None:
    async def scenario() -> None:
        async with _environment(auth_database) as environment:
            await _create_user(environment.session_factory)
            idle_login = await _login(environment)
            absolute_login = await _login(environment)
            idle_session, _, _ = _session_cookie(idle_login)
            absolute_session, _, _ = _session_cookie(absolute_login)

            async with environment.session_factory() as session:
                database_now = sa.func.current_timestamp()
                await session.execute(
                    sa.update(SESSIONS)
                    .where(SESSIONS.c.session_hash == hash_token(idle_session))
                    .values(
                        created_at=database_now - timedelta(hours=1),
                        last_seen_at=database_now - SESSION_IDLE_TIMEOUT - timedelta(seconds=1),
                        expires_at=database_now + timedelta(hours=7),
                    )
                )
                await session.execute(
                    sa.update(SESSIONS)
                    .where(SESSIONS.c.session_hash == hash_token(absolute_session))
                    .values(
                        created_at=database_now - SESSION_ABSOLUTE_TIMEOUT - timedelta(minutes=1),
                        last_seen_at=database_now - timedelta(minutes=1),
                        expires_at=database_now - timedelta(seconds=1),
                    )
                )
                await session.commit()

            for expired in (idle_session, absolute_session):
                with pytest.raises(AuthenticationFailure) as raised:
                    await environment.authentication.authenticate_session(session_token=expired)
                assert raised.value.code == "authentication_required"

    asyncio.run(scenario())


def test_TC_SEC_16_unknown_and_wrong_password_are_indistinguishable_in_cost_and_response(
    auth_database: PostgresFixture,
) -> None:
    async def scenario() -> None:
        hasher = RecordingPasswordHasher()
        async with _environment(
            auth_database,
            hasher=hasher,
            minimum_login_failure_seconds=0.15,
        ) as environment:
            await _create_user(environment.session_factory)
            # Warm the lazily generated dummy hash outside the two measurements.
            assert (await _login(environment)).status == 200
            hasher.verified_hashes.clear()

            started = time.monotonic()
            wrong_password = await _login(environment, password="definitely-wrong")
            wrong_elapsed = time.monotonic() - started
            started = time.monotonic()
            unknown_user = await _login(
                environment,
                username="account-does-not-exist",
                password="definitely-wrong",
            )
            unknown_elapsed = time.monotonic() - started

            assert wrong_password.status == unknown_user.status == 401
            assert _error_without_request_id(wrong_password) == _error_without_request_id(
                unknown_user
            )
            assert len(hasher.verified_hashes) == 2
            assert parse_argon2id_parameters(hasher.verified_hashes[0]) == CHEAP
            assert parse_argon2id_parameters(hasher.verified_hashes[1]) == CHEAP
            assert wrong_elapsed >= 0.14
            assert unknown_elapsed >= 0.14
            assert abs(wrong_elapsed - unknown_elapsed) < 0.10
            async with environment.session_factory() as session:
                states = (
                    await session.execute(
                        sa.select(
                            LOGIN_PROTECTION.c.principal_hash,
                            LOGIN_PROTECTION.c.failed_attempts,
                        ).where(
                            LOGIN_PROTECTION.c.principal_hash.in_(
                                (
                                    login_principal_hash(USERNAME),
                                    login_principal_hash("account-does-not-exist"),
                                )
                            )
                        )
                    )
                ).all()
                assert sorted(int(row.failed_attempts) for row in states) == [1, 1]

                await session.execute(
                    sa.update(LOGIN_PROTECTION)
                    .where(
                        LOGIN_PROTECTION.c.principal_hash
                        == login_principal_hash("account-does-not-exist")
                    )
                    .values(updated_at=sa.func.clock_timestamp() - timedelta(hours=2))
                )
                await session.commit()

            # A later attempt incrementally prunes inactive rows beyond the
            # bounded retention window; random unknown names cannot live forever.
            assert (
                await _login(
                    environment,
                    username="cleanup-trigger-does-not-exist",
                    password="definitely-wrong",
                )
            ).status == 401
            async with environment.session_factory() as session:
                stale_count = (
                    await session.execute(
                        sa.select(sa.func.count())
                        .select_from(LOGIN_PROTECTION)
                        .where(
                            LOGIN_PROTECTION.c.principal_hash
                            == login_principal_hash("account-does-not-exist")
                        )
                    )
                ).scalar_one()
                assert stale_count == 0

    asyncio.run(scenario())


def test_TC_SEC_16_cancellation_does_not_release_a_running_argon_worker_slot(
    auth_database: PostgresFixture,
) -> None:
    async def scenario() -> None:
        hasher = BlockingPasswordHasher(expected_concurrency=2)
        async with _environment(
            auth_database,
            hasher=hasher,
            max_concurrent_password_operations=2,
        ) as environment:
            running = [
                asyncio.create_task(
                    _login(
                        environment,
                        username=f"cancelled-unknown-{index}",
                        password="wrong-password",
                    )
                )
                for index in range(2)
            ]
            entered = await asyncio.to_thread(
                hasher.expected_workers_entered.wait,
                5,
            )
            assert entered is True
            for task in running:
                task.cancel()

            waiting = [
                asyncio.create_task(
                    _login(
                        environment,
                        username=f"waiting-unknown-{index}",
                        password="wrong-password",
                    )
                )
                for index in range(2)
            ]
            await asyncio.sleep(0.05)
            # Cancelled awaiters still own both slots until their native Argon
            # calls exit, so no waiting request has entered the worker yet.
            assert hasher.calls == 2
            assert hasher.maximum_active == 2

            hasher.release.set()
            outcomes = await asyncio.gather(*running, *waiting, return_exceptions=True)
            assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes[:2])
            assert all(
                isinstance(outcome, AsgiResponse) and outcome.status == 401
                for outcome in outcomes[2:]
            )
            assert hasher.maximum_active == 2
            assert hasher.calls == 4

    asyncio.run(scenario())


def test_TC_SEC_16_invalid_json_unicode_is_sanitized_before_account_lookup(
    auth_database: PostgresFixture,
) -> None:
    async def scenario() -> None:
        hasher = RecordingPasswordHasher()
        async with _environment(auth_database, hasher=hasher) as environment:
            response = await _login(
                environment,
                username="invalid-\ud800-identifier",
                password="wrong-password",
            )
            assert response.status == 400
            assert _error_without_request_id(response)["code"] == "invalid_request"
            assert "surrogate" not in response.text.casefold()
            # This is a JSON/Unicode shape error that no valid account can have,
            # so it is rejected before identity lookup. Valid-but-nonexistent
            # account names take the dummy Argon path tested above.
            assert hasher.verified_hashes == []

    asyncio.run(scenario())


def test_TC_SEC_16_concurrent_stale_cleanup_cannot_deadlock_current_principals(
    auth_database: PostgresFixture,
) -> None:
    async def scenario() -> None:
        async with _environment(auth_database) as environment:
            identifiers = ("stale-unknown-a", "stale-unknown-b")
            for identifier in identifiers:
                assert (
                    await _login(
                        environment,
                        username=identifier,
                        password="wrong-password",
                    )
                ).status == 401

            async with environment.session_factory() as session:
                await session.execute(
                    sa.update(LOGIN_PROTECTION)
                    .where(
                        LOGIN_PROTECTION.c.principal_hash.in_(
                            tuple(login_principal_hash(value) for value in identifiers)
                        )
                    )
                    .values(updated_at=sa.func.clock_timestamp() - timedelta(hours=2))
                )
                await session.commit()

            async with asyncio.timeout(5):
                responses = await asyncio.gather(
                    *(
                        _login(
                            environment,
                            username=identifier,
                            password="wrong-password",
                        )
                        for identifier in identifiers
                    )
                )
            assert [response.status for response in responses] == [401, 401]

    asyncio.run(scenario())


def test_ENV_05_TC_SEC_16_ensure_and_lock_closes_stale_cleanup_window(
    auth_database: PostgresFixture,
) -> None:
    async def scenario() -> None:
        async with _environment(auth_database) as environment:
            principal_hash = login_principal_hash("stale-login-in-progress")
            cleanup_principal_hash = login_principal_hash("independent-cleanup-login")
            async with environment.session_factory() as seed_session:
                seeded_updated_at = (
                    await seed_session.execute(
                        sa.insert(LOGIN_PROTECTION)
                        .values(
                            principal_hash=principal_hash,
                            failed_attempts=2,
                            locked_until=None,
                            updated_at=sa.func.clock_timestamp() - timedelta(hours=2),
                        )
                        .returning(LOGIN_PROTECTION.c.updated_at)
                    )
                ).scalar_one()
                await seed_session.commit()

            async with (
                environment.session_factory() as login_session,
                environment.session_factory() as cleanup_session,
            ):
                pausing_session = PausingLoginProtectionSession(login_session)
                protection = LoginProtectionRepository(cast(AsyncSession, pausing_session))
                in_progress = asyncio.create_task(protection.ensure_and_lock(principal_hash))
                await asyncio.wait_for(pausing_session.statement_completed.wait(), timeout=2)

                try:
                    pruned = await LoginProtectionRepository(cleanup_session).prune_stale(
                        retention=timedelta(hours=1),
                        limit=10,
                        exclude_principal_hash=cleanup_principal_hash,
                    )
                    await cleanup_session.commit()
                finally:
                    pausing_session.resume.set()

                state = await asyncio.wait_for(in_progress, timeout=2)
                assert pruned == 0
                assert state.principal_hash == principal_hash
                assert state.failed_attempts == 2
                assert state.updated_at == seeded_updated_at
                await login_session.commit()

            async with environment.session_factory() as verification_session:
                remaining = (
                    await verification_session.execute(
                        sa.select(sa.func.count())
                        .select_from(LOGIN_PROTECTION)
                        .where(LOGIN_PROTECTION.c.principal_hash == principal_hash)
                    )
                ).scalar_one()
                assert remaining == 1

    asyncio.run(scenario())


async def _expire_login_lock(session_factory: AsyncSessionFactory, principal_hash: str) -> None:
    async with session_factory() as session:
        database_now = sa.func.current_timestamp()
        await session.execute(
            sa.update(LOGIN_PROTECTION)
            .where(LOGIN_PROTECTION.c.principal_hash == principal_hash)
            .values(
                locked_until=database_now - timedelta(seconds=1),
                updated_at=database_now - timedelta(seconds=2),
            )
        )
        await session.commit()


def test_TC_SEC_16_lock_is_persisted_across_app_rebuild_and_caps_at_fifteen_minutes(
    auth_database: PostgresFixture,
) -> None:
    async def scenario() -> None:
        async with _environment(auth_database) as environment:
            await _create_user(environment.session_factory)
            for _ in range(4):
                assert (await _login(environment, password="wrong-password")).status == 401
            fifth = await _login(environment, password="wrong-password")
            assert fifth.status == 429
            assert fifth.json()["error"]["details"] == {"retry_after_seconds": 60}

            # A new service and ASGI application have no shared process memory.
            rebuilt_service = AuthenticationService(
                session_factory=environment.session_factory,
                password_hasher=PasswordHasher(CHEAP),
                minimum_login_failure_seconds=0,
            )
            rebuilt = AuthEnvironment(
                app=_build_app(rebuilt_service),
                authentication=rebuilt_service,
                session_factory=environment.session_factory,
            )
            still_locked = await _login(rebuilt, password=PASSWORD)
            assert still_locked.status == 429
            assert still_locked.json()["error"]["details"]["retry_after_seconds"] >= 59

            principal_hash = login_principal_hash(USERNAME)
            observed: list[int] = []
            for expected in (120, 240, 480, 900, 900):
                await _expire_login_lock(environment.session_factory, principal_hash)
                response = await _login(rebuilt, password="wrong-password")
                assert response.status == 429
                observed.append(response.json()["error"]["details"]["retry_after_seconds"])
                assert observed[-1] == expected
            assert observed == [120, 240, 480, 900, 900]

            async with environment.session_factory() as session:
                row = (
                    await session.execute(
                        sa.select(
                            LOGIN_PROTECTION.c.failed_attempts,
                            LOGIN_PROTECTION.c.locked_until,
                        ).where(LOGIN_PROTECTION.c.principal_hash == principal_hash)
                    )
                ).one()
                assert row.failed_attempts == 10
                assert row.locked_until is not None

    asyncio.run(scenario())


def _files_containing(root: Path, needle: bytes) -> list[str]:
    hits: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            content = path.read_bytes()
        except OSError:
            continue
        if needle in content:
            hits.append(str(path.relative_to(root)))
    return hits


def test_TC_SEC_10_http_credentials_never_reach_database_files_with_positive_control(
    auth_database: PostgresFixture,
) -> None:
    password = "PWSSNTL-PASSWD-TCSEC10-A1B2C3D4E5F6"
    pair = SessionTokenPair(
        session_token="PWSSNTL-SESSION-TCSEC10-112233445566",
        csrf_token="PWSSNTL-SESSION-TCSEC10-AABBCCDDEEFF",
    )

    async def scenario() -> None:
        async with _environment(
            auth_database,
            token_factory=TokenSequence(pair),
        ) as environment:
            await _create_user(environment.session_factory, password=password)
            response = await _login(environment, password=password)
            assert response.status == 200
            assert _session_cookie(response)[0] == pair.session_token
            assert _csrf_token(response) == pair.csrf_token

    asyncio.run(scenario())

    autocommit = auth_database.engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    with autocommit as connection:
        connection.exec_driver_sql("CHECKPOINT")

    plaintexts = {
        "password": password,
        "session token": pair.session_token,
        "csrf token": pair.csrf_token,
    }
    for label, secret in plaintexts.items():
        assert _files_containing(auth_database.data_directory, secret.encode()) == [], label

    # Positive control: the exact same byte sweep must find the digest that is
    # supposed to be durable, otherwise a clean plaintext result proves nothing.
    assert _files_containing(
        auth_database.data_directory,
        pair.session_hash.encode("ascii"),
    )

    with auth_database.engine.connect() as connection:
        for table in ACCOUNT_TABLES:
            for label, secret in plaintexts.items():
                found = connection.execute(
                    sa.text(
                        f"SELECT count(*) FROM {table.schema}.{table.name} AS row_alias "
                        "WHERE strpos(row_alias::text, :needle) > 0"
                    ),
                    {"needle": secret},
                ).scalar_one()
                assert found == 0, f"{label} was found in {table.schema}.{table.name}"


def test_TC_SEC_20_http_actions_persist_complete_sanitized_audit_events(
    auth_database: PostgresFixture,
) -> None:
    sentinel = "PWSSNTL-PASSWD-TCSEC20-A1B2C3D4E5F6"

    async def scenario() -> None:
        async with _environment(auth_database) as environment:
            await _create_user(environment.session_factory, password=sentinel)

            login = await _login(environment, password=sentinel)
            assert login.status == 200
            session_token, _, _ = _session_cookie(login)
            csrf_token = _csrf_token(login)

            failed_login = await _login(environment, password=f"{sentinel}-WRONG")
            assert failed_login.status == 401

            csrf_denial = await _request(
                environment.app,
                "POST",
                PASSWORD_PATH,
                body={
                    "current_password": sentinel,
                    "new_password": f"{sentinel}-NEW",
                    "confirm_password": f"{sentinel}-NEW",
                },
                headers={CSRF_HEADER_NAME: f"{sentinel}-BAD-CSRF"},
                cookies={SESSION_COOKIE_NAME: session_token},
            )
            assert csrf_denial.status == 403

            password_change = await _request(
                environment.app,
                "POST",
                PASSWORD_PATH,
                body={
                    "current_password": sentinel,
                    "new_password": f"{sentinel}-NEW",
                    "confirm_password": f"{sentinel}-NEW",
                },
                headers={CSRF_HEADER_NAME: csrf_token},
                cookies={SESSION_COOKIE_NAME: session_token},
            )
            assert password_change.status == 200
            rotated_session, _, _ = _session_cookie(password_change)
            rotated_csrf = _csrf_token(password_change)

            reached = False

            @environment.app.post("/test/audit-permission-denial")
            @capability_route(Capability.MANAGE_USERS)
            async def denied_handler(body: dict[str, str]) -> dict[str, bool]:
                nonlocal reached
                reached = True
                return {"reached": True}

            permission_denial = await _request(
                environment.app,
                "POST",
                "/test/audit-permission-denial",
                body={"request_body": sentinel},
                headers={CSRF_HEADER_NAME: rotated_csrf},
                cookies={SESSION_COOKIE_NAME: rotated_session},
            )
            assert permission_denial.status == 403
            assert reached is False

            logout = await _request(
                environment.app,
                "POST",
                LOGOUT_PATH,
                headers={CSRF_HEADER_NAME: rotated_csrf},
                cookies={SESSION_COOKIE_NAME: rotated_session},
            )
            assert logout.status == 200

            responses = (
                login,
                failed_login,
                password_change,
                permission_denial,
                logout,
            )
            response_request_ids = {
                UUID(_header(response, "x-request-id")) for response in responses
            }

            async with environment.session_factory() as session:
                rows = (
                    (
                        await session.execute(
                            sa.select(AUDIT_EVENTS).order_by(AUDIT_EVENTS.c.created_at)
                        )
                    )
                    .mappings()
                    .all()
                )
                assert len(rows) == 5
                assert {row["request_id"] for row in rows} == response_request_ids
                assert UUID(_header(csrf_denial, "x-request-id")) not in response_request_ids
                assert all(row["created_at"].utcoffset() == timedelta(0) for row in rows)
                assert all(
                    row["actor_type"] in {member.value for member in AuditActorType} for row in rows
                )
                assert all(
                    row["object_type"] in {member.value for member in AuditObjectType}
                    for row in rows
                )

                observed = [(row["action"], row["result"]) for row in rows]
                assert observed == [
                    (AuditAction.LOGIN.value, AuditResult.SUCCEEDED.value),
                    (AuditAction.LOGIN.value, AuditResult.FAILED.value),
                    (AuditAction.PASSWORD_CHANGE.value, AuditResult.SUCCEEDED.value),
                    (AuditAction.AUTHORIZATION_CHECK.value, AuditResult.DENIED.value),
                    (AuditAction.LOGOUT.value, AuditResult.SUCCEEDED.value),
                ]
                assert rows[0]["actor_id"] == rows[0]["object_id"]
                assert rows[1]["actor_id"] is rows[1]["object_id"] is None
                assert rows[2]["details"] == {"changed_fields": ["password"]}
                assert rows[3]["details"] == {
                    "capability": Capability.MANAGE_USERS.value,
                    "reason_code": "permission_denied",
                    "route_template": "/test/audit-permission-denial",
                }

                scan = sa.text(
                    "SELECT count(*) FROM core.audit_events AS event "
                    "WHERE strpos(to_jsonb(event)::text, :needle) > 0"
                )
                clean_count = int((await session.execute(scan, {"needle": sentinel})).scalar_one())
                assert clean_count == 0

                # Positive control deliberately bypasses the application
                # builder inside this uncommitted transaction.  The same full-
                # row scan must detect it, then rollback removes it.
                await session.execute(
                    sa.insert(AUDIT_EVENTS).values(
                        id=new_uuid7(),
                        actor_type=AuditActorType.SYSTEM.value,
                        actor_id=None,
                        action=AuditAction.SYSTEM_CONFIG_CHANGE.value,
                        object_type=AuditObjectType.SYSTEM_CONFIG.value,
                        object_id=None,
                        result=AuditResult.SUCCEEDED.value,
                        request_id=new_uuid7(),
                        details={"source": sentinel},
                        created_at=sa.func.clock_timestamp(),
                    )
                )
                positive_count = int(
                    (await session.execute(scan, {"needle": sentinel})).scalar_one()
                )
                assert positive_count == 1
                await session.rollback()
                print(
                    "TC_SEC_20 audit sentinel scan: "
                    f"clean={clean_count}, positive_control={positive_count}"
                )

    asyncio.run(scenario())


def test_TC_SEC_20_granted_capability_check_writes_no_authorization_check_row(
    auth_database: PostgresFixture,
) -> None:
    async def scenario() -> None:
        async with _environment(auth_database) as environment:
            await _create_user(environment.session_factory, must_change_password=False)

            login = await _login(environment)
            assert login.status == 200
            session_token, _, _ = _session_cookie(login)

            reached = False

            @environment.app.get("/test/audit-permission-granted")
            @capability_route(Capability.VIEW_USAGE)
            async def granted_handler() -> dict[str, bool]:
                nonlocal reached
                reached = True
                return {"reached": True}

            granted = await _request(
                environment.app,
                "GET",
                "/test/audit-permission-granted",
                cookies={SESSION_COOKIE_NAME: session_token},
            )
            assert granted.status == 200
            assert reached is True

            async with environment.session_factory() as session:
                rows = (
                    (
                        await session.execute(
                            sa.select(AUDIT_EVENTS).order_by(AUDIT_EVENTS.c.created_at)
                        )
                    )
                    .mappings()
                    .all()
                )

            # A passing capability check is not one of the audited operations in
            # TDD 15.8, so the long-retained table keeps only the login row; the
            # granted request contributes neither a row nor a database write.
            assert [row["action"] for row in rows] == [AuditAction.LOGIN.value]
            assert AuditAction.AUTHORIZATION_CHECK.value not in {row["action"] for row in rows}

    asyncio.run(scenario())


def test_TC_SEC_20_system_config_classification_is_written_without_an_http_endpoint(
    auth_database: PostgresFixture,
) -> None:
    async def scenario() -> None:
        async with _environment(auth_database) as environment:
            request_id = new_uuid7()
            await AuditWriter(environment.session_factory).write(
                AuditEventDraft(
                    actor_type=AuditActorType.SYSTEM,
                    actor_id=None,
                    action=AuditAction.SYSTEM_CONFIG_CHANGE,
                    object_type=AuditObjectType.SYSTEM_CONFIG,
                    object_id=None,
                    result=AuditResult.SUCCEEDED,
                    request_id=request_id,
                    details=build_audit_details(
                        AuditAction.SYSTEM_CONFIG_CHANGE.value,
                        {
                            "changed_fields": ["retention_days"],
                            "source": "direct_writer_contract_test",
                            "unreviewed": "dropped",
                        },
                    ),
                )
            )
            async with environment.session_factory() as session:
                row = (
                    (
                        await session.execute(
                            sa.select(AUDIT_EVENTS).where(AUDIT_EVENTS.c.request_id == request_id)
                        )
                    )
                    .mappings()
                    .one()
                )
                assert row["details"] == {
                    "changed_fields": ["retention_days"],
                    "source": "direct_writer_contract_test",
                }

    asyncio.run(scenario())


class _FailingAuditWriter:
    def __init__(self, *, fail_add: bool = False, fail_write: bool = False) -> None:
        self._fail_add = fail_add
        self._fail_write = fail_write

    async def add(
        self,
        session: AsyncSession,
        event: AuditEventDraft,
    ) -> AuditEventRecord:
        if self._fail_add:
            raise RuntimeError("injected audit add failure")
        raise AssertionError("unexpected same-transaction audit call")

    async def write(self, event: AuditEventDraft) -> AuditEventRecord:
        if self._fail_write:
            raise RuntimeError("injected independent audit failure")
        raise AssertionError("unexpected independent audit call")


def test_TC_SEC_20_business_change_rolls_back_when_same_transaction_audit_fails(
    auth_database: PostgresFixture,
) -> None:
    async def scenario() -> None:
        async with _environment(auth_database) as environment:
            await _create_user(environment.session_factory)
            login = await _login(environment)
            session_token, _, _ = _session_cookie(login)
            csrf_token = _csrf_token(login)

            async with environment.session_factory() as session:
                before_hash = (
                    await session.execute(
                        sa.select(USERS.c.password_hash).where(USERS.c.username_norm == USERNAME)
                    )
                ).scalar_one()
                before_audits = int(
                    (
                        await session.execute(sa.select(sa.func.count()).select_from(AUDIT_EVENTS))
                    ).scalar_one()
                )

            failing = AuthenticationService(
                session_factory=environment.session_factory,
                password_hasher=PasswordHasher(CHEAP),
                minimum_login_failure_seconds=0,
                audit_writer=_FailingAuditWriter(fail_add=True),
            )
            with pytest.raises(RuntimeError, match="injected audit add failure"):
                await failing.change_password(
                    session_token=session_token,
                    csrf_token=csrf_token,
                    current_password=PASSWORD,
                    new_password=NEW_PASSWORD,
                    request_id=new_uuid7(),
                )

            async with environment.session_factory() as session:
                after_hash = (
                    await session.execute(
                        sa.select(USERS.c.password_hash).where(USERS.c.username_norm == USERNAME)
                    )
                ).scalar_one()
                after_audits = int(
                    (
                        await session.execute(sa.select(sa.func.count()).select_from(AUDIT_EVENTS))
                    ).scalar_one()
                )
                active_session = (
                    await session.execute(
                        sa.select(SESSIONS.c.revoked_at).where(
                            SESSIONS.c.session_hash == hash_token(session_token)
                        )
                    )
                ).scalar_one()
                assert after_hash == before_hash
                assert after_audits == before_audits == 1
                assert active_session is None

    asyncio.run(scenario())


def test_TC_SEC_20_independent_audit_failure_is_not_silently_swallowed(
    auth_database: PostgresFixture,
) -> None:
    async def scenario() -> None:
        async with _environment(auth_database) as environment:
            await _create_user(environment.session_factory)
            failing = AuthenticationService(
                session_factory=environment.session_factory,
                password_hasher=PasswordHasher(CHEAP),
                minimum_login_failure_seconds=0,
                audit_writer=_FailingAuditWriter(fail_write=True),
            )
            with pytest.raises(RuntimeError, match="injected independent audit failure"):
                await failing.login(
                    username=USERNAME,
                    password="wrong-password",
                    request_id=new_uuid7(),
                )

            async with environment.session_factory() as session:
                failures = int(
                    (
                        await session.execute(
                            sa.select(LOGIN_PROTECTION.c.failed_attempts).where(
                                LOGIN_PROTECTION.c.principal_hash == login_principal_hash(USERNAME)
                            )
                        )
                    ).scalar_one()
                )
                audit_count = int(
                    (
                        await session.execute(sa.select(sa.func.count()).select_from(AUDIT_EVENTS))
                    ).scalar_one()
                )
                assert failures == 1
                assert audit_count == 0

    asyncio.run(scenario())

"""TC-SEC-10: ``core.users`` and ``core.sessions`` on a real PostgreSQL cluster.

Everything here runs against the task-owned PostgreSQL 16 cluster from
``FX-DB-M0-05`` and against the real Alembic Migration, because the claims being
made are claims about a database: that a UNIQUE index really rejects a
same-looking account name, that a check constraint really refuses a plaintext
credential, and that after a commit and a checkpoint no plaintext is anywhere in
the cluster's files. None of those can be established against a Mock
(the integration-test policy).

The cryptographic side is ``tests/security/test_TC_SEC_10_password_and_tokens.py``.
The HTTP login flow, the session cookie, CSRF enforcement and login lockout are
M1-01C and are deliberately absent.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Protocol
from uuid import UUID

import pytest
import sqlalchemy as sa
from holm_persistence import (
    ACCOUNT_TABLES,
    USERS,
    AccountRepositoryError,
    DuplicateSessionError,
    DuplicateUsernameError,
    SessionRepository,
    UnknownUserError,
    UserRepository,
    UserRole,
    UserStatus,
    create_database_engine,
    create_session_factory,
)
from holm_security import (
    OWASP_MINIMUM_MEMORY_COST_KIB,
    OWASP_MINIMUM_TIME_COST,
    Argon2idParameters,
    PasswordHasher,
    generate_session_token_pair,
    generate_temporary_password,
    issue_temporary_password,
)
from migration_test_support import upgrade_head
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

CLUSTER_USER: Final[str] = "ubuntu"
CLUSTER_DATABASE: Final[str] = "postgres"
#: The suite hashes a handful of passwords; the TDD 15.2 baseline cost is
#: asserted in the security suite, and paying it here would only add seconds.
CHEAP: Final[Argon2idParameters] = Argon2idParameters(
    memory_cost_kib=OWASP_MINIMUM_MEMORY_COST_KIB,
    time_cost=OWASP_MINIMUM_TIME_COST,
    parallelism=1,
)
NOW: Final[datetime] = datetime(2026, 8, 28, 9, 0, 0, tzinfo=UTC)
SESSION_LIFETIME: Final[timedelta] = timedelta(hours=8)
NOT_A_HASH: Final[str] = "0" * 64


class PostgresFixture(Protocol):
    """The part of FX-DB-M0-05 these cases need."""

    @property
    def engine(self) -> Engine: ...

    @property
    def socket_directory(self) -> Path: ...

    @property
    def data_directory(self) -> Path: ...

    @property
    def port(self) -> int: ...


@pytest.fixture(scope="module")
def migrated_postgres(ephemeral_postgres: PostgresFixture) -> PostgresFixture:
    """Run the real Migration once for the whole module."""
    upgrade_head(ephemeral_postgres.engine)
    return ephemeral_postgres


@pytest.fixture
def accounts(migrated_postgres: PostgresFixture) -> Iterator[PostgresFixture]:
    """Give each case an empty pair of account tables."""
    with migrated_postgres.engine.begin() as connection:
        connection.exec_driver_sql(
            "TRUNCATE core.request_records, core.user_service_permissions, "
            "core.api_key_service_scopes, "
            "core.api_keys, core.idempotency_records, "
            "core.system_initialization, core.login_protection, core.sessions, "
            "core.bootstrap_tokens, core.users"
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


@asynccontextmanager
async def _unit_of_work(fixture: PostgresFixture) -> AsyncIterator[AsyncSession]:
    """One async engine, session and transaction, disposed on the way out."""
    engine = create_database_engine(_async_url(fixture), connect_timeout_seconds=5)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


def _hasher() -> PasswordHasher:
    return PasswordHasher(CHEAP)


# ------------------------------------------------------- schema and Migration


def test_TC_SEC_10_migrated_schema_matches_the_declared_account_metadata(
    migrated_postgres: PostgresFixture,
) -> None:
    """The Migration and the table metadata are two files; keep them in step.

    The Migration spells its DDL out so it stays a truthful record of what it
    produced. That is only safe if something checks the two against each other,
    which is this case: every column, type, nullability and constraint name the
    repositories rely on has to exist in the database the Migration built.
    """
    # The live connection's own dialect, so declared and reflected types are
    # rendered by exactly the same compiler.
    dialect = migrated_postgres.engine.dialect
    inspector = sa.inspect(migrated_postgres.engine)

    for table in ACCOUNT_TABLES:
        assert table.schema is not None
        reflected_columns = {
            column["name"]: (column["type"].compile(dialect), column["nullable"])
            for column in inspector.get_columns(table.name, schema=table.schema)
        }
        declared_columns = {
            column.name: (column.type.compile(dialect), bool(column.nullable))
            for column in table.columns
        }
        assert reflected_columns == declared_columns, table.name

        primary_key = inspector.get_pk_constraint(table.name, schema=table.schema)
        assert primary_key["name"] == table.primary_key.name
        assert tuple(primary_key["constrained_columns"]) == tuple(
            column.name for column in table.primary_key.columns
        )

        reflected_unique = {
            constraint["name"]
            for constraint in inspector.get_unique_constraints(table.name, schema=table.schema)
        }
        declared_unique = {
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, sa.UniqueConstraint)
        }
        assert reflected_unique == declared_unique, table.name

        reflected_checks = {
            constraint["name"]
            for constraint in inspector.get_check_constraints(table.name, schema=table.schema)
        }
        declared_checks = {
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, sa.CheckConstraint)
        }
        assert reflected_checks == declared_checks, table.name

        reflected_indexes = {
            index["name"]
            for index in inspector.get_indexes(table.name, schema=table.schema)
            if not index["unique"]
        }
        assert reflected_indexes == {index.name for index in table.indexes}, table.name

        reflected_foreign_keys = {
            constraint["name"]
            for constraint in inspector.get_foreign_keys(table.name, schema=table.schema)
        }
        declared_foreign_keys = {
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, sa.ForeignKeyConstraint)
        }
        assert reflected_foreign_keys == declared_foreign_keys, table.name


# ---------------------------------------------------------- name uniqueness


@pytest.mark.parametrize(
    "second_form",
    [
        "alice",
        "ALICE",
        "  Alice  ",
        "\uff41\uff4c\uff49\uff43\uff45",
        "ALICE\u00a0",
    ],
)
def test_TC_SEC_10_username_uniqueness_is_enforced_on_the_normalized_value(
    accounts: PostgresFixture, second_form: str
) -> None:
    """Every same-looking spelling collides with the account that already exists.

    Without normalization before the UNIQUE index, each of these would create a
    second account rendering identically to the first -- the account-collision
    bypass the normalization rule exists to close.
    """

    async def scenario() -> None:
        hasher = _hasher()
        async with _unit_of_work(accounts) as session:
            users = UserRepository(session)
            first = await users.create(
                username="Alice",
                display_name="Alice Zhang",
                role=UserRole.USER,
                password_hash=hasher.hash("first-password"),
                now=NOW,
            )
            await session.commit()
            assert first.username_norm == "alice"

            with pytest.raises(DuplicateUsernameError) as raised:
                await users.create(
                    username=second_form,
                    display_name="Someone Else",
                    role=UserRole.USER,
                    password_hash=hasher.hash("second-password"),
                    now=NOW,
                )
            assert raised.value.code == "duplicate_username"
            await session.rollback()

            # ...and the collided-with account is still reachable by any of the
            # spellings, because lookups normalize with the same rule.
            found = await users.find_by_username(second_form)
            assert found is not None
            assert found.id == first.id

    asyncio.run(scenario())


def test_TC_SEC_10_distinct_names_are_not_collapsed(accounts: PostgresFixture) -> None:
    async def scenario() -> None:
        hasher = _hasher()
        password_hash = hasher.hash("shared-password")
        async with _unit_of_work(accounts) as session:
            users = UserRepository(session)
            for name in ("alice", "alicia", "alice1", "bob"):
                await users.create(
                    username=name,
                    display_name=name.title(),
                    role=UserRole.USER,
                    password_hash=password_hash,
                    now=NOW,
                )
            await session.commit()
            total = await session.execute(sa.select(sa.func.count()).select_from(USERS))
            assert total.scalar_one() == 4

    asyncio.run(scenario())


# ------------------------------------------------- credentials stay hashed


def test_TC_SEC_10_password_column_refuses_anything_but_an_argon2id_hash(
    accounts: PostgresFixture,
) -> None:
    """The column, not just the caller, is what makes a plaintext write fail."""
    plaintext = "HOLM-M1-01B-PLAINTEXT-PASSWORD"
    statement = sa.text(
        """
        INSERT INTO core.users (
            id, username_norm, display_name, role, status,
            password_hash, must_change_password, created_at, updated_at, deleted_at
        ) VALUES (
            gen_random_uuid(), 'mallory', 'Mallory', 'user', 'active',
            :password_hash, true, :now, :now, NULL
        )
        """
    )
    with accounts.engine.connect() as connection:
        for rejected in (plaintext, "", "$2b$12$abcdefghijklmnopqrstuv", "argon2id$v=19$"):
            with pytest.raises(IntegrityError) as raised:
                connection.execute(statement, {"password_hash": rejected, "now": NOW})
            assert "ck_users_password_hash_argon2id" in str(raised.value.orig)
            connection.rollback()


def test_TC_SEC_10_repository_refuses_a_password_hash_it_cannot_recognize(
    accounts: PostgresFixture,
) -> None:
    async def scenario() -> None:
        async with _unit_of_work(accounts) as session:
            users = UserRepository(session)
            with pytest.raises(AccountRepositoryError) as raised:
                await users.create(
                    username="mallory",
                    display_name="Mallory",
                    role=UserRole.USER,
                    password_hash="HOLM-M1-01B-PLAINTEXT-PASSWORD",
                    now=NOW,
                )
            assert raised.value.code == "password_hash_not_argon2id"

    asyncio.run(scenario())


def test_TC_SEC_10_session_columns_refuse_a_plaintext_cookie_value(
    accounts: PostgresFixture,
) -> None:
    """TDD's ``core.sessions``: 不保存 Cookie 明文.

    A session token is URL-safe base64, so it cannot pass a "64 lowercase hex
    characters" constraint. Writing one is an error, not a silent leak.
    """
    pair = generate_session_token_pair()
    statement = sa.text(
        """
        INSERT INTO core.sessions (
            id, user_id, session_hash, csrf_hash,
            created_at, last_seen_at, expires_at, revoked_at
        ) VALUES (
            gen_random_uuid(), :user_id, :session_hash, :csrf_hash,
            :now, :now, :expires_at, NULL
        )
        """
    )
    with accounts.engine.begin() as connection:
        user_id = connection.execute(
            sa.text(
                """
                INSERT INTO core.users (
                    id, username_norm, display_name, role, status,
                    password_hash, must_change_password, created_at, updated_at, deleted_at
                ) VALUES (
                    gen_random_uuid(), 'cookie-owner', 'Cookie Owner', 'user', 'active',
                    :password_hash, true, :now, :now, NULL
                ) RETURNING id
                """
            ),
            {"password_hash": _hasher().hash("owner-password"), "now": NOW},
        ).scalar_one()

    parameters: dict[str, Any] = {
        "user_id": user_id,
        "now": NOW,
        "expires_at": NOW + SESSION_LIFETIME,
    }
    with accounts.engine.connect() as connection:
        for column, values in (
            (
                "ck_sessions_session_hash_sha256",
                {"session_hash": pair.session_token, "csrf_hash": pair.csrf_hash},
            ),
            (
                "ck_sessions_csrf_hash_sha256",
                {"session_hash": pair.session_hash, "csrf_hash": pair.csrf_token},
            ),
            (
                "ck_sessions_session_hash_sha256",
                {"session_hash": "A" * 64, "csrf_hash": pair.csrf_hash},
            ),
        ):
            with pytest.raises(IntegrityError) as raised:
                connection.execute(statement, {**parameters, **values})
            assert column in str(raised.value.orig)
            connection.rollback()


def test_TC_SEC_10_session_repository_refuses_a_plaintext_token(
    accounts: PostgresFixture,
) -> None:
    """The repository has no parameter that accepts plaintext, and says so."""

    async def scenario() -> None:
        pair = generate_session_token_pair()
        async with _unit_of_work(accounts) as session:
            users = UserRepository(session)
            owner = await users.create(
                username="carol",
                display_name="Carol",
                role=UserRole.USER,
                password_hash=_hasher().hash("carol-password"),
                now=NOW,
            )
            sessions = SessionRepository(session)
            for session_hash, csrf_hash, code in (
                (pair.session_token, pair.csrf_hash, "session_hash_not_a_digest"),
                (pair.session_hash, pair.csrf_token, "csrf_hash_not_a_digest"),
            ):
                with pytest.raises(AccountRepositoryError) as raised:
                    await sessions.create(
                        user_id=owner.id,
                        session_hash=session_hash,
                        csrf_hash=csrf_hash,
                        expires_at=NOW + SESSION_LIFETIME,
                        now=NOW,
                    )
                assert raised.value.code == code
            await session.rollback()

    asyncio.run(scenario())


# ----------------------------------------------------------------- lifecycle


def test_TC_SEC_10_a_new_account_must_change_its_temporary_password(
    accounts: PostgresFixture,
) -> None:
    """TDD 15.2: temporary password, forced change, only the hash is stored."""

    async def scenario() -> None:
        hasher = _hasher()
        issued = issue_temporary_password(hasher)
        async with _unit_of_work(accounts) as session:
            users = UserRepository(session)
            created = await users.create(
                username="Dave",
                display_name="Dave",
                role=UserRole.ADMIN,
                password_hash=issued.password_hash,
                now=NOW,
            )
            await session.commit()

            assert created.must_change_password is True
            assert created.role is UserRole.ADMIN
            assert created.status is UserStatus.ACTIVE
            assert created.can_authenticate is True
            assert created.deleted_at is None
            assert created.created_at == NOW
            assert created.created_at.utcoffset() == timedelta(0)
            assert hasher.verify(created.password_hash, issued.password) is True
            assert issued.password not in created.password_hash

            chosen = "a-password-the-operator-picked"
            later = NOW + timedelta(minutes=5)
            updated = await users.set_password(
                created.id, password_hash=hasher.hash(chosen), now=later
            )
            await session.commit()

            assert updated.must_change_password is False
            assert updated.updated_at == later
            assert hasher.verify(updated.password_hash, chosen) is True
            # The temporary password stops working the moment it is replaced.
            assert hasher.verify(updated.password_hash, issued.password) is False

    asyncio.run(scenario())


def test_TC_SEC_10_account_status_transitions_keep_deleted_at_consistent(
    accounts: PostgresFixture,
) -> None:
    async def scenario() -> None:
        async with _unit_of_work(accounts) as session:
            users = UserRepository(session)
            created = await users.create(
                username="erin",
                display_name="Erin",
                role=UserRole.USER,
                password_hash=_hasher().hash("erin-password"),
                now=NOW,
            )

            disabled = await users.set_status(created.id, UserStatus.DISABLED, now=NOW)
            assert disabled.status is UserStatus.DISABLED
            assert disabled.deleted_at is None
            assert disabled.can_authenticate is False

            with pytest.raises(AccountRepositoryError) as raised:
                await users.set_status(created.id, UserStatus.DELETED, now=NOW)
            assert raised.value.code == "use_soft_delete_for_deleted_status"

            deleted_at = NOW + timedelta(days=1)
            deleted = await users.soft_delete(created.id, now=deleted_at)
            assert deleted.status is UserStatus.DELETED
            assert deleted.deleted_at == deleted_at
            assert deleted.can_authenticate is False

            # A logically deleted account cannot be revived through set_status.
            with pytest.raises(UnknownUserError):
                await users.set_status(created.id, UserStatus.ACTIVE, now=deleted_at)
            await session.rollback()

    asyncio.run(scenario())


def test_TC_SEC_10_session_lifecycle_uses_digests_and_aware_utc_times(
    accounts: PostgresFixture,
) -> None:
    async def scenario() -> None:
        pair = generate_session_token_pair()
        expires_at = NOW + SESSION_LIFETIME
        async with _unit_of_work(accounts) as session:
            users = UserRepository(session)
            owner = await users.create(
                username="frank",
                display_name="Frank",
                role=UserRole.USER,
                password_hash=_hasher().hash("frank-password"),
                now=NOW,
            )
            sessions = SessionRepository(session)
            created = await sessions.create(
                user_id=owner.id,
                session_hash=pair.session_hash,
                csrf_hash=pair.csrf_hash,
                expires_at=expires_at,
                now=NOW,
            )
            await session.commit()

            assert created.session_hash == pair.session_hash
            assert created.csrf_hash == pair.csrf_hash
            assert created.expires_at == expires_at
            for moment in (created.created_at, created.last_seen_at, created.expires_at):
                assert moment.utcoffset() == timedelta(0)
            assert created.is_active_at(NOW) is True
            assert created.is_active_at(expires_at) is False

            # A row is found by digest; there is no lookup that takes plaintext.
            found = await sessions.find_by_session_hash(pair.session_hash)
            assert found == created
            assert await sessions.find_by_session_hash(NOT_A_HASH) is None

            # The same digest cannot be inserted twice.
            with pytest.raises(DuplicateSessionError):
                await sessions.create(
                    user_id=owner.id,
                    session_hash=pair.session_hash,
                    csrf_hash=pair.csrf_hash,
                    expires_at=expires_at,
                    now=NOW,
                )
            await session.rollback()

            seen_at = NOW + timedelta(minutes=10)
            touched = await sessions.touch(created.id, now=seen_at)
            assert touched is not None
            assert touched.last_seen_at == seen_at

            revoked_at = NOW + timedelta(minutes=20)
            revoked = await sessions.revoke(created.id, now=revoked_at)
            assert revoked is not None
            assert revoked.revoked_at == revoked_at
            assert revoked.is_active_at(revoked_at) is False
            # Revoking twice is a no-op rather than a second revocation time.
            assert await sessions.revoke(created.id, now=NOW + timedelta(minutes=30)) is None
            assert await sessions.touch(created.id, now=NOW + timedelta(minutes=30)) is None
            await session.commit()

    asyncio.run(scenario())


def test_TC_SEC_10_password_change_can_revoke_every_live_session(
    accounts: PostgresFixture,
) -> None:
    """TDD 15.2 requires this on password change; M1-01C decides when to call it."""

    async def scenario() -> None:
        async with _unit_of_work(accounts) as session:
            users = UserRepository(session)
            owner = await users.create(
                username="grace",
                display_name="Grace",
                role=UserRole.USER,
                password_hash=_hasher().hash("grace-password"),
                now=NOW,
            )
            other = await users.create(
                username="heidi",
                display_name="Heidi",
                role=UserRole.USER,
                password_hash=_hasher().hash("heidi-password"),
                now=NOW,
            )
            sessions = SessionRepository(session)
            for holder in (owner, owner, owner, other):
                pair = generate_session_token_pair()
                await sessions.create(
                    user_id=holder.id,
                    session_hash=pair.session_hash,
                    csrf_hash=pair.csrf_hash,
                    expires_at=NOW + SESSION_LIFETIME,
                    now=NOW,
                )

            revoked = await sessions.revoke_all_for_user(owner.id, now=NOW + timedelta(minutes=1))
            assert revoked == 3
            # Only that account's sessions; the other user keeps hers.
            assert await sessions.revoke_all_for_user(owner.id, now=NOW) == 0
            assert await sessions.revoke_all_for_user(other.id, now=NOW) == 1
            await session.rollback()

    asyncio.run(scenario())


def test_TC_SEC_10_expired_sessions_are_removable(accounts: PostgresFixture) -> None:
    async def scenario() -> None:
        async with _unit_of_work(accounts) as session:
            users = UserRepository(session)
            owner = await users.create(
                username="ivan",
                display_name="Ivan",
                role=UserRole.USER,
                password_hash=_hasher().hash("ivan-password"),
                now=NOW,
            )
            sessions = SessionRepository(session)
            short_pair = generate_session_token_pair()
            long_pair = generate_session_token_pair()
            await sessions.create(
                user_id=owner.id,
                session_hash=short_pair.session_hash,
                csrf_hash=short_pair.csrf_hash,
                expires_at=NOW + timedelta(minutes=30),
                now=NOW,
            )
            await sessions.create(
                user_id=owner.id,
                session_hash=long_pair.session_hash,
                csrf_hash=long_pair.csrf_hash,
                expires_at=NOW + SESSION_LIFETIME,
                now=NOW,
            )

            assert await sessions.delete_expired(now=NOW + timedelta(hours=1)) == 1
            assert await sessions.find_by_session_hash(short_pair.session_hash) is None
            assert await sessions.find_by_session_hash(long_pair.session_hash) is not None
            await session.rollback()

    asyncio.run(scenario())


def test_TC_SEC_10_a_session_cannot_reference_an_unknown_account(
    accounts: PostgresFixture,
) -> None:
    async def scenario() -> None:
        pair = generate_session_token_pair()
        async with _unit_of_work(accounts) as session:
            sessions = SessionRepository(session)
            with pytest.raises(IntegrityError):
                await sessions.create(
                    user_id=UUID(int=0),
                    session_hash=pair.session_hash,
                    csrf_hash=pair.csrf_hash,
                    expires_at=NOW + SESSION_LIFETIME,
                    now=NOW,
                )
            await session.rollback()

    asyncio.run(scenario())


# --------------------------------------------------------- zero-plaintext sweep


def _files_containing(root: Path, needle: bytes) -> list[str]:
    """Search every byte the cluster has on disk, heap and WAL alike."""
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


def test_TC_SEC_10_no_plaintext_credential_reaches_the_database(
    accounts: PostgresFixture,
) -> None:
    """Sentinel sweep: after a real commit, no plaintext is anywhere on disk.

    The order matters. The on-disk sweep runs *first*, before any sentinel value
    has been sent to the server as a query parameter -- otherwise the sweep
    could be blamed for putting there what it went looking for. Only then does
    the SQL sweep cast every row of every account table to text and confirm the
    same thing from inside the database.

    The positive control at the end is what keeps this from being a test that
    passes because it cannot find anything: the stored session digest, which is
    supposed to be on disk, must be found by the very same sweep.
    """
    hasher = _hasher()
    chosen_password = "HOLM-M1-01B-CHOSEN-PASSWORD-4d1e77"
    temporary_password = generate_temporary_password()
    pair = generate_session_token_pair()

    async def write_one_account() -> str:
        async with _unit_of_work(accounts) as session:
            users = UserRepository(session)
            created = await users.create(
                username="judy",
                display_name="Judy",
                role=UserRole.USER,
                password_hash=hasher.hash(temporary_password),
                now=NOW,
            )
            await users.set_password(
                created.id,
                password_hash=hasher.hash(chosen_password),
                now=NOW + timedelta(minutes=1),
            )
            sessions = SessionRepository(session)
            await sessions.create(
                user_id=created.id,
                session_hash=pair.session_hash,
                csrf_hash=pair.csrf_hash,
                expires_at=NOW + SESSION_LIFETIME,
                now=NOW,
            )
            await session.commit()
            return pair.session_hash

    stored_digest = asyncio.run(write_one_account())

    # Force the committed rows out of shared buffers and into the data files.
    autocommit = accounts.engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    with autocommit as connection:
        connection.exec_driver_sql("CHECKPOINT")

    secrets_that_must_not_exist = {
        "chosen password": chosen_password,
        "temporary password": temporary_password,
        "session token": pair.session_token,
        "csrf token": pair.csrf_token,
    }
    for label, secret in secrets_that_must_not_exist.items():
        hits = _files_containing(accounts.data_directory, secret.encode("utf-8"))
        assert hits == [], f"{label} was found in the cluster's files: {hits}"

    # The sweep can find things: the digest that is supposed to be stored is.
    assert _files_containing(accounts.data_directory, stored_digest.encode("ascii")) != []

    # The same claim from inside the database. Casting the whole row to text
    # covers every column without naming them, so a column added later is
    # swept too.
    with accounts.engine.connect() as connection:
        for table in ACCOUNT_TABLES:
            for label, secret in secrets_that_must_not_exist.items():
                found = connection.execute(
                    sa.text(
                        f"SELECT count(*) FROM {table.schema}.{table.name} AS row_alias "
                        "WHERE strpos(row_alias::text, :needle) > 0"
                    ),
                    {"needle": secret},
                ).scalar_one()
                assert found == 0, f"{label} was found in {table.schema}.{table.name}"

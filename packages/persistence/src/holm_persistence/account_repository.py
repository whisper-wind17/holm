"""Repositories for accounts, browser sessions, and login protection.

The single design rule here: **no plaintext credential can reach this layer**.
``create_user`` takes an already-encoded Argon2id hash and ``create_session``
takes already-computed SHA-256 digests. There is no parameter anywhere below
that accepts a password or a session cookie, so the persistence layer cannot
store, log or accidentally return one, and the reviewer does not have to trace
call sites to be sure of it. Hashing lives in ``packages/security``; the login
flow that connects the two belongs to the Control API.

Account administration keeps caller-supplied UTC instants for deterministic
transactions. Authentication expiry and lockout decisions use PostgreSQL
``clock_timestamp()`` instead, so application-host wall-clock skew cannot
shorten or extend a security deadline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

import sqlalchemy as sa
from holm_common.ids import new_uuid7
from holm_common.time import as_utc
from holm_common.usernames import normalize_display_name, normalize_username
from holm_domain import AuthorizationSubject, DomainEventType, UserRole, UserStatus
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from holm_persistence.accounts import (
    LOGIN_PROTECTION,
    SESSIONS,
    USERS,
)
from holm_persistence.domain_event_repository import append_route_invalidation_event
from holm_persistence.repository_scope import RepositoryPage, owned_or_admin_clause

_ARGON2ID_PREFIX: Final[str] = "$argon2id$"
_TOKEN_HASH: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_UNIQUE_VIOLATION: Final[str] = "23505"


class AccountRepositoryError(RuntimeError):
    """A rejected account write carrying a stable machine-readable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"account write rejected: {code}")


class DuplicateUsernameError(AccountRepositoryError):
    """The normalized account name is already taken by another row."""

    def __init__(self) -> None:
        super().__init__("duplicate_username")


class DuplicateSessionError(AccountRepositoryError):
    """The session digest already exists; the caller must draw a new token."""

    def __init__(self) -> None:
        super().__init__("duplicate_session")


class UnknownUserError(AccountRepositoryError):
    """The addressed user row does not exist."""

    def __init__(self) -> None:
        super().__init__("unknown_user")


@dataclass(frozen=True, slots=True)
class UserRecord:
    """One ``core.users`` row. ``password_hash`` is kept out of ``repr``."""

    id: UUID
    username_norm: str
    display_name: str
    role: UserRole
    status: UserStatus
    must_change_password: bool
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
    password_hash: str = field(repr=False)

    @property
    def can_authenticate(self) -> bool:
        """Only ``Active`` accounts may log in."""
        return self.status is UserStatus.ACTIVE and self.deleted_at is None


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """One ``core.sessions`` row.

    It has no field for the session or CSRF plaintext, by construction: the
    values the browser holds are not knowable from a row, only comparable
    against it.
    """

    id: UUID
    user_id: UUID
    session_hash: str
    csrf_hash: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    revoked_at: datetime | None

    def is_active_at(self, moment: datetime) -> bool:
        """Whether the session is neither revoked nor expired at ``moment``."""
        return self.revoked_at is None and as_utc(moment) < self.expires_at


@dataclass(frozen=True, slots=True)
class LoginProtectionRecord:
    """Persisted failure state plus the database instant used to evaluate it."""

    principal_hash: str
    failed_attempts: int
    locked_until: datetime | None
    updated_at: datetime
    database_now: datetime

    @property
    def is_locked(self) -> bool:
        return self.locked_until is not None and self.database_now < self.locked_until


def _require_argon2id_hash(password_hash: str) -> str:
    if not isinstance(password_hash, str) or not password_hash.startswith(_ARGON2ID_PREFIX):
        raise AccountRepositoryError("password_hash_not_argon2id")
    return password_hash


def _require_token_hash(value: str, code: str) -> str:
    """Reject anything that is not a SHA-256 hex digest before it reaches SQL.

    The column has the same check constraint. This one exists so the failure is
    a typed error at the boundary instead of a database round trip that carries
    the rejected value into the server's error message.
    """
    if not isinstance(value, str) or _TOKEN_HASH.match(value) is None:
        raise AccountRepositoryError(code)
    return value


def _is_unique_violation(error: IntegrityError, constraint: str) -> bool:
    original = error.orig
    if getattr(original, "sqlstate", None) != _UNIQUE_VIOLATION:
        return False
    diagnostic = getattr(original, "diag", None)
    return getattr(diagnostic, "constraint_name", None) == constraint


def _user_record(mapping: sa.RowMapping) -> UserRecord:
    deleted_at = mapping["deleted_at"]
    return UserRecord(
        id=mapping["id"],
        username_norm=mapping["username_norm"],
        display_name=mapping["display_name"],
        role=UserRole(mapping["role"]),
        status=UserStatus(mapping["status"]),
        must_change_password=bool(mapping["must_change_password"]),
        created_at=as_utc(mapping["created_at"]),
        updated_at=as_utc(mapping["updated_at"]),
        deleted_at=None if deleted_at is None else as_utc(deleted_at),
        password_hash=mapping["password_hash"],
    )


def _session_record(mapping: sa.RowMapping) -> SessionRecord:
    revoked_at = mapping["revoked_at"]
    return SessionRecord(
        id=mapping["id"],
        user_id=mapping["user_id"],
        session_hash=mapping["session_hash"],
        csrf_hash=mapping["csrf_hash"],
        created_at=as_utc(mapping["created_at"]),
        last_seen_at=as_utc(mapping["last_seen_at"]),
        expires_at=as_utc(mapping["expires_at"]),
        revoked_at=None if revoked_at is None else as_utc(revoked_at),
    )


def _login_protection_record(mapping: sa.RowMapping) -> LoginProtectionRecord:
    locked_until = mapping["locked_until"]
    return LoginProtectionRecord(
        principal_hash=mapping["principal_hash"],
        failed_attempts=int(mapping["failed_attempts"]),
        locked_until=None if locked_until is None else as_utc(locked_until),
        updated_at=as_utc(mapping["updated_at"]),
        database_now=as_utc(mapping["database_now"]),
    )


async def database_utc_now(session: AsyncSession) -> datetime:
    """Read PostgreSQL's live clock as an aware UTC instant."""
    value = (await session.execute(sa.select(sa.func.clock_timestamp()))).scalar_one()
    if not isinstance(value, datetime):
        raise RuntimeError("database clock_timestamp did not return a datetime")
    return as_utc(value)


class UserRepository:
    """Read and write ``core.users`` inside the caller's Unit of Work."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        username: str,
        display_name: str,
        role: UserRole,
        password_hash: str,
        now: datetime,
        must_change_password: bool = True,
    ) -> UserRecord:
        """Insert one account.

        ``must_change_password`` defaults to ``True`` because the only way an
        account is created in the current release is with a generated temporary password.
        A caller that wants otherwise has to say so.
        """
        moment = as_utc(now)
        statement = (
            sa.insert(USERS)
            .values(
                id=new_uuid7(),
                username_norm=normalize_username(username),
                display_name=normalize_display_name(display_name),
                role=UserRole(role).value,
                status=UserStatus.ACTIVE.value,
                password_hash=_require_argon2id_hash(password_hash),
                must_change_password=bool(must_change_password),
                created_at=moment,
                updated_at=moment,
                deleted_at=None,
            )
            .returning(*USERS.c)
        )
        try:
            result = await self._session.execute(statement)
        except IntegrityError as error:
            if _is_unique_violation(error, "uq_users_username_norm"):
                raise DuplicateUsernameError() from None
            raise
        record = _user_record(result.mappings().one())
        await append_route_invalidation_event(
            self._session,
            event=DomainEventType.USER_ACCESS_CHANGED,
            aggregate_id=record.id,
        )
        return record

    async def get_for_authentication(self, user_id: UUID) -> UserRecord | None:
        """Internal credential lookup, including logically deleted rows.

        This method is deliberately named for its non-resource use.  Product
        handlers must use :meth:`get_visible`, whose subject is mandatory.
        """
        result = await self._session.execute(sa.select(*USERS.c).where(USERS.c.id == user_id))
        mapping = result.mappings().one_or_none()
        return None if mapping is None else _user_record(mapping)

    async def get_visible(
        self,
        subject: AuthorizationSubject,
        user_id: UUID,
    ) -> UserRecord | None:
        """Return a visible user or ``None`` for both absent and unauthorized IDs."""
        statement = sa.select(*USERS.c).where(
            USERS.c.id == user_id,
            USERS.c.deleted_at.is_(None),
            owned_or_admin_clause(subject, USERS.c.id),
        )
        mapping = (await self._session.execute(statement)).mappings().one_or_none()
        return None if mapping is None else _user_record(mapping)

    async def list_visible(
        self,
        subject: AuthorizationSubject,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> RepositoryPage[UserRecord]:
        """Page only the subject-visible set and count that same filtered set."""
        if page < 1:
            raise ValueError("page must be positive")
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        predicates = (
            USERS.c.deleted_at.is_(None),
            owned_or_admin_clause(subject, USERS.c.id),
        )
        total_statement = sa.select(sa.func.count()).select_from(USERS).where(*predicates)
        total = int((await self._session.execute(total_statement)).scalar_one())
        page_statement = (
            sa.select(*USERS.c)
            .where(*predicates)
            .order_by(USERS.c.id)
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
        mappings = (await self._session.execute(page_statement)).mappings().all()
        return RepositoryPage(
            items=tuple(_user_record(mapping) for mapping in mappings),
            total=total,
            page=page,
            page_size=page_size,
        )

    async def get_for_update(self, user_id: UUID) -> UserRecord | None:
        """Lock one account row for a credential-changing transaction."""
        result = await self._session.execute(
            sa.select(*USERS.c).where(USERS.c.id == user_id).with_for_update()
        )
        mapping = result.mappings().one_or_none()
        return None if mapping is None else _user_record(mapping)

    async def find_by_username(self, username: str) -> UserRecord | None:
        """Look up by the normalized name, which is what the UNIQUE index covers.

        The caller's raw input is normalized here rather than by the caller, so
        every lookup necessarily uses the same rule the constraint was built on.
        """
        result = await self._session.execute(
            sa.select(*USERS.c).where(USERS.c.username_norm == normalize_username(username))
        )
        mapping = result.mappings().one_or_none()
        return None if mapping is None else _user_record(mapping)

    async def set_password(
        self,
        user_id: UUID,
        *,
        password_hash: str,
        now: datetime,
        must_change_password: bool = False,
    ) -> UserRecord:
        """Replace the stored hash; revoking sessions is the caller's job."""
        statement = (
            sa.update(USERS)
            .where(USERS.c.id == user_id)
            .values(
                password_hash=_require_argon2id_hash(password_hash),
                must_change_password=bool(must_change_password),
                updated_at=as_utc(now),
            )
            .returning(*USERS.c)
        )
        mapping = (await self._session.execute(statement)).mappings().one_or_none()
        if mapping is None:
            raise UnknownUserError()
        return _user_record(mapping)

    async def set_status(self, user_id: UUID, status: UserStatus, *, now: datetime) -> UserRecord:
        """Move the account between ``Active`` and ``Disabled``.

        ``Deleted`` is not reachable from here: it is a logical deletion with
        its own timestamp, so it goes through :meth:`soft_delete`.
        """
        if status is UserStatus.DELETED:
            raise AccountRepositoryError("use_soft_delete_for_deleted_status")
        moment = as_utc(now)
        statement = (
            sa.update(USERS)
            .where(USERS.c.id == user_id, USERS.c.deleted_at.is_(None))
            .values(status=status.value, updated_at=moment)
            .returning(*USERS.c)
        )
        mapping = (await self._session.execute(statement)).mappings().one_or_none()
        if mapping is None:
            raise UnknownUserError()
        record = _user_record(mapping)
        await append_route_invalidation_event(
            self._session,
            event=DomainEventType.USER_ACCESS_CHANGED,
            aggregate_id=record.id,
        )
        return record

    async def soft_delete(self, user_id: UUID, *, now: datetime) -> UserRecord:
        """Logically delete the account; the row and its history stay."""
        moment = as_utc(now)
        statement = (
            sa.update(USERS)
            .where(USERS.c.id == user_id, USERS.c.deleted_at.is_(None))
            .values(status=UserStatus.DELETED.value, deleted_at=moment, updated_at=moment)
            .returning(*USERS.c)
        )
        mapping = (await self._session.execute(statement)).mappings().one_or_none()
        if mapping is None:
            raise UnknownUserError()
        record = _user_record(mapping)
        await append_route_invalidation_event(
            self._session,
            event=DomainEventType.USER_ACCESS_CHANGED,
            aggregate_id=record.id,
        )
        return record


class SessionRepository:
    """Read and write ``core.sessions`` inside the caller's Unit of Work."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        user_id: UUID,
        session_hash: str,
        csrf_hash: str,
        expires_at: datetime,
        now: datetime,
    ) -> SessionRecord:
        """Insert one browser session from digests the caller already computed."""
        moment = as_utc(now)
        statement = (
            sa.insert(SESSIONS)
            .values(
                id=new_uuid7(),
                user_id=user_id,
                session_hash=_require_token_hash(session_hash, "session_hash_not_a_digest"),
                csrf_hash=_require_token_hash(csrf_hash, "csrf_hash_not_a_digest"),
                created_at=moment,
                last_seen_at=moment,
                expires_at=as_utc(expires_at),
                revoked_at=None,
            )
            .returning(*SESSIONS.c)
        )
        try:
            result = await self._session.execute(statement)
        except IntegrityError as error:
            if _is_unique_violation(error, "uq_sessions_session_hash"):
                raise DuplicateSessionError() from None
            raise
        return _session_record(result.mappings().one())

    async def find_by_session_hash(self, session_hash: str) -> SessionRecord | None:
        """Look one session up by digest. There is no lookup by plaintext."""
        result = await self._session.execute(
            sa.select(*SESSIONS.c).where(
                SESSIONS.c.session_hash
                == _require_token_hash(session_hash, "session_hash_not_a_digest")
            )
        )
        mapping = result.mappings().one_or_none()
        return None if mapping is None else _session_record(mapping)

    async def find_active_and_touch(
        self,
        session_hash: str,
        *,
        idle_timeout: timedelta,
    ) -> SessionRecord | None:
        """Atomically validate absolute/idle expiry and advance activity time.

        Both comparisons use PostgreSQL time.  No application-host wall clock
        participates in the authentication decision.
        """
        if idle_timeout <= timedelta(0):
            raise ValueError("idle_timeout must be positive")
        database_now = sa.func.clock_timestamp()
        statement = (
            sa.update(SESSIONS)
            .where(
                SESSIONS.c.session_hash
                == _require_token_hash(session_hash, "session_hash_not_a_digest"),
                SESSIONS.c.revoked_at.is_(None),
                SESSIONS.c.expires_at > database_now,
                SESSIONS.c.last_seen_at > database_now - idle_timeout,
            )
            .values(last_seen_at=database_now)
            .returning(*SESSIONS.c)
        )
        mapping = (await self._session.execute(statement)).mappings().one_or_none()
        return None if mapping is None else _session_record(mapping)

    async def find_active_by_session_hash(
        self,
        session_hash: str,
        *,
        idle_timeout: timedelta,
    ) -> SessionRecord | None:
        """Read an active session without locking it or advancing activity."""
        if idle_timeout <= timedelta(0):
            raise ValueError("idle_timeout must be positive")
        database_now = sa.func.clock_timestamp()
        statement = sa.select(*SESSIONS.c).where(
            SESSIONS.c.session_hash
            == _require_token_hash(session_hash, "session_hash_not_a_digest"),
            SESSIONS.c.revoked_at.is_(None),
            SESSIONS.c.expires_at > database_now,
            SESSIONS.c.last_seen_at > database_now - idle_timeout,
        )
        mapping = (await self._session.execute(statement)).mappings().one_or_none()
        return None if mapping is None else _session_record(mapping)

    async def touch(self, session_id: UUID, *, now: datetime) -> SessionRecord | None:
        """Advance ``last_seen_at`` for the idle timeout enforced by the login flow."""
        statement = (
            sa.update(SESSIONS)
            .where(SESSIONS.c.id == session_id, SESSIONS.c.revoked_at.is_(None))
            .values(last_seen_at=as_utc(now))
            .returning(*SESSIONS.c)
        )
        mapping = (await self._session.execute(statement)).mappings().one_or_none()
        return None if mapping is None else _session_record(mapping)

    async def revoke(self, session_id: UUID, *, now: datetime) -> SessionRecord | None:
        """Revoke one session; revoking an already revoked one changes nothing."""
        statement = (
            sa.update(SESSIONS)
            .where(SESSIONS.c.id == session_id, SESSIONS.c.revoked_at.is_(None))
            .values(revoked_at=as_utc(now))
            .returning(*SESSIONS.c)
        )
        mapping = (await self._session.execute(statement)).mappings().one_or_none()
        return None if mapping is None else _session_record(mapping)

    async def revoke_all_for_user(self, user_id: UUID, *, now: datetime) -> int:
        """Revoke every live session of one account and return how many.

        Every password change or reset requires this; the caller decides when,
        and this method decides what "every" means.
        """
        statement = (
            sa.update(SESSIONS)
            .where(SESSIONS.c.user_id == user_id, SESSIONS.c.revoked_at.is_(None))
            .values(revoked_at=as_utc(now))
            .returning(SESSIONS.c.id)
        )
        return len((await self._session.execute(statement)).all())

    async def revoke_others_for_user(
        self,
        user_id: UUID,
        *,
        current_session_id: UUID,
        now: datetime,
    ) -> int:
        """Revoke every live session except the one being safely rotated."""
        statement = (
            sa.update(SESSIONS)
            .where(
                SESSIONS.c.user_id == user_id,
                SESSIONS.c.id != current_session_id,
                SESSIONS.c.revoked_at.is_(None),
            )
            .values(revoked_at=as_utc(now))
            .returning(SESSIONS.c.id)
        )
        return len((await self._session.execute(statement)).all())

    async def delete_expired(self, *, now: datetime) -> int:
        """Remove rows that can no longer authenticate anything."""
        statement = (
            sa.delete(SESSIONS).where(SESSIONS.c.expires_at <= as_utc(now)).returning(SESSIONS.c.id)
        )
        return len((await self._session.execute(statement)).all())


class LoginProtectionRepository:
    """Serialize and persist login failures for real and unknown identifiers."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def prune_stale(
        self,
        *,
        retention: timedelta,
        limit: int,
        exclude_principal_hash: str,
    ) -> int:
        """Bound unauthenticated identifier storage with incremental expiry.

        Active locks are never candidates.  ``SKIP LOCKED`` keeps concurrent
        login transactions from waiting on cleanup, and the batch limit keeps
        one request from turning retention into an unbounded maintenance job.
        """
        if retention <= timedelta(0):
            raise ValueError("retention must be positive")
        if limit < 1:
            raise ValueError("limit must be positive")
        excluded = _require_token_hash(
            exclude_principal_hash,
            "principal_hash_not_a_digest",
        )
        database_now = sa.func.clock_timestamp()
        stale = (
            sa.select(LOGIN_PROTECTION.c.principal_hash)
            .where(
                LOGIN_PROTECTION.c.updated_at < database_now - retention,
                LOGIN_PROTECTION.c.principal_hash != excluded,
                sa.or_(
                    LOGIN_PROTECTION.c.locked_until.is_(None),
                    LOGIN_PROTECTION.c.locked_until < database_now,
                ),
            )
            .order_by(LOGIN_PROTECTION.c.updated_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        stale_hashes = tuple((await self._session.execute(stale)).scalars())
        if not stale_hashes:
            return 0
        statement = (
            sa.delete(LOGIN_PROTECTION)
            .where(LOGIN_PROTECTION.c.principal_hash.in_(stale_hashes))
            .returning(LOGIN_PROTECTION.c.principal_hash)
        )
        return len((await self._session.execute(statement)).all())

    async def ensure_and_lock(self, principal_hash: str) -> LoginProtectionRecord:
        """Create the protection row if needed and take its lock atomically."""
        digest = _require_token_hash(principal_hash, "principal_hash_not_a_digest")
        insert = postgresql_insert(LOGIN_PROTECTION).values(
            principal_hash=digest,
            failed_attempts=0,
            locked_until=None,
            updated_at=sa.func.clock_timestamp(),
        )
        database_now = sa.func.clock_timestamp()
        statement = insert.on_conflict_do_update(
            index_elements=[LOGIN_PROTECTION.c.principal_hash],
            set_={LOGIN_PROTECTION.c.principal_hash: insert.excluded.principal_hash},
        ).returning(
            *LOGIN_PROTECTION.c,
            database_now.label("database_now"),
        )
        return _login_protection_record((await self._session.execute(statement)).mappings().one())

    async def record_failure(
        self,
        record: LoginProtectionRecord,
        *,
        lock_seconds: int,
    ) -> LoginProtectionRecord:
        """Persist the next failure while the caller holds this row's lock."""
        if lock_seconds < 0:
            raise ValueError("lock_seconds must not be negative")
        failed_attempts = record.failed_attempts + 1
        database_now = sa.func.clock_timestamp()
        locked_until = None if lock_seconds == 0 else database_now + timedelta(seconds=lock_seconds)
        statement = (
            sa.update(LOGIN_PROTECTION)
            .where(LOGIN_PROTECTION.c.principal_hash == record.principal_hash)
            .values(
                failed_attempts=failed_attempts,
                locked_until=locked_until,
                updated_at=database_now,
            )
            .returning(
                *LOGIN_PROTECTION.c,
                database_now.label("database_now"),
            )
        )
        return _login_protection_record((await self._session.execute(statement)).mappings().one())

    async def clear(self, principal_hash: str) -> None:
        """Reset consecutive failures after a successful, unlocked login."""
        await self._session.execute(
            sa.delete(LOGIN_PROTECTION).where(
                LOGIN_PROTECTION.c.principal_hash
                == _require_token_hash(principal_hash, "principal_hash_not_a_digest")
            )
        )

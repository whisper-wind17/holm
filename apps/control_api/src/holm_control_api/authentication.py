"""Database-backed browser authentication.

Plaintext passwords and browser tokens stop at this application boundary.
Repositories receive only Argon2id or SHA-256 digests, and every Argon2 call is
offloaded from the ASGI event loop behind a bounded concurrency gate.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from http import HTTPStatus
from typing import Final, Protocol, TypeVar
from uuid import UUID

from holm_common import new_uuid7
from holm_common.usernames import UsernameError, normalize_username
from holm_domain import AuditAction, AuditActorType, AuditObjectType, AuditResult
from holm_persistence import (
    AsyncSessionFactory,
    AuditEventDraft,
    AuditEventRecord,
    AuditWriter,
    LoginProtectionRecord,
    LoginProtectionRepository,
    SessionRecord,
    SessionRepository,
    UserRecord,
    UserRepository,
    database_utc_now,
)
from holm_security import (
    PasswordHashError,
    PasswordPolicyError,
    SessionTokenPair,
    TokenError,
    build_audit_details,
    generate_session_token_pair,
    hash_token,
    login_lock_seconds,
    login_principal_hash,
    token_matches_hash,
)
from sqlalchemy.ext.asyncio import AsyncSession

SESSION_IDLE_TIMEOUT: Final[timedelta] = timedelta(minutes=30)
SESSION_ABSOLUTE_TIMEOUT: Final[timedelta] = timedelta(hours=8)
DEFAULT_MINIMUM_LOGIN_FAILURE_SECONDS: Final[float] = 0.5
DEFAULT_MAX_CONCURRENT_PASSWORD_OPERATIONS: Final[int] = 4
LOGIN_PROTECTION_RETENTION: Final[timedelta] = timedelta(hours=1)
LOGIN_PROTECTION_CLEANUP_BATCH_SIZE: Final[int] = 100


class PasswordHasherPort(Protocol):
    """The Argon2 operations used by the authentication flow."""

    def hash_user_password(
        self,
        password: str,
        *,
        username: str,
        display_name: str,
    ) -> str: ...

    def hash_authentication_sentinel(self) -> str: ...

    def verify(self, encoded_hash: str, password: str) -> bool: ...


type SessionTokenFactory = Callable[[], SessionTokenPair]
_PasswordResult = TypeVar("_PasswordResult")


class AuditWriterPort(Protocol):
    """The two explicit audit transaction modes used by authentication."""

    async def add(
        self,
        session: AsyncSession,
        event: AuditEventDraft,
    ) -> AuditEventRecord: ...

    async def write(self, event: AuditEventDraft) -> AuditEventRecord: ...


class AuthenticationFailure(RuntimeError):
    """A sanitized failure that can safely cross the HTTP boundary."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int,
        retry_after_seconds: int | None = None,
        details: dict[str, object] | None = None,
        audit_actor_id: UUID | None = None,
        audit_object_id: UUID | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.details = {} if details is None else details
        self.audit_actor_id = audit_actor_id
        self.audit_object_id = audit_object_id
        super().__init__(f"authentication rejected: {code}")


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """One newly issued browser session; plaintext values stay out of repr."""

    user: UserRecord
    token_pair: SessionTokenPair


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    """The database identities established by an active browser session."""

    user: UserRecord
    session: SessionRecord


def _invalid_credentials() -> AuthenticationFailure:
    return AuthenticationFailure(
        code="invalid_credentials",
        message="用户名或密码不正确",
        status_code=HTTPStatus.UNAUTHORIZED,
    )


def _login_locked(retry_after_seconds: int) -> AuthenticationFailure:
    return AuthenticationFailure(
        code="login_locked",
        message=f"登录失败次数过多，请在 {retry_after_seconds} 秒后重试",
        status_code=HTTPStatus.TOO_MANY_REQUESTS,
        retry_after_seconds=retry_after_seconds,
    )


def _authentication_required() -> AuthenticationFailure:
    return AuthenticationFailure(
        code="authentication_required",
        message="登录会话无效或已过期，请重新登录",
        status_code=HTTPStatus.UNAUTHORIZED,
    )


def _csrf_rejected(*, actor_id: UUID, object_id: UUID) -> AuthenticationFailure:
    return AuthenticationFailure(
        code="csrf_validation_failed",
        message="安全校验失败，本次操作未执行，请刷新页面后重试",
        status_code=HTTPStatus.FORBIDDEN,
        audit_actor_id=actor_id,
        audit_object_id=object_id,
    )


class AuthenticationService:
    """Authenticate local users and own the complete browser-session lifecycle."""

    __slots__ = (
        "_audit_writer",
        "_dummy_password_hash",
        "_dummy_password_hash_lock",
        "_minimum_login_failure_seconds",
        "_password_hasher",
        "_password_slots",
        "_session_factory",
        "_token_factory",
    )

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory,
        password_hasher: PasswordHasherPort,
        token_factory: SessionTokenFactory = generate_session_token_pair,
        minimum_login_failure_seconds: float = DEFAULT_MINIMUM_LOGIN_FAILURE_SECONDS,
        max_concurrent_password_operations: int = DEFAULT_MAX_CONCURRENT_PASSWORD_OPERATIONS,
        audit_writer: AuditWriterPort | None = None,
    ) -> None:
        if minimum_login_failure_seconds < 0:
            raise ValueError("minimum_login_failure_seconds must not be negative")
        if max_concurrent_password_operations < 1:
            raise ValueError("max_concurrent_password_operations must be positive")
        self._session_factory = session_factory
        self._audit_writer = AuditWriter(session_factory) if audit_writer is None else audit_writer
        self._password_hasher = password_hasher
        self._token_factory = token_factory
        self._minimum_login_failure_seconds = minimum_login_failure_seconds
        self._password_slots = asyncio.BoundedSemaphore(max_concurrent_password_operations)
        self._dummy_password_hash: str | None = None
        self._dummy_password_hash_lock = asyncio.Lock()

    @property
    def audit_writer(self) -> AuditWriterPort:
        """Writer shared with the authorization middleware."""
        return self._audit_writer

    async def login(
        self,
        *,
        username: str,
        password: str,
        presented_session_token: str | None = None,
        request_id: UUID | None = None,
    ) -> IssuedSession:
        """Authenticate without exposing account existence in behavior or cost."""
        correlation_id = new_uuid7() if request_id is None else request_id
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            username_norm = normalize_username(username)
        except UsernameError:
            username_norm = None
        principal_hash = login_principal_hash(username if username_norm is None else username_norm)

        failure: AuthenticationFailure | None = None
        issued: IssuedSession | None = None
        dummy_password_hash = await self._dummy_hash()
        async with self._session_factory() as session:
            users = UserRepository(session)
            user = None if username_norm is None else await users.find_by_username(username_norm)
            candidate_hash = dummy_password_hash if user is None else user.password_hash
            password_matches = await self._verify_login_candidate(
                candidate_hash,
                password,
                dummy_password_hash=dummy_password_hash,
            )
            if user is not None and password_matches:
                # Re-read under a row lock after the expensive verification.
                # A concurrent password change must not let the old password
                # create a fresh session after that change revoked the others.
                locked_user = await users.get_for_update(user.id)
                if (
                    locked_user is None
                    or not locked_user.can_authenticate
                    or locked_user.password_hash != user.password_hash
                ):
                    password_matches = False
                user = locked_user

            protection = LoginProtectionRepository(session)
            state = await protection.ensure_and_lock(principal_hash)
            await protection.prune_stale(
                retention=LOGIN_PROTECTION_RETENTION,
                limit=LOGIN_PROTECTION_CLEANUP_BATCH_SIZE,
                exclude_principal_hash=principal_hash,
            )
            if state.is_locked:
                failure = _login_locked(self._remaining_lock_seconds(state))
            elif user is None or not user.can_authenticate or not password_matches:
                next_attempt = state.failed_attempts + 1
                lock_seconds = login_lock_seconds(next_attempt)
                await protection.record_failure(state, lock_seconds=lock_seconds)
                failure = _login_locked(lock_seconds) if lock_seconds else _invalid_credentials()
            else:
                await protection.clear(principal_hash)
                sessions = SessionRepository(session)
                await self._revoke_presented_session(
                    sessions,
                    presented_session_token=presented_session_token,
                    session=session,
                )
                now = await database_utc_now(session)
                issued = await self._create_session(sessions, user=user, now=now)
                await self._audit_writer.add(
                    session,
                    self._audit_event(
                        action=AuditAction.LOGIN,
                        actor_id=user.id,
                        object_type=AuditObjectType.USER,
                        object_id=user.id,
                        result=AuditResult.SUCCEEDED,
                        request_id=correlation_id,
                    ),
                )
            await session.commit()

        if failure is not None:
            await self._pad_failed_login(started)
            await self._audit_writer.write(
                self._audit_event(
                    action=AuditAction.LOGIN,
                    actor_id=None,
                    object_type=AuditObjectType.USER,
                    object_id=None,
                    result=AuditResult.FAILED,
                    request_id=correlation_id,
                    detail_candidates={"reason_code": failure.code},
                )
            )
            raise failure
        if issued is None:
            raise RuntimeError("login completed without a result")
        return issued

    async def logout(
        self,
        *,
        session_token: str | None,
        csrf_token: str | None,
        request_id: UUID | None = None,
    ) -> None:
        """Validate CSRF and revoke the current session."""
        correlation_id = new_uuid7() if request_id is None else request_id
        try:
            async with self._session_factory() as session:
                authenticated = await self._authenticate_in_transaction(
                    session,
                    session_token=session_token,
                    csrf_token=csrf_token,
                    require_csrf=True,
                )
                now = await database_utc_now(session)
                await SessionRepository(session).revoke(authenticated.session.id, now=now)
                await self._audit_writer.add(
                    session,
                    self._audit_event(
                        action=AuditAction.LOGOUT,
                        actor_id=authenticated.user.id,
                        object_type=AuditObjectType.SESSION,
                        object_id=authenticated.session.id,
                        result=AuditResult.SUCCEEDED,
                        request_id=correlation_id,
                    ),
                )
                await session.commit()
        except AuthenticationFailure as failure:
            await self._write_failed_authentication_action(
                action=AuditAction.LOGOUT,
                object_type=AuditObjectType.SESSION,
                failure=failure,
                request_id=correlation_id,
            )
            raise

    async def change_password(
        self,
        *,
        session_token: str | None,
        csrf_token: str | None,
        current_password: str | None,
        new_password: str,
        request_id: UUID | None = None,
    ) -> IssuedSession:
        """Replace the password, revoke other sessions, and rotate the current one."""
        correlation_id = new_uuid7() if request_id is None else request_id
        try:
            async with self._session_factory() as session:
                authenticated = await self._authenticate_for_password_change(
                    session,
                    session_token=session_token,
                    csrf_token=csrf_token,
                )
                current_password_matches = (
                    authenticated.user.must_change_password and current_password is None
                ) or (
                    current_password is not None
                    and await self._verify_password(
                        authenticated.user.password_hash,
                        current_password,
                    )
                )
                if not current_password_matches:
                    raise AuthenticationFailure(
                        code="current_password_incorrect",
                        message="当前密码不正确，密码未修改，请重新输入",
                        status_code=HTTPStatus.UNAUTHORIZED,
                        audit_actor_id=authenticated.user.id,
                        audit_object_id=authenticated.user.id,
                    )
                effective_display_name = authenticated.user.display_name
                if not effective_display_name:
                    effective_display_name = authenticated.user.username_norm
                try:
                    password_hash = await self._hash_user_password(
                        new_password,
                        username=authenticated.user.username_norm,
                        display_name=effective_display_name,
                    )
                except PasswordPolicyError as error:
                    raise AuthenticationFailure(
                        code="invalid_new_password",
                        message="新密码不符合要求，密码未修改，请检查后重试",
                        status_code=HTTPStatus.BAD_REQUEST,
                        details={
                            "password_policy": [
                                violation.as_dict() for violation in error.violations
                            ]
                        },
                        audit_actor_id=authenticated.user.id,
                        audit_object_id=authenticated.user.id,
                    ) from None
                except PasswordHashError:
                    raise AuthenticationFailure(
                        code="invalid_new_password",
                        message="新密码无法安全处理，密码未修改，请更换后重试",
                        status_code=HTTPStatus.BAD_REQUEST,
                        audit_actor_id=authenticated.user.id,
                        audit_object_id=authenticated.user.id,
                    ) from None

                now = await database_utc_now(session)
                users = UserRepository(session)
                user = await users.set_password(
                    authenticated.user.id,
                    password_hash=password_hash,
                    now=now,
                    must_change_password=False,
                )
                sessions = SessionRepository(session)
                await sessions.revoke_others_for_user(
                    user.id,
                    current_session_id=authenticated.session.id,
                    now=now,
                )
                issued = await self._rotate_current_session(
                    sessions,
                    current=authenticated.session,
                    user=user,
                    now=now,
                )
                await self._audit_writer.add(
                    session,
                    self._audit_event(
                        action=AuditAction.PASSWORD_CHANGE,
                        actor_id=user.id,
                        object_type=AuditObjectType.USER,
                        object_id=user.id,
                        result=AuditResult.SUCCEEDED,
                        request_id=correlation_id,
                        detail_candidates={"changed_fields": ["password"]},
                    ),
                )
                await session.commit()
                return issued
        except AuthenticationFailure as failure:
            await self._write_failed_authentication_action(
                action=AuditAction.PASSWORD_CHANGE,
                object_type=AuditObjectType.USER,
                failure=failure,
                request_id=correlation_id,
            )
            raise

    async def rotate_for_privilege_elevation(
        self,
        *,
        session_token: str | None,
        csrf_token: str | None,
    ) -> IssuedSession:
        """Rotation hook for a future permission-elevation transaction."""
        async with self._session_factory() as session:
            authenticated = await self._authenticate_in_transaction(
                session,
                session_token=session_token,
                csrf_token=csrf_token,
                require_csrf=True,
            )
            now = await database_utc_now(session)
            issued = await self._rotate_current_session(
                SessionRepository(session),
                current=authenticated.session,
                user=authenticated.user,
                now=now,
            )
            await session.commit()
            return issued

    async def hash_new_user_password(
        self,
        password: str,
        *,
        username: str,
        display_name: str,
    ) -> str:
        """Enforce the password policy and hash, through the same bounded worker as login.

        Exposed so the first-administrator flow cannot end up with a second
        password rule or an unbounded Argon2 call on the Event Loop: it is the
        very method the password-change path uses.
        """
        return await self._hash_user_password(
            password,
            username=username,
            display_name=display_name,
        )

    async def issue_session_in_transaction(
        self,
        session: AsyncSession,
        *,
        user: UserRecord,
        now: datetime,
    ) -> IssuedSession:
        """Issue a session inside a transaction the caller owns and commits.

        First-administrator creation must hand back a logged-in
        session, and that session has to be indistinguishable from a session
        obtained by logging in.  Exposing the existing minting path is what
        makes that true by construction rather than by duplicated constants:
        the caller cannot choose the cookie attributes, the CSRF binding or the
        absolute timeout, and a session only exists if the caller's transaction
        commits.
        """
        return await self._create_session(SessionRepository(session), user=user, now=now)

    async def authenticate_session(
        self,
        *,
        session_token: str | None,
        csrf_token: str | None = None,
        require_csrf: bool = False,
    ) -> AuthenticatedSession:
        """Reusable session dependency for later authenticated API routers."""
        async with self._session_factory() as session:
            authenticated = await self._authenticate_in_transaction(
                session,
                session_token=session_token,
                csrf_token=csrf_token,
                require_csrf=require_csrf,
            )
            await session.commit()
            return authenticated

    async def reauthenticate_high_risk_operation(
        self,
        *,
        session_token: str | None,
        csrf_token: str | None,
        password: str | None,
    ) -> AuthenticatedSession:
        """Recheck one active user's password under the existing login fence."""

        failure: AuthenticationFailure | None = None
        authenticated: AuthenticatedSession | None = None
        async with self._session_factory() as session:
            authenticated = await self._authenticate_in_transaction(
                session,
                session_token=session_token,
                csrf_token=csrf_token,
                require_csrf=True,
            )
            principal_hash = login_principal_hash(authenticated.user.username_norm)
            password_matches = False
            if password is not None:
                password_matches = await self._verify_login_candidate(
                    authenticated.user.password_hash,
                    password,
                    dummy_password_hash=await self._dummy_hash(),
                )

            protection = LoginProtectionRepository(session)
            state = await protection.ensure_and_lock(principal_hash)
            if state.is_locked:
                failure = _login_locked(self._remaining_lock_seconds(state))
            elif not password_matches:
                next_attempt = state.failed_attempts + 1
                lock_seconds = login_lock_seconds(next_attempt)
                await protection.record_failure(state, lock_seconds=lock_seconds)
                failure = _login_locked(lock_seconds) if lock_seconds else _invalid_credentials()
            else:
                await protection.clear(principal_hash)
            await session.commit()

        if failure is not None:
            raise failure
        if authenticated is None:
            raise RuntimeError("high-risk reauthentication completed without an identity")
        return authenticated

    async def _authenticate_in_transaction(
        self,
        session: AsyncSession,
        *,
        session_token: str | None,
        csrf_token: str | None,
        require_csrf: bool,
    ) -> AuthenticatedSession:
        if not session_token:
            raise _authentication_required()
        try:
            session_hash = hash_token(session_token)
        except TokenError:
            raise _authentication_required() from None
        sessions = SessionRepository(session)
        record = await sessions.find_active_and_touch(
            session_hash,
            idle_timeout=SESSION_IDLE_TIMEOUT,
        )
        if record is None:
            raise _authentication_required()
        user = await UserRepository(session).get_for_authentication(record.user_id)
        if user is None or not user.can_authenticate:
            raise _authentication_required()
        if require_csrf:
            if not csrf_token:
                raise _csrf_rejected(actor_id=user.id, object_id=record.id)
            try:
                matches = token_matches_hash(csrf_token, record.csrf_hash)
            except TokenError:
                matches = False
            if not matches:
                raise _csrf_rejected(actor_id=user.id, object_id=record.id)
        return AuthenticatedSession(user=user, session=record)

    async def _authenticate_for_password_change(
        self,
        session: AsyncSession,
        *,
        session_token: str | None,
        csrf_token: str | None,
    ) -> AuthenticatedSession:
        """Authenticate in the common User->Session lock order.

        Two devices can change one password concurrently.  Locking each current
        Session first and then trying to revoke the other creates a cycle.  This
        method resolves identities read-only, locks the shared User row, and
        only then locks/touches the current Session, so one transaction wins and
        the other observes its Session revoked.
        """
        if not session_token:
            raise _authentication_required()
        try:
            session_hash = hash_token(session_token)
        except TokenError:
            raise _authentication_required() from None

        sessions = SessionRepository(session)
        candidate = await sessions.find_active_by_session_hash(
            session_hash,
            idle_timeout=SESSION_IDLE_TIMEOUT,
        )
        if candidate is None:
            raise _authentication_required()
        users = UserRepository(session)
        user = await users.get_for_update(candidate.user_id)
        if user is None or not user.can_authenticate:
            raise _authentication_required()
        current = await sessions.find_active_and_touch(
            session_hash,
            idle_timeout=SESSION_IDLE_TIMEOUT,
        )
        if current is None or current.user_id != user.id:
            raise _authentication_required()
        if not csrf_token:
            raise _csrf_rejected(actor_id=user.id, object_id=user.id)
        try:
            matches = token_matches_hash(csrf_token, current.csrf_hash)
        except TokenError:
            matches = False
        if not matches:
            raise _csrf_rejected(actor_id=user.id, object_id=user.id)
        return AuthenticatedSession(user=user, session=current)

    @staticmethod
    def _audit_event(
        *,
        action: AuditAction,
        actor_id: UUID | None,
        object_type: AuditObjectType,
        object_id: UUID | None,
        result: AuditResult,
        request_id: UUID,
        detail_candidates: dict[str, object] | None = None,
    ) -> AuditEventDraft:
        return AuditEventDraft(
            actor_type=AuditActorType.ANONYMOUS if actor_id is None else AuditActorType.USER,
            actor_id=actor_id,
            action=action,
            object_type=object_type,
            object_id=object_id,
            result=result,
            request_id=request_id,
            details=build_audit_details(action.value, detail_candidates or {}),
        )

    async def _write_failed_authentication_action(
        self,
        *,
        action: AuditAction,
        object_type: AuditObjectType,
        failure: AuthenticationFailure,
        request_id: UUID,
    ) -> None:
        await self._audit_writer.write(
            self._audit_event(
                action=action,
                actor_id=failure.audit_actor_id,
                object_type=object_type,
                object_id=failure.audit_object_id,
                result=(
                    AuditResult.DENIED
                    if failure.status_code == HTTPStatus.FORBIDDEN
                    else AuditResult.FAILED
                ),
                request_id=request_id,
                detail_candidates={"reason_code": failure.code},
            )
        )

    async def _rotate_current_session(
        self,
        sessions: SessionRepository,
        *,
        current: SessionRecord,
        user: UserRecord,
        now: datetime,
    ) -> IssuedSession:
        await sessions.revoke(current.id, now=now)
        return await self._create_session(sessions, user=user, now=now)

    async def _create_session(
        self,
        sessions: SessionRepository,
        *,
        user: UserRecord,
        now: datetime,
    ) -> IssuedSession:
        """The single place a browser session is minted.

        Login, rotation and the first-administrator bootstrap all reach the
        database through this one method, so a session issued by one of them
        cannot drift from a session issued by another: same CSPRNG token pair,
        same Session-bound CSRF digest, same absolute timeout.
        """
        pair = self._token_factory()
        await sessions.create(
            user_id=user.id,
            session_hash=pair.session_hash,
            csrf_hash=pair.csrf_hash,
            expires_at=now + SESSION_ABSOLUTE_TIMEOUT,
            now=now,
        )
        return IssuedSession(user=user, token_pair=pair)

    async def _revoke_presented_session(
        self,
        sessions: SessionRepository,
        *,
        presented_session_token: str | None,
        session: AsyncSession,
    ) -> None:
        if not presented_session_token:
            return
        try:
            digest = hash_token(presented_session_token)
        except TokenError:
            return
        current = await sessions.find_by_session_hash(digest)
        if current is not None and current.revoked_at is None:
            await sessions.revoke(current.id, now=await database_utc_now(session))

    async def _verify_login_candidate(
        self,
        encoded_hash: str,
        password: str,
        *,
        dummy_password_hash: str,
    ) -> bool:
        try:
            return await self._verify_password(encoded_hash, password)
        except PasswordHashError:
            # A corrupt row must not turn into a distinct 500/account-exists
            # oracle.  Pay the configured dummy cost and reject identically.
            try:
                await self._verify_password(dummy_password_hash, password)
            except PasswordHashError:
                return False
            return False

    async def _verify_password(self, encoded_hash: str, password: str) -> bool:
        return await self._run_password_operation(
            lambda: self._password_hasher.verify(encoded_hash, password)
        )

    async def _hash_user_password(
        self,
        password: str,
        *,
        username: str,
        display_name: str,
    ) -> str:
        return await self._run_password_operation(
            lambda: self._password_hasher.hash_user_password(
                password,
                username=username,
                display_name=display_name,
            )
        )

    async def _hash_authentication_sentinel(self) -> str:
        return await self._run_password_operation(
            self._password_hasher.hash_authentication_sentinel
        )

    async def _run_password_operation(
        self,
        operation: Callable[[], _PasswordResult],
    ) -> _PasswordResult:
        """Hold the concurrency slot until an uncancellable worker has exited.

        Cancelling ``asyncio.to_thread`` only cancels its awaiter; the native
        Argon2 call keeps running.  Releasing the semaphore at that point would
        let repeated client cancellations bypass the process's CPU/memory bound.
        The outer request still receives cancellation, but only after the worker
        has finished and the slot can truthfully be released.
        """
        async with self._password_slots:
            worker = asyncio.create_task(asyncio.to_thread(operation))
            cancelled = False
            while True:
                try:
                    result = await asyncio.shield(worker)
                    break
                except asyncio.CancelledError:
                    cancelled = True
                    continue
                except BaseException:
                    if cancelled:
                        raise asyncio.CancelledError from None
                    raise
            if cancelled:
                raise asyncio.CancelledError
            return result

    async def _dummy_hash(self) -> str:
        """Lazily build the equal-cost hash without blocking the Event Loop."""
        if self._dummy_password_hash is not None:
            return self._dummy_password_hash
        async with self._dummy_password_hash_lock:
            if self._dummy_password_hash is None:
                self._dummy_password_hash = await self._hash_authentication_sentinel()
            return self._dummy_password_hash

    async def _pad_failed_login(self, started: float) -> None:
        remaining = self._minimum_login_failure_seconds - (
            asyncio.get_running_loop().time() - started
        )
        if remaining > 0:
            await asyncio.sleep(remaining)

    @staticmethod
    def _remaining_lock_seconds(state: LoginProtectionRecord) -> int:
        locked_until = state.locked_until
        database_now = state.database_now
        if locked_until is None or database_now is None:
            raise RuntimeError("locked login state has no database deadline")
        return max(1, math.ceil((locked_until - database_now).total_seconds()))


__all__ = [
    "DEFAULT_MAX_CONCURRENT_PASSWORD_OPERATIONS",
    "DEFAULT_MINIMUM_LOGIN_FAILURE_SECONDS",
    "LOGIN_PROTECTION_CLEANUP_BATCH_SIZE",
    "LOGIN_PROTECTION_RETENTION",
    "SESSION_ABSOLUTE_TIMEOUT",
    "SESSION_IDLE_TIMEOUT",
    "AuthenticatedSession",
    "AuditWriterPort",
    "AuthenticationFailure",
    "AuthenticationService",
    "IssuedSession",
    "PasswordHasherPort",
    "SessionTokenFactory",
]

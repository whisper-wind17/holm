"""First-time initialization: state, detection and the one transaction.

Four properties are decided here rather than at the HTTP boundary, because each
of them has to hold for a caller that never loads the web client:

1.  **One-time really means one time.** The authoritative decision is the single
    conditional ``UPDATE`` in :meth:`BootstrapTokenRepository.consume`, executed
    inside the same transaction that creates the administrator. The cheap
    read-only classification that runs first exists only so a bad Token does not
    cost an Agent round trip; it is not what makes the Token one-time, and
    removing the conditional update would leave two concurrent callers both
    winning.
2.  **Missing, expired and spent are one refusal outwardly and three facts
    inwardly.** The three take the same code path, the same number of database
    statements and produce a byte-identical response body, so an unauthenticated
    caller cannot use them to learn whether this machine was ever initialized.
    The audit row keeps them apart, because an operator has to be able to tell a
    typo from a scan.
3.  **A failing detection card blocks completion on the server.** The verdict is
    recomputed from the Agent while the request is being handled; nothing in the
    request body can state, imply or influence a detection result. A warning does
    not block -- initialization warnings allow continuation with their impact explained, and
    treating them as blocking would make a compliant but small machine
    uninstallable.
4.  **The administrator, the session and ``system_initialized_at`` are one
    transaction.** A session handed to the browser therefore *implies* that both
    rows exist; there is no state in which the caller is logged in as an
    administrator the database does not have.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, Literal
from uuid import UUID

from holm_common.usernames import (
    UsernameError,
    normalize_account_name,
    normalize_display_name,
)
from holm_domain import (
    AuditAction,
    AuditActorType,
    AuditObjectType,
    AuditResult,
    UserRole,
)
from holm_persistence import (
    AsyncSessionFactory,
    AuditEventDraft,
    AuditWriter,
    BootstrapTokenRepository,
    BootstrapTokenState,
    DuplicateUsernameError,
    SystemInitializationRepository,
    UserRepository,
    database_utc_now,
)
from holm_security import (
    PasswordHashError,
    PasswordPolicyError,
    PasswordPolicyViolation,
    build_audit_details,
    hash_token,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from holm_control_api.agent_client import HostAgentClient
from holm_control_api.authentication import (
    AuditWriterPort,
    AuthenticationService,
    IssuedSession,
)
from holm_control_api.bootstrap_checks import (
    BootstrapCheckId,
    BootstrapCheckOutcome,
    BootstrapCheckReason,
    blocking_check_ids,
    build_environment,
    evaluate_bootstrap_checks,
)
from holm_control_api.system_status import (
    AgentResponse,
    HostEnvironmentResponse,
    SystemStatusService,
    agent_response,
)

#: The Token travels in a request header, never in the query string: the API
#: and the privacy policy keep Secrets out of URLs, which are logged by
#: proxies and kept in browser history. The one-time *link* carries it in the
#: URL fragment, which browsers never transmit.
BOOTSTRAP_TOKEN_HEADER: Final[str] = "X-PWS-Bootstrap-Token"

#: Presented when no Token was supplied at all. Classifying an absent Token with
#: the same single database lookup as a present one is what keeps "缺失" from
#: being distinguishable from "过期"/"已用" by response time.
_ABSENT_TOKEN_DIGEST: Final[str] = hash_token("holm.bootstrap.absent-token.lookup-placeholder")

BOOTSTRAP_DENIED_CODE: Final[str] = "bootstrap_token_invalid"
BOOTSTRAP_DENIED_MESSAGE: Final[str] = (
    "初始化链接无效或已失效，本次操作未执行，请向管理员索取新的初始化链接"
)
BOOTSTRAP_BLOCKED_CODE: Final[str] = "bootstrap_checks_failed"
BOOTSTRAP_BLOCKED_MESSAGE: Final[str] = "系统检测存在失败项，初始化未完成，请按提示处理后重新检测"
BOOTSTRAP_INVALID_USERNAME_CODE: Final[str] = "invalid_username"
BOOTSTRAP_INVALID_USERNAME_MESSAGE: Final[str] = (
    "用户名不符合要求，管理员未创建，请使用 3～32 位字母、数字、点、下划线或短横线"
)
BOOTSTRAP_USERNAME_TAKEN_CODE: Final[str] = "username_taken"
BOOTSTRAP_USERNAME_TAKEN_MESSAGE: Final[str] = "该用户名已被占用，管理员未创建，请换一个用户名"
BOOTSTRAP_INVALID_PASSWORD_CODE: Final[str] = "invalid_password"
BOOTSTRAP_INVALID_PASSWORD_MESSAGE: Final[str] = "密码不符合要求，管理员未创建，请按提示修改后重试"
BOOTSTRAP_NOT_FOUND_CODE: Final[str] = "not_found"
BOOTSTRAP_NOT_FOUND_MESSAGE: Final[str] = "请求的资源不存在"


class BootstrapDenialReason(StrEnum):
    """Why initialization was refused. Internal only: the response never carries it."""

    TOKEN_MISSING = "token_missing"
    TOKEN_UNKNOWN = "token_unknown"
    TOKEN_EXPIRED = "token_expired"
    TOKEN_CONSUMED = "token_consumed"


_TOKEN_STATE_DENIALS: Final[dict[BootstrapTokenState, BootstrapDenialReason]] = {
    BootstrapTokenState.UNKNOWN: BootstrapDenialReason.TOKEN_UNKNOWN,
    BootstrapTokenState.EXPIRED: BootstrapDenialReason.TOKEN_EXPIRED,
    BootstrapTokenState.CONSUMED: BootstrapDenialReason.TOKEN_CONSUMED,
}


class _ClosedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BootstrapCheckResponse(_ClosedResponse):
    id: BootstrapCheckId
    outcome: BootstrapCheckOutcome
    reasons: tuple[BootstrapCheckReason, ...]


class BootstrapStatusResponse(_ClosedResponse):
    """Reached only while uninitialized; once initialized the route is 404."""

    initialized: Literal[False] = False
    token_expires_at: datetime


class BootstrapCheckReportResponse(_ClosedResponse):
    observed_at: datetime
    checks: tuple[BootstrapCheckResponse, ...]
    blocking: tuple[BootstrapCheckId, ...]
    can_complete: bool
    agent: AgentResponse
    host_environment: HostEnvironmentResponse


class BootstrapAdminResponse(_ClosedResponse):
    csrf_token: str = Field(min_length=1)
    must_change_password: bool
    system_initialized_at: datetime


@dataclass(frozen=True, slots=True)
class BootstrapClosed:
    """The platform is already initialized, so the endpoint no longer exists."""


@dataclass(frozen=True, slots=True)
class BootstrapDenied:
    """One refusal. ``reason`` is audited and never rendered to the caller."""

    reason: BootstrapDenialReason


@dataclass(frozen=True, slots=True)
class BootstrapBlocked:
    """Detection found failing cards, so initialization cannot be completed."""

    blocking: tuple[BootstrapCheckId, ...]


@dataclass(frozen=True, slots=True)
class BootstrapInvalidUsername:
    """The submitted account name does not satisfy the account-name rule."""

    code: str


@dataclass(frozen=True, slots=True)
class BootstrapUsernameTaken:
    """A user with that normalized name already exists."""


@dataclass(frozen=True, slots=True)
class BootstrapInvalidPassword:
    """The submitted password was rejected by the password policy."""

    violations: tuple[PasswordPolicyViolation, ...]


@dataclass(frozen=True, slots=True)
class BootstrapCreated:
    """The one transaction committed; the caller is now a logged-in administrator."""

    issued: IssuedSession
    system_initialized_at: datetime


type BootstrapStatusOutcome = BootstrapClosed | BootstrapDenied | BootstrapStatusResponse
type BootstrapCheckOutcomeResult = BootstrapClosed | BootstrapDenied | BootstrapCheckReportResponse
type BootstrapAdminOutcome = (
    BootstrapClosed
    | BootstrapDenied
    | BootstrapBlocked
    | BootstrapInvalidUsername
    | BootstrapUsernameTaken
    | BootstrapInvalidPassword
    | BootstrapCreated
)

type InitializationRepositoryFactory = Callable[[AsyncSession], SystemInitializationRepository]


class BootstrapService:
    """Own the whole first-initialization surface, including its transaction."""

    __slots__ = (
        "_agent",
        "_audit_writer",
        "_authentication",
        "_initialization_repository",
        "_session_factory",
        "_system_status",
    )

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory,
        agent: HostAgentClient,
        system_status: SystemStatusService,
        authentication: AuthenticationService,
        audit_writer: AuditWriterPort | None = None,
        initialization_repository: InitializationRepositoryFactory = (
            SystemInitializationRepository
        ),
    ) -> None:
        self._session_factory = session_factory
        self._agent = agent
        self._system_status = system_status
        self._authentication = authentication
        self._audit_writer = AuditWriter(session_factory) if audit_writer is None else audit_writer
        # Injected rather than constructed so a test can make the initialization
        # write fail inside the real transaction. Production always gets the real
        # repository; nothing here branches on a test-only flag.
        self._initialization_repository = initialization_repository

    async def read_status(
        self,
        *,
        token: str | None,
        request_id: UUID,
    ) -> BootstrapStatusOutcome:
        """Report that this machine is waiting to be initialized."""
        async with self._session_factory() as session:
            if await self._initialization_repository(session).read() is not None:
                return BootstrapClosed()
            classified = await BootstrapTokenRepository(session).classify(_lookup_digest(token))
            if classified.state is not BootstrapTokenState.VALID:
                return await self._deny(session, token, classified.state, request_id=request_id)
            if classified.expires_at is None:
                raise AssertionError("a valid Token always carries its deadline")
            await session.commit()
            return BootstrapStatusResponse(token_expires_at=classified.expires_at)

    async def run_checks(
        self,
        *,
        token: str | None,
        request_id: UUID,
    ) -> BootstrapCheckOutcomeResult:
        """Re-observe the host and classify the seven detection cards."""
        async with self._session_factory() as session:
            if await self._initialization_repository(session).read() is not None:
                return BootstrapClosed()
            classified = await BootstrapTokenRepository(session).classify(_lookup_digest(token))
            if classified.state is not BootstrapTokenState.VALID:
                return await self._deny(session, token, classified.state, request_id=request_id)
            await session.commit()
        return await self._observe()

    async def create_first_administrator(
        self,
        *,
        token: str | None,
        username: str,
        display_name: str,
        password: str,
        request_id: UUID,
    ) -> BootstrapAdminOutcome:
        """Create the first administrator, or refuse without changing anything."""
        async with self._session_factory() as session:
            if await self._initialization_repository(session).read() is not None:
                return BootstrapClosed()
            classified = await BootstrapTokenRepository(session).classify(_lookup_digest(token))
            if classified.state is not BootstrapTokenState.VALID:
                return await self._deny(session, token, classified.state, request_id=request_id)
            await session.commit()

        try:
            username_norm = normalize_account_name(username)
        except UsernameError as error:
            return BootstrapInvalidUsername(code=error.code)
        effective_display_name = username_norm
        if display_name.strip():
            try:
                effective_display_name = normalize_display_name(display_name)
            except UsernameError as error:
                return BootstrapInvalidUsername(code=error.code)

        # The verdict is produced here, from the Agent, while this request is
        # being handled. No field of the request body reaches this call.
        report = await self._observe()
        if report.blocking:
            await self._audit_denied(
                request_id=request_id,
                reason_code="detection_failed",
            )
            return BootstrapBlocked(blocking=report.blocking)

        try:
            password_hash = await self._authentication.hash_new_user_password(
                password,
                username=username_norm,
                display_name=effective_display_name,
            )
        except PasswordPolicyError as error:
            return BootstrapInvalidPassword(violations=error.violations)
        except PasswordHashError:
            return BootstrapInvalidPassword(violations=())

        return await self._commit_initialization(
            token=token,
            username_norm=username_norm,
            display_name=effective_display_name,
            password_hash=password_hash,
            request_id=request_id,
        )

    async def _commit_initialization(
        self,
        *,
        token: str | None,
        username_norm: str,
        display_name: str,
        password_hash: str,
        request_id: UUID,
    ) -> BootstrapAdminOutcome:
        """The single transaction: spend the Token, create everything, or nothing."""
        async with self._session_factory() as session:
            tokens = BootstrapTokenRepository(session)
            if await tokens.consume(_lookup_digest(token)) is None:
                # Losing the race is indistinguishable outwardly from presenting
                # a spent Token, because that is exactly what happened.
                classified = await tokens.classify(_lookup_digest(token))
                return await self._deny(session, token, classified.state, request_id=request_id)
            now = await database_utc_now(session)
            users = UserRepository(session)
            try:
                administrator = await users.create(
                    username=username_norm,
                    display_name=display_name,
                    role=UserRole.ADMIN,
                    password_hash=password_hash,
                    now=now,
                    # The administrator chose this password. Requiring a
                    # change would lock the first account behind the password-change gate on
                    # a page it has no reason to see.
                    must_change_password=False,
                )
            except DuplicateUsernameError:
                await session.rollback()
                return BootstrapUsernameTaken()
            issued = await self._authentication.issue_session_in_transaction(
                session,
                user=administrator,
                now=now,
            )
            initialization = await self._initialization_repository(session).mark_initialized(
                admin_user_id=administrator.id,
                now=now,
            )
            await self._audit_writer.add(
                session,
                _audit_event(
                    actor_type=AuditActorType.USER,
                    actor_id=administrator.id,
                    action=AuditAction.ACCOUNT_CHANGE,
                    object_type=AuditObjectType.USER,
                    object_id=administrator.id,
                    result=AuditResult.SUCCEEDED,
                    request_id=request_id,
                    detail_candidates={
                        "changed_fields": ["username", "display_name", "password", "role"],
                        "reason_code": "first_administrator_created",
                        "operation": "bootstrap",
                    },
                ),
            )
            await self._audit_writer.add(
                session,
                _audit_event(
                    actor_type=AuditActorType.SYSTEM,
                    actor_id=None,
                    action=AuditAction.SYSTEM_CONFIG_CHANGE,
                    object_type=AuditObjectType.SYSTEM_CONFIG,
                    object_id=None,
                    result=AuditResult.SUCCEEDED,
                    request_id=request_id,
                    detail_candidates={
                        "changed_fields": ["system_initialized_at"],
                        "source": "bootstrap",
                        "reason_code": "system_initialized",
                    },
                ),
            )
            await session.commit()
            return BootstrapCreated(
                issued=issued,
                system_initialized_at=initialization.system_initialized_at,
            )

    async def _observe(self) -> BootstrapCheckReportResponse:
        """Read the host once and classify every detection card from that read."""
        observation = await self._agent.observe()
        components = await self._system_status.read_components(observation=observation)
        rendered = agent_response(observation)
        environment = build_environment(agent=rendered, components=components)
        results = evaluate_bootstrap_checks(environment)
        blocking = blocking_check_ids(results)
        return BootstrapCheckReportResponse(
            observed_at=datetime.now(UTC),
            checks=tuple(
                BootstrapCheckResponse(
                    id=result.id,
                    outcome=result.outcome,
                    reasons=result.reasons,
                )
                for result in results
            ),
            blocking=blocking,
            can_complete=not blocking,
            agent=rendered,
            host_environment=components.host_environment,
        )

    async def _deny(
        self,
        session: AsyncSession,
        token: str | None,
        state: BootstrapTokenState,
        *,
        request_id: UUID,
    ) -> BootstrapDenied:
        reason = BootstrapDenialReason.TOKEN_MISSING if not token else _TOKEN_STATE_DENIALS[state]
        await self._audit_writer.add(
            session,
            _audit_event(
                actor_type=AuditActorType.ANONYMOUS,
                actor_id=None,
                action=AuditAction.SYSTEM_CONFIG_CHANGE,
                object_type=AuditObjectType.SYSTEM_CONFIG,
                object_id=None,
                result=AuditResult.DENIED,
                request_id=request_id,
                detail_candidates={"reason_code": reason.value, "source": "bootstrap"},
            ),
        )
        await session.commit()
        return BootstrapDenied(reason=reason)

    async def _audit_denied(self, *, request_id: UUID, reason_code: str) -> None:
        await self._audit_writer.write(
            _audit_event(
                actor_type=AuditActorType.ANONYMOUS,
                actor_id=None,
                action=AuditAction.SYSTEM_CONFIG_CHANGE,
                object_type=AuditObjectType.SYSTEM_CONFIG,
                object_id=None,
                result=AuditResult.DENIED,
                request_id=request_id,
                detail_candidates={"reason_code": reason_code, "source": "bootstrap"},
            )
        )


def _lookup_digest(token: str | None) -> str:
    """Digest the presented Token, or the fixed placeholder when none was sent.

    The placeholder keeps an absent Token on the same code path -- one lookup,
    one classification -- as a wrong, expired or spent one.
    """
    if not isinstance(token, str) or not token:
        return _ABSENT_TOKEN_DIGEST
    try:
        return hash_token(token)
    except ValueError:
        return _ABSENT_TOKEN_DIGEST


def _audit_event(
    *,
    actor_type: AuditActorType,
    actor_id: UUID | None,
    action: AuditAction,
    object_type: AuditObjectType,
    object_id: UUID | None,
    result: AuditResult,
    request_id: UUID,
    detail_candidates: dict[str, object],
) -> AuditEventDraft:
    return AuditEventDraft(
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        object_type=object_type,
        object_id=object_id,
        result=result,
        request_id=request_id,
        details=build_audit_details(action.value, detail_candidates),
    )


__all__ = [
    "BOOTSTRAP_BLOCKED_CODE",
    "BOOTSTRAP_BLOCKED_MESSAGE",
    "BOOTSTRAP_DENIED_CODE",
    "BOOTSTRAP_DENIED_MESSAGE",
    "BOOTSTRAP_INVALID_PASSWORD_CODE",
    "BOOTSTRAP_INVALID_PASSWORD_MESSAGE",
    "BOOTSTRAP_INVALID_USERNAME_CODE",
    "BOOTSTRAP_INVALID_USERNAME_MESSAGE",
    "BOOTSTRAP_NOT_FOUND_CODE",
    "BOOTSTRAP_NOT_FOUND_MESSAGE",
    "BOOTSTRAP_TOKEN_HEADER",
    "BOOTSTRAP_USERNAME_TAKEN_CODE",
    "BOOTSTRAP_USERNAME_TAKEN_MESSAGE",
    "BootstrapAdminOutcome",
    "BootstrapAdminResponse",
    "BootstrapBlocked",
    "BootstrapCheckReportResponse",
    "BootstrapCheckResponse",
    "BootstrapClosed",
    "BootstrapCreated",
    "BootstrapDenialReason",
    "BootstrapDenied",
    "BootstrapInvalidPassword",
    "BootstrapInvalidUsername",
    "BootstrapService",
    "BootstrapStatusResponse",
    "BootstrapUsernameTaken",
]

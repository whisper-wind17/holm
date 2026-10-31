"""Repositories for the one-time initialization Token and the initialization fact.

No plaintext Token reaches this layer: every method takes a SHA-256 digest, and
the column's check constraint rejects anything else, so storing a plaintext
initialization Token is a failed write rather than a silent leak.

Expiry and one-time consumption are decided with PostgreSQL's ``clock_timestamp()``
for the same reason session expiry is (``account_repository``): an application
host's wall clock must not be able to lengthen or shorten a security deadline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

import sqlalchemy as sa
from holm_common.ids import new_uuid7
from holm_common.time import as_utc
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from holm_persistence.bootstrap import (
    BOOTSTRAP_TOKENS,
    SYSTEM_INITIALIZATION,
    SYSTEM_INITIALIZATION_ROW_ID,
)

_TOKEN_HASH: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_UNIQUE_VIOLATION: Final[str] = "23505"


class BootstrapRepositoryError(RuntimeError):
    """A rejected bootstrap write carrying a stable machine-readable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"bootstrap write rejected: {code}")


class AlreadyInitializedError(BootstrapRepositoryError):
    """``core.system_initialization`` already holds its one and only row."""

    def __init__(self) -> None:
        super().__init__("already_initialized")


class BootstrapTokenState(StrEnum):
    """Why a presented initialization Token was accepted or refused.

    Every value here is an *internal* classification. The HTTP boundary renders
    one identical refusal for all of the refusing values, so an unauthenticated
    caller cannot use the response to learn whether this machine was ever
    initialized or whether a Token ever existed.
    """

    VALID = "valid"
    UNKNOWN = "unknown"
    EXPIRED = "expired"
    CONSUMED = "consumed"


@dataclass(frozen=True, slots=True)
class BootstrapTokenRecord:
    """One ``core.bootstrap_tokens`` row; the plaintext is not knowable from it."""

    id: UUID
    token_hash: str
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None


@dataclass(frozen=True, slots=True)
class BootstrapTokenClassification:
    """What one read-only lookup concluded about a presented Token digest."""

    state: BootstrapTokenState
    expires_at: datetime | None

    def __post_init__(self) -> None:
        if (self.state is BootstrapTokenState.UNKNOWN) != (self.expires_at is None):
            raise ValueError("only an unknown Token has no deadline")


@dataclass(frozen=True, slots=True)
class SystemInitializationRecord:
    """The single ``core.system_initialization`` row, once it exists."""

    system_initialized_at: datetime
    admin_user_id: UUID


def _require_token_hash(value: str) -> str:
    if not isinstance(value, str) or _TOKEN_HASH.match(value) is None:
        raise BootstrapRepositoryError("token_hash_not_a_digest")
    return value


def _token_record(mapping: sa.RowMapping) -> BootstrapTokenRecord:
    consumed_at = mapping["consumed_at"]
    return BootstrapTokenRecord(
        id=mapping["id"],
        token_hash=mapping["token_hash"],
        created_at=as_utc(mapping["created_at"]),
        expires_at=as_utc(mapping["expires_at"]),
        consumed_at=None if consumed_at is None else as_utc(consumed_at),
    )


def _is_unique_violation(error: IntegrityError) -> bool:
    return getattr(error.orig, "sqlstate", None) == _UNIQUE_VIOLATION


class BootstrapTokenRepository:
    """Issue, classify and atomically consume one-time initialization Tokens."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        token_hash: str,
        expires_at: datetime,
        now: datetime,
    ) -> BootstrapTokenRecord:
        """Insert one Token from a digest the caller already computed."""
        statement = (
            sa.insert(BOOTSTRAP_TOKENS)
            .values(
                id=new_uuid7(),
                token_hash=_require_token_hash(token_hash),
                created_at=as_utc(now),
                expires_at=as_utc(expires_at),
                consumed_at=None,
            )
            .returning(*BOOTSTRAP_TOKENS.c)
        )
        return _token_record((await self._session.execute(statement)).mappings().one())

    async def classify(self, token_hash: str) -> BootstrapTokenClassification:
        """Read-only classification using exactly one statement in every case.

        The statement is the same whether the digest matches nothing, a spent
        Token or an expired one, so the three refusals cost the same round trip.
        A caller that special-cased "no such Token" into an early return would
        make the refusals distinguishable by timing.
        """
        statement = sa.select(
            BOOTSTRAP_TOKENS.c.consumed_at,
            BOOTSTRAP_TOKENS.c.expires_at,
            sa.func.clock_timestamp().label("database_now"),
        ).where(BOOTSTRAP_TOKENS.c.token_hash == _require_token_hash(token_hash))
        mapping = (await self._session.execute(statement)).mappings().one_or_none()
        if mapping is None:
            return BootstrapTokenClassification(
                state=BootstrapTokenState.UNKNOWN,
                expires_at=None,
            )
        expires_at = as_utc(mapping["expires_at"])
        if mapping["consumed_at"] is not None:
            state = BootstrapTokenState.CONSUMED
        elif expires_at <= as_utc(mapping["database_now"]):
            state = BootstrapTokenState.EXPIRED
        else:
            state = BootstrapTokenState.VALID
        return BootstrapTokenClassification(state=state, expires_at=expires_at)

    async def consume(self, token_hash: str) -> BootstrapTokenRecord | None:
        """Spend one Token atomically, or return ``None`` without spending any.

        One conditional ``UPDATE`` decides everything.  Two transactions running
        it against the same row serialize on that row's lock; when the loser
        resumes it re-evaluates ``consumed_at IS NULL`` against the committed
        row version and matches nothing.  A read-then-write implementation would
        let both callers observe an unspent Token and then both spend it.
        """
        database_now = sa.func.clock_timestamp()
        statement = (
            sa.update(BOOTSTRAP_TOKENS)
            .where(
                BOOTSTRAP_TOKENS.c.token_hash == _require_token_hash(token_hash),
                BOOTSTRAP_TOKENS.c.consumed_at.is_(None),
                BOOTSTRAP_TOKENS.c.expires_at > database_now,
            )
            .values(consumed_at=database_now)
            .returning(*BOOTSTRAP_TOKENS.c)
        )
        mapping = (await self._session.execute(statement)).mappings().one_or_none()
        return None if mapping is None else _token_record(mapping)

    async def count_usable(self) -> int:
        """How many unspent, unexpired Tokens exist right now."""
        statement = (
            sa.select(sa.func.count())
            .select_from(BOOTSTRAP_TOKENS)
            .where(
                BOOTSTRAP_TOKENS.c.consumed_at.is_(None),
                BOOTSTRAP_TOKENS.c.expires_at > sa.func.clock_timestamp(),
            )
        )
        return int((await self._session.execute(statement)).scalar_one())


class SystemInitializationRepository:
    """Read and write the singleton initialization fact."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def read(self) -> SystemInitializationRecord | None:
        """Return the recorded initialization, or ``None`` while uninitialized."""
        statement = sa.select(
            SYSTEM_INITIALIZATION.c.system_initialized_at,
            SYSTEM_INITIALIZATION.c.admin_user_id,
        )
        mapping = (await self._session.execute(statement)).mappings().one_or_none()
        if mapping is None:
            return None
        return SystemInitializationRecord(
            system_initialized_at=as_utc(mapping["system_initialized_at"]),
            admin_user_id=mapping["admin_user_id"],
        )

    async def mark_initialized(
        self,
        *,
        admin_user_id: UUID,
        now: datetime,
    ) -> SystemInitializationRecord:
        """Record the instant. A second call is a unique violation, not a read.

        There is deliberately no "check whether a row already exists, then
        insert" here: two concurrent callers can both pass such a check.  The
        primary key on a column pinned to one literal value is what makes the
        second write fail.
        """
        statement = (
            sa.insert(SYSTEM_INITIALIZATION)
            .values(
                id=SYSTEM_INITIALIZATION_ROW_ID,
                system_initialized_at=as_utc(now),
                admin_user_id=admin_user_id,
            )
            .returning(
                SYSTEM_INITIALIZATION.c.system_initialized_at,
                SYSTEM_INITIALIZATION.c.admin_user_id,
            )
        )
        try:
            mapping = (await self._session.execute(statement)).mappings().one()
        except IntegrityError as error:
            if _is_unique_violation(error):
                raise AlreadyInitializedError() from None
            raise
        return SystemInitializationRecord(
            system_initialized_at=as_utc(mapping["system_initialized_at"]),
            admin_user_id=mapping["admin_user_id"],
        )


__all__ = [
    "AlreadyInitializedError",
    "BootstrapRepositoryError",
    "BootstrapTokenClassification",
    "BootstrapTokenRecord",
    "BootstrapTokenRepository",
    "BootstrapTokenState",
    "SystemInitializationRecord",
    "SystemInitializationRepository",
]

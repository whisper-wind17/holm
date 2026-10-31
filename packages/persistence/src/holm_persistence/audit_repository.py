"""Repository and explicit transaction boundary for security audit events."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from holm_common.ids import new_uuid7
from holm_common.time import as_utc
from holm_domain import AuditAction, AuditActorType, AuditObjectType, AuditResult
from sqlalchemy.ext.asyncio import AsyncSession

from holm_persistence.accounts import USERS
from holm_persistence.ai_services import API_KEYS
from holm_persistence.audit_events import AUDIT_EVENTS
from holm_persistence.database import AsyncSessionFactory


@dataclass(frozen=True, slots=True)
class AuditEventDraft:
    """A fully classified event whose details were constructed by the caller."""

    actor_type: AuditActorType
    actor_id: UUID | None
    action: AuditAction
    object_type: AuditObjectType
    object_id: UUID | None
    result: AuditResult
    request_id: UUID
    details: Mapping[str, object]

    def __post_init__(self) -> None:
        if (self.actor_type is AuditActorType.USER) != (self.actor_id is not None):
            raise ValueError("only user audit actors carry actor_id")
        if getattr(self.details, "audit_allowlisted", False) is not True:
            raise ValueError("audit details must come from the default-deny builder")


@dataclass(frozen=True, slots=True)
class AuditEventRecord:
    """One persisted audit row; no request or response body field exists."""

    id: UUID
    actor_type: AuditActorType
    actor_id: UUID | None
    action: AuditAction
    object_type: AuditObjectType
    object_id: UUID | None
    result: AuditResult
    request_id: UUID
    details: Mapping[str, object]
    created_at: datetime


def _record(mapping: sa.RowMapping) -> AuditEventRecord:
    return AuditEventRecord(
        id=mapping["id"],
        actor_type=AuditActorType(mapping["actor_type"]),
        actor_id=mapping["actor_id"],
        action=AuditAction(mapping["action"]),
        object_type=AuditObjectType(mapping["object_type"]),
        object_id=mapping["object_id"],
        result=AuditResult(mapping["result"]),
        request_id=mapping["request_id"],
        details=dict(mapping["details"]),
        created_at=as_utc(mapping["created_at"]),
    )


class AuditRepository:
    """Append audit rows inside a caller-owned transaction."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, event: AuditEventDraft) -> AuditEventRecord:
        statement = (
            sa.insert(AUDIT_EVENTS)
            .values(
                id=new_uuid7(),
                actor_type=event.actor_type.value,
                actor_id=event.actor_id,
                action=event.action.value,
                object_type=event.object_type.value,
                object_id=event.object_id,
                result=event.result.value,
                request_id=event.request_id,
                details=dict(event.details),
                created_at=sa.func.clock_timestamp(),
            )
            .returning(*AUDIT_EVENTS.c)
        )
        return _record((await self._session.execute(statement)).mappings().one())

    async def user_activity(
        self,
        user_id: UUID,
        *,
        actions: tuple[str, ...],
        before: tuple[datetime, UUID] | None,
        limit: int,
    ) -> tuple[sa.RowMapping, ...]:
        """Read subject events, including hidden keys and deleted actors, with keyset paging.

        Select only public actor/key metadata; credentials never enter this projection.
        """
        events = AUDIT_EVENTS.c
        actor = USERS.alias("actor")
        key = API_KEYS.alias("activity_key")
        statement = (
            sa.select(
                events.id,
                events.created_at,
                events.action,
                events.result,
                events.actor_type,
                events.actor_id,
                events.object_id,
                events.details,
                actor.c.username_norm.label("actor_username"),
                actor.c.display_name.label("actor_display_name"),
                key.c.id.label("key_id"),
                key.c.name.label("key_name"),
                key.c.display_prefix,
                key.c.last4,
            )
            .select_from(
                AUDIT_EVENTS.outerjoin(
                    actor, sa.and_(events.actor_type == "user", events.actor_id == actor.c.id)
                ).outerjoin(
                    key, sa.and_(events.object_type == "api_key", events.object_id == key.c.id)
                )
            )
            .where(
                sa.or_(
                    sa.and_(events.object_type == "user", events.object_id == user_id),
                    sa.and_(events.object_type == "api_key", key.c.user_id == user_id),
                ),
                events.action.in_(actions),
            )
            .order_by(events.created_at.desc(), events.id.desc())
            .limit(limit)
        )
        if before is not None:
            statement = statement.where(sa.tuple_(events.created_at, events.id) < before)
        return tuple((await self._session.execute(statement)).mappings().all())


class AuditWriter:
    """Select same-transaction or independent-transaction audit semantics."""

    __slots__ = ("_session_factory",)

    def __init__(self, session_factory: AsyncSessionFactory) -> None:
        self._session_factory = session_factory

    async def add(self, session: AsyncSession, event: AuditEventDraft) -> AuditEventRecord:
        """Add to an existing business transaction without committing it."""
        return await AuditRepository(session).add(event)

    async def write(self, event: AuditEventDraft) -> AuditEventRecord:
        """Persist an event in its own transaction; failures propagate."""
        async with self._session_factory() as session:
            record = await self.add(session, event)
            await session.commit()
            return record


__all__ = [
    "AuditEventDraft",
    "AuditEventRecord",
    "AuditRepository",
    "AuditWriter",
]

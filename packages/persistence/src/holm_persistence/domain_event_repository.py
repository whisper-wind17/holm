"""Transaction-bound Domain Event append and snapshot-safe replay queries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final
from uuid import UUID

import sqlalchemy as sa
from holm_common.time import as_utc
from holm_domain import (
    PRODUCTION_EVENT_REGISTRY,
    DomainEvent,
    DomainEventDraft,
    DomainEventType,
)
from sqlalchemy.ext.asyncio import AsyncSession

from holm_persistence.domain_events import (
    _DOMAIN_EVENTS,
    DOMAIN_EVENT_LOCK_CLASS_ID,
    DOMAIN_EVENT_LOCK_OBJECT_ID,
    DOMAIN_EVENT_NOTIFY_CHANNEL,
)

DEFAULT_EVENT_PAGE_SIZE: Final[int] = 256
EVENT_REPLAY_WINDOW: Final[timedelta] = timedelta(hours=24)


class ReplayAnchorStatus(StrEnum):
    VALID = "valid"
    MISSING = "missing"
    EXPIRED = "expired"
    AHEAD = "ahead"


@dataclass(frozen=True, slots=True)
class ReplayAnchor:
    status: ReplayAnchorStatus
    current_max_id: int


def _record(mapping: sa.RowMapping) -> DomainEvent:
    occurred_at = mapping["occurred_at"]
    if not isinstance(occurred_at, datetime):
        raise RuntimeError("Domain Event timestamp has an unexpected database type")
    return DomainEvent(
        id=int(mapping["id"]),
        event=str(mapping["event"]),
        aggregate_id=mapping["aggregate_id"],
        version=int(mapping["version"]),
        occurred_at=as_utc(occurred_at),
        payload=dict(mapping["payload"]),
    )


def _positive_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("event page limit must be a positive integer")
    return limit


class DomainEventRepository:
    """Operate only inside a caller-owned SQLAlchemy session/transaction."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, draft: DomainEventDraft) -> DomainEvent:
        """Append and transactionally notify without committing the business transaction."""
        # All appenders share this transaction-level fence. They may allocate
        # IDs and commit in either order, but a reader's exclusive side cannot
        # advance until every transaction that allocated an earlier ID ended.
        await self._session.execute(
            sa.select(
                sa.func.pg_advisory_xact_lock_shared(
                    DOMAIN_EVENT_LOCK_CLASS_ID,
                    DOMAIN_EVENT_LOCK_OBJECT_ID,
                )
            )
        )
        statement = (
            sa.insert(_DOMAIN_EVENTS)
            .values(
                aggregate_id=draft.aggregate_id,
                event=draft.event,
                version=draft.version,
                payload=draft.payload.json(),
                occurred_at=sa.func.clock_timestamp(),
            )
            .returning(*_DOMAIN_EVENTS.c)
        )
        event = _record((await self._session.execute(statement)).mappings().one())
        # Empty payload by design: the notification is only a low-latency hint.
        # PostgreSQL delivers it at commit and discards it on rollback.
        await self._session.execute(sa.select(sa.func.pg_notify(DOMAIN_EVENT_NOTIFY_CHANNEL, "")))
        return event

    async def has_event(self, *, aggregate_id: UUID, event: str) -> bool:
        """Return whether an aggregate already carries this idempotent event type."""

        value = await self._session.scalar(
            sa.select(sa.literal(True))
            .select_from(_DOMAIN_EVENTS)
            .where(
                _DOMAIN_EVENTS.c.aggregate_id == aggregate_id,
                _DOMAIN_EVENTS.c.event == event,
            )
            .limit(1)
        )
        return value is True

    async def next_version(self, *, aggregate_id: UUID) -> int:
        """Return the next aggregate-local positive version inside the caller transaction."""

        value = await self._session.scalar(
            sa.select(sa.func.coalesce(sa.func.max(_DOMAIN_EVENTS.c.version), 0)).where(
                _DOMAIN_EVENTS.c.aggregate_id == aggregate_id
            )
        )
        return int(value or 0) + 1

    async def stable_frontier(self) -> int:
        """Highest ID that cannot be overtaken by a still-open lower-ID transaction."""
        await self._acquire_read_fence()
        value = (
            await self._session.execute(
                sa.select(sa.func.coalesce(sa.func.max(_DOMAIN_EVENTS.c.id), 0))
            )
        ).scalar_one()
        return int(value)

    async def stable_page(
        self,
        *,
        after_id: int,
        through_id: int,
        limit: int = DEFAULT_EVENT_PAGE_SIZE,
    ) -> tuple[DomainEvent, ...]:
        """Read a bounded monotonic page without crossing the stable frontier."""
        _positive_limit(limit)
        if after_id < 0 or through_id < 0:
            raise ValueError("event cursors must not be negative")
        await self._acquire_read_fence()
        rows = (
            (
                await self._session.execute(
                    sa.select(_DOMAIN_EVENTS)
                    .where(
                        _DOMAIN_EVENTS.c.id > after_id,
                        _DOMAIN_EVENTS.c.id <= through_id,
                    )
                    .order_by(_DOMAIN_EVENTS.c.id)
                    .limit(limit)
                )
            )
            .mappings()
            .all()
        )
        return tuple(_record(row) for row in rows)

    async def replay_anchor(
        self,
        event_id: int,
        *,
        window: timedelta = EVENT_REPLAY_WINDOW,
    ) -> ReplayAnchor:
        """Validate existence, range, and the explicit time-based replay window."""
        if event_id <= 0:
            raise ValueError("replay anchor must be positive")
        if window <= timedelta(0):
            raise ValueError("replay window must be positive")
        row = (
            (
                await self._session.execute(
                    sa.select(
                        sa.func.coalesce(sa.func.max(_DOMAIN_EVENTS.c.id), 0).label("maximum"),
                        sa.func.max(_DOMAIN_EVENTS.c.occurred_at)
                        .filter(_DOMAIN_EVENTS.c.id == event_id)
                        .label("anchor_time"),
                        sa.func.clock_timestamp().label("database_now"),
                    )
                )
            )
            .mappings()
            .one()
        )
        maximum = int(row["maximum"])
        if event_id > maximum:
            return ReplayAnchor(ReplayAnchorStatus.AHEAD, maximum)
        anchor_time = row["anchor_time"]
        if anchor_time is None:
            return ReplayAnchor(ReplayAnchorStatus.MISSING, maximum)
        database_now = row["database_now"]
        if not isinstance(anchor_time, datetime) or not isinstance(database_now, datetime):
            raise RuntimeError("replay window query returned an invalid timestamp")
        if as_utc(anchor_time) < as_utc(database_now) - window:
            return ReplayAnchor(ReplayAnchorStatus.EXPIRED, maximum)
        return ReplayAnchor(ReplayAnchorStatus.VALID, maximum)

    async def _acquire_read_fence(self) -> None:
        # Exclusive is compatible with this transaction's repeated call, but
        # waits for every shared writer fence before any cursor can advance.
        await self._session.execute(
            sa.select(
                sa.func.pg_advisory_xact_lock(
                    DOMAIN_EVENT_LOCK_CLASS_ID,
                    DOMAIN_EVENT_LOCK_OBJECT_ID,
                )
            )
        )


async def append_route_invalidation_event(
    session: AsyncSession,
    *,
    event: DomainEventType,
    aggregate_id: UUID,
) -> DomainEvent:
    """Append a content-free routing invalidation inside the caller transaction."""

    repository = DomainEventRepository(session)
    return await repository.append(
        PRODUCTION_EVENT_REGISTRY.build_draft(
            event.value,
            aggregate_id=aggregate_id,
            version=await repository.next_version(aggregate_id=aggregate_id),
            candidates={},
        )
    )


__all__ = [
    "DEFAULT_EVENT_PAGE_SIZE",
    "EVENT_REPLAY_WINDOW",
    "DomainEventRepository",
    "ReplayAnchor",
    "ReplayAnchorStatus",
    "append_route_invalidation_event",
]

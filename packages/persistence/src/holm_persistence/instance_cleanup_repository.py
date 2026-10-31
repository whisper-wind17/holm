"""Durable cleanup handshake, independent of the SSE replay window.

Control creates the request in the configuration deletion transaction. Instance
only appends results. Failed reasons are recorded once each, bounding events per
request; success supersedes failures and stops future retries.
"""

from uuid import UUID

import sqlalchemy as sa
from holm_domain import PRODUCTION_EVENT_REGISTRY, DomainEventType
from holm_domain.instance_cleanup import CleanupFailure
from sqlalchemy.ext.asyncio import AsyncSession

from holm_persistence.domain_event_repository import DomainEventRepository
from holm_persistence.domain_events import _DOMAIN_EVENTS

_REQUEST = DomainEventType.INSTANCE_CLEANUP_REQUESTED
_COMPLETE = DomainEventType.INSTANCE_CLEANUP_COMPLETED
_FAILED = DomainEventType.INSTANCE_CLEANUP_FAILED
CLEANUP_BATCH_SIZE = 8


class InstanceCleanupRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def request(self, instance_id: UUID) -> None:
        await self._append(instance_id, _REQUEST, {})

    async def pending(self, after: UUID | None = None) -> tuple[UUID, ...]:
        completed = _DOMAIN_EVENTS.alias("cleanup_completed")
        query = sa.select(_DOMAIN_EVENTS.c.aggregate_id).where(
            _DOMAIN_EVENTS.c.event == _REQUEST.value,
            ~sa.exists(
                sa.select(completed.c.id).where(
                    completed.c.aggregate_id == _DOMAIN_EVENTS.c.aggregate_id,
                    completed.c.event == _COMPLETE.value,
                )
            ),
        )
        if after is not None:
            query = query.where(_DOMAIN_EVENTS.c.aggregate_id > after)
        return tuple(
            (
                await self._session.scalars(
                    query.order_by(_DOMAIN_EVENTS.c.aggregate_id).limit(CLEANUP_BATCH_SIZE)
                )
            ).all()
        )

    async def result(self, instance_id: UUID, failure: CleanupFailure | None) -> None:
        events = DomainEventRepository(self._session)
        if not await events.has_event(aggregate_id=instance_id, event=_REQUEST.value):
            raise ValueError("cleanup request missing")
        if await events.has_event(aggregate_id=instance_id, event=_COMPLETE.value):
            return
        event = _COMPLETE if failure is None else _FAILED
        payload: dict[str, object] = {} if failure is None else {"reason": failure.value}
        exists = await self._session.scalar(
            sa.select(_DOMAIN_EVENTS.c.id)
            .where(
                _DOMAIN_EVENTS.c.aggregate_id == instance_id,
                _DOMAIN_EVENTS.c.event == event.value,
                _DOMAIN_EVENTS.c.payload == payload,
            )
            .limit(1)
        )
        if exists is None:
            await self._append(instance_id, event, payload)

    async def _append(
        self, instance_id: UUID, event: DomainEventType, payload: dict[str, object]
    ) -> None:
        events = DomainEventRepository(self._session)
        await events.append(
            PRODUCTION_EVENT_REGISTRY.build_draft(
                event.value,
                aggregate_id=instance_id,
                version=await events.next_version(aggregate_id=instance_id),
                candidates=payload,
            )
        )

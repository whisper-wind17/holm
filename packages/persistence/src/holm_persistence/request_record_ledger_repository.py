"""Idempotent PostgreSQL replay for closed Request Ledger events."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final
from uuid import UUID

import sqlalchemy as sa
from holm_common import as_utc
from holm_domain import RequestRecordState
from holm_ledger import (
    AbortedEvent,
    CompletedEvent,
    EventType,
    FailedEvent,
    InProgressEvent,
    LedgerEvent,
    ReceivedEvent,
    RejectedEvent,
    UnknownEvent,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from holm_persistence.ledger_request_records import (
    LEDGER_REQUEST_RECORDS,
    REQUEST_ID_REGISTRY,
)

_LOGGER: Final[logging.Logger] = logging.getLogger("holm.persistence.request_ledger")
_TERMINAL_TYPES: Final[frozenset[EventType]] = frozenset(
    {
        EventType.REJECTED,
        EventType.COMPLETED,
        EventType.FAILED,
        EventType.ABORTED,
        EventType.UNKNOWN,
    }
)
_STATE_BY_EVENT: Final[Mapping[EventType, RequestRecordState]] = {
    EventType.RECEIVED: RequestRecordState.RECEIVED,
    EventType.REJECTED: RequestRecordState.REJECTED,
    EventType.IN_PROGRESS: RequestRecordState.IN_PROGRESS,
    EventType.COMPLETED: RequestRecordState.COMPLETED,
    EventType.FAILED: RequestRecordState.FAILED,
    EventType.ABORTED: RequestRecordState.ABORTED,
    EventType.UNKNOWN: RequestRecordState.UNKNOWN,
}


class RequestRecordUpsertOutcome(StrEnum):
    APPLIED = "applied"
    IGNORED = "ignored"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class RequestRecordUpsertResult:
    request_id: UUID
    event_seq: int
    event_type: EventType
    outcome: RequestRecordUpsertOutcome
    registered_received_at: datetime | None = None


def _month_bounds(value: datetime) -> tuple[datetime, datetime]:
    start = as_utc(value).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def _partition_statement(start: datetime, end: datetime) -> sa.TextClause:
    del end  # The DDL-owner function derives the verified next month.
    return sa.text(
        "SELECT core.holm_ensure_month_partition('usage.request_records', :month_start)"
    ).bindparams(month_start=start.date())


async def ensure_request_record_partitions(
    engine: AsyncEngine,
    received_at_values: Iterable[datetime],
) -> tuple[str, ...]:
    """Create each required UTC month partition in one independent short transaction."""

    bounds = sorted({_month_bounds(value) for value in received_at_values})
    if not bounds:
        return ()
    async with engine.begin() as connection:
        for start, end in bounds:
            await connection.execute(_partition_statement(start, end))
    return tuple(f"request_records_{start.year:04d}{start.month:02d}" for start, _ in bounds)


async def drop_expired_request_record_partitions(
    engine: AsyncEngine,
    *,
    retention_days: int,
    now: datetime,
) -> tuple[str, ...]:
    """Drop only managed month partitions wholly older than the retention cutoff."""

    if isinstance(retention_days, bool) or retention_days <= 0:
        raise ValueError("Request Record retention days must be positive")
    cutoff = as_utc(now) - timedelta(days=retention_days)
    async with engine.begin() as connection:
        dropped = await connection.scalar(
            sa.text(
                "SELECT core.holm_drop_expired_month_partitions('usage.request_records', :cutoff)"
            ),
            {"cutoff": cutoff},
        )
    return tuple(dropped or ())


def _metering_values(
    event: CompletedEvent | FailedEvent | AbortedEvent,
) -> dict[str, object | None]:
    """Project the metering provenance without inventing a default."""

    return {
        "metering_source": (None if event.metering_source is None else event.metering_source.value),
        "metrics_capability": (
            None if event.metrics_capability is None else event.metrics_capability.value
        ),
    }


def _event_values(event: LedgerEvent) -> dict[str, object]:
    values: dict[str, object] = {
        "received_at": event.received_at,
        "request_id": event.request_id,
        "event_seq": event.event_seq,
        "record_state": _STATE_BY_EVENT[event.event_type].value,
        "updated_at": event.created_at,
    }
    if event.event_type in _TERMINAL_TYPES:
        values["terminal_at"] = event.created_at

    event_values: dict[str, object | None]
    if isinstance(event, ReceivedEvent):
        event_values = {
            "protocol": event.protocol.value,
            "operation": event.operation.value,
            "endpoint": event.endpoint,
            "stream": event.stream,
            "input_bytes": event.input_bytes,
        }
    elif isinstance(event, RejectedEvent):
        event_values = {
            "error_code": event.error_code.value,
            "http_status": event.http_status,
            "user_id": event.user_id,
            "api_key_id": event.api_key_id,
            "service_id": event.service_id,
            "instance_id": event.instance_id,
        }
    elif isinstance(event, InProgressEvent):
        event_values = {
            "user_id": event.user_id,
            "api_key_id": event.api_key_id,
            "service_id": event.service_id,
            "instance_id": event.instance_id,
            "requested_max_tokens": event.requested_max_tokens,
        }
    elif isinstance(event, CompletedEvent):
        event_values = {
            "input_tokens": event.input_tokens,
            "output_tokens": event.output_tokens,
            "ttft_ms": event.ttft_ms,
            "duration_ms": event.duration_ms,
            "finish_reason": (None if event.finish_reason is None else event.finish_reason.value),
            "http_status": event.http_status,
            **_metering_values(event),
        }
    elif isinstance(event, FailedEvent):
        event_values = {
            "input_tokens": event.input_tokens,
            "output_tokens": event.output_tokens,
            "ttft_ms": event.ttft_ms,
            "duration_ms": event.duration_ms,
            "error_code": event.error_code.value,
            "http_status": event.http_status,
            **_metering_values(event),
        }
    elif isinstance(event, AbortedEvent):
        event_values = {
            "input_tokens": event.input_tokens,
            "output_tokens": event.output_tokens,
            "duration_ms": event.duration_ms,
            "client_disconnected": event.client_disconnected,
            "user_id": event.user_id,
            "api_key_id": event.api_key_id,
            "service_id": event.service_id,
            "instance_id": event.instance_id,
            **_metering_values(event),
        }
    else:
        if not isinstance(event, UnknownEvent):  # pragma: no cover - closed union guard
            raise TypeError("unsupported Ledger event")
        event_values = {
            "last_confirmed_event_type": event.last_confirmed_event_type.value,
            "last_confirmed_event_seq": event.last_confirmed_event_seq,
            "user_id": event.user_id,
            "api_key_id": event.api_key_id,
            "service_id": event.service_id,
            "instance_id": event.instance_id,
            "input_tokens": event.input_tokens,
            "output_tokens": event.output_tokens,
        }
    values.update({key: value for key, value in event_values.items() if value is not None})
    return values


class RequestRecordLedgerRepository:
    """Replay events without committing the caller-owned PostgreSQL transaction."""

    __slots__ = ("_partition_engine", "_session")

    def __init__(self, session: AsyncSession, partition_engine: AsyncEngine) -> None:
        self._session = session
        self._partition_engine = partition_engine

    async def upsert_batch(
        self, events: Iterable[LedgerEvent]
    ) -> tuple[RequestRecordUpsertResult, ...]:
        pending = tuple(events)
        if not pending:
            return ()
        await ensure_request_record_partitions(
            self._partition_engine,
            (event.received_at for event in pending),
        )
        results: list[RequestRecordUpsertResult] = []
        for event in pending:
            results.append(await self._upsert_one(event))
        return tuple(results)

    async def _upsert_one(self, event: LedgerEvent) -> RequestRecordUpsertResult:
        registry_insert = postgresql_insert(REQUEST_ID_REGISTRY).values(
            request_id=event.request_id,
            received_at=event.received_at,
            highest_event_seq=event.event_seq,
        )
        registry_statement = registry_insert.on_conflict_do_update(
            index_elements=[REQUEST_ID_REGISTRY.c.request_id],
            set_={
                "highest_event_seq": sa.func.greatest(
                    REQUEST_ID_REGISTRY.c.highest_event_seq,
                    registry_insert.excluded.highest_event_seq,
                )
            },
            where=(REQUEST_ID_REGISTRY.c.received_at == registry_insert.excluded.received_at),
        ).returning(REQUEST_ID_REGISTRY.c.received_at)
        registered_at = (await self._session.execute(registry_statement)).scalar_one_or_none()
        if registered_at is None:
            registered_at = (
                await self._session.execute(
                    sa.select(REQUEST_ID_REGISTRY.c.received_at).where(
                        REQUEST_ID_REGISTRY.c.request_id == event.request_id
                    )
                )
            ).scalar_one()
            _LOGGER.critical(
                "Request ID received_at partition conflict",
                extra={
                    "request_id": str(event.request_id),
                    "event_seq": event.event_seq,
                    "event_type": event.event_type.value,
                },
            )
            return RequestRecordUpsertResult(
                event.request_id,
                event.event_seq,
                event.event_type,
                RequestRecordUpsertOutcome.CONFLICT,
                as_utc(registered_at),
            )

        values = _event_values(event)
        record_insert = postgresql_insert(LEDGER_REQUEST_RECORDS).values(**values)
        update_values = {
            key: getattr(record_insert.excluded, key)
            for key in values
            if key not in {"received_at", "request_id"}
        }
        record_statement = record_insert.on_conflict_do_update(
            index_elements=[
                LEDGER_REQUEST_RECORDS.c.received_at,
                LEDGER_REQUEST_RECORDS.c.request_id,
            ],
            set_=update_values,
            where=(LEDGER_REQUEST_RECORDS.c.event_seq < record_insert.excluded.event_seq),
        ).returning(LEDGER_REQUEST_RECORDS.c.event_seq)
        applied_seq = (await self._session.execute(record_statement)).scalar_one_or_none()
        outcome = (
            RequestRecordUpsertOutcome.APPLIED
            if applied_seq is not None
            else RequestRecordUpsertOutcome.IGNORED
        )
        return RequestRecordUpsertResult(
            event.request_id,
            event.event_seq,
            event.event_type,
            outcome,
            as_utc(registered_at),
        )


@dataclass(frozen=True, slots=True)
class UnknownRequestSummary:
    """Aggregate only: no request identifiers or request content leave the query."""

    count: int
    latest_received_at: datetime | None


async def summarize_unknown_request_records(
    session: AsyncSession, *, since: datetime
) -> UnknownRequestSummary:
    """Count Unknown Request Records received at or after ``since``."""

    records = LEDGER_REQUEST_RECORDS.c
    row = (
        await session.execute(
            sa.select(sa.func.count(), sa.func.max(records.received_at)).where(
                records.record_state == RequestRecordState.UNKNOWN.value,
                records.received_at >= since,
            )
        )
    ).one()
    count = int(row[0])
    latest = row[1]
    return UnknownRequestSummary(
        count=count,
        latest_received_at=None if latest is None else as_utc(latest),
    )


__all__ = [
    "RequestRecordLedgerRepository",
    "RequestRecordUpsertOutcome",
    "RequestRecordUpsertResult",
    "UnknownRequestSummary",
    "drop_expired_request_record_partitions",
    "ensure_request_record_partitions",
    "summarize_unknown_request_records",
]

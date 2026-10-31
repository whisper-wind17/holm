"""Query the closed Request Record metadata projection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from holm_common import as_utc
from holm_domain import (
    ErrorCode,
    RequestFinishReason,
    RequestRecordState,
)
from sqlalchemy.ext.asyncio import AsyncSession

from holm_persistence.accounts import USERS
from holm_persistence.ai_services import AI_SERVICES, API_KEYS
from holm_persistence.ledger_request_records import LEDGER_REQUEST_RECORDS


@dataclass(frozen=True, slots=True)
class RequestRecordPosition:
    received_at: datetime
    request_id: UUID


@dataclass(frozen=True, slots=True)
class RequestRecordFilters:
    service_id: UUID | None = None
    user_id: UUID | None = None
    api_key_id: UUID | None = None
    status: RequestRecordState | None = None
    error_code: ErrorCode | None = None
    request_id: UUID | None = None
    from_time: datetime | None = None
    to_time: datetime | None = None


@dataclass(frozen=True, slots=True)
class RequestRecordListItem:
    request_id: UUID
    received_at: datetime
    completed_at: datetime | None
    user_id: UUID | None
    username: str | None
    api_key_id: UUID | None
    api_key_name: str | None
    api_key_display_prefix: str | None
    api_key_last4: str | None
    service_id: UUID | None
    public_model_name: str | None
    instance_id: UUID | None
    stream: bool | None
    status: RequestRecordState
    http_status: int | None
    error_code: ErrorCode | None
    input_tokens: int | None
    output_tokens: int | None
    ttft_ms: int | None
    duration_ms: int | None
    finish_reason: RequestFinishReason | None


@dataclass(frozen=True, slots=True)
class RequestRecordErrorGroup:
    status: RequestRecordState
    error_code: ErrorCode | None
    count: int
    first_at: datetime
    last_at: datetime
    latest_request_id: UUID


class RequestRecordRepository:
    """Query the Usage ledger projection."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list(
        self,
        *,
        filters: RequestRecordFilters,
        after: RequestRecordPosition | None,
        limit: int,
    ) -> tuple[RequestRecordListItem, ...]:
        if limit < 1:
            raise ValueError("request record page limit must be positive")
        statement = (
            sa.select(
                LEDGER_REQUEST_RECORDS,
                USERS.c.username_norm.label("username"),
                API_KEYS.c.name.label("api_key_name"),
                API_KEYS.c.display_prefix.label("api_key_display_prefix"),
                API_KEYS.c.last4.label("api_key_last4"),
                AI_SERVICES.c.public_model_name.label("public_model_name"),
            )
            .select_from(
                LEDGER_REQUEST_RECORDS.outerjoin(
                    USERS,
                    LEDGER_REQUEST_RECORDS.c.user_id == USERS.c.id,
                )
                .outerjoin(
                    API_KEYS,
                    LEDGER_REQUEST_RECORDS.c.api_key_id == API_KEYS.c.id,
                )
                .outerjoin(
                    AI_SERVICES,
                    LEDGER_REQUEST_RECORDS.c.service_id == AI_SERVICES.c.id,
                )
            )
            .order_by(
                LEDGER_REQUEST_RECORDS.c.received_at.desc(),
                LEDGER_REQUEST_RECORDS.c.request_id.desc(),
            )
            .limit(limit)
        )
        clauses: list[sa.ColumnElement[bool]] = []
        if filters.service_id is not None:
            clauses.append(LEDGER_REQUEST_RECORDS.c.service_id == filters.service_id)
        if filters.user_id is not None:
            clauses.append(LEDGER_REQUEST_RECORDS.c.user_id == filters.user_id)
        if filters.api_key_id is not None:
            clauses.append(LEDGER_REQUEST_RECORDS.c.api_key_id == filters.api_key_id)
        if filters.status is not None:
            clauses.append(LEDGER_REQUEST_RECORDS.c.record_state == filters.status.value)
        if filters.error_code is not None:
            clauses.append(LEDGER_REQUEST_RECORDS.c.error_code == filters.error_code.value)
        if filters.request_id is not None:
            clauses.append(LEDGER_REQUEST_RECORDS.c.request_id == filters.request_id)
        if filters.from_time is not None:
            clauses.append(LEDGER_REQUEST_RECORDS.c.received_at >= filters.from_time)
        if filters.to_time is not None:
            clauses.append(LEDGER_REQUEST_RECORDS.c.received_at <= filters.to_time)
        if after is not None:
            clauses.append(
                sa.or_(
                    LEDGER_REQUEST_RECORDS.c.received_at < after.received_at,
                    sa.and_(
                        LEDGER_REQUEST_RECORDS.c.received_at == after.received_at,
                        LEDGER_REQUEST_RECORDS.c.request_id < after.request_id,
                    ),
                )
            )
        if clauses:
            statement = statement.where(*clauses)
        rows = (await self._session.execute(statement)).mappings().all()
        return tuple(_list_item(row) for row in rows)

    async def error_groups(
        self,
        *,
        from_time: datetime,
        to_time: datetime | None,
        service_id: UUID | None,
    ) -> tuple[RequestRecordErrorGroup, ...]:
        records = LEDGER_REQUEST_RECORDS.c
        clauses = [
            records.received_at >= from_time,
            records.record_state.in_(
                [
                    state.value
                    for state in (
                        RequestRecordState.FAILED,
                        RequestRecordState.REJECTED,
                        RequestRecordState.UNKNOWN,
                    )
                ]
            ),
        ]
        if to_time is not None:
            clauses.append(records.received_at <= to_time)
        if service_id is not None:
            clauses.append(records.service_id == service_id)
        # Aggregate the entire window before limiting groups. The correlated lookup
        # selects only one metadata identifier per group, including NULL error codes.
        groups = (
            sa.select(
                records.record_state.label("status"),
                records.error_code,
                sa.func.count().label("count"),
                sa.func.min(records.received_at).label("first_at"),
                sa.func.max(records.received_at).label("last_at"),
            )
            .where(*clauses)
            .group_by(records.record_state, records.error_code)
            .order_by(
                sa.func.max(records.received_at).desc(), records.record_state, records.error_code
            )
            .limit(5)
            .subquery()
        )
        latest = (
            sa.select(records.request_id)
            .where(
                *clauses,
                records.record_state == groups.c.status,
                records.error_code.is_not_distinct_from(groups.c.error_code),
            )
            .order_by(records.received_at.desc(), records.request_id.desc())
            .limit(1)
            .correlate(groups)
            .scalar_subquery()
        )
        statement = sa.select(groups, latest.label("latest_request_id")).order_by(
            groups.c.last_at.desc(), groups.c.status, groups.c.error_code
        )
        rows = (await self._session.execute(statement)).mappings().all()
        return tuple(
            RequestRecordErrorGroup(
                status=RequestRecordState(row["status"]),
                error_code=None if row["error_code"] is None else ErrorCode(row["error_code"]),
                count=int(row["count"]),
                first_at=as_utc(row["first_at"]),
                last_at=as_utc(row["last_at"]),
                latest_request_id=row["latest_request_id"],
            )
            for row in rows
        )


def _list_item(row: sa.RowMapping) -> RequestRecordListItem:
    return RequestRecordListItem(
        request_id=row["request_id"],
        received_at=as_utc(row["received_at"]),
        completed_at=None if row["terminal_at"] is None else as_utc(row["terminal_at"]),
        user_id=row["user_id"],
        username=None if row["username"] is None else str(row["username"]),
        api_key_id=row["api_key_id"],
        api_key_name=row["api_key_name"],
        api_key_display_prefix=(
            None if row["api_key_display_prefix"] is None else str(row["api_key_display_prefix"])
        ),
        api_key_last4=None if row["api_key_last4"] is None else str(row["api_key_last4"]),
        service_id=row["service_id"],
        public_model_name=(
            None if row["public_model_name"] is None else str(row["public_model_name"])
        ),
        instance_id=row["instance_id"],
        stream=None if row["stream"] is None else bool(row["stream"]),
        status=RequestRecordState(row["record_state"]),
        http_status=None if row["http_status"] is None else int(row["http_status"]),
        error_code=None if row["error_code"] is None else ErrorCode(row["error_code"]),
        input_tokens=None if row["input_tokens"] is None else int(row["input_tokens"]),
        output_tokens=None if row["output_tokens"] is None else int(row["output_tokens"]),
        ttft_ms=None if row["ttft_ms"] is None else int(row["ttft_ms"]),
        duration_ms=None if row["duration_ms"] is None else int(row["duration_ms"]),
        finish_reason=(
            None if row["finish_reason"] is None else RequestFinishReason(row["finish_reason"])
        ),
    )


__all__ = [
    "RequestRecordErrorGroup",
    "RequestRecordFilters",
    "RequestRecordListItem",
    "RequestRecordPosition",
    "RequestRecordRepository",
]

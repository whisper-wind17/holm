"""Usage aggregation over the Request Ledger projection and UTC buckets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final
from uuid import UUID

import sqlalchemy as sa
from holm_common import as_utc
from holm_domain import ApiKeyStatus, MetricsCapability, RequestRecordState
from holm_ledger import EventType
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from holm_persistence.accounts import USERS
from holm_persistence.ai_services import (
    AI_SERVICES,
    API_KEYS,
    USER_SERVICE_PERMISSIONS,
)
from holm_persistence.ledger_request_records import LEDGER_REQUEST_RECORDS
from holm_persistence.usage_buckets import USAGE_15M, USAGE_BUCKET_SECONDS

USAGE_BUCKET_LOCK_CLASS_ID: Final[int] = 1_347_895_110
_BUCKET_WIDTH: Final[timedelta] = timedelta(seconds=USAGE_BUCKET_SECONDS)
_DIRECTLY_COUNTED_STATES: Final[tuple[str, ...]] = (
    RequestRecordState.IN_PROGRESS.value,
    RequestRecordState.COMPLETED.value,
    RequestRecordState.FAILED.value,
    RequestRecordState.ABORTED.value,
)


class UsageGroupBy(StrEnum):
    SERVICE = "service"
    USER = "user"
    API_KEY = "api_key"


class UsageBucketWidth(StrEnum):
    HOUR = "hour"
    DAY = "day"


@dataclass(frozen=True, slots=True)
class UsageFilters:
    from_time: datetime | None = None
    to_time: datetime | None = None
    service_id: UUID | None = None
    user_id: UUID | None = None
    api_key_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class UsageTotals:
    requests: int
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    error_requests: int
    aborted_requests: int
    partial_token_requests: int
    error_rate: float | None


@dataclass(frozen=True, slots=True)
class UsageGroup:
    group_id: UUID | None
    display_name: str
    status: ApiKeyStatus | None
    totals: UsageTotals
    is_deleted: bool = False
    api_key_name: str | None = None
    owner_username: str | None = None
    owner_is_deleted: bool | None = None


@dataclass(frozen=True, slots=True)
class UsageTrendPoint:
    bucket_start: datetime
    requests: int
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None


def usage_bucket_start(value: datetime) -> datetime:
    """Floor an aware timestamp to its containing UTC 15-minute bucket."""

    utc_value = as_utc(value)
    return utc_value.replace(
        minute=(utc_value.minute // 15) * 15,
        second=0,
        microsecond=0,
    )


def _next_usage_bucket(value: datetime) -> datetime:
    start = usage_bucket_start(value)
    return start if value == start else start + _BUCKET_WIDTH


def _counted_request_clause() -> sa.ColumnElement[bool]:
    records = LEDGER_REQUEST_RECORDS.c
    return sa.or_(
        records.record_state.in_(_DIRECTLY_COUNTED_STATES),
        sa.and_(
            records.record_state == RequestRecordState.UNKNOWN.value,
            records.last_confirmed_event_type == EventType.IN_PROGRESS.value,
        ),
    )


def _filter_clauses(filters: UsageFilters) -> list[sa.ColumnElement[bool]]:
    records = LEDGER_REQUEST_RECORDS.c
    clauses: list[sa.ColumnElement[bool]] = []
    if filters.from_time is not None:
        clauses.append(records.received_at >= as_utc(filters.from_time))
    if filters.to_time is not None:
        clauses.append(records.received_at < as_utc(filters.to_time))
    if filters.service_id is not None:
        clauses.append(records.service_id == filters.service_id)
    if filters.user_id is not None:
        clauses.append(records.user_id == filters.user_id)
    if filters.api_key_id is not None:
        clauses.append(records.api_key_id == filters.api_key_id)
    return clauses


def _accepted_clauses(filters: UsageFilters) -> list[sa.ColumnElement[bool]]:
    records = LEDGER_REQUEST_RECORDS.c
    return [
        *_filter_clauses(filters),
        _counted_request_clause(),
        records.user_id.is_not(None),
        records.api_key_id.is_not(None),
        records.service_id.is_not(None),
    ]


def _record_metric_columns() -> tuple[sa.ColumnElement[Any], ...]:
    records = LEDGER_REQUEST_RECORDS.c
    input_sum = sa.func.sum(records.input_tokens)
    output_sum = sa.func.sum(records.output_tokens)
    confirmed_token_values = sa.func.count(records.input_tokens) + sa.func.count(
        records.output_tokens
    )
    total_tokens = sa.case(
        (confirmed_token_values == 0, sa.null()),
        else_=sa.func.coalesce(input_sum, 0) + sa.func.coalesce(output_sum, 0),
    )
    return (
        sa.func.count().label("requests"),
        input_sum.label("input_tokens"),
        output_sum.label("output_tokens"),
        total_tokens.label("total_tokens"),
        sa.func.count()
        .filter(records.record_state == RequestRecordState.FAILED.value)
        .label("error_requests"),
        sa.func.count()
        .filter(records.record_state == RequestRecordState.ABORTED.value)
        .label("aborted_requests"),
        sa.func.count()
        .filter(records.metrics_capability == MetricsCapability.PARTIAL.value)
        .label("partial_token_requests"),
    )


def _bucket_filter_clauses(filters: UsageFilters) -> list[sa.ColumnElement[bool]]:
    buckets = USAGE_15M.c
    clauses: list[sa.ColumnElement[bool]] = []
    if filters.service_id is not None:
        clauses.append(buckets.service_id == filters.service_id)
    if filters.user_id is not None:
        clauses.append(buckets.user_id == filters.user_id)
    if filters.api_key_id is not None:
        clauses.append(buckets.api_key_id == filters.api_key_id)
    return clauses


def _bucket_metric_columns() -> tuple[sa.ColumnElement[Any], ...]:
    buckets = USAGE_15M.c
    input_sum = sa.func.sum(buckets.input_tokens)
    output_sum = sa.func.sum(buckets.output_tokens)
    confirmed_token_values = sa.func.count(buckets.input_tokens) + sa.func.count(
        buckets.output_tokens
    )
    total_tokens = sa.case(
        (confirmed_token_values == 0, sa.null()),
        else_=sa.func.coalesce(input_sum, 0) + sa.func.coalesce(output_sum, 0),
    )
    return (
        sa.func.coalesce(sa.func.sum(buckets.requests), 0).label("requests"),
        input_sum.label("input_tokens"),
        output_sum.label("output_tokens"),
        total_tokens.label("total_tokens"),
        sa.func.coalesce(sa.func.sum(buckets.error_requests), 0).label("error_requests"),
        sa.func.coalesce(sa.func.sum(buckets.aborted_requests), 0).label("aborted_requests"),
        sa.func.coalesce(sa.func.sum(buckets.partial_token_requests), 0).label(
            "partial_token_requests"
        ),
    )


def _totals(row: sa.RowMapping) -> UsageTotals:
    request_count = int(row["requests"])
    error_requests = int(row["error_requests"])
    return UsageTotals(
        requests=request_count,
        input_tokens=None if row["input_tokens"] is None else int(row["input_tokens"]),
        output_tokens=None if row["output_tokens"] is None else int(row["output_tokens"]),
        total_tokens=None if row["total_tokens"] is None else int(row["total_tokens"]),
        error_requests=error_requests,
        aborted_requests=int(row["aborted_requests"]),
        partial_token_requests=int(row["partial_token_requests"]),
        error_rate=None if request_count == 0 else error_requests / request_count,
    )


def _merge_totals(parts: tuple[UsageTotals, ...]) -> UsageTotals:
    requests = sum(part.requests for part in parts)
    error_requests = sum(part.error_requests for part in parts)

    def token_total(name: str) -> int | None:
        values = [getattr(part, name) for part in parts]
        confirmed = [value for value in values if value is not None]
        return None if not confirmed else sum(confirmed)

    input_tokens = token_total("input_tokens")
    output_tokens = token_total("output_tokens")
    token_values = [value for value in (input_tokens, output_tokens) if value is not None]
    return UsageTotals(
        requests=requests,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=None if not token_values else sum(token_values),
        error_requests=error_requests,
        aborted_requests=sum(part.aborted_requests for part in parts),
        partial_token_requests=sum(part.partial_token_requests for part in parts),
        error_rate=None if requests == 0 else error_requests / requests,
    )


def _bucketed_window_parts(
    filters: UsageFilters, *, now: datetime
) -> tuple[datetime, datetime, list[tuple[datetime, datetime]]]:
    if filters.from_time is None or filters.to_time is None:
        raise ValueError("bucketed Usage windows require both boundaries")
    from_time = as_utc(filters.from_time)
    to_time = as_utc(filters.to_time)
    current_start = usage_bucket_start(now)
    closed_start = _next_usage_bucket(from_time)
    closed_end = min(usage_bucket_start(to_time), current_start)
    live_intervals: list[tuple[datetime, datetime]] = []
    live_start = max(from_time, current_start)
    live_end = min(to_time, as_utc(now))
    if live_start < live_end:
        live_intervals.append((live_start, live_end))

    right_bucket_start = usage_bucket_start(to_time)
    if to_time < current_start and right_bucket_start < to_time:
        right_start = max(from_time, right_bucket_start)
        if right_start < to_time:
            live_intervals.append((right_start, to_time))

    return closed_start, closed_end, live_intervals


class UsageRepository:
    """Read Usage and idempotently materialize recent UTC buckets."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def recompute_recent_buckets(self, *, now: datetime) -> int:
        current = usage_bucket_start(now)
        starts = (current - _BUCKET_WIDTH, current)
        processed = 0
        for bucket_start in starts:
            lock_object_id = int(bucket_start.timestamp()) // USAGE_BUCKET_SECONDS
            locked = await self._session.scalar(
                sa.select(
                    sa.func.pg_try_advisory_xact_lock(
                        USAGE_BUCKET_LOCK_CLASS_ID,
                        lock_object_id,
                    )
                )
            )
            if not locked:
                continue
            await self._recompute_bucket(bucket_start)
            processed += 1
        return processed

    async def _recompute_bucket(self, bucket_start: datetime) -> None:
        records = LEDGER_REQUEST_RECORDS.c
        bucket_end = bucket_start + _BUCKET_WIDTH
        filters = UsageFilters(from_time=bucket_start, to_time=bucket_end)
        rows = (
            (
                await self._session.execute(
                    sa.select(
                        records.user_id,
                        records.api_key_id,
                        records.service_id,
                        *_record_metric_columns(),
                    )
                    .where(*_accepted_clauses(filters))
                    .group_by(records.user_id, records.api_key_id, records.service_id)
                )
            )
            .mappings()
            .all()
        )

        identities = {(row["user_id"], row["api_key_id"], row["service_id"]) for row in rows}
        existing = (
            await self._session.execute(
                sa.select(
                    USAGE_15M.c.user_id,
                    USAGE_15M.c.api_key_id,
                    USAGE_15M.c.service_id,
                ).where(USAGE_15M.c.bucket_start == bucket_start)
            )
        ).all()
        for identity in existing:
            if tuple(identity) in identities:
                continue
            await self._session.execute(
                sa.delete(USAGE_15M).where(
                    USAGE_15M.c.bucket_start == bucket_start,
                    USAGE_15M.c.user_id == identity.user_id,
                    USAGE_15M.c.api_key_id == identity.api_key_id,
                    USAGE_15M.c.service_id == identity.service_id,
                )
            )

        metric_names = (
            "requests",
            "input_tokens",
            "output_tokens",
            "error_requests",
            "aborted_requests",
            "partial_token_requests",
        )
        for row in rows:
            values = {
                "bucket_start": bucket_start,
                "user_id": row["user_id"],
                "api_key_id": row["api_key_id"],
                "service_id": row["service_id"],
                **{name: row[name] for name in metric_names},
                "updated_at": sa.func.clock_timestamp(),
            }
            insert = postgresql_insert(USAGE_15M).values(**values)
            changed = sa.or_(
                *(
                    USAGE_15M.c[name].is_distinct_from(getattr(insert.excluded, name))
                    for name in metric_names
                )
            )
            await self._session.execute(
                insert.on_conflict_do_update(
                    index_elements=[
                        USAGE_15M.c.bucket_start,
                        USAGE_15M.c.user_id,
                        USAGE_15M.c.api_key_id,
                        USAGE_15M.c.service_id,
                    ],
                    set_={
                        **{name: getattr(insert.excluded, name) for name in metric_names},
                        "updated_at": sa.func.clock_timestamp(),
                    },
                    where=changed,
                )
            )

    async def _summarize_records(self, filters: UsageFilters) -> UsageTotals:
        statement = sa.select(*_record_metric_columns()).where(*_accepted_clauses(filters))
        row = (await self._session.execute(statement)).mappings().one()
        return _totals(row)

    async def summarize(self, filters: UsageFilters) -> UsageTotals:
        return await self._summarize_records(filters)

    async def summarize_bucketed_window(
        self,
        filters: UsageFilters,
        *,
        now: datetime,
    ) -> UsageTotals:
        """Combine complete closed buckets with only the allowed live edge buckets."""

        closed_start, closed_end, live_intervals = _bucketed_window_parts(filters, now=now)
        parts: list[UsageTotals] = []
        if closed_start < closed_end:
            row = (
                (
                    await self._session.execute(
                        sa.select(*_bucket_metric_columns()).where(
                            USAGE_15M.c.bucket_start >= closed_start,
                            USAGE_15M.c.bucket_start < closed_end,
                            *_bucket_filter_clauses(filters),
                        )
                    )
                )
                .mappings()
                .one()
            )
            parts.append(_totals(row))

        for interval_start, interval_end in live_intervals:
            parts.append(
                await self._summarize_records(
                    UsageFilters(
                        from_time=interval_start,
                        to_time=interval_end,
                        service_id=filters.service_id,
                        user_id=filters.user_id,
                        api_key_id=filters.api_key_id,
                    )
                )
            )
        return _merge_totals(tuple(parts))

    async def active_users_bucketed_window(self, filters: UsageFilters, *, now: datetime) -> int:
        """Distinct users across exactly the same buckets and live edges as Requests."""
        closed_start, closed_end, live_intervals = _bucketed_window_parts(filters, now=now)
        users = sa.select(USAGE_15M.c.user_id).where(
            USAGE_15M.c.bucket_start >= closed_start,
            USAGE_15M.c.bucket_start < closed_end,
            USAGE_15M.c.requests > 0,
            USAGE_15M.c.user_id.is_not(None),
            *_bucket_filter_clauses(filters),
        )
        parts = [users]
        for start, end in live_intervals:
            parts.append(
                sa.select(LEDGER_REQUEST_RECORDS.c.user_id).where(
                    *_accepted_clauses(
                        UsageFilters(
                            from_time=start,
                            to_time=end,
                            service_id=filters.service_id,
                            user_id=filters.user_id,
                            api_key_id=filters.api_key_id,
                        )
                    )
                )
            )
        distinct_users = sa.union(*parts).subquery()
        return int(
            await self._session.scalar(sa.select(sa.func.count()).select_from(distinct_users)) or 0
        )

    async def group(
        self,
        filters: UsageFilters,
        *,
        group_by: UsageGroupBy,
    ) -> tuple[UsageGroup, ...]:
        records = LEDGER_REQUEST_RECORDS.c
        group_id: sa.ColumnElement[Any]
        display_name: sa.ColumnElement[Any]
        status: sa.ColumnElement[Any]
        is_deleted: sa.ColumnElement[bool]
        api_key_name: sa.ColumnElement[Any] = sa.null()
        owner_username: sa.ColumnElement[Any] = sa.null()
        owner_is_deleted: sa.ColumnElement[Any] = sa.null()
        source: sa.FromClause
        grouping: tuple[sa.ColumnElement[Any], ...]
        if group_by is UsageGroupBy.SERVICE:
            group_id = records.service_id
            display_name = sa.func.coalesce(
                AI_SERVICES.c.public_model_name,
                sa.literal("已删除的 AI 服务"),
            )
            status = sa.null()
            is_deleted = AI_SERVICES.c.deleted_at.is_not(None)
            grouping = (group_id, display_name, is_deleted)
            source = LEDGER_REQUEST_RECORDS.outerjoin(
                AI_SERVICES,
                records.service_id == AI_SERVICES.c.id,
            )
        elif group_by is UsageGroupBy.USER:
            group_id = records.user_id
            display_name = sa.func.coalesce(USERS.c.username_norm, sa.literal("未知用户"))
            status = sa.null()
            is_deleted = USERS.c.deleted_at.is_not(None)
            grouping = (group_id, display_name, is_deleted)
            source = LEDGER_REQUEST_RECORDS.outerjoin(
                USERS,
                records.user_id == USERS.c.id,
            )
        else:
            group_id = records.api_key_id
            display_name = sa.func.coalesce(
                API_KEYS.c.display_prefix + sa.literal("••••") + API_KEYS.c.last4,
                sa.literal("未知 API 密钥"),
            )
            status = sa.case(
                (
                    sa.and_(
                        API_KEYS.c.status == ApiKeyStatus.ACTIVE.value,
                        API_KEYS.c.expires_at.is_not(None),
                        API_KEYS.c.expires_at <= sa.func.clock_timestamp(),
                    ),
                    ApiKeyStatus.EXPIRED.value,
                ),
                else_=API_KEYS.c.status,
            )
            source = LEDGER_REQUEST_RECORDS.outerjoin(
                API_KEYS,
                records.api_key_id == API_KEYS.c.id,
            ).outerjoin(USERS, API_KEYS.c.user_id == USERS.c.id)
            is_deleted = API_KEYS.c.deleted_at.is_not(None)
            api_key_name = API_KEYS.c.name
            owner_username = USERS.c.username_norm
            owner_is_deleted = USERS.c.deleted_at.is_not(None)
            grouping = (
                group_id,
                display_name,
                status,
                is_deleted,
                api_key_name,
                owner_username,
                owner_is_deleted,
            )

        statement = (
            sa.select(
                group_id.label("group_id"),
                display_name.label("display_name"),
                status.label("status"),
                is_deleted.label("is_deleted"),
                api_key_name.label("api_key_name"),
                owner_username.label("owner_username"),
                owner_is_deleted.label("owner_is_deleted"),
                *_record_metric_columns(),
            )
            .select_from(source)
            .where(*_accepted_clauses(filters))
            .group_by(*grouping)
            .order_by(sa.desc("requests"), display_name)
        )
        rows = (await self._session.execute(statement)).mappings().all()
        return tuple(
            UsageGroup(
                group_id=row["group_id"],
                display_name=str(row["display_name"]),
                status=None if row["status"] is None else ApiKeyStatus(row["status"]),
                totals=_totals(row),
                is_deleted=bool(row["is_deleted"]),
                api_key_name=row["api_key_name"],
                owner_username=row["owner_username"],
                owner_is_deleted=row["owner_is_deleted"],
            )
            for row in rows
        )

    async def trend(
        self,
        filters: UsageFilters,
        *,
        bucket_width: UsageBucketWidth,
        time_zone: str,
    ) -> tuple[UsageTrendPoint, ...]:
        records = LEDGER_REQUEST_RECORDS.c
        local_received_at = sa.func.timezone(time_zone, records.received_at)
        local_bucket = sa.func.date_trunc(bucket_width.value, local_received_at)
        bucket_start = sa.func.timezone(time_zone, local_bucket).label("bucket_start")
        input_sum = sa.func.sum(records.input_tokens)
        output_sum = sa.func.sum(records.output_tokens)
        confirmed_token_values = sa.func.count(records.input_tokens) + sa.func.count(
            records.output_tokens
        )
        total_tokens = sa.case(
            (confirmed_token_values == 0, sa.null()),
            else_=sa.func.coalesce(input_sum, 0) + sa.func.coalesce(output_sum, 0),
        )
        statement = (
            sa.select(
                bucket_start,
                sa.func.count().label("requests"),
                input_sum.label("input_tokens"),
                output_sum.label("output_tokens"),
                total_tokens.label("total_tokens"),
            )
            .where(*_accepted_clauses(filters))
            .group_by(bucket_start)
            .order_by(bucket_start)
        )
        rows = (await self._session.execute(statement)).mappings().all()
        return tuple(
            UsageTrendPoint(
                bucket_start=as_utc(row["bucket_start"]),
                requests=int(row["requests"]),
                input_tokens=(None if row["input_tokens"] is None else int(row["input_tokens"])),
                output_tokens=(None if row["output_tokens"] is None else int(row["output_tokens"])),
                total_tokens=None if row["total_tokens"] is None else int(row["total_tokens"]),
            )
            for row in rows
        )

    async def latest_completed_at(self, filters: UsageFilters) -> datetime | None:
        value = await self._session.scalar(
            sa.select(sa.func.max(LEDGER_REQUEST_RECORDS.c.terminal_at)).where(
                *_filter_clauses(filters)
            )
        )
        return None if value is None else as_utc(value)

    async def service_is_authorized(self, *, user_id: UUID, service_id: UUID) -> bool:
        return bool(
            await self._session.scalar(
                sa.select(
                    sa.exists().where(
                        USER_SERVICE_PERMISSIONS.c.user_id == user_id,
                        USER_SERVICE_PERMISSIONS.c.service_id == service_id,
                    )
                )
            )
        )

    async def api_key_is_owned(self, *, user_id: UUID, api_key_id: UUID) -> bool:
        return bool(
            await self._session.scalar(
                sa.select(
                    sa.exists().where(
                        API_KEYS.c.user_id == user_id,
                        API_KEYS.c.id == api_key_id,
                    )
                )
            )
        )


__all__ = [
    "USAGE_BUCKET_LOCK_CLASS_ID",
    "UsageBucketWidth",
    "UsageFilters",
    "UsageGroup",
    "UsageGroupBy",
    "UsageRepository",
    "UsageTotals",
    "UsageTrendPoint",
    "usage_bucket_start",
]

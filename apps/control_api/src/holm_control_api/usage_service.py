"""Usage query policy, time-zone boundaries, and aggregation orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from holm_common import as_utc
from holm_domain import UserRole
from holm_persistence import (
    AsyncSessionFactory,
    UsageBucketWidth,
    UsageFilters,
    UsageGroup,
    UsageGroupBy,
    UsageRepository,
    UsageTotals,
    UsageTrendPoint,
    database_utc_now,
)

MAXIMUM_USAGE_RANGE = timedelta(days=31)
HOURLY_USAGE_RANGE = timedelta(hours=48)


class UsageQueryInvalid(ValueError):
    """A Usage filter cannot be evaluated without changing its meaning."""


class UsageForbiddenReason(StrEnum):
    OTHER_USER = "other_user"
    SERVICE = "service"
    API_KEY = "api_key"


class UsageQueryForbidden(PermissionError):
    """An ordinary user attempted to cross an OWN boundary."""

    def __init__(self, reason: UsageForbiddenReason) -> None:
        self.reason = reason
        super().__init__("usage query is outside the caller's permission scope")


@dataclass(frozen=True, slots=True)
class UsageQuery:
    from_time: datetime
    to_time: datetime
    time_zone: str = "UTC"
    service_id: UUID | None = None
    user_id: UUID | None = None
    api_key_id: UUID | None = None
    group_by: UsageGroupBy = UsageGroupBy.SERVICE


@dataclass(frozen=True, slots=True)
class UsageReport:
    from_time: datetime
    to_time: datetime
    time_zone: str
    bucket_width: UsageBucketWidth
    summary: UsageTotals
    details: tuple[UsageGroup, ...]
    trend: tuple[UsageTrendPoint, ...]


@dataclass(frozen=True, slots=True)
class UsageSummarySnapshot:
    today_active_users: int
    today: UsageTotals
    this_month: UsageTotals
    data_updated_at: datetime | None


def _time_zone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError):
        raise UsageQueryInvalid() from None


def _aware_utc(value: datetime) -> datetime:
    try:
        return as_utc(value)
    except ValueError:
        raise UsageQueryInvalid() from None


def _next_month(value: date) -> date:
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)


def _local_midnight(value: date, zone: ZoneInfo) -> datetime:
    return datetime.combine(value, time.min, tzinfo=zone).astimezone(UTC)


class UsageQueryService:
    __slots__ = ("_session_factory",)

    def __init__(self, *, session_factory: AsyncSessionFactory) -> None:
        self._session_factory = session_factory

    async def summary(
        self,
        *,
        time_zone: str,
        actor_id: UUID,
        actor_role: UserRole,
    ) -> UsageSummarySnapshot:
        zone = _time_zone(time_zone)
        scoped_user_id = actor_id if actor_role is UserRole.USER else None
        async with self._session_factory() as session:
            now = await database_utc_now(session)
            local_date = now.astimezone(zone).date()
            today_start = _local_midnight(local_date, zone)
            tomorrow_start = _local_midnight(local_date + timedelta(days=1), zone)
            month_start = _local_midnight(local_date.replace(day=1), zone)
            next_month_start = _local_midnight(_next_month(local_date), zone)
            repository = UsageRepository(session)
            return UsageSummarySnapshot(
                today_active_users=await repository.active_users_bucketed_window(
                    UsageFilters(
                        from_time=today_start, to_time=tomorrow_start, user_id=scoped_user_id
                    ),
                    now=now,
                ),
                today=await repository.summarize_bucketed_window(
                    UsageFilters(
                        from_time=today_start,
                        to_time=tomorrow_start,
                        user_id=scoped_user_id,
                    ),
                    now=now,
                ),
                this_month=await repository.summarize_bucketed_window(
                    UsageFilters(
                        from_time=month_start,
                        to_time=next_month_start,
                        user_id=scoped_user_id,
                    ),
                    now=now,
                ),
                data_updated_at=await repository.latest_completed_at(
                    UsageFilters(to_time=now + timedelta(microseconds=1), user_id=scoped_user_id)
                ),
            )

    async def report(
        self,
        query: UsageQuery,
        *,
        actor_id: UUID,
        actor_role: UserRole,
    ) -> UsageReport:
        _time_zone(query.time_zone)
        from_time = _aware_utc(query.from_time)
        to_time = _aware_utc(query.to_time)
        duration = to_time - from_time
        if duration <= timedelta(0) or duration > MAXIMUM_USAGE_RANGE:
            raise UsageQueryInvalid()
        user_id: UUID | None
        if actor_role is UserRole.USER:
            if query.user_id is not None and query.user_id != actor_id:
                raise UsageQueryForbidden(UsageForbiddenReason.OTHER_USER)
            if query.group_by is UsageGroupBy.USER:
                raise UsageQueryInvalid()
            user_id = actor_id
        else:
            user_id = query.user_id

        filters = UsageFilters(
            from_time=from_time,
            to_time=to_time,
            service_id=query.service_id,
            user_id=user_id,
            api_key_id=query.api_key_id,
        )
        bucket_width = (
            UsageBucketWidth.HOUR if duration <= HOURLY_USAGE_RANGE else UsageBucketWidth.DAY
        )
        async with self._session_factory() as session:
            repository = UsageRepository(session)
            if actor_role is UserRole.USER:
                if query.service_id is not None and not await repository.service_is_authorized(
                    user_id=actor_id,
                    service_id=query.service_id,
                ):
                    raise UsageQueryForbidden(UsageForbiddenReason.SERVICE)
                if query.api_key_id is not None and not await repository.api_key_is_owned(
                    user_id=actor_id,
                    api_key_id=query.api_key_id,
                ):
                    raise UsageQueryForbidden(UsageForbiddenReason.API_KEY)
            return UsageReport(
                from_time=from_time,
                to_time=to_time,
                time_zone=query.time_zone,
                bucket_width=bucket_width,
                summary=await repository.summarize(filters),
                details=await repository.group(filters, group_by=query.group_by),
                trend=await repository.trend(
                    filters,
                    bucket_width=bucket_width,
                    time_zone=query.time_zone,
                ),
            )


__all__ = [
    "HOURLY_USAGE_RANGE",
    "MAXIMUM_USAGE_RANGE",
    "UsageForbiddenReason",
    "UsageQuery",
    "UsageQueryForbidden",
    "UsageQueryInvalid",
    "UsageQueryService",
    "UsageReport",
    "UsageSummarySnapshot",
]

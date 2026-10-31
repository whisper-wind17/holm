"""PostgreSQL job lease and component heartbeat repositories."""

from __future__ import annotations

from collections.abc import Mapping, Set
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

import sqlalchemy as sa
from holm_common.ids import new_uuid7
from holm_common.time import as_utc
from holm_domain import Component, ComponentStatus, JobExecutionKey, JobStatus, RetryPolicy
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from holm_persistence.jobs import COMPONENT_HEARTBEATS, JOBS

DEFAULT_LEASE_SECONDS: Final[float] = 30.0
DEFAULT_HEARTBEAT_SECONDS: Final[float] = 10.0


@dataclass(frozen=True, slots=True)
class JobDraft:
    """Operational job metadata; payload must never contain request/response content."""

    job_type: str
    payload: Mapping[str, object]
    target_object_id: UUID
    target_object_version: int
    priority: int = 0
    retry_policy: RetryPolicy = RetryPolicy()

    def __post_init__(self) -> None:
        if not self.job_type or self.job_type != self.job_type.strip() or len(self.job_type) > 128:
            raise ValueError("job_type must be trimmed and between 1 and 128 characters")
        if not isinstance(self.target_object_id, UUID):
            raise TypeError("target_object_id must be a UUID")
        if (
            isinstance(self.target_object_version, bool)
            or not isinstance(self.target_object_version, int)
            or self.target_object_version < 0
        ):
            raise ValueError("target_object_version must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class JobRecord:
    id: UUID
    job_type: str
    payload: Mapping[str, object]
    target_object_id: UUID
    target_object_version: int
    status: JobStatus
    priority: int
    lease_owner: str | None
    lease_until: datetime | None
    attempt: int
    max_attempts: int
    run_after: datetime
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    @property
    def execution_key(self) -> JobExecutionKey:
        return JobExecutionKey(self.id, self.target_object_version)


@dataclass(frozen=True, slots=True)
class ComponentHeartbeatRecord:
    component: Component
    instance_id: str
    version: str
    status: ComponentStatus
    last_heartbeat_at: datetime
    reason: str | None
    database_now: datetime
    age_seconds: float


def _optional_utc(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise RuntimeError("database timestamp has an unexpected type")
    return as_utc(value)


def _job_record(mapping: sa.RowMapping) -> JobRecord:
    return JobRecord(
        id=mapping["id"],
        job_type=str(mapping["job_type"]),
        payload=dict(mapping["payload"]),
        target_object_id=mapping["target_object_id"],
        target_object_version=int(mapping["target_object_version"]),
        status=JobStatus(mapping["status"]),
        priority=int(mapping["priority"]),
        lease_owner=mapping["lease_owner"],
        lease_until=_optional_utc(mapping["lease_until"]),
        attempt=int(mapping["attempt"]),
        max_attempts=int(mapping["max_attempts"]),
        run_after=as_utc(mapping["run_after"]),
        last_error_code=mapping["last_error_code"],
        created_at=as_utc(mapping["created_at"]),
        updated_at=as_utc(mapping["updated_at"]),
        completed_at=_optional_utc(mapping["completed_at"]),
    )


def _validate_owner(owner: str) -> str:
    if not owner or owner != owner.strip() or len(owner) > 128:
        raise ValueError("lease owner must be trimmed and between 1 and 128 characters")
    return owner


def _validate_duration(seconds: float, name: str) -> float:
    if isinstance(seconds, bool) or seconds <= 0:
        raise ValueError(f"{name} must be positive")
    return float(seconds)


class JobRepository:
    """Job operations inside a caller-owned transaction.

    Every lease comparison and lease deadline is evaluated by PostgreSQL's
    ``clock_timestamp()``. No worker wall-clock value crosses this boundary.
    """

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(self, draft: JobDraft, *, run_after: datetime | None = None) -> JobRecord:
        run_after_value: datetime | sa.ColumnElement[datetime]
        run_after_value = sa.func.clock_timestamp() if run_after is None else as_utc(run_after)
        statement = (
            sa.insert(JOBS)
            .values(
                id=new_uuid7(),
                job_type=draft.job_type,
                payload=dict(draft.payload),
                target_object_id=draft.target_object_id,
                target_object_version=draft.target_object_version,
                status=JobStatus.QUEUED.value,
                priority=draft.priority,
                lease_owner=None,
                lease_until=None,
                attempt=0,
                max_attempts=draft.retry_policy.max_attempts,
                run_after=run_after_value,
                last_error_code=None,
                created_at=sa.func.clock_timestamp(),
                updated_at=sa.func.clock_timestamp(),
                completed_at=None,
            )
            .returning(*JOBS.c)
        )
        return _job_record((await self._session.execute(statement)).mappings().one())

    async def get(self, job_id: UUID) -> JobRecord | None:
        row = (
            (await self._session.execute(sa.select(JOBS).where(JOBS.c.id == job_id)))
            .mappings()
            .one_or_none()
        )
        return None if row is None else _job_record(row)

    async def claim(
        self,
        *,
        owner: str,
        limit: int = 1,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        job_types: Set[str] | None = None,
    ) -> tuple[JobRecord, ...]:
        """Claim with SKIP LOCKED; a nonempty type set restricts candidates only."""
        owner = _validate_owner(owner)
        lease_seconds = _validate_duration(lease_seconds, "lease_seconds")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("claim limit must be a positive integer")

        # A process that crashes on its final permitted attempt must converge to
        # a terminal row rather than remain Running forever after its lease ends.
        await self._session.execute(
            sa.update(JOBS)
            .where(
                JOBS.c.status == JobStatus.RUNNING.value,
                JOBS.c.lease_until <= sa.func.clock_timestamp(),
                JOBS.c.attempt >= JOBS.c.max_attempts,
            )
            .values(
                status=JobStatus.FAILED.value,
                lease_owner=None,
                lease_until=None,
                last_error_code="attempts_exhausted_after_lease_loss",
                updated_at=sa.func.clock_timestamp(),
                completed_at=sa.func.clock_timestamp(),
            )
        )

        candidate = (
            sa.select(JOBS.c.id)
            .where(
                JOBS.c.job_type.in_(job_types) if job_types else sa.true(),
                JOBS.c.attempt < JOBS.c.max_attempts,
                sa.or_(
                    sa.and_(
                        JOBS.c.status == JobStatus.QUEUED.value,
                        JOBS.c.run_after <= sa.func.clock_timestamp(),
                    ),
                    sa.and_(
                        JOBS.c.status == JobStatus.RUNNING.value,
                        JOBS.c.lease_until <= sa.func.clock_timestamp(),
                    ),
                ),
            )
            .order_by(JOBS.c.priority.desc(), JOBS.c.created_at, JOBS.c.id)
            .with_for_update(skip_locked=True)
            .limit(limit)
            .cte("claimable_jobs")
        )
        lease_interval = sa.func.make_interval(
            0,
            0,
            0,
            0,
            0,
            0,
            sa.bindparam("lease_seconds", lease_seconds),
        )
        statement = (
            sa.update(JOBS)
            .where(JOBS.c.id.in_(sa.select(candidate.c.id)))
            .values(
                status=JobStatus.RUNNING.value,
                lease_owner=owner,
                lease_until=sa.func.clock_timestamp() + lease_interval,
                attempt=JOBS.c.attempt + 1,
                updated_at=sa.func.clock_timestamp(),
                completed_at=None,
            )
            .returning(*JOBS.c)
        )
        rows = (await self._session.execute(statement)).mappings().all()
        return tuple(_job_record(row) for row in rows)

    async def release(self, job_id: UUID, *, owner: str) -> bool:
        """Return a cooperatively stopped job only while its lease is still ours."""
        owner = _validate_owner(owner)
        statement = (
            sa.update(JOBS)
            .where(
                JOBS.c.id == job_id,
                JOBS.c.status == JobStatus.RUNNING.value,
                JOBS.c.lease_owner == owner,
                JOBS.c.lease_until > sa.func.clock_timestamp(),
            )
            .values(
                status=JobStatus.QUEUED.value,
                attempt=sa.func.greatest(JOBS.c.attempt - 1, 0),
                lease_owner=None,
                lease_until=None,
                run_after=sa.func.clock_timestamp(),
                updated_at=sa.func.clock_timestamp(),
            )
            .returning(JOBS.c.id)
        )
        return (await self._session.execute(statement)).scalar_one_or_none() is not None

    async def renew(
        self,
        job_id: UUID,
        *,
        owner: str,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> bool:
        """Renew only an unexpired lease still owned by this worker."""
        owner = _validate_owner(owner)
        lease_seconds = _validate_duration(lease_seconds, "lease_seconds")
        locked_id = (
            await self._session.execute(
                sa.select(JOBS.c.id)
                .where(
                    JOBS.c.id == job_id,
                    JOBS.c.status == JobStatus.RUNNING.value,
                    JOBS.c.lease_owner == owner,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if locked_id is None:
            return False
        lease_interval = sa.func.make_interval(
            0,
            0,
            0,
            0,
            0,
            0,
            sa.bindparam("lease_seconds", lease_seconds),
        )
        statement = (
            sa.update(JOBS)
            .where(
                JOBS.c.id == job_id,
                JOBS.c.status == JobStatus.RUNNING.value,
                JOBS.c.lease_owner == owner,
                JOBS.c.lease_until > sa.func.clock_timestamp(),
            )
            .values(
                lease_until=sa.func.clock_timestamp() + lease_interval,
                updated_at=sa.func.clock_timestamp(),
            )
            .returning(JOBS.c.id)
        )
        return (await self._session.execute(statement)).scalar_one_or_none() is not None

    async def lock_owned_lease(self, job_id: UUID, *, owner: str) -> bool:
        """Fence one effect behind the current unexpired database lease.

        The row lock keeps an expired-lease takeover from committing until the
        caller's surrounding transaction has finished its effect.
        """

        owner = _validate_owner(owner)
        locked_id = (
            await self._session.execute(
                sa.select(JOBS.c.id)
                .where(
                    JOBS.c.id == job_id,
                    JOBS.c.status == JobStatus.RUNNING.value,
                    JOBS.c.lease_owner == owner,
                    JOBS.c.lease_until > sa.func.clock_timestamp(),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        return locked_id is not None

    async def complete(self, job_id: UUID, *, owner: str) -> bool:
        """Complete only while the caller still holds an unexpired lease."""
        owner = _validate_owner(owner)
        locked_id = (
            await self._session.execute(
                sa.select(JOBS.c.id)
                .where(
                    JOBS.c.id == job_id,
                    JOBS.c.status == JobStatus.RUNNING.value,
                    JOBS.c.lease_owner == owner,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if locked_id is None:
            return False
        statement = (
            sa.update(JOBS)
            .where(
                JOBS.c.id == job_id,
                JOBS.c.status == JobStatus.RUNNING.value,
                JOBS.c.lease_owner == owner,
                JOBS.c.lease_until > sa.func.clock_timestamp(),
            )
            .values(
                status=JobStatus.SUCCEEDED.value,
                lease_owner=None,
                lease_until=None,
                updated_at=sa.func.clock_timestamp(),
                completed_at=sa.func.clock_timestamp(),
            )
            .returning(JOBS.c.id)
        )
        return (await self._session.execute(statement)).scalar_one_or_none() is not None

    async def fail(
        self,
        job_id: UUID,
        *,
        owner: str,
        error_code: str,
        retry_policy: RetryPolicy,
    ) -> JobRecord | None:
        """Record a visible failure, retrying only while the policy permits."""
        owner = _validate_owner(owner)
        if not error_code or error_code != error_code.strip() or len(error_code) > 128:
            raise ValueError("error_code must be trimmed and between 1 and 128 characters")
        await self._session.execute(
            sa.select(JOBS.c.id)
            .where(
                JOBS.c.id == job_id,
                JOBS.c.status == JobStatus.RUNNING.value,
                JOBS.c.lease_owner == owner,
            )
            .with_for_update()
        )
        locked = (
            (
                await self._session.execute(
                    sa.select(JOBS).where(
                        JOBS.c.id == job_id,
                        JOBS.c.status == JobStatus.RUNNING.value,
                        JOBS.c.lease_owner == owner,
                        JOBS.c.lease_until > sa.func.clock_timestamp(),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if locked is None:
            return None
        current = _job_record(locked)
        if current.max_attempts != retry_policy.max_attempts:
            raise ValueError("retry policy does not match the persisted job bound")

        terminal = current.attempt >= current.max_attempts
        values: dict[str, object] = {
            "status": JobStatus.FAILED.value if terminal else JobStatus.QUEUED.value,
            "lease_owner": None,
            "lease_until": None,
            "last_error_code": error_code,
            "updated_at": sa.func.clock_timestamp(),
            "completed_at": sa.func.clock_timestamp() if terminal else None,
        }
        if not terminal:
            delay = retry_policy.delay_after(current.attempt)
            values["run_after"] = sa.func.clock_timestamp() + sa.func.make_interval(
                0, 0, 0, 0, 0, 0, delay
            )
        statement = sa.update(JOBS).where(JOBS.c.id == job_id).values(**values).returning(*JOBS.c)
        return _job_record((await self._session.execute(statement)).mappings().one())


class ComponentHeartbeatRepository:
    """Upsert worker heartbeats and derive age from the same database clock."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def write(
        self,
        *,
        component: Component,
        instance_id: str,
        version: str,
        status: ComponentStatus,
        reason: str | None = None,
    ) -> ComponentHeartbeatRecord:
        if not instance_id or instance_id != instance_id.strip() or len(instance_id) > 128:
            raise ValueError("instance_id must be trimmed and between 1 and 128 characters")
        if not version or version != version.strip() or len(version) > 128:
            raise ValueError("version must be trimmed and between 1 and 128 characters")
        if reason is not None and len(reason) > 512:
            raise ValueError("heartbeat reason must not exceed 512 characters")
        insert = postgresql_insert(COMPONENT_HEARTBEATS).values(
            component=Component(component).value,
            instance_id=instance_id,
            version=version,
            status=ComponentStatus(status).value,
            last_heartbeat_at=sa.func.clock_timestamp(),
            reason=reason,
        )
        await self._session.execute(
            insert.on_conflict_do_update(
                index_elements=[
                    COMPONENT_HEARTBEATS.c.component,
                    COMPONENT_HEARTBEATS.c.instance_id,
                ],
                set_={
                    "version": insert.excluded.version,
                    "status": insert.excluded.status,
                    "last_heartbeat_at": sa.func.clock_timestamp(),
                    "reason": insert.excluded.reason,
                },
            )
        )
        record = await self.read(component=component, instance_id=instance_id)
        if record is None:
            raise RuntimeError("heartbeat upsert returned no row")
        return record

    async def read(
        self, *, component: Component, instance_id: str
    ) -> ComponentHeartbeatRecord | None:
        database_now = sa.func.clock_timestamp().label("database_now")
        age_seconds = sa.extract(
            "epoch", database_now - COMPONENT_HEARTBEATS.c.last_heartbeat_at
        ).label("age_seconds")
        row = (
            (
                await self._session.execute(
                    sa.select(COMPONENT_HEARTBEATS, database_now, age_seconds).where(
                        COMPONENT_HEARTBEATS.c.component == Component(component).value,
                        COMPONENT_HEARTBEATS.c.instance_id == instance_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return ComponentHeartbeatRecord(
            component=Component(row["component"]),
            instance_id=str(row["instance_id"]),
            version=str(row["version"]),
            status=ComponentStatus(row["status"]),
            last_heartbeat_at=as_utc(row["last_heartbeat_at"]),
            reason=row["reason"],
            database_now=as_utc(row["database_now"]),
            age_seconds=float(row["age_seconds"]),
        )


__all__ = [
    "DEFAULT_HEARTBEAT_SECONDS",
    "DEFAULT_LEASE_SECONDS",
    "ComponentHeartbeatRecord",
    "ComponentHeartbeatRepository",
    "JobDraft",
    "JobRecord",
    "JobRepository",
]

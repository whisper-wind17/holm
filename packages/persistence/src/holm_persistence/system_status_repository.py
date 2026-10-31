"""Read-only PostgreSQL signals used by the System component aggregation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

import sqlalchemy as sa
from holm_domain import Component, ComponentStatus, JobStatus, ModelInstanceStatus
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from holm_persistence.jobs import COMPONENT_HEARTBEATS, JOBS
from holm_persistence.model_instances import MODEL_INSTANCES


@dataclass(frozen=True, slots=True)
class LatestHeartbeat:
    component: Component
    instance_id: str
    version: str
    status: ComponentStatus
    reason: str | None
    age_seconds: float


@dataclass(frozen=True, slots=True)
class SystemStatusSnapshot:
    postgres_version: str
    heartbeats: Mapping[Component, LatestHeartbeat]
    oldest_executable_job_age_seconds: float | None
    expired_lease_count: int
    running_job_count: int
    schema_revision: str | None = None
    active_instance_counts: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))


class SystemStatusRepository:
    """Read one coherent set of component signals using PostgreSQL's clock."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def read(self) -> SystemStatusSnapshot:
        database_now = sa.func.clock_timestamp()
        ranked = sa.select(
            COMPONENT_HEARTBEATS.c.component,
            COMPONENT_HEARTBEATS.c.instance_id,
            COMPONENT_HEARTBEATS.c.version,
            COMPONENT_HEARTBEATS.c.status,
            COMPONENT_HEARTBEATS.c.reason,
            sa.extract("epoch", database_now - COMPONENT_HEARTBEATS.c.last_heartbeat_at).label(
                "age_seconds"
            ),
            sa.func.row_number()
            .over(
                partition_by=COMPONENT_HEARTBEATS.c.component,
                order_by=(
                    COMPONENT_HEARTBEATS.c.last_heartbeat_at.desc(),
                    COMPONENT_HEARTBEATS.c.instance_id,
                ),
            )
            .label("rank"),
        ).subquery("ranked_component_heartbeats")
        rows = (await self._session.execute(sa.select(ranked).where(ranked.c.rank == 1))).mappings()
        heartbeats = {
            Component(row["component"]): LatestHeartbeat(
                component=Component(row["component"]),
                instance_id=str(row["instance_id"]),
                version=str(row["version"]),
                status=ComponentStatus(row["status"]),
                reason=None if row["reason"] is None else str(row["reason"]),
                age_seconds=max(float(row["age_seconds"]), 0.0),
            )
            for row in rows
        }

        job_signals = (
            (
                await self._session.execute(
                    sa.select(
                        sa.extract(
                            "epoch",
                            database_now
                            - sa.func.min(JOBS.c.created_at).filter(
                                JOBS.c.status == JobStatus.QUEUED.value,
                                JOBS.c.run_after <= database_now,
                            ),
                        ).label("oldest_executable_job_age_seconds"),
                        sa.func.count()
                        .filter(
                            JOBS.c.status == JobStatus.RUNNING.value,
                            JOBS.c.lease_until <= database_now,
                        )
                        .label("expired_lease_count"),
                        sa.func.count()
                        .filter(JOBS.c.status == JobStatus.RUNNING.value)
                        .label("running_job_count"),
                    )
                )
            )
            .mappings()
            .one()
        )
        active_counts = (
            sa.select(MODEL_INSTANCES.c.runtime_type, sa.func.count().label("count"))
            .where(
                MODEL_INSTANCES.c.status.in_(
                    (
                        ModelInstanceStatus.STARTING,
                        ModelInstanceStatus.RUNNING,
                        ModelInstanceStatus.RESTARTING,
                        ModelInstanceStatus.UNHEALTHY,
                        ModelInstanceStatus.STOPPING,
                    )
                )
            )
            .group_by(MODEL_INSTANCES.c.runtime_type)
            .subquery("active_instance_counts")
        )
        # Fold the counts into the existing version query: no extra database round trip.
        counts_query = sa.select(
            sa.func.json_object_agg(active_counts.c.runtime_type, active_counts.c.count)
        ).scalar_subquery()
        postgres_version, counts = (
            await self._session.execute(
                sa.select(sa.func.current_setting("server_version"), counts_query)
            )
        ).one()
        if not isinstance(postgres_version, str) or not postgres_version.strip():
            raise RuntimeError("PostgreSQL returned an invalid server version")
        schema_revision = None
        try:
            async with self._session.begin_nested():
                value = (
                    await self._session.execute(sa.text("SELECT version_num FROM alembic_version"))
                ).scalar_one_or_none()
                if isinstance(value, str) and value.strip():
                    schema_revision = value.strip()
        except SQLAlchemyError:
            # Missing schema metadata is an unknown observation, not version 0.
            pass
        oldest = job_signals["oldest_executable_job_age_seconds"]
        return SystemStatusSnapshot(
            postgres_version=postgres_version.strip(),
            active_instance_counts=MappingProxyType(counts or {}),
            schema_revision=schema_revision,
            heartbeats=MappingProxyType(heartbeats),
            oldest_executable_job_age_seconds=None if oldest is None else max(float(oldest), 0.0),
            expired_lease_count=int(job_signals["expired_lease_count"]),
            running_job_count=int(job_signals["running_job_count"]),
        )


SYSTEM_STATUS_SIGNAL_TABLES: Final[tuple[sa.Table, ...]] = (
    COMPONENT_HEARTBEATS,
    JOBS,
    MODEL_INSTANCES,
)


__all__ = [
    "SYSTEM_STATUS_SIGNAL_TABLES",
    "LatestHeartbeat",
    "SystemStatusRepository",
    "SystemStatusSnapshot",
]

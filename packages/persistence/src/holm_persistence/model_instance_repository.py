"""Transactional persistence operations for Model Instance GPU reservations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import blake2b
from typing import cast
from uuid import UUID

import sqlalchemy as sa
from holm_common import as_utc, new_uuid7
from holm_domain import (
    DomainEventType,
    GpuAllocationStatus,
    ModelArtifactStatus,
    ModelInstanceStatus,
    RuntimeType,
)
from holm_domain.instance_names import default_instance_name, normalize_instance_name
from holm_domain.model_instances import ModelInstanceEventType
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from holm_persistence.domain_event_repository import append_route_invalidation_event
from holm_persistence.launch_verification_repository import LaunchVerificationRepository
from holm_persistence.model_domain import MODEL_ARTIFACTS, MODELS
from holm_persistence.model_instance_events import (
    MODEL_INSTANCE_EVENTS,
    ModelInstanceEventActor,
    safe_model_instance_event_detail,
)
from holm_persistence.model_instances import GPU_ALLOCATIONS, MODEL_INSTANCES

ACTIVE_GPU_UNIQUE_INDEX = "uq_gpu_allocations_active_gpu_uuid"


class ModelInstanceNameConflict(RuntimeError):
    """The normalized display name already belongs to an instance."""


class ModelInstanceArtifactNotReady(RuntimeError):
    """The selected local Model is absent or not Ready."""


@dataclass(frozen=True, slots=True)
class ModelInstanceCreate:
    artifact_id: UUID
    runtime_type: RuntimeType
    expected_gpu_uuids: tuple[str, ...]
    tensor_parallel_size: int
    context_length: int
    gpu_memory_utilization: float
    name: str | None = None


@dataclass(frozen=True, slots=True)
class ModelArtifactDeploymentMetadata:
    """Artifact fields needed for synchronous deployment validation."""

    status: ModelArtifactStatus
    attention_heads: int | None
    kv_heads: int | None


@dataclass(frozen=True, slots=True)
class ModelInstanceRecord:
    id: UUID
    artifact_id: UUID
    runtime_type: RuntimeType
    expected_gpu_uuids: tuple[str, ...]
    tensor_parallel_size: int
    context_length: int
    gpu_memory_utilization: float
    internal_model_name: str
    name: str
    status: ModelInstanceStatus
    error_code: str | None
    internal_endpoint: str | None
    config_version: int
    created_at: datetime
    updated_at: datetime
    restart_count: int = 0
    restart_window_started_at: datetime | None = None
    last_healthy_at: datetime | None = None
    last_crashed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ModelInstanceEventRecord:
    id: UUID
    instance_id: UUID
    event_type: ModelInstanceEventType
    from_status: ModelInstanceStatus | None
    to_status: ModelInstanceStatus | None
    occurred_at: datetime
    actor: ModelInstanceEventActor
    detail: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ModelInstanceEventPosition:
    occurred_at: datetime
    event_id: UUID


@dataclass(frozen=True, slots=True)
class ControllerModelInstanceRecord:
    """A locked lifecycle item with the Artifact path and active GPU set."""

    instance: ModelInstanceRecord
    artifact_path: str
    active_gpu_uuids: tuple[str, ...]


def _gpu_uuids(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise RuntimeError("persisted expected GPU UUID collection is malformed")
    return tuple(value)


def _record(mapping: sa.RowMapping) -> ModelInstanceRecord:
    return ModelInstanceRecord(
        id=mapping["id"],
        artifact_id=mapping["artifact_id"],
        runtime_type=RuntimeType(mapping["runtime_type"]),
        expected_gpu_uuids=_gpu_uuids(mapping["expected_gpu_uuids"]),
        tensor_parallel_size=int(mapping["tensor_parallel_size"]),
        context_length=int(mapping["context_length"]),
        gpu_memory_utilization=float(mapping["gpu_memory_utilization"]),
        internal_model_name=str(mapping["internal_model_name"]),
        name=str(mapping["name"]),
        status=ModelInstanceStatus(mapping["status"]),
        error_code=mapping["error_code"],
        internal_endpoint=mapping["internal_endpoint"],
        config_version=int(mapping["config_version"]),
        created_at=as_utc(mapping["created_at"]),
        updated_at=as_utc(mapping["updated_at"]),
        restart_count=int(mapping["restart_count"]),
        restart_window_started_at=(
            None
            if mapping["restart_window_started_at"] is None
            else as_utc(mapping["restart_window_started_at"])
        ),
        last_healthy_at=(
            None if mapping["last_healthy_at"] is None else as_utc(mapping["last_healthy_at"])
        ),
        last_crashed_at=(
            None if mapping["last_crashed_at"] is None else as_utc(mapping["last_crashed_at"])
        ),
    )


def _event_record(mapping: sa.RowMapping) -> ModelInstanceEventRecord:
    actor = str(mapping["actor"])
    if actor not in {"admin", "controller", "system"}:
        raise RuntimeError("persisted Model Instance event actor is invalid")
    detail = mapping["detail"]
    if not isinstance(detail, Mapping):
        raise RuntimeError("persisted Model Instance event detail is malformed")
    return ModelInstanceEventRecord(
        id=mapping["id"],
        instance_id=mapping["instance_id"],
        event_type=ModelInstanceEventType(mapping["event_type"]),
        from_status=(
            None if mapping["from_status"] is None else ModelInstanceStatus(mapping["from_status"])
        ),
        to_status=(
            None if mapping["to_status"] is None else ModelInstanceStatus(mapping["to_status"])
        ),
        occurred_at=as_utc(mapping["occurred_at"]),
        actor=cast(ModelInstanceEventActor, actor),
        detail=safe_model_instance_event_detail(detail),
    )


def _is_active_gpu_conflict(error: IntegrityError) -> bool:
    diagnostic = getattr(getattr(error, "orig", None), "diag", None)
    return getattr(diagnostic, "constraint_name", None) == ACTIVE_GPU_UNIQUE_INDEX


def _lifecycle_lock_key(instance_id: UUID) -> int:
    """Map one instance identity to PostgreSQL's signed 64-bit advisory-lock key."""

    digest = blake2b(instance_id.bytes, digest_size=8, person=b"pw-mi-lk").digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


class ModelInstanceRepository:
    """Keep instance state and GPU reservations in one PostgreSQL transaction."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def artifact_deployment_metadata_for_update(
        self, artifact_id: UUID
    ) -> ModelArtifactDeploymentMetadata | None:
        from holm_persistence.artifact_lock import lock_artifact

        await lock_artifact(self._session, artifact_id)
        row = (
            (
                await self._session.execute(
                    sa.select(
                        MODEL_ARTIFACTS.c.status,
                        MODEL_ARTIFACTS.c.attention_heads,
                        MODEL_ARTIFACTS.c.kv_heads,
                    ).where(MODEL_ARTIFACTS.c.id == artifact_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return ModelArtifactDeploymentMetadata(
            status=ModelArtifactStatus(row["status"]),
            attention_heads=row["attention_heads"],
            kv_heads=row["kv_heads"],
        )

    async def create(
        self,
        value: ModelInstanceCreate,
        *,
        now: datetime,
        actor: ModelInstanceEventActor = "admin",
    ) -> ModelInstanceRecord:
        instance_id = new_uuid7()
        if value.name is None:
            model_name = (
                await self._session.execute(
                    sa.select(MODELS.c.display_name)
                    .join(MODEL_ARTIFACTS, MODEL_ARTIFACTS.c.model_id == MODELS.c.id)
                    .where(MODEL_ARTIFACTS.c.id == value.artifact_id)
                )
            ).scalar_one()
            name = default_instance_name(str(model_name))
        else:
            name = normalize_instance_name(value.name)
        statement = (
            sa.insert(MODEL_INSTANCES)
            .values(
                id=instance_id,
                artifact_id=value.artifact_id,
                runtime_type=value.runtime_type.value,
                expected_gpu_uuids=value.expected_gpu_uuids,
                tensor_parallel_size=value.tensor_parallel_size,
                context_length=value.context_length,
                gpu_memory_utilization=value.gpu_memory_utilization,
                name=name,
                internal_model_name=f"mi_{instance_id.hex[:12]}",
                status=ModelInstanceStatus.WAITING_FOR_RESOURCE.value,
                error_code=None,
                internal_endpoint=None,
                config_version=1,
                restart_count=0,
                restart_window_started_at=None,
                last_healthy_at=None,
                last_crashed_at=None,
                created_at=as_utc(now),
                updated_at=as_utc(now),
            )
            .returning(*MODEL_INSTANCES.c)
        )
        # A failed unique insert must not abort the surrounding creation transaction.
        for sequence in range(1, 21):
            try:
                async with self._session.begin_nested():
                    record = _record((await self._session.execute(statement)).mappings().one())
                break
            except IntegrityError as error:
                diagnostic = getattr(getattr(error, "orig", None), "diag", None)
                if getattr(diagnostic, "constraint_name", None) != "uq_model_instances_name":
                    raise
                if value.name is not None or sequence == 20:
                    raise ModelInstanceNameConflict() from None
                statement = statement.values(
                    name=default_instance_name(str(model_name), sequence + 1)
                )
        await self._append_status_event(
            record.id,
            actor=actor,
            occurred_at=now,
            from_status=None,
            to_status=record.status,
        )
        await self._route_changed(record.id)
        return record

    async def _route_changed(self, instance_id: UUID) -> None:
        await append_route_invalidation_event(
            self._session,
            event=DomainEventType.INSTANCE_ROUTE_CHANGED,
            aggregate_id=instance_id,
        )

    async def append_event(
        self,
        instance_id: UUID,
        *,
        event_type: ModelInstanceEventType,
        actor: ModelInstanceEventActor,
        occurred_at: datetime,
        from_status: ModelInstanceStatus | None,
        to_status: ModelInstanceStatus | None,
        detail: Mapping[str, object] | None = None,
    ) -> ModelInstanceEventRecord:
        """Append one default-deny event inside the caller-owned transaction."""

        statement = (
            sa.insert(MODEL_INSTANCE_EVENTS)
            .values(
                id=new_uuid7(),
                instance_id=instance_id,
                event_type=event_type.value,
                from_status=None if from_status is None else from_status.value,
                to_status=None if to_status is None else to_status.value,
                occurred_at=as_utc(occurred_at),
                actor=actor,
                detail=safe_model_instance_event_detail({} if detail is None else detail),
            )
            .returning(*MODEL_INSTANCE_EVENTS.c)
        )
        return _event_record((await self._session.execute(statement)).mappings().one())

    async def _append_status_event(
        self,
        instance_id: UUID,
        *,
        actor: ModelInstanceEventActor,
        occurred_at: datetime,
        from_status: ModelInstanceStatus | None,
        to_status: ModelInstanceStatus,
    ) -> None:
        if from_status is to_status:
            return
        await self.append_event(
            instance_id,
            event_type=ModelInstanceEventType.STATUS_CHANGED,
            actor=actor,
            occurred_at=occurred_at,
            from_status=from_status,
            to_status=to_status,
        )

    async def get(
        self,
        instance_id: UUID,
        *,
        for_update: bool = False,
        nowait: bool = False,
    ) -> ModelInstanceRecord | None:
        statement = sa.select(MODEL_INSTANCES).where(MODEL_INSTANCES.c.id == instance_id)
        if for_update:
            statement = statement.with_for_update(nowait=nowait)
        mapping = (await self._session.execute(statement)).mappings().one_or_none()
        return None if mapping is None else _record(mapping)

    async def list(self) -> tuple[ModelInstanceRecord, ...]:
        mappings = (
            await self._session.execute(sa.select(MODEL_INSTANCES).order_by(MODEL_INSTANCES.c.id))
        ).mappings()
        return tuple(_record(mapping) for mapping in mappings)

    async def list_events(
        self,
        instance_id: UUID,
        *,
        before: ModelInstanceEventPosition | None,
        limit: int,
    ) -> tuple[ModelInstanceEventRecord, ...]:
        if not 1 <= limit <= 101:
            raise ValueError("Model Instance event page limit is out of range")
        statement = (
            sa.select(MODEL_INSTANCE_EVENTS)
            .where(MODEL_INSTANCE_EVENTS.c.instance_id == instance_id)
            .order_by(
                MODEL_INSTANCE_EVENTS.c.occurred_at.desc(),
                MODEL_INSTANCE_EVENTS.c.id.desc(),
            )
            .limit(limit)
        )
        if before is not None:
            statement = statement.where(
                sa.tuple_(
                    MODEL_INSTANCE_EVENTS.c.occurred_at,
                    MODEL_INSTANCE_EVENTS.c.id,
                )
                < (as_utc(before.occurred_at), before.event_id)
            )
        mappings = (await self._session.execute(statement)).mappings()
        return tuple(_event_record(mapping) for mapping in mappings)

    async def acquire_lifecycle_lock(self, instance_id: UUID) -> None:
        """Hold the affected-instance mutex until the surrounding transaction ends."""

        await self._session.execute(
            sa.select(sa.func.pg_advisory_xact_lock(_lifecycle_lock_key(instance_id)))
        )

    async def try_lifecycle_lock(self, instance_id: UUID) -> bool:
        """Fail fast when reconciliation owns this instance's lifecycle mutex."""

        acquired = await self._session.scalar(
            sa.select(sa.func.pg_try_advisory_xact_lock(_lifecycle_lock_key(instance_id)))
        )
        return bool(acquired)

    async def list_for_controller(
        self,
        statuses: tuple[ModelInstanceStatus, ...],
    ) -> tuple[ControllerModelInstanceRecord, ...]:
        """Load one bounded reconciliation snapshot without locking unrelated readers."""

        mappings = (
            (
                await self._session.execute(
                    sa.select(
                        *MODEL_INSTANCES.c,
                        MODEL_ARTIFACTS.c.path.label("artifact_path"),
                    )
                    .join(
                        MODEL_ARTIFACTS,
                        MODEL_ARTIFACTS.c.id == MODEL_INSTANCES.c.artifact_id,
                    )
                    .where(MODEL_INSTANCES.c.status.in_(tuple(status.value for status in statuses)))
                    .order_by(MODEL_INSTANCES.c.created_at, MODEL_INSTANCES.c.id)
                )
            )
            .mappings()
            .all()
        )
        if not mappings:
            return ()
        instance_ids = tuple(mapping["id"] for mapping in mappings)
        allocation_rows = (
            (
                await self._session.execute(
                    sa.select(GPU_ALLOCATIONS.c.instance_id, GPU_ALLOCATIONS.c.gpu_uuid)
                    .where(
                        GPU_ALLOCATIONS.c.instance_id.in_(instance_ids),
                        GPU_ALLOCATIONS.c.status != GpuAllocationStatus.RELEASED.value,
                    )
                    .order_by(GPU_ALLOCATIONS.c.instance_id, GPU_ALLOCATIONS.c.gpu_uuid)
                )
            )
            .tuples()
            .all()
        )
        allocations: dict[UUID, list[str]] = {instance_id: [] for instance_id in instance_ids}
        for instance_id, gpu_uuid in allocation_rows:
            allocations[instance_id].append(str(gpu_uuid))
        return tuple(
            ControllerModelInstanceRecord(
                instance=_record(mapping),
                artifact_path=str(mapping["artifact_path"]),
                active_gpu_uuids=tuple(allocations[mapping["id"]]),
            )
            for mapping in mappings
        )

    async def reserve_or_wait(
        self,
        instance: ModelInstanceRecord,
        *,
        now: datetime,
        reset_restart_window: bool = False,
        actor: ModelInstanceEventActor = "controller",
        action_event: ModelInstanceEventType | None = None,
    ) -> ModelInstanceRecord:
        artifact = await self.artifact_deployment_metadata_for_update(instance.artifact_id)
        if artifact is None or artifact.status is not ModelArtifactStatus.READY:
            raise ModelInstanceArtifactNotReady()
        if action_event is not None:
            await self.append_event(
                instance.id,
                event_type=action_event,
                actor=actor,
                occurred_at=now,
                from_status=instance.status,
                to_status=instance.status,
            )
        gpu_uuids = tuple(sorted(instance.expected_gpu_uuids))
        occupied = (
            (
                await self._session.execute(
                    sa.select(GPU_ALLOCATIONS.c.gpu_uuid)
                    .where(
                        GPU_ALLOCATIONS.c.gpu_uuid.in_(gpu_uuids),
                        GPU_ALLOCATIONS.c.status != GpuAllocationStatus.RELEASED.value,
                    )
                    .order_by(GPU_ALLOCATIONS.c.gpu_uuid)
                    .with_for_update()
                )
            )
            .scalars()
            .first()
        )
        reserved = occupied is None
        if reserved:
            try:
                async with self._session.begin_nested():
                    await self._session.execute(
                        sa.insert(GPU_ALLOCATIONS),
                        [
                            {
                                "id": new_uuid7(),
                                "instance_id": instance.id,
                                "gpu_uuid": gpu_uuid,
                                "status": GpuAllocationStatus.RESERVED.value,
                                "created_at": as_utc(now),
                                "updated_at": as_utc(now),
                                "released_at": None,
                            }
                            for gpu_uuid in gpu_uuids
                        ],
                    )
            except IntegrityError as error:
                if not _is_active_gpu_conflict(error):
                    raise
                reserved = False

        status = (
            ModelInstanceStatus.STARTING if reserved else ModelInstanceStatus.WAITING_FOR_RESOURCE
        )
        mapping = (
            (
                await self._session.execute(
                    sa.update(MODEL_INSTANCES)
                    .where(MODEL_INSTANCES.c.id == instance.id)
                    .values(
                        status=status.value,
                        error_code=None,
                        updated_at=as_utc(now),
                        **(
                            {"restart_count": 0, "restart_window_started_at": None}
                            if reset_restart_window
                            else {}
                        ),
                    )
                    .returning(*MODEL_INSTANCES.c)
                )
            )
            .mappings()
            .one()
        )
        record = _record(mapping)
        await self._append_status_event(
            record.id,
            actor=actor,
            occurred_at=now,
            from_status=instance.status,
            to_status=record.status,
        )
        if record.status is ModelInstanceStatus.STARTING:
            await LaunchVerificationRepository(self._session).request(
                instance_id=record.id,
                artifact_id=record.artifact_id,
                config_version=record.config_version,
                status=record.status,
            )
        await self._route_changed(record.id)
        return record

    async def mark_unhealthy(
        self,
        instance_id: UUID,
        *,
        expected_status: ModelInstanceStatus,
        error_code: str,
        now: datetime,
    ) -> bool:
        """Remove a failed backend from routing while retaining its Allocations."""

        result = await self._session.execute(
            sa.update(MODEL_INSTANCES)
            .where(
                MODEL_INSTANCES.c.id == instance_id,
                MODEL_INSTANCES.c.status == expected_status.value,
            )
            .values(
                status=ModelInstanceStatus.UNHEALTHY.value,
                error_code=error_code,
                internal_endpoint=None,
                last_crashed_at=as_utc(now),
                updated_at=as_utc(now),
            )
            .returning(MODEL_INSTANCES.c.id)
        )
        changed = result.scalar_one_or_none() is not None
        if changed:
            await self._append_status_event(
                instance_id,
                actor="controller",
                occurred_at=now,
                from_status=expected_status,
                to_status=ModelInstanceStatus.UNHEALTHY,
            )
            await self._route_changed(instance_id)
        return changed

    async def begin_restart(
        self,
        instance_id: UUID,
        *,
        now: datetime,
        restart_window_seconds: int,
    ) -> bool:
        """Enter Restarting without releasing or replacing the selected GPUs."""

        current = await self.get(instance_id, for_update=True)
        if current is None or current.status is not ModelInstanceStatus.UNHEALTHY:
            return False
        window_expired = current.restart_window_started_at is None or as_utc(
            now
        ) - current.restart_window_started_at >= timedelta(seconds=restart_window_seconds)
        await self._session.execute(
            sa.update(MODEL_INSTANCES)
            .where(MODEL_INSTANCES.c.id == instance_id)
            .values(
                status=ModelInstanceStatus.RESTARTING.value,
                restart_count=0 if window_expired else current.restart_count,
                restart_window_started_at=(
                    as_utc(now) if window_expired else current.restart_window_started_at
                ),
                updated_at=as_utc(now),
            )
        )
        await self._append_status_event(
            instance_id,
            actor="controller",
            occurred_at=now,
            from_status=current.status,
            to_status=ModelInstanceStatus.RESTARTING,
        )
        await self._route_changed(instance_id)
        return True

    async def request_manual_restart(
        self,
        instance: ModelInstanceRecord,
        *,
        now: datetime,
    ) -> ModelInstanceRecord | None:
        """Enter Restarting only while the complete original GPU set is still owned."""

        if instance.status not in {
            ModelInstanceStatus.RUNNING,
            ModelInstanceStatus.UNHEALTHY,
        }:
            return None
        active_gpu_uuids = tuple(
            (
                await self._session.execute(
                    sa.select(GPU_ALLOCATIONS.c.gpu_uuid)
                    .where(
                        GPU_ALLOCATIONS.c.instance_id == instance.id,
                        GPU_ALLOCATIONS.c.status != GpuAllocationStatus.RELEASED.value,
                    )
                    .order_by(GPU_ALLOCATIONS.c.gpu_uuid)
                    .with_for_update()
                )
            ).scalars()
        )
        expected = tuple(sorted(instance.expected_gpu_uuids))
        if active_gpu_uuids != expected or len(expected) != instance.tensor_parallel_size:
            return None
        await self.append_event(
            instance.id,
            event_type=ModelInstanceEventType.RESTART_REQUESTED,
            actor="admin",
            occurred_at=now,
            from_status=instance.status,
            to_status=instance.status,
        )
        mapping = (
            (
                await self._session.execute(
                    sa.update(MODEL_INSTANCES)
                    .where(MODEL_INSTANCES.c.id == instance.id)
                    .values(
                        status=ModelInstanceStatus.RESTARTING.value,
                        error_code=None,
                        internal_endpoint=None,
                        restart_count=0,
                        restart_window_started_at=None,
                        updated_at=as_utc(now),
                    )
                    .returning(*MODEL_INSTANCES.c)
                )
            )
            .mappings()
            .one()
        )
        record = _record(mapping)
        await self._append_status_event(
            instance.id,
            actor="admin",
            occurred_at=now,
            from_status=instance.status,
            to_status=record.status,
        )
        await self._route_changed(instance.id)
        return record

    async def select_gpus_and_reserve(
        self,
        instance: ModelInstanceRecord,
        *,
        gpu_uuids: tuple[str, ...],
        now: datetime,
    ) -> ModelInstanceRecord:
        """Persist an explicit selection before retrying the same reservation primitive."""

        mapping = (
            (
                await self._session.execute(
                    sa.update(MODEL_INSTANCES)
                    .where(MODEL_INSTANCES.c.id == instance.id)
                    .values(
                        expected_gpu_uuids=gpu_uuids,
                        tensor_parallel_size=len(gpu_uuids),
                        config_version=MODEL_INSTANCES.c.config_version + 1,
                        updated_at=as_utc(now),
                    )
                    .returning(*MODEL_INSTANCES.c)
                )
            )
            .mappings()
            .one()
        )
        selected = _record(mapping)
        return await self.reserve_or_wait(
            selected,
            now=now,
            actor="admin",
            action_event=ModelInstanceEventType.GPU_SELECTION_CHANGED,
        )

    async def cancel_waiting(
        self,
        instance: ModelInstanceRecord,
        *,
        now: datetime,
    ) -> None:
        await self.append_event(
            instance.id,
            event_type=ModelInstanceEventType.DEPLOYMENT_CANCELLED,
            actor="admin",
            occurred_at=now,
            from_status=instance.status,
            to_status=None,
        )
        await self.delete(instance.id)

    async def record_restart_attempt(self, instance_id: UUID, *, now: datetime) -> int | None:
        """Persist the next automatic attempt before issuing the Agent command."""

        result = await self._session.execute(
            sa.update(MODEL_INSTANCES)
            .where(
                MODEL_INSTANCES.c.id == instance_id,
                MODEL_INSTANCES.c.status == ModelInstanceStatus.RESTARTING.value,
            )
            .values(
                restart_count=MODEL_INSTANCES.c.restart_count + 1,
                updated_at=as_utc(now),
            )
            .returning(MODEL_INSTANCES.c.restart_count)
        )
        value = result.scalar_one_or_none()
        if value is not None:
            current = await self.get(instance_id)
            assert current is not None
            await LaunchVerificationRepository(self._session).request(
                instance_id=instance_id,
                artifact_id=current.artifact_id,
                config_version=current.config_version,
                status=current.status,
                attempt=int(value),
            )
        return None if value is None else int(value)

    async def mark_restarted_running(
        self,
        instance_id: UUID,
        *,
        endpoint: str,
        now: datetime,
    ) -> bool:
        """Publish a recovered backend and retain its original GPU ownership."""

        current = await self._session.scalar(
            sa.select(MODEL_INSTANCES.c.status)
            .where(MODEL_INSTANCES.c.id == instance_id)
            .with_for_update()
        )
        if current != ModelInstanceStatus.RESTARTING.value:
            return False
        await self._session.execute(
            sa.update(GPU_ALLOCATIONS)
            .where(
                GPU_ALLOCATIONS.c.instance_id == instance_id,
                GPU_ALLOCATIONS.c.status == GpuAllocationStatus.RESERVED.value,
            )
            .values(status=GpuAllocationStatus.ALLOCATED.value, updated_at=as_utc(now))
        )
        await self._session.execute(
            sa.update(MODEL_INSTANCES)
            .where(MODEL_INSTANCES.c.id == instance_id)
            .values(
                status=ModelInstanceStatus.RUNNING.value,
                error_code=None,
                internal_endpoint=endpoint,
                last_healthy_at=as_utc(now),
                updated_at=as_utc(now),
            )
        )
        await self._append_status_event(
            instance_id,
            actor="controller",
            occurred_at=now,
            from_status=ModelInstanceStatus.RESTARTING,
            to_status=ModelInstanceStatus.RUNNING,
        )
        await self._route_changed(instance_id)
        return True

    async def release_terminal_allocations(self, *, now: datetime) -> tuple[UUID, ...]:
        """Release leaked ownership for terminal instances in one auditable update."""

        released = (
            (
                await self._session.execute(
                    sa.update(GPU_ALLOCATIONS)
                    .where(
                        GPU_ALLOCATIONS.c.status != GpuAllocationStatus.RELEASED.value,
                        GPU_ALLOCATIONS.c.instance_id.in_(
                            sa.select(MODEL_INSTANCES.c.id).where(
                                MODEL_INSTANCES.c.status.in_(
                                    (
                                        ModelInstanceStatus.STOPPED.value,
                                        ModelInstanceStatus.FAILED.value,
                                    )
                                )
                            )
                        ),
                    )
                    .values(
                        status=GpuAllocationStatus.RELEASED.value,
                        released_at=as_utc(now),
                        updated_at=as_utc(now),
                    )
                    .returning(GPU_ALLOCATIONS.c.instance_id)
                )
            )
            .scalars()
            .all()
        )
        return tuple(dict.fromkeys(released))

    async def stop(
        self,
        instance: ModelInstanceRecord,
        *,
        now: datetime,
    ) -> ModelInstanceRecord:
        mapping = (
            (
                await self._session.execute(
                    sa.update(MODEL_INSTANCES)
                    .where(MODEL_INSTANCES.c.id == instance.id)
                    .values(
                        status=ModelInstanceStatus.STOPPING.value,
                        error_code=None,
                        internal_endpoint=None,
                        updated_at=as_utc(now),
                    )
                    .returning(*MODEL_INSTANCES.c)
                )
            )
            .mappings()
            .one()
        )
        record = _record(mapping)
        await self._append_status_event(
            record.id,
            actor="admin",
            occurred_at=now,
            from_status=instance.status,
            to_status=record.status,
        )
        await self._route_changed(record.id)
        return record

    async def claim_for_controller(
        self,
        status: ModelInstanceStatus,
        *,
        excluded_ids: tuple[UUID, ...] = (),
    ) -> ControllerModelInstanceRecord | None:
        """Claim the oldest lifecycle item without waiting behind another controller."""

        if status not in {
            ModelInstanceStatus.STARTING,
            ModelInstanceStatus.RUNNING,
            ModelInstanceStatus.STOPPING,
            ModelInstanceStatus.UNHEALTHY,
            ModelInstanceStatus.RESTARTING,
        }:
            raise ValueError("controller can claim only active lifecycle states")
        statement = (
            sa.select(
                *MODEL_INSTANCES.c,
                MODEL_ARTIFACTS.c.path.label("artifact_path"),
            )
            .join(MODEL_ARTIFACTS, MODEL_ARTIFACTS.c.id == MODEL_INSTANCES.c.artifact_id)
            .where(MODEL_INSTANCES.c.status == status.value)
            .order_by(MODEL_INSTANCES.c.created_at, MODEL_INSTANCES.c.id)
            .limit(1)
            .with_for_update(skip_locked=True, of=MODEL_INSTANCES)
        )
        if excluded_ids:
            statement = statement.where(MODEL_INSTANCES.c.id.not_in(excluded_ids))
        mapping = (await self._session.execute(statement)).mappings().one_or_none()
        if mapping is None:
            return None
        instance = _record(mapping)
        active_gpu_uuids = tuple(
            (
                await self._session.execute(
                    sa.select(GPU_ALLOCATIONS.c.gpu_uuid)
                    .where(
                        GPU_ALLOCATIONS.c.instance_id == instance.id,
                        GPU_ALLOCATIONS.c.status != GpuAllocationStatus.RELEASED.value,
                    )
                    .order_by(GPU_ALLOCATIONS.c.gpu_uuid)
                )
            ).scalars()
        )
        return ControllerModelInstanceRecord(
            instance=instance,
            artifact_path=str(mapping["artifact_path"]),
            active_gpu_uuids=active_gpu_uuids,
        )

    async def set_controller_endpoint(
        self,
        instance_id: UUID,
        *,
        expected_status: ModelInstanceStatus,
        endpoint: str,
    ) -> bool:
        """Publish a trusted loopback Endpoint without resetting the startup clock."""

        result = await self._session.execute(
            sa.update(MODEL_INSTANCES)
            .where(
                MODEL_INSTANCES.c.id == instance_id,
                MODEL_INSTANCES.c.status == expected_status.value,
            )
            .values(internal_endpoint=endpoint)
            .returning(MODEL_INSTANCES.c.id)
        )
        changed = result.scalar_one_or_none() is not None
        if changed:
            await self._route_changed(instance_id)
        return changed

    async def mark_running(self, instance_id: UUID, *, now: datetime) -> bool:
        """Atomically promote reservations and the owning instance to Running."""

        status = await self._session.scalar(
            sa.select(MODEL_INSTANCES.c.status)
            .where(MODEL_INSTANCES.c.id == instance_id)
            .with_for_update()
        )
        if status != ModelInstanceStatus.STARTING.value:
            return False
        await self._session.execute(
            sa.update(GPU_ALLOCATIONS)
            .where(
                GPU_ALLOCATIONS.c.instance_id == instance_id,
                GPU_ALLOCATIONS.c.status == GpuAllocationStatus.RESERVED.value,
            )
            .values(status=GpuAllocationStatus.ALLOCATED.value, updated_at=as_utc(now))
        )
        await self._session.execute(
            sa.update(MODEL_INSTANCES)
            .where(MODEL_INSTANCES.c.id == instance_id)
            .values(
                status=ModelInstanceStatus.RUNNING.value,
                error_code=None,
                last_healthy_at=as_utc(now),
                updated_at=as_utc(now),
            )
        )
        await self._append_status_event(
            instance_id,
            actor="controller",
            occurred_at=now,
            from_status=ModelInstanceStatus.STARTING,
            to_status=ModelInstanceStatus.RUNNING,
        )
        await self._route_changed(instance_id)
        return True

    async def release_and_transition(
        self,
        instance_id: UUID,
        *,
        expected_statuses: tuple[ModelInstanceStatus, ...],
        target_status: ModelInstanceStatus,
        error_code: str | None,
        now: datetime,
    ) -> bool:
        """Release every active Allocation with one terminal instance transition."""

        if target_status not in {ModelInstanceStatus.STOPPED, ModelInstanceStatus.FAILED}:
            raise ValueError("controller release target must be terminal")
        current = await self._session.scalar(
            sa.select(MODEL_INSTANCES.c.status)
            .where(MODEL_INSTANCES.c.id == instance_id)
            .with_for_update()
        )
        if current not in {status.value for status in expected_statuses}:
            return False
        await self._session.execute(
            sa.update(GPU_ALLOCATIONS)
            .where(
                GPU_ALLOCATIONS.c.instance_id == instance_id,
                GPU_ALLOCATIONS.c.status != GpuAllocationStatus.RELEASED.value,
            )
            .values(
                status=GpuAllocationStatus.RELEASED.value,
                released_at=as_utc(now),
                updated_at=as_utc(now),
            )
        )
        await self._session.execute(
            sa.update(MODEL_INSTANCES)
            .where(MODEL_INSTANCES.c.id == instance_id)
            .values(
                status=target_status.value,
                error_code=error_code,
                internal_endpoint=None,
                updated_at=as_utc(now),
            )
        )
        await self._append_status_event(
            instance_id,
            actor="controller",
            occurred_at=now,
            from_status=ModelInstanceStatus(current),
            to_status=target_status,
        )
        await self._route_changed(instance_id)
        return True

    async def retry_waiting_after_release(self, *, now: datetime) -> tuple[UUID, ...]:
        """Retry waiting reservations in creation order through the primitive."""

        mappings = (
            (
                await self._session.execute(
                    sa.select(MODEL_INSTANCES)
                    .where(
                        MODEL_INSTANCES.c.status == ModelInstanceStatus.WAITING_FOR_RESOURCE.value
                    )
                    .order_by(MODEL_INSTANCES.c.created_at, MODEL_INSTANCES.c.id)
                    .with_for_update(skip_locked=True)
                )
            )
            .mappings()
            .all()
        )
        started: list[UUID] = []
        for mapping in mappings:
            try:
                result = await self.reserve_or_wait(_record(mapping), now=now)
            except ModelInstanceArtifactNotReady:
                continue
            if result.status is ModelInstanceStatus.STARTING:
                started.append(result.id)
        return tuple(started)

    async def delete(self, instance_id: UUID) -> None:
        await self._session.execute(
            sa.delete(MODEL_INSTANCES).where(MODEL_INSTANCES.c.id == instance_id)
        )

    async def has_ai_service_reference(self, instance_id: UUID) -> bool:
        # Local import prevents the table-definition modules from forming an
        # import cycle while keeping the deletion guard in this repository.
        from holm_persistence.ai_services import AI_SERVICE_BACKENDS

        count = (
            await self._session.execute(
                sa.select(sa.func.count())
                .select_from(AI_SERVICE_BACKENDS)
                .where(
                    AI_SERVICE_BACKENDS.c.instance_id == instance_id,
                    AI_SERVICE_BACKENDS.c.retired_at.is_(None),
                )
            )
        ).scalar_one()
        return int(count) > 0

    async def count_for_model(self, model_id: UUID) -> int:
        return int(
            (
                await self._session.execute(
                    sa.select(sa.func.count())
                    .select_from(
                        MODEL_INSTANCES.join(
                            MODEL_ARTIFACTS,
                            MODEL_INSTANCES.c.artifact_id == MODEL_ARTIFACTS.c.id,
                        )
                    )
                    .where(MODEL_ARTIFACTS.c.model_id == model_id)
                )
            ).scalar_one()
        )


class ModelInstanceReferenceRepository:
    """Adapter for the Model catalog's pre-existing probe."""

    async def count(self, *, session: AsyncSession, model_id: UUID) -> int:
        return await ModelInstanceRepository(session).count_for_model(model_id)


__all__ = [
    "ACTIVE_GPU_UNIQUE_INDEX",
    "ControllerModelInstanceRecord",
    "ModelInstanceCreate",
    "ModelInstanceEventPosition",
    "ModelInstanceEventRecord",
    "ModelInstanceRecord",
    "ModelInstanceReferenceRepository",
    "ModelInstanceRepository",
]

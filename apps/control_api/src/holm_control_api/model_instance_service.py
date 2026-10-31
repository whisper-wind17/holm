"""Model Instance lifecycle and atomic GPU reservation service."""

from __future__ import annotations

import base64
import binascii
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from holm_contracts.agent.v1 import agent_pb2
from holm_domain import (
    AuditAction,
    AuditActorType,
    AuditObjectType,
    AuditResult,
    DomainEventType,
    ModelArtifactStatus,
    ModelInstanceStatus,
    RuntimeType,
    TensorParallelPartitionResult,
    validate_tensor_parallel_partition,
)
from holm_domain.model_instances import ModelInstanceEventType
from holm_persistence import (
    AiServiceRepository,
    AsyncSessionFactory,
    AuditEventDraft,
    AuditWriter,
    ModelArtifactDeploymentMetadata,
    ModelInstanceCreate,
    ModelInstanceRecord,
    ModelInstanceRepository,
    append_route_invalidation_event,
    database_utc_now,
)
from holm_persistence.instance_cleanup_repository import InstanceCleanupRepository
from holm_persistence.model_instance_repository import (
    ModelInstanceArtifactNotReady,
    ModelInstanceEventPosition,
    ModelInstanceEventRecord,
)
from holm_runtimes import RuntimeCompatibilityCatalog, validate_runtime_launch_policy
from holm_security import build_audit_details

from holm_control_api.agent_client import (
    AgentLogEntry,
    AgentLogPage,
    AgentLogUnavailable,
    AgentLogUnavailableReason,
    AgentSnapshot,
    HostAgentClient,
    HostAgentLogClient,
)

_LOGGER = logging.getLogger("holm.control_api.model_instances")


class ModelInstanceNotFound(LookupError):
    """The requested Model Instance does not exist."""


class ModelInstanceInvalidConfiguration(ValueError):
    """The requested GPU or TP configuration is invalid."""

    def __init__(self, code: str = "invalid_request") -> None:
        super().__init__(code)
        self.code = code


class ModelInstanceInvalidState(RuntimeError):
    """The lifecycle command is not valid in the current state."""


class ModelInstanceGpuInventoryUnavailable(RuntimeError):
    """The Host Agent could not provide a usable GPU inventory."""


class ModelInstanceReferenced(RuntimeError):
    """An active AI Service still uses this Model Instance."""


class ModelInstanceLogCursorInvalid(ValueError):
    """The log cursor is malformed, out of range, or belongs to another instance."""


class ModelInstanceLogsUnavailable(RuntimeError):
    """The Host Agent could not provide a safe bounded log page."""


@dataclass(frozen=True, slots=True)
class CreateModelInstance:
    artifact_id: UUID
    runtime_type: RuntimeType
    gpu_uuids: tuple[str, ...]
    tensor_parallel_size: int
    context_length: int
    gpu_memory_utilization: float
    name: str | None = None


@dataclass(frozen=True, slots=True)
class ModelInstanceLogPage:
    entries: tuple[AgentLogEntry, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class ModelInstanceEventPage:
    items: tuple[ModelInstanceEventRecord, ...]
    next_cursor: str | None


class ModelInstanceService:
    """Apply lifecycle commands without starting a Runtime directly."""

    __slots__ = (
        "_agent",
        "_audit_writer",
        "_log_agent",
        "_runtime_catalog",
        "_session_factory",
    )

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory,
        agent: HostAgentClient,
        log_agent: HostAgentLogClient | None = None,
        runtime_catalog: RuntimeCompatibilityCatalog | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._agent = agent
        self._log_agent = log_agent
        self._runtime_catalog = runtime_catalog
        self._audit_writer = AuditWriter(session_factory)

    async def list(self) -> tuple[ModelInstanceRecord, ...]:
        async with self._session_factory() as session:
            return await ModelInstanceRepository(session).list()

    async def get(self, instance_id: UUID) -> ModelInstanceRecord:
        async with self._session_factory() as session:
            record = await ModelInstanceRepository(session).get(instance_id)
        if record is None:
            raise ModelInstanceNotFound()
        return record

    async def logs(
        self,
        instance_id: UUID,
        *,
        cursor: str | None,
        page_size: int,
    ) -> ModelInstanceLogPage:
        await self.get(instance_id)
        if not 1 <= page_size <= 100:
            raise ModelInstanceLogCursorInvalid()
        offset = 0 if cursor is None else _decode_log_cursor(cursor, instance_id)
        if self._log_agent is None:
            raise ModelInstanceLogsUnavailable()
        observation = await self._log_agent.read_logs(
            str(instance_id),
            cursor_offset=offset,
            page_size=page_size,
        )
        if isinstance(observation, AgentLogUnavailable):
            if observation.reason is AgentLogUnavailableReason.REQUEST_REJECTED:
                raise ModelInstanceLogCursorInvalid()
            raise ModelInstanceLogsUnavailable()
        if not _valid_log_page(observation, offset=offset, page_size=page_size):
            raise ModelInstanceLogsUnavailable()
        next_cursor = (
            _encode_log_cursor(instance_id, observation.next_cursor_offset)
            if observation.has_more
            else None
        )
        return ModelInstanceLogPage(entries=observation.entries, next_cursor=next_cursor)

    async def events(
        self,
        instance_id: UUID,
        *,
        cursor: str | None,
        page_size: int,
    ) -> ModelInstanceEventPage:
        if not 1 <= page_size <= 100:
            raise ModelInstanceInvalidConfiguration()
        before = None if cursor is None else _decode_event_cursor(cursor, instance_id)
        async with self._session_factory() as session:
            repository = ModelInstanceRepository(session)
            records = await repository.list_events(
                instance_id,
                before=before,
                limit=page_size + 1,
            )
            if not records and before is None and await repository.get(instance_id) is None:
                raise ModelInstanceNotFound()
        has_more = len(records) > page_size
        items = records[:page_size]
        next_cursor = _encode_event_cursor(instance_id, items[-1]) if has_more and items else None
        return ModelInstanceEventPage(items=items, next_cursor=next_cursor)

    async def create(self, value: CreateModelInstance) -> ModelInstanceRecord:
        normalized = self._validate_configuration(value)
        await self._validate_gpu_inventory(normalized.gpu_uuids)
        async with self._session_factory() as session, session.begin():
            repository = ModelInstanceRepository(session)
            artifact = await repository.artifact_deployment_metadata_for_update(
                normalized.artifact_id
            )
            if artifact is None or artifact.status is not ModelArtifactStatus.READY:
                raise ModelInstanceArtifactNotReady()
            self._validate_model_partition(normalized, artifact)
            now = await database_utc_now(session)
            instance = await repository.create(
                ModelInstanceCreate(
                    name=normalized.name,
                    artifact_id=normalized.artifact_id,
                    runtime_type=normalized.runtime_type,
                    expected_gpu_uuids=normalized.gpu_uuids,
                    tensor_parallel_size=normalized.tensor_parallel_size,
                    context_length=normalized.context_length,
                    gpu_memory_utilization=normalized.gpu_memory_utilization,
                ),
                now=now,
                actor="admin",
            )
            return await repository.reserve_or_wait(instance, now=now, actor="admin")

    async def start(self, instance_id: UUID) -> ModelInstanceRecord:
        current = await self.get(instance_id)
        await self._validate_gpu_inventory(current.expected_gpu_uuids)
        async with self._session_factory() as session, session.begin():
            repository = ModelInstanceRepository(session)
            if not await repository.try_lifecycle_lock(instance_id):
                raise ModelInstanceInvalidState()
            instance = await repository.get(instance_id, for_update=True)
            if instance is None:
                raise ModelInstanceNotFound()
            if instance.status not in {
                ModelInstanceStatus.STOPPED,
                ModelInstanceStatus.FAILED,
                ModelInstanceStatus.WAITING_FOR_RESOURCE,
            }:
                raise ModelInstanceInvalidState()
            now = await database_utc_now(session)
            return await repository.reserve_or_wait(
                instance,
                now=now,
                reset_restart_window=True,
                actor="admin",
                action_event=(
                    ModelInstanceEventType.WAITING_FOR_GPU
                    if instance.status is ModelInstanceStatus.WAITING_FOR_RESOURCE
                    else None
                ),
            )

    async def retry(self, instance_id: UUID) -> ModelInstanceRecord:
        current = await self.get(instance_id)
        await self._validate_gpu_inventory(current.expected_gpu_uuids)
        async with self._session_factory() as session, session.begin():
            repository = ModelInstanceRepository(session)
            if not await repository.try_lifecycle_lock(instance_id):
                raise ModelInstanceInvalidState()
            instance = await repository.get(instance_id, for_update=True)
            if instance is None:
                raise ModelInstanceNotFound()
            if instance.status is not ModelInstanceStatus.FAILED:
                raise ModelInstanceInvalidState()
            return await repository.reserve_or_wait(
                instance,
                now=await database_utc_now(session),
                reset_restart_window=True,
                actor="admin",
                action_event=ModelInstanceEventType.RETRY_REQUESTED,
            )

    async def restart(self, instance_id: UUID) -> ModelInstanceRecord:
        async with self._session_factory() as session, session.begin():
            repository = ModelInstanceRepository(session)
            if not await repository.try_lifecycle_lock(instance_id):
                raise ModelInstanceInvalidState()
            instance = await repository.get(instance_id, for_update=True)
            if instance is None:
                raise ModelInstanceNotFound()
            restarted = await repository.request_manual_restart(
                instance,
                now=await database_utc_now(session),
            )
            if restarted is None:
                raise ModelInstanceInvalidState()
            return restarted

    async def select_gpus(
        self,
        instance_id: UUID,
        gpu_uuids: tuple[str, ...],
    ) -> ModelInstanceRecord:
        await self.get(instance_id)
        normalized = tuple(sorted(gpu_uuids))
        if (
            not normalized
            or len(set(normalized)) != len(normalized)
            or any(not value or value != value.strip() for value in normalized)
        ):
            raise ModelInstanceInvalidConfiguration()
        await self._validate_gpu_inventory(normalized)
        async with self._session_factory() as session, session.begin():
            repository = ModelInstanceRepository(session)
            if not await repository.try_lifecycle_lock(instance_id):
                raise ModelInstanceInvalidState()
            instance = await repository.get(instance_id, for_update=True)
            if instance is None:
                raise ModelInstanceNotFound()
            if instance.status is not ModelInstanceStatus.WAITING_FOR_RESOURCE:
                raise ModelInstanceInvalidState()
            return await repository.select_gpus_and_reserve(
                instance,
                gpu_uuids=normalized,
                now=await database_utc_now(session),
            )

    async def cancel(
        self,
        instance_id: UUID,
        *,
        actor_id: UUID,
        request_id: UUID,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            repository = ModelInstanceRepository(session)
            if not await repository.try_lifecycle_lock(instance_id):
                raise ModelInstanceInvalidState()
            instance = await repository.get(instance_id, for_update=True)
            if instance is None:
                raise ModelInstanceNotFound()
            if instance.status is not ModelInstanceStatus.WAITING_FOR_RESOURCE:
                raise ModelInstanceInvalidState()
            now = await database_utc_now(session)
            await repository.cancel_waiting(instance, now=now)
            await self._audit_writer.add(
                session,
                AuditEventDraft(
                    actor_type=AuditActorType.USER,
                    actor_id=actor_id,
                    action=AuditAction.MODEL_INSTANCE_LIFECYCLE,
                    object_type=AuditObjectType.MODEL_INSTANCE,
                    object_id=instance_id,
                    result=AuditResult.SUCCEEDED,
                    request_id=request_id,
                    details=build_audit_details(
                        AuditAction.MODEL_INSTANCE_LIFECYCLE.value,
                        {},
                    ),
                ),
            )

    async def stop(self, instance_id: UUID) -> ModelInstanceRecord:
        async with self._session_factory() as session, session.begin():
            repository = ModelInstanceRepository(session)
            if not await repository.try_lifecycle_lock(instance_id):
                raise ModelInstanceInvalidState()
            instance = await repository.get(instance_id, for_update=True)
            if instance is None:
                raise ModelInstanceNotFound()
            if instance.status not in {
                ModelInstanceStatus.RUNNING,
                ModelInstanceStatus.STARTING,
                ModelInstanceStatus.UNHEALTHY,
                ModelInstanceStatus.RESTARTING,
            }:
                raise ModelInstanceInvalidState()
            return await repository.stop(instance, now=await database_utc_now(session))

    async def delete(
        self,
        instance_id: UUID,
        *,
        delete_bound_services: bool = False,
        actor_id: UUID | None = None,
        request_id: UUID | None = None,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            repository = ModelInstanceRepository(session)
            if not await repository.try_lifecycle_lock(instance_id):
                raise ModelInstanceInvalidState()
            instance = await repository.get(instance_id, for_update=True)
            if instance is None:
                raise ModelInstanceNotFound()
            if instance.status not in {
                ModelInstanceStatus.STOPPED,
                ModelInstanceStatus.FAILED,
            }:
                raise ModelInstanceInvalidState()
            if delete_bound_services:
                if actor_id is None or request_id is None:
                    raise ValueError("Service deletion requires an audit actor and request")
                service_ids = await AiServiceRepository(session).soft_delete_bound_to_instance(
                    instance_id, now=await database_utc_now(session)
                )
                for service_id in service_ids:
                    await self._audit_writer.add(
                        session,
                        AuditEventDraft(
                            actor_type=AuditActorType.USER,
                            actor_id=actor_id,
                            action=AuditAction.AI_SERVICE_CHANGE,
                            object_type=AuditObjectType.AI_SERVICE,
                            object_id=service_id,
                            result=AuditResult.SUCCEEDED,
                            request_id=request_id,
                            details=build_audit_details(AuditAction.AI_SERVICE_CHANGE.value, {}),
                        ),
                    )
                    await append_route_invalidation_event(
                        session,
                        event=DomainEventType.AI_SERVICE_CHANGED,
                        aggregate_id=service_id,
                    )
            if await repository.has_ai_service_reference(instance_id):
                raise ModelInstanceReferenced()
            await InstanceCleanupRepository(session).request(instance_id)
            await repository.delete(instance_id)

    def _validate_configuration(self, value: CreateModelInstance) -> CreateModelInstance:
        gpu_uuids = tuple(sorted(value.gpu_uuids))
        if (
            not gpu_uuids
            or any(not gpu_uuid or gpu_uuid != gpu_uuid.strip() for gpu_uuid in gpu_uuids)
            or len(set(gpu_uuids)) != len(gpu_uuids)
            or value.tensor_parallel_size != len(gpu_uuids)
            or value.context_length <= 0
            or not 0 < value.gpu_memory_utilization <= 1
        ):
            raise ModelInstanceInvalidConfiguration()
        if self._runtime_catalog is None:
            if value.runtime_type is not RuntimeType.VLLM:
                raise ModelInstanceInvalidConfiguration("runtime_not_supported")
        else:
            runtime = self._runtime_catalog.runtime(value.runtime_type.value)
            if runtime is None:
                raise ModelInstanceInvalidConfiguration("runtime_not_supported")
            rejection = validate_runtime_launch_policy(
                runtime,
                selected_gpu_count=len(gpu_uuids),
                context_length=value.context_length,
                tensor_parallel_size=value.tensor_parallel_size,
            )
            if rejection is not None:
                raise ModelInstanceInvalidConfiguration(rejection)
        return CreateModelInstance(
            name=value.name,
            artifact_id=value.artifact_id,
            runtime_type=value.runtime_type,
            gpu_uuids=gpu_uuids,
            tensor_parallel_size=value.tensor_parallel_size,
            context_length=value.context_length,
            gpu_memory_utilization=value.gpu_memory_utilization,
        )

    def _validate_model_partition(
        self,
        value: CreateModelInstance,
        artifact: ModelArtifactDeploymentMetadata,
    ) -> None:
        runtime = (
            None
            if self._runtime_catalog is None
            else self._runtime_catalog.runtime(value.runtime_type.value)
        )
        result = TensorParallelPartitionResult.UNAVAILABLE
        if runtime is not None and runtime.tp_policy.model_partition_check == "runtime_adapter":
            result = validate_tensor_parallel_partition(
                artifact.attention_heads,
                artifact.kv_heads,
                value.tensor_parallel_size,
            )
        if result is TensorParallelPartitionResult.INCOMPATIBLE:
            raise ModelInstanceInvalidConfiguration("invalid_tensor_parallel_size")
        if result is TensorParallelPartitionResult.UNAVAILABLE:
            _LOGGER.info(
                "model partition metadata unavailable; synchronous TP check not completed",
                extra={
                    "event_name": "model_instance_tp_partition_check_unavailable",
                    "component": "control-api",
                    "artifact_id": str(value.artifact_id),
                    "attention_heads_collected": artifact.attention_heads is not None,
                    "kv_heads_collected": artifact.kv_heads is not None,
                },
            )

    async def _validate_gpu_inventory(self, requested: tuple[str, ...]) -> None:
        observation = await self._agent.observe()
        if not isinstance(observation, AgentSnapshot):
            raise ModelInstanceGpuInventoryUnavailable()
        inventory = observation.gpu_inventory
        if inventory.discovery_state != agent_pb2.GPU_DISCOVERY_STATE_READY:
            raise ModelInstanceGpuInventoryUnavailable()
        known = {device.uuid for device in inventory.devices if device.uuid}
        if not set(requested).issubset(known):
            raise ModelInstanceInvalidConfiguration()


def _encode_log_cursor(instance_id: UUID, offset: int) -> str:
    payload = json.dumps(
        {"instance_id": str(instance_id), "offset": offset, "version": 1},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _encode_event_cursor(
    instance_id: UUID,
    event: ModelInstanceEventRecord,
) -> str:
    payload = json.dumps(
        {
            "event_id": str(event.id),
            "instance_id": str(instance_id),
            "occurred_at": event.occurred_at.isoformat(),
            "version": 1,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_event_cursor(cursor: str, instance_id: UUID) -> ModelInstanceEventPosition:
    try:
        padding = "=" * (-len(cursor) % 4)
        payload: object = json.loads(
            base64.b64decode(cursor + padding, altchars=b"-_", validate=True).decode("utf-8")
        )
        if not isinstance(payload, dict) or set(payload) != {
            "event_id",
            "instance_id",
            "occurred_at",
            "version",
        }:
            raise ValueError
        occurred_at = datetime.fromisoformat(str(payload["occurred_at"]))
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError
        if payload["instance_id"] != str(instance_id) or payload["version"] != 1:
            raise ValueError
        return ModelInstanceEventPosition(
            occurred_at=occurred_at.astimezone(UTC),
            event_id=UUID(str(payload["event_id"])),
        )
    except (binascii.Error, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ModelInstanceInvalidConfiguration() from None


def _decode_log_cursor(cursor: str, instance_id: UUID) -> int:
    try:
        padding = "=" * (-len(cursor) % 4)
        payload: object = json.loads(
            base64.b64decode(cursor + padding, altchars=b"-_", validate=True).decode("utf-8")
        )
    except (binascii.Error, UnicodeError, json.JSONDecodeError, ValueError):
        payload = None
    if not isinstance(payload, dict) or set(payload) != {"instance_id", "offset", "version"}:
        raise ModelInstanceLogCursorInvalid()
    offset = payload["offset"]
    if (
        payload["instance_id"] != str(instance_id)
        or payload["version"] != 1
        or isinstance(offset, bool)
        or not isinstance(offset, int)
        or not 0 <= offset <= 500
    ):
        raise ModelInstanceLogCursorInvalid()
    return offset


def _valid_log_page(page: AgentLogPage, *, offset: int, page_size: int) -> bool:
    expected_next = offset + len(page.entries)
    if (
        len(page.entries) > page_size
        or page.next_cursor_offset != expected_next
        or page.next_cursor_offset > 500
    ):
        return False
    return not page.has_more or len(page.entries) == page_size


__all__ = [
    "CreateModelInstance",
    "ModelInstanceArtifactNotReady",
    "ModelInstanceGpuInventoryUnavailable",
    "ModelInstanceInvalidConfiguration",
    "ModelInstanceInvalidState",
    "ModelInstanceEventPage",
    "ModelInstanceLogCursorInvalid",
    "ModelInstanceLogPage",
    "ModelInstanceLogsUnavailable",
    "ModelInstanceNotFound",
    "ModelInstanceReferenced",
    "ModelInstanceService",
]

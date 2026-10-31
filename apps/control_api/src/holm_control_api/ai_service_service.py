"""AI Service publishing and Primary Backend updates."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import sqlalchemy as sa
from holm_domain import (
    AiServiceRuntimeStatus,
    AuditAction,
    AuditActorType,
    AuditObjectType,
    AuditResult,
    DomainEventType,
    ModelInstanceStatus,
    derive_ai_service_runtime_status,
)
from holm_persistence import (
    AiServiceCreate,
    AiServiceRecord,
    AiServiceRepository,
    AsyncSessionFactory,
    AuditEventDraft,
    AuditWriter,
    DuplicateAiServiceNameError,
    append_route_invalidation_event,
    database_utc_now,
)
from holm_persistence.ai_service_repository import DuplicateAiServiceDisplayNameError
from holm_persistence.model_instances import MODEL_INSTANCES
from holm_security import build_audit_details
from sqlalchemy.ext.asyncio import AsyncSession

from holm_control_api.model_instance_service import ModelInstanceInvalidState


class AiServiceDisplayNameConflict(RuntimeError):
    """The UI name belongs to another service."""


class AiServiceNotFound(LookupError):
    """The requested AI Service does not exist."""


class AiServicePrimaryInstanceNotFound(LookupError):
    """The requested Primary Backend Model Instance does not exist."""


class AiServiceVersionConflict(RuntimeError):
    """The service route changed since the administrator loaded it."""


class AiServiceDeleteConflict(RuntimeError):
    """An enabled service still has a nonterminal backend instance."""


class AiServiceNameConflict(RuntimeError):
    """The public model name belongs to another active AI Service."""


@dataclass(frozen=True, slots=True)
class CreateAiService:
    public_model_name: str
    description: str
    enabled: bool
    primary_instance_id: UUID
    name: str | None = None


@dataclass(frozen=True, slots=True)
class UpdateAiService:
    description: str | None = None
    enabled: bool | None = None
    primary_instance_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class AiServiceRuntimeRecord:
    """AI Service plus the current fact used to derive its four-state status."""

    service: AiServiceRecord
    primary_instance_status: ModelInstanceStatus | None
    runtime_status: AiServiceRuntimeStatus


class AiServiceManagementService:
    """Keep service mutations and their audit records atomic."""

    __slots__ = ("_audit_writer", "_session_factory")

    def __init__(self, *, session_factory: AsyncSessionFactory) -> None:
        self._session_factory = session_factory
        self._audit_writer = AuditWriter(session_factory)

    async def list(self) -> tuple[AiServiceRecord, ...]:
        async with self._session_factory() as session:
            return await AiServiceRepository(session).list()

    async def get(self, service_id: UUID) -> AiServiceRecord:
        async with self._session_factory() as session:
            record = await AiServiceRepository(session).get(service_id)
        if record is None:
            raise AiServiceNotFound()
        return record

    async def list_with_runtime_status(self) -> tuple[AiServiceRuntimeRecord, ...]:
        async with self._session_factory() as session:
            records = await AiServiceRepository(session).list()
            statuses = await self._instance_statuses(
                session,
                tuple(record.primary_instance_id for record in records),
            )
        return tuple(self._runtime_record(record, statuses) for record in records)

    async def get_with_runtime_status(self, service_id: UUID) -> AiServiceRuntimeRecord:
        async with self._session_factory() as session:
            record = await AiServiceRepository(session).get(service_id)
            if record is None:
                raise AiServiceNotFound()
            statuses = await self._instance_statuses(session, (record.primary_instance_id,))
        return self._runtime_record(record, statuses)

    async def create(
        self,
        value: CreateAiService,
        *,
        actor_id: UUID,
        request_id: UUID,
    ) -> AiServiceRecord:
        async with self._session_factory() as session, session.begin():
            repository = AiServiceRepository(session)
            target = await repository.instance_for_share(value.primary_instance_id)
            if target is None:
                raise AiServicePrimaryInstanceNotFound()
            if target.status is not ModelInstanceStatus.RUNNING:
                raise ModelInstanceInvalidState()
            now = await database_utc_now(session)
            try:
                record = await repository.create(
                    AiServiceCreate(
                        name=value.name,
                        public_model_name=value.public_model_name,
                        description=value.description,
                        enabled=value.enabled,
                        primary_instance_id=value.primary_instance_id,
                    ),
                    now=now,
                )
            except DuplicateAiServiceDisplayNameError:
                raise AiServiceDisplayNameConflict() from None
            except DuplicateAiServiceNameError:
                raise AiServiceNameConflict() from None
            await self._audit_writer.add(
                session,
                self._audit_event(
                    actor_id=actor_id,
                    object_id=record.id,
                    request_id=request_id,
                ),
            )
            await append_route_invalidation_event(
                session,
                event=DomainEventType.AI_SERVICE_CHANGED,
                aggregate_id=record.id,
            )
            return record

    async def update(
        self,
        service_id: UUID,
        value: UpdateAiService,
        *,
        actor_id: UUID,
        request_id: UUID,
    ) -> AiServiceRecord:
        async with self._session_factory() as session, session.begin():
            repository = AiServiceRepository(session)
            current = await repository.get(service_id, for_update=True)
            if current is None:
                raise AiServiceNotFound()
            primary_instance_id = (
                current.primary_instance_id
                if value.primary_instance_id is None
                else value.primary_instance_id
            )
            rebind = primary_instance_id != current.primary_instance_id
            target = None
            # The settings form resubmits the current binding with every save, so only an
            # actual rebind is gated on Running; otherwise a stopped backend would block
            # unrelated edits such as the description.
            if rebind:
                target = await repository.instance_for_share(primary_instance_id)
                if target is None:
                    raise AiServicePrimaryInstanceNotFound()
                if target.status is not ModelInstanceStatus.RUNNING:
                    raise ModelInstanceInvalidState()
            now = await database_utc_now(session)
            if rebind:
                if target is None:
                    raise RuntimeError("rebind target was not loaded")
                await repository.rebind_primary(service_id, target=target, now=now)
            updated = await repository.update(
                service_id,
                description=current.description if value.description is None else value.description,
                enabled=current.enabled if value.enabled is None else value.enabled,
                primary_instance_id=primary_instance_id,
                route_version=current.route_version + int(rebind),
                now=now,
            )
            await self._audit_writer.add(
                session,
                self._audit_event(
                    actor_id=actor_id,
                    object_id=updated.id,
                    request_id=request_id,
                ),
            )
            await append_route_invalidation_event(
                session,
                event=DomainEventType.AI_SERVICE_CHANGED,
                aggregate_id=updated.id,
            )
            return updated

    async def delete(self, service_id: UUID, *, actor_id: UUID, request_id: UUID) -> None:
        async with self._session_factory() as session, session.begin():
            repository = AiServiceRepository(session)
            current = await repository.get(service_id, for_update=True)
            if current is None:
                raise AiServiceNotFound()
            statuses = await self._instance_statuses(session, (current.primary_instance_id,))
            if current.enabled and statuses.get(current.primary_instance_id) not in {
                ModelInstanceStatus.STOPPED,
                ModelInstanceStatus.FAILED,
            }:
                raise AiServiceDeleteConflict()
            await repository.soft_delete((service_id,), now=await database_utc_now(session))
            await self._audit_writer.add(
                session,
                self._audit_event(actor_id=actor_id, object_id=service_id, request_id=request_id),
            )
            await append_route_invalidation_event(
                session,
                event=DomainEventType.AI_SERVICE_CHANGED,
                aggregate_id=service_id,
            )

    async def rename(
        self,
        service_id: UUID,
        *,
        public_model_name: str,
        expected_route_version: int,
        actor_id: UUID,
        request_id: UUID,
    ) -> AiServiceRecord:
        async with self._session_factory() as session, session.begin():
            repository = AiServiceRepository(session)
            current = await repository.get(service_id, for_update=True)
            if current is None:
                raise AiServiceNotFound()
            if current.route_version != expected_route_version:
                raise AiServiceVersionConflict()
            if current.public_model_name == public_model_name:
                return current
            try:
                updated = await repository.rename(
                    current,
                    public_model_name=public_model_name,
                    now=await database_utc_now(session),
                )
            except DuplicateAiServiceNameError:
                raise AiServiceNameConflict() from None
            await self._audit_writer.add(
                session,
                self._audit_event(actor_id=actor_id, object_id=service_id, request_id=request_id),
            )
            await append_route_invalidation_event(
                session,
                event=DomainEventType.AI_SERVICE_CHANGED,
                aggregate_id=service_id,
            )
            return updated

    async def rename_display_name(
        self,
        service_id: UUID,
        *,
        name: str,
        expected_route_version: int,
        actor_id: UUID,
        request_id: UUID,
    ) -> AiServiceRecord:
        async with self._session_factory() as session, session.begin():
            repository = AiServiceRepository(session)
            current = await repository.get(service_id, for_update=True)
            if current is None:
                raise AiServiceNotFound()
            if current.route_version != expected_route_version:
                raise AiServiceVersionConflict()
            if current.name == name:
                return current
            try:
                updated = await repository.rename_display_name(
                    current,
                    name=name,
                    now=await database_utc_now(session),
                )
            except DuplicateAiServiceDisplayNameError:
                raise AiServiceDisplayNameConflict() from None
            await self._audit_writer.add(
                session,
                self._audit_event(actor_id=actor_id, object_id=service_id, request_id=request_id),
            )
            await append_route_invalidation_event(
                session,
                event=DomainEventType.AI_SERVICE_CHANGED,
                aggregate_id=service_id,
            )
            return updated

    @staticmethod
    async def _instance_statuses(
        session: AsyncSession,
        instance_ids: tuple[UUID, ...],
    ) -> dict[UUID, ModelInstanceStatus]:
        if not instance_ids:
            return {}
        rows = (
            await session.execute(
                sa.select(MODEL_INSTANCES.c.id, MODEL_INSTANCES.c.status).where(
                    MODEL_INSTANCES.c.id.in_(instance_ids)
                )
            )
        ).all()
        return {instance_id: ModelInstanceStatus(status) for instance_id, status in rows}

    @staticmethod
    def _runtime_record(
        record: AiServiceRecord,
        statuses: dict[UUID, ModelInstanceStatus],
    ) -> AiServiceRuntimeRecord:
        primary_instance_status = statuses.get(record.primary_instance_id)
        return AiServiceRuntimeRecord(
            service=record,
            primary_instance_status=primary_instance_status,
            runtime_status=derive_ai_service_runtime_status(
                enabled=record.enabled,
                primary_instance_status=primary_instance_status,
            ),
        )

    @staticmethod
    def _audit_event(*, actor_id: UUID, object_id: UUID, request_id: UUID) -> AuditEventDraft:
        return AuditEventDraft(
            actor_type=AuditActorType.USER,
            actor_id=actor_id,
            action=AuditAction.AI_SERVICE_CHANGE,
            object_type=AuditObjectType.AI_SERVICE,
            object_id=object_id,
            result=AuditResult.SUCCEEDED,
            request_id=request_id,
            details=build_audit_details(AuditAction.AI_SERVICE_CHANGE.value, {}),
        )


__all__ = [
    "AiServiceManagementService",
    "AiServiceNameConflict",
    "AiServiceNotFound",
    "AiServicePrimaryInstanceNotFound",
    "AiServiceRuntimeRecord",
    "CreateAiService",
    "UpdateAiService",
]

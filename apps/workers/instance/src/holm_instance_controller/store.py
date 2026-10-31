"""Transactional persistence boundary for the instance controller."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Protocol
from uuid import UUID

from holm_domain import PRODUCTION_EVENT_REGISTRY, DomainEventType, ModelInstanceStatus
from holm_domain.instance_cleanup import CleanupFailure
from holm_domain.launch_verification import LaunchVerification
from holm_persistence import (
    AsyncSessionFactory,
    ControllerModelInstanceRecord,
    DomainEventRepository,
    ModelInstanceRepository,
    database_utc_now,
)
from holm_persistence.instance_cleanup_repository import InstanceCleanupRepository
from holm_persistence.launch_verification_repository import LaunchVerificationRepository


class ControllerStore(Protocol):
    async def pending_cleanup(self, after: UUID | None) -> tuple[UUID, ...]:
        """Read a bounded durable page, including previously failed requests."""

    async def cleanup_result(self, instance_id: UUID, failure: CleanupFailure | None) -> None:
        """Record a sanitized result under the caller-held lifecycle lock."""

    async def launch_verification(self, instance_id: UUID) -> LaunchVerification | None:
        """Read only the current attempt's durable verification result."""

    async def authorize_launch(self, expected: LaunchVerification) -> bool:
        """Fence a verified attempt against Stop and a newer attempt."""

    async def list_for_reconciliation(self) -> tuple[ControllerModelInstanceRecord, ...]:
        """Read the active instance/Allocation snapshot for one reconciliation pass."""

    def lifecycle_lock(self, instance_id: UUID) -> AbstractAsyncContextManager[None]:
        """Lock only one affected object's lifecycle writes for the pass."""

    async def claim(
        self,
        status: ModelInstanceStatus,
        *,
        excluded_ids: tuple[UUID, ...] = (),
    ) -> ControllerModelInstanceRecord | None:
        """Claim the oldest untracked item with SKIP LOCKED."""

    async def status(self, instance_id: UUID) -> ModelInstanceStatus | None:
        """Read the current lifecycle state."""

    async def set_endpoint(
        self,
        instance_id: UUID,
        *,
        expected_status: ModelInstanceStatus,
        endpoint: str,
    ) -> bool:
        """Publish the Agent-selected loopback Endpoint conditionally."""

    async def mark_running(self, instance_id: UUID) -> bool:
        """Atomically promote the instance and its Allocations."""

    async def mark_failed(
        self,
        instance_id: UUID,
        *,
        expected_status: ModelInstanceStatus,
        error_code: str,
    ) -> bool:
        """Release Allocations, fail the instance and retry waiting work."""

    async def mark_stopped(self, instance_id: UUID) -> bool:
        """Release Allocations, stop the instance and retry waiting work."""

    async def mark_unhealthy(
        self,
        instance_id: UUID,
        *,
        expected_status: ModelInstanceStatus,
        error_code: str,
    ) -> bool:
        """Retain Allocations but remove an unhealthy backend from routing."""

    async def begin_restart(self, instance_id: UUID, *, restart_window_seconds: int) -> bool:
        """Move Unhealthy to Restarting without releasing its GPUs."""

    async def record_restart_attempt(self, instance_id: UUID) -> int | None:
        """Persist and return the automatic attempt number."""

    async def mark_restarted_running(self, instance_id: UUID, *, endpoint: str) -> bool:
        """Publish a recovered Runtime while retaining the original GPU set."""

    async def release_terminal_allocations(self) -> tuple[UUID, ...]:
        """Release dangling Allocations owned by terminal instances."""

    async def record_orphan_isolated(self, instance_id: UUID) -> bool:
        """Append the content-free Orphan event once for this Runtime identity."""


class PostgresControllerStore:
    __slots__ = ("_session_factory",)

    def __init__(self, session_factory: AsyncSessionFactory) -> None:
        self._session_factory = session_factory

    async def pending_cleanup(self, after: UUID | None) -> tuple[UUID, ...]:
        async with self._session_factory() as session:
            return await InstanceCleanupRepository(session).pending(after)

    async def cleanup_result(self, instance_id: UUID, failure: CleanupFailure | None) -> None:
        async with self._session_factory() as session, session.begin():
            await InstanceCleanupRepository(session).result(instance_id, failure)

    async def launch_verification(self, instance_id: UUID) -> LaunchVerification | None:
        async with self._session_factory() as session:
            return await LaunchVerificationRepository(session).current(instance_id)

    async def authorize_launch(self, expected: LaunchVerification) -> bool:
        async with self._session_factory() as session, session.begin():
            return await LaunchVerificationRepository(session).authorize(expected)

    async def list_for_reconciliation(self) -> tuple[ControllerModelInstanceRecord, ...]:
        async with self._session_factory() as session:
            return await ModelInstanceRepository(session).list_for_controller(
                (
                    ModelInstanceStatus.STARTING,
                    ModelInstanceStatus.RUNNING,
                    ModelInstanceStatus.STOPPING,
                    ModelInstanceStatus.UNHEALTHY,
                    ModelInstanceStatus.RESTARTING,
                )
            )

    @asynccontextmanager
    async def lifecycle_lock(self, instance_id: UUID) -> AsyncIterator[None]:
        async with self._session_factory() as session, session.begin():
            await ModelInstanceRepository(session).acquire_lifecycle_lock(instance_id)
            yield

    async def claim(
        self,
        status: ModelInstanceStatus,
        *,
        excluded_ids: tuple[UUID, ...] = (),
    ) -> ControllerModelInstanceRecord | None:
        async with self._session_factory() as session, session.begin():
            return await ModelInstanceRepository(session).claim_for_controller(
                status,
                excluded_ids=excluded_ids,
            )

    async def status(self, instance_id: UUID) -> ModelInstanceStatus | None:
        async with self._session_factory() as session:
            record = await ModelInstanceRepository(session).get(instance_id)
        return None if record is None else record.status

    async def set_endpoint(
        self,
        instance_id: UUID,
        *,
        expected_status: ModelInstanceStatus,
        endpoint: str,
    ) -> bool:
        async with self._session_factory() as session, session.begin():
            return await ModelInstanceRepository(session).set_controller_endpoint(
                instance_id,
                expected_status=expected_status,
                endpoint=endpoint,
            )

    async def mark_running(self, instance_id: UUID) -> bool:
        async with self._session_factory() as session, session.begin():
            repository = ModelInstanceRepository(session)
            return await repository.mark_running(
                instance_id,
                now=await database_utc_now(session),
            )

    async def mark_failed(
        self,
        instance_id: UUID,
        *,
        expected_status: ModelInstanceStatus,
        error_code: str,
    ) -> bool:
        async with self._session_factory() as session, session.begin():
            repository = ModelInstanceRepository(session)
            now = await database_utc_now(session)
            changed = await repository.release_and_transition(
                instance_id,
                expected_statuses=(expected_status,),
                target_status=ModelInstanceStatus.FAILED,
                error_code=error_code,
                now=now,
            )
            if changed:
                await repository.retry_waiting_after_release(now=now)
            return changed

    async def mark_stopped(self, instance_id: UUID) -> bool:
        async with self._session_factory() as session, session.begin():
            repository = ModelInstanceRepository(session)
            now = await database_utc_now(session)
            changed = await repository.release_and_transition(
                instance_id,
                expected_statuses=(ModelInstanceStatus.STOPPING,),
                target_status=ModelInstanceStatus.STOPPED,
                error_code=None,
                now=now,
            )
            if changed:
                await repository.retry_waiting_after_release(now=now)
            return changed

    async def mark_unhealthy(
        self,
        instance_id: UUID,
        *,
        expected_status: ModelInstanceStatus,
        error_code: str,
    ) -> bool:
        async with self._session_factory() as session, session.begin():
            return await ModelInstanceRepository(session).mark_unhealthy(
                instance_id,
                expected_status=expected_status,
                error_code=error_code,
                now=await database_utc_now(session),
            )

    async def begin_restart(self, instance_id: UUID, *, restart_window_seconds: int) -> bool:
        async with self._session_factory() as session, session.begin():
            return await ModelInstanceRepository(session).begin_restart(
                instance_id,
                now=await database_utc_now(session),
                restart_window_seconds=restart_window_seconds,
            )

    async def record_restart_attempt(self, instance_id: UUID) -> int | None:
        async with self._session_factory() as session, session.begin():
            return await ModelInstanceRepository(session).record_restart_attempt(
                instance_id,
                now=await database_utc_now(session),
            )

    async def mark_restarted_running(self, instance_id: UUID, *, endpoint: str) -> bool:
        async with self._session_factory() as session, session.begin():
            return await ModelInstanceRepository(session).mark_restarted_running(
                instance_id,
                endpoint=endpoint,
                now=await database_utc_now(session),
            )

    async def release_terminal_allocations(self) -> tuple[UUID, ...]:
        async with self._session_factory() as session, session.begin():
            repository = ModelInstanceRepository(session)
            now = await database_utc_now(session)
            released = await repository.release_terminal_allocations(now=now)
            if released:
                await repository.retry_waiting_after_release(now=now)
            return released

    async def record_orphan_isolated(self, instance_id: UUID) -> bool:
        async with self._session_factory() as session, session.begin():
            repository = DomainEventRepository(session)
            event = DomainEventType.INSTANCE_RUNTIME_ORPHAN_ISOLATED.value
            if await repository.has_event(aggregate_id=instance_id, event=event):
                return False
            version = await repository.next_version(aggregate_id=instance_id)
            await repository.append(
                PRODUCTION_EVENT_REGISTRY.build_draft(
                    event,
                    aggregate_id=instance_id,
                    version=version,
                    candidates={"reason": "database_instance_missing_or_terminal"},
                )
            )
            return True


__all__ = ["ControllerStore", "PostgresControllerStore"]

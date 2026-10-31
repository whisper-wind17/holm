"""Registration and execution skeleton for lease-fenced idempotent job handlers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Set
from contextlib import suppress
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Protocol
from uuid import UUID

from holm_domain import JobExecutionKey, RetryPolicy

from holm_persistence.database import AsyncSessionFactory
from holm_persistence.job_repository import (
    DEFAULT_HEARTBEAT_SECONDS,
    DEFAULT_LEASE_SECONDS,
    JobRecord,
    JobRepository,
)

_LOGGER = logging.getLogger("holm.job_executor")

DEFAULT_RETRY_POLICY: Final[RetryPolicy] = RetryPolicy()


class JobFailure(RuntimeError):
    """A handler-declared, non-sensitive error code safe for job records."""

    def __init__(self, code: str) -> None:
        if not code or len(code) > 128 or not code.replace("_", "").isalnum():
            raise ValueError("invalid job failure code")
        self.code = code
        super().__init__("job operation failed")


class LeaseLostError(RuntimeError):
    """The handler no longer owns the job and must not perform more work."""

    def __init__(self) -> None:
        super().__init__("job lease ownership was lost")


@dataclass(frozen=True, slots=True)
class JobHandlerContext:
    """Handler input, including the required job/version idempotency key."""

    job: JobRecord
    lease_lost: asyncio.Event

    @property
    def idempotency_key(self) -> JobExecutionKey:
        return self.job.execution_key

    def ensure_lease(self) -> None:
        """Cooperative fence for handlers before every externally visible effect."""
        if self.lease_lost.is_set():
            raise LeaseLostError()


class JobHandler(Protocol):
    """A handler uses ``context.idempotency_key`` for each side effect."""

    async def __call__(self, context: JobHandlerContext) -> None: ...


class JobHandlerRegistry:
    """Default-deny registry: unknown and duplicate job types fail loudly."""

    __slots__ = ("_handlers",)

    def __init__(self) -> None:
        self._handlers: dict[str, JobHandler] = {}

    @property
    def handlers(self) -> Mapping[str, JobHandler]:
        return MappingProxyType(self._handlers)

    def register(self, job_type: str, handler: JobHandler) -> None:
        if not job_type or job_type != job_type.strip():
            raise ValueError("job handler type must be non-empty and trimmed")
        if job_type in self._handlers:
            raise ValueError("job handler type is already registered")
        self._handlers[job_type] = handler

    def resolve(self, job_type: str) -> JobHandler:
        try:
            return self._handlers[job_type]
        except KeyError:
            raise LookupError("job handler type is not registered") from None


@dataclass(frozen=True, slots=True)
class JobRunResult:
    job_id: UUID
    completed: bool


class JobExecutor:
    """Claim one job, renew it, and cancel the handler immediately on lease loss."""

    __slots__ = (
        "_session_factory",
        "_registry",
        "_owner",
        "_lease_seconds",
        "_heartbeat_seconds",
        "_retry_policy",
        "_sleep",
        "_job_types",
    )

    def __init__(
        self,
        session_factory: AsyncSessionFactory,
        registry: JobHandlerRegistry,
        *,
        owner: str,
        retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        job_types: Set[str] | None = None,
    ) -> None:
        if heartbeat_seconds <= 0 or lease_seconds <= heartbeat_seconds:
            raise ValueError("heartbeat must be positive and shorter than the lease")
        self._session_factory = session_factory
        self._registry = registry
        self._owner = owner
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._retry_policy = retry_policy
        self._sleep = sleep
        self._job_types = None if job_types is None else frozenset(job_types)

    async def _claim_one(self) -> JobRecord | None:
        async with self._session_factory() as session, session.begin():
            records = await JobRepository(session).claim(
                owner=self._owner,
                lease_seconds=self._lease_seconds,
                job_types=self._job_types,
            )
            return records[0] if records else None

    async def _renew(self, job: JobRecord, lease_lost: asyncio.Event) -> bool:
        await self._sleep(self._heartbeat_seconds)
        async with self._session_factory() as session, session.begin():
            renewed = await JobRepository(session).renew(
                job.id,
                owner=self._owner,
                lease_seconds=self._lease_seconds,
            )
        if not renewed:
            lease_lost.set()
        return renewed

    async def _complete(self, job: JobRecord) -> bool:
        async with self._session_factory() as session, session.begin():
            return await JobRepository(session).complete(job.id, owner=self._owner)

    async def _fail(self, job: JobRecord, error: BaseException) -> None:
        error_code = error.code if isinstance(error, JobFailure) else type(error).__name__[:128]
        async with self._session_factory() as session, session.begin():
            await JobRepository(session).fail(
                job.id,
                owner=self._owner,
                error_code=error_code,
                retry_policy=self._retry_policy,
            )

    async def run_once(self) -> JobRunResult | None:
        """Run at most one job; handler exceptions are persisted then re-raised."""
        job = await self._claim_one()
        if job is None:
            return None
        if job.max_attempts != self._retry_policy.max_attempts:
            raise ValueError("executor retry policy does not match the claimed job")

        try:
            handler = self._registry.resolve(job.job_type)
        except LookupError as error:
            await self._fail(job, error)
            raise
        lease_lost = asyncio.Event()
        context = JobHandlerContext(job=job, lease_lost=lease_lost)
        handler_task: asyncio.Task[None] = asyncio.create_task(handler(context))
        renew_task: asyncio.Task[bool] | None = None
        try:
            while not handler_task.done():
                renew_task = asyncio.create_task(self._renew(job, lease_lost))
                done, _ = await asyncio.wait(
                    {handler_task, renew_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if renew_task not in done:
                    renew_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await renew_task
                    break
                try:
                    renewed = renew_task.result()
                except BaseException:
                    lease_lost.set()
                    handler_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await handler_task
                    raise
                if not renewed:
                    handler_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await handler_task
                    raise LeaseLostError()
        except asyncio.CancelledError:
            lease_lost.set()
            if renew_task is not None and not renew_task.done():
                renew_task.cancel()
                with suppress(asyncio.CancelledError):
                    await renew_task
            handler_task.cancel()
            try:
                with suppress(asyncio.CancelledError):
                    await handler_task
            finally:
                # The handler (including its child process) must finish before release.
                try:
                    async with asyncio.timeout(2):
                        async with self._session_factory() as session, session.begin():
                            await JobRepository(session).release(job.id, owner=self._owner)
                except Exception:
                    _LOGGER.warning("error_code=job_release_failed")
            raise

        try:
            await handler_task
        except BaseException as error:
            await self._fail(job, error)
            raise

        if not await self._complete(job):
            lease_lost.set()
            raise LeaseLostError()
        return JobRunResult(job_id=job.id, completed=True)


__all__ = [
    "JobFailure",
    "JobExecutor",
    "JobHandler",
    "JobHandlerContext",
    "JobHandlerRegistry",
    "JobRunResult",
    "LeaseLostError",
]

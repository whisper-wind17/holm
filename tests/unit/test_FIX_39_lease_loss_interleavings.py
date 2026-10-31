"""FIX-39: deterministically reproduce the handler-first lease-loss interleaving."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest
from holm_domain import JobStatus
from holm_persistence import (
    JobExecutor,
    JobHandlerContext,
    JobHandlerRegistry,
    JobRecord,
    LeaseLostError,
)
from holm_persistence.database import AsyncSessionFactory


def test_FIX_39_handler_ensure_lease_raises_before_executor_cancels() -> None:
    async def scenario() -> None:
        # FX-FIX-39: in-memory claimed job, based on M2-09A pending_18's fixture.
        now = datetime.now(UTC)
        job = JobRecord(
            id=uuid4(),
            job_type="test.effect",
            payload={},
            target_object_id=uuid4(),
            target_object_version=1,
            status=JobStatus.RUNNING,
            priority=0,
            lease_owner="fix-39-owner",
            lease_until=now,
            attempt=1,
            max_attempts=3,
            run_after=now,
            last_error_code=None,
            created_at=now,
            updated_at=now,
            completed_at=None,
        )
        started = asyncio.Event()
        handler_exited = asyncio.Event()
        cancellation_started = asyncio.Event()
        side_effect_after_loss = False
        handler_errors: list[LeaseLostError] = []
        persistence_calls: list[str] = []

        async def handler(context: JobHandlerContext) -> None:
            nonlocal side_effect_after_loss
            started.set()
            try:
                # Replace the original handler's timer with an explicit rendezvous.
                await context.lease_lost.wait()
                context.ensure_lease()
                side_effect_after_loss = True
            except asyncio.CancelledError:
                cancellation_started.set()
                raise
            except LeaseLostError as error:
                handler_errors.append(error)
                raise
            finally:
                # No await follows: the handler finishes before renewal can resume.
                handler_exited.set()

        class ForcedLeaseLoss(JobExecutor):
            async def _claim_one(self) -> JobRecord | None:
                return job

            async def _renew(self, selected: JobRecord, lease_lost: asyncio.Event) -> bool:
                assert selected is job
                await started.wait()
                lease_lost.set()
                await handler_exited.wait()
                return False

            async def _fail(self, selected: JobRecord, error: BaseException) -> None:
                persistence_calls.append("fail")

            async def _complete(self, selected: JobRecord) -> bool:
                persistence_calls.append("complete")
                return True

        def forbidden_session() -> None:
            persistence_calls.append("session (including release)")
            raise AssertionError("FIX-39 lease loss must not access persistence")

        registry = JobHandlerRegistry()
        registry.register("test.effect", handler)
        executor = ForcedLeaseLoss(
            cast(AsyncSessionFactory, forbidden_session), registry, owner="fix-39-owner"
        )
        with pytest.raises(LeaseLostError) as raised:
            await asyncio.wait_for(executor.run_once(), timeout=2)
        assert handler_exited.is_set()
        assert handler_errors == [raised.value]
        assert cancellation_started.is_set() is False
        assert persistence_calls == []
        assert side_effect_after_loss is False

    asyncio.run(scenario())

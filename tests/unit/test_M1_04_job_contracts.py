"""M1-04 unit contracts for bounded retries, registration, and idempotency keys."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast

import pytest
from holm_common.ids import new_uuid7
from holm_domain import JOB_TRANSITIONS, JobExecutionKey, JobStatus, RetryPolicy
from holm_persistence import (
    ComponentHeartbeatRepository,
    JobDraft,
    JobExecutor,
    JobHandlerContext,
    JobHandlerRegistry,
    JobRecord,
    JobRepository,
    LeaseLostError,
)
from holm_persistence.database import AsyncSessionFactory
from sqlalchemy.ext.asyncio import AsyncSession


def _job_record() -> JobRecord:
    now = datetime.now(UTC)
    return JobRecord(
        id=new_uuid7(),
        job_type="test.effect",
        payload={"object_kind": "fixture"},
        target_object_id=new_uuid7(),
        target_object_version=7,
        status=JobStatus.RUNNING,
        priority=0,
        lease_owner="worker-a",
        lease_until=now,
        attempt=1,
        max_attempts=3,
        run_after=now,
        last_error_code=None,
        created_at=now,
        updated_at=now,
        completed_at=None,
    )


def test_M1_04_job_state_graph_is_derived_from_the_single_enum_source() -> None:
    assert set(JOB_TRANSITIONS) == set(JobStatus)
    assert JOB_TRANSITIONS[JobStatus.QUEUED] == {JobStatus.RUNNING}
    assert JOB_TRANSITIONS[JobStatus.RUNNING] == {
        JobStatus.QUEUED,
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
    }
    assert not JOB_TRANSITIONS[JobStatus.SUCCEEDED]
    assert not JOB_TRANSITIONS[JobStatus.FAILED]


def test_M1_04_retry_policy_is_exponential_capped_and_rejects_unbounded_inputs() -> None:
    policy = RetryPolicy(max_attempts=5, base_delay_seconds=2, max_delay_seconds=9)
    assert [policy.delay_after(attempt) for attempt in range(1, 6)] == [2, 4, 8, 9, 9]

    for invalid in (0, -1, True):
        with pytest.raises(ValueError):
            RetryPolicy(max_attempts=invalid)
    with pytest.raises(ValueError):
        RetryPolicy(base_delay_seconds=0)
    with pytest.raises(ValueError):
        RetryPolicy(base_delay_seconds=2, max_delay_seconds=1)
    for outside in (0, 6, True):
        with pytest.raises(ValueError):
            policy.delay_after(outside)


def test_M1_04_handler_registry_fails_loudly_for_unknown_and_duplicate_types() -> None:
    async def handler(context: JobHandlerContext) -> None:
        context.ensure_lease()

    registry = JobHandlerRegistry()
    with pytest.raises(LookupError):
        registry.resolve("test.effect")
    registry.register("test.effect", handler)
    assert registry.resolve("test.effect") is handler
    with pytest.raises(ValueError):
        registry.register("test.effect", handler)


def test_M1_04_handler_uses_job_and_target_version_as_idempotency_identity() -> None:
    context = JobHandlerContext(job=_job_record(), lease_lost=asyncio.Event())
    effects: set[object] = set()

    async def idempotent_handler(handler_context: JobHandlerContext) -> None:
        handler_context.ensure_lease()
        effects.add(handler_context.idempotency_key)

    asyncio.run(idempotent_handler(context))
    asyncio.run(idempotent_handler(context))
    assert effects == {context.idempotency_key}
    assert context.idempotency_key.job_id == context.job.id
    assert context.idempotency_key.target_object_version == context.job.target_object_version

    context.lease_lost.set()
    with pytest.raises(LeaseLostError):
        asyncio.run(idempotent_handler(context))


def test_M1_04_boundary_guards_have_rejecting_positive_controls() -> None:
    target_id = new_uuid7()
    for invalid_type in ("", " padded", "x" * 129):
        with pytest.raises(ValueError):
            JobDraft(
                job_type=invalid_type,
                payload={},
                target_object_id=target_id,
                target_object_version=0,
            )
    with pytest.raises(ValueError):
        JobDraft(
            job_type="test.effect",
            payload={},
            target_object_id=target_id,
            target_object_version=-1,
        )
    with pytest.raises(ValueError):
        JobExecutionKey(target_id, -1)

    registry = JobHandlerRegistry()
    with pytest.raises(ValueError):
        registry.register("", lambda _: asyncio.sleep(0))
    fake_factory = cast(AsyncSessionFactory, object())
    with pytest.raises(ValueError):
        JobExecutor(
            fake_factory,
            registry,
            owner="worker-a",
            lease_seconds=10,
            heartbeat_seconds=10,
        )

    fake_session = cast(AsyncSession, object())
    job_repository = JobRepository(fake_session)

    async def invalid_job_operations() -> None:
        with pytest.raises(ValueError):
            await job_repository.claim(owner="")
        with pytest.raises(ValueError):
            await job_repository.claim(owner="worker-a", lease_seconds=0)
        with pytest.raises(ValueError):
            await job_repository.claim(owner="worker-a", limit=0)
        with pytest.raises(ValueError):
            await job_repository.fail(
                target_id,
                owner="worker-a",
                error_code="",
                retry_policy=RetryPolicy(),
            )

        heartbeat_repository = ComponentHeartbeatRepository(fake_session)
        with pytest.raises(ValueError):
            await heartbeat_repository.write(
                component="download-worker",  # type: ignore[arg-type]
                instance_id="",
                version="1",
                status="Healthy",  # type: ignore[arg-type]
            )
        with pytest.raises(ValueError):
            await heartbeat_repository.write(
                component="download-worker",  # type: ignore[arg-type]
                instance_id="worker-a",
                version="",
                status="Healthy",  # type: ignore[arg-type]
            )

    asyncio.run(invalid_job_operations())

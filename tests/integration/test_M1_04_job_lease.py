"""M1-04 real-PostgreSQL job lease, crash recovery, and heartbeat tests."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import signal
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from multiprocessing.connection import Connection as PipeConnection
from multiprocessing.context import SpawnProcess
from pathlib import Path
from typing import Any, Final, Protocol, cast
from uuid import UUID, uuid4

import pytest
from fixtures.source_mutation import load_mutated_module
from holm_common.ids import new_uuid7
from holm_domain import Component, ComponentStatus, JobStatus, RetryPolicy
from holm_persistence import (
    ComponentHeartbeatRepository,
    JobDraft,
    JobExecutor,
    JobHandlerContext,
    JobHandlerRegistry,
    JobRecord,
    JobRepository,
    LeaseLostError,
    create_database_engine,
    create_session_factory,
)
from migration_test_support import upgrade_head
from sqlalchemy import text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import IntegrityError

PROCESS_TIMEOUT_SECONDS: Final[float] = 15.0
JOB_REPOSITORY_SOURCE: Final[Path] = (
    Path(__file__).resolve().parents[2]
    / "packages/persistence/src/holm_persistence/job_repository.py"
)
JOB_EXECUTOR_SOURCE: Final[Path] = (
    Path(__file__).resolve().parents[2]
    / "packages/persistence/src/holm_persistence/job_executor.py"
)


class PostgresFixture(Protocol):
    @property
    def engine(self) -> Engine: ...


@pytest.fixture(autouse=True)
def migrated_empty_job_tables(ephemeral_postgres: PostgresFixture) -> Iterator[None]:
    upgrade_head(ephemeral_postgres.engine)
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(text("TRUNCATE core.jobs, core.component_heartbeats"))
    yield
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(text("TRUNCATE core.jobs, core.component_heartbeats"))


async def _enqueue_jobs(url: URL, count: int, *, policy: RetryPolicy | None = None) -> list[UUID]:
    selected_policy = RetryPolicy() if policy is None else policy
    engine = create_database_engine(url)
    factory = create_session_factory(engine)
    try:
        async with factory() as session, session.begin():
            repository = JobRepository(session)
            records = [
                await repository.enqueue(
                    JobDraft(
                        job_type="test.effect",
                        payload={"ordinal": ordinal},
                        target_object_id=new_uuid7(),
                        target_object_version=ordinal,
                        retry_policy=selected_policy,
                    )
                )
                for ordinal in range(count)
            ]
        return [record.id for record in records]
    finally:
        await engine.dispose()


async def _claim_once(url: URL, owner: str, lease_seconds: float) -> list[UUID]:
    engine = create_database_engine(url)
    factory = create_session_factory(engine)
    try:
        async with factory() as session, session.begin():
            records = await JobRepository(session).claim(
                owner=owner,
                lease_seconds=lease_seconds,
            )
        return [record.id for record in records]
    finally:
        await engine.dispose()


async def _claim_record(
    url: URL,
    owner: str,
    lease_seconds: float,
    repository_type: Any = JobRepository,
) -> Any:
    engine = create_database_engine(url)
    factory = create_session_factory(engine)
    try:
        async with factory() as session, session.begin():
            records = await repository_type(session).claim(
                owner=owner,
                lease_seconds=lease_seconds,
            )
        assert len(records) == 1
        return records[0]
    finally:
        await engine.dispose()


async def _claim_all_async(
    url: URL,
    owner: str,
    barrier: Any,
    sender: PipeConnection,
) -> None:
    engine = create_database_engine(url)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            backend_pid = int((await session.execute(text("SELECT pg_backend_pid()"))).scalar_one())
        sender.send(("ready", os.getpid(), backend_pid))
        barrier.wait(timeout=PROCESS_TIMEOUT_SECONDS)
        claimed: list[UUID] = []
        while True:
            async with factory() as session, session.begin():
                records = await JobRepository(session).claim(owner=owner)
            if not records:
                break
            claimed.extend(record.id for record in records)
        sender.send(("claimed", os.getpid(), backend_pid, claimed))
    finally:
        await engine.dispose()


def _claim_all_process(
    url: URL,
    owner: str,
    barrier: Any,
    sender: PipeConnection,
) -> None:
    try:
        asyncio.run(_claim_all_async(url, owner, barrier, sender))
    finally:
        sender.close()


async def _claim_and_hang_async(
    url: URL,
    owner: str,
    lease_seconds: float,
    sender: PipeConnection,
) -> None:
    engine = create_database_engine(url)
    factory = create_session_factory(engine)
    try:
        async with factory() as session, session.begin():
            backend_pid = int((await session.execute(text("SELECT pg_backend_pid()"))).scalar_one())
            records = await JobRepository(session).claim(
                owner=owner,
                lease_seconds=lease_seconds,
            )
        sender.send(("claimed", os.getpid(), backend_pid, [record.id for record in records]))
        while True:
            await asyncio.sleep(60)
    finally:
        await engine.dispose()


def _claim_and_hang_process(
    url: URL,
    owner: str,
    lease_seconds: float,
    sender: PipeConnection,
) -> None:
    try:
        asyncio.run(_claim_and_hang_async(url, owner, lease_seconds, sender))
    finally:
        sender.close()


def _claim_once_process(
    url: URL,
    owner: str,
    lease_seconds: float,
    sender: PipeConnection,
) -> None:
    try:
        sender.send(("claimed", os.getpid(), asyncio.run(_claim_once(url, owner, lease_seconds))))
    finally:
        sender.close()


def _receive(receiver: PipeConnection) -> tuple[Any, ...]:
    if not receiver.poll(PROCESS_TIMEOUT_SECONDS):
        pytest.fail("job worker process did not report before the timeout")
    message = receiver.recv()
    if not isinstance(message, tuple):
        pytest.fail("job worker returned an invalid message")
    return message


def _stop_processes(processes: list[SpawnProcess]) -> None:
    for process in processes:
        process.join(timeout=0)
        if process.is_alive():
            process.kill()
            process.join(timeout=5)


def _run_claim_process(url: URL, owner: str, lease_seconds: float) -> tuple[Any, ...]:
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_claim_once_process,
        args=(url, owner, lease_seconds, sender),
    )
    process.start()
    sender.close()
    try:
        message = _receive(receiver)
        process.join(timeout=PROCESS_TIMEOUT_SECONDS)
        assert process.exitcode == 0
        return message
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
        receiver.close()


def _wait_until_database_says_expired(engine: Engine, job_id: UUID) -> None:
    deadline = time.monotonic() + PROCESS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            expired = connection.execute(
                text("SELECT lease_until <= clock_timestamp() FROM core.jobs WHERE id = :job_id"),
                {"job_id": job_id},
            ).scalar_one()
        if bool(expired):
            return
        time.sleep(0.01)
    pytest.fail("database clock did not observe lease expiry before the timeout")


def test_M1_04_two_real_processes_claim_every_job_once_without_duplicates(
    ephemeral_postgres: PostgresFixture,
) -> None:
    expected = asyncio.run(_enqueue_jobs(ephemeral_postgres.engine.url, 31))
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    processes: list[SpawnProcess] = []
    receivers: list[PipeConnection] = []
    try:
        for ordinal in range(2):
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(
                target=_claim_all_process,
                args=(ephemeral_postgres.engine.url, f"worker-{ordinal}", barrier, sender),
            )
            process.start()
            sender.close()
            processes.append(process)
            receivers.append(receiver)

        ready = [_receive(receiver) for receiver in receivers]
        assert all(message[0] == "ready" for message in ready)
        assert len({message[1] for message in ready}) == 2
        assert len({message[2] for message in ready}) == 2
        barrier.wait(timeout=PROCESS_TIMEOUT_SECONDS)
        results = [_receive(receiver) for receiver in receivers]
        claimed = [job_id for message in results for job_id in message[3]]

        assert len(claimed) == len(expected)
        assert len(set(claimed)) == len(claimed)
        assert set(claimed) == set(expected)
        for process in processes:
            process.join(timeout=PROCESS_TIMEOUT_SECONDS)
            assert process.exitcode == 0
    finally:
        _stop_processes(processes)
        for receiver in receivers:
            receiver.close()


def test_M1_04_killed_process_lease_cannot_be_stolen_early_then_is_reclaimed(
    ephemeral_postgres: PostgresFixture,
) -> None:
    [job_id] = asyncio.run(_enqueue_jobs(ephemeral_postgres.engine.url, 1))
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    holder = context.Process(
        target=_claim_and_hang_process,
        args=(ephemeral_postgres.engine.url, "crashed-worker", 1.5, sender),
    )
    holder.start()
    sender.close()
    try:
        claimed = _receive(receiver)
        assert claimed[0] == "claimed"
        assert claimed[3] == [job_id]
        assert claimed[1] != os.getpid()
        assert holder.pid is not None
        os.kill(holder.pid, signal.SIGKILL)
        holder.join(timeout=5)
        assert holder.exitcode == -signal.SIGKILL
    finally:
        if holder.is_alive():
            holder.kill()
            holder.join(timeout=5)
        receiver.close()

    early = _run_claim_process(ephemeral_postgres.engine.url, "early-worker", 1.5)
    assert early[0] == "claimed"
    assert early[1] != os.getpid()
    assert early[2] == []

    _wait_until_database_says_expired(ephemeral_postgres.engine, job_id)
    recovered = _run_claim_process(ephemeral_postgres.engine.url, "recovery-worker", 1.5)
    assert recovered[2] == [job_id]


def test_M2_09A_TC_DL_13_positive_control_removing_expiry_fence_allows_early_takeover(
    ephemeral_postgres: PostgresFixture,
    tmp_path: Path,
) -> None:
    [job_id] = asyncio.run(_enqueue_jobs(ephemeral_postgres.engine.url, 1))
    assert asyncio.run(_claim_once(ephemeral_postgres.engine.url, "lease-holder", 5)) == [job_id]
    mutated = load_mutated_module(
        JOB_REPOSITORY_SOURCE,
        [
            (
                "                    sa.and_(\n"
                "                        JOBS.c.status == JobStatus.RUNNING.value,\n"
                "                        JOBS.c.lease_until <= sa.func.clock_timestamp(),\n"
                "                    ),",
                "                    sa.and_(\n"
                "                        JOBS.c.status == JobStatus.RUNNING.value,\n"
                "                        JOBS.c.lease_until.is_not(None),\n"
                "                    ),",
            )
        ],
        module_name="_m2_09a_expiry_fence_mutant",
        destination=tmp_path / "expiry_fence_mutant.py",
    )

    async def mutant_claim() -> tuple[Any, ...]:
        engine = create_database_engine(ephemeral_postgres.engine.url)
        factory = create_session_factory(engine)
        try:
            async with factory() as session, session.begin():
                return cast(
                    tuple[Any, ...],
                    await mutated.JobRepository(session).claim(
                        owner="early-takeover-mutant",
                        lease_seconds=5,
                    ),
                )
        finally:
            await engine.dispose()

    stolen = asyncio.run(mutant_claim())
    assert [record.id for record in stolen] == [job_id]
    with pytest.raises(AssertionError):
        assert stolen == ()
    print("TC-DL-13 positive control: expiry-fence mutant stole the live lease")


def test_M1_04_old_owner_cannot_renew_after_database_expiry_and_takeover(
    ephemeral_postgres: PostgresFixture,
) -> None:
    [job_id] = asyncio.run(_enqueue_jobs(ephemeral_postgres.engine.url, 1))
    assert asyncio.run(_claim_once(ephemeral_postgres.engine.url, "owner-a", 0.15)) == [job_id]
    _wait_until_database_says_expired(ephemeral_postgres.engine, job_id)
    assert asyncio.run(_claim_once(ephemeral_postgres.engine.url, "owner-b", 1.0)) == [job_id]

    async def renew(owner: str) -> bool:
        engine = create_database_engine(ephemeral_postgres.engine.url)
        factory = create_session_factory(engine)
        try:
            async with factory() as session, session.begin():
                return await JobRepository(session).renew(job_id, owner=owner, lease_seconds=1)
        finally:
            await engine.dispose()

    assert asyncio.run(renew("owner-a")) is False
    assert asyncio.run(renew("owner-b")) is True


def test_M2_09A_pending_9_real_reclaim_preserves_execution_key_and_mutant_fails(
    ephemeral_postgres: PostgresFixture,
    tmp_path: Path,
) -> None:
    asyncio.run(_enqueue_jobs(ephemeral_postgres.engine.url, 1))
    first = asyncio.run(_claim_record(ephemeral_postgres.engine.url, "execution-key-a", 0.15))
    _wait_until_database_says_expired(ephemeral_postgres.engine, first.id)
    reclaimed = asyncio.run(_claim_record(ephemeral_postgres.engine.url, "execution-key-b", 5.0))
    assert reclaimed.execution_key == first.execution_key

    asyncio.run(_enqueue_jobs(ephemeral_postgres.engine.url, 1))
    mutated = load_mutated_module(
        JOB_REPOSITORY_SOURCE,
        [
            (
                "            .values(\n"
                "                status=JobStatus.RUNNING.value,\n"
                "                lease_owner=owner,",
                "            .values(\n"
                "                status=JobStatus.RUNNING.value,\n"
                "                target_object_version=JOBS.c.target_object_version + 1,\n"
                "                lease_owner=owner,",
            )
        ],
        module_name="_m2_09a_execution_key_mutant",
        destination=tmp_path / "execution_key_mutant.py",
    )
    mutant_first = asyncio.run(
        _claim_record(
            ephemeral_postgres.engine.url,
            "execution-key-mutant-a",
            0.15,
            mutated.JobRepository,
        )
    )
    _wait_until_database_says_expired(ephemeral_postgres.engine, mutant_first.id)
    mutant_reclaimed = asyncio.run(
        _claim_record(
            ephemeral_postgres.engine.url,
            "execution-key-mutant-b",
            0.15,
            mutated.JobRepository,
        )
    )
    with pytest.raises(AssertionError):
        assert mutant_reclaimed.execution_key == mutant_first.execution_key


def test_M1_04_executor_cancels_handler_when_real_renewal_fence_fails(
    ephemeral_postgres: PostgresFixture,
) -> None:
    [job_id] = asyncio.run(_enqueue_jobs(ephemeral_postgres.engine.url, 1))

    async def exercise() -> None:
        async_engine = create_database_engine(ephemeral_postgres.engine.url)
        factory = create_session_factory(async_engine)
        registry = JobHandlerRegistry()
        started = asyncio.Event()
        cancellation_started = asyncio.Event()
        allow_cleanup_to_finish = asyncio.Event()
        cleanup_finished = asyncio.Event()
        side_effect_after_loss = False

        async def handler(context: JobHandlerContext) -> None:
            nonlocal side_effect_after_loss
            started.set()
            try:
                for _ in range(10_000):
                    await asyncio.sleep(0.01)
                side_effect_after_loss = True
            except asyncio.CancelledError:
                cancellation_started.set()
                await allow_cleanup_to_finish.wait()
                cleanup_finished.set()
                raise

        registry.register("test.effect", handler)
        executor = JobExecutor(
            factory,
            registry,
            owner="executor-a",
            lease_seconds=0.2,
            heartbeat_seconds=0.05,
        )
        task = asyncio.create_task(executor.run_once())
        await asyncio.wait_for(started.wait(), timeout=2)

        blocker = ephemeral_postgres.engine.connect()
        transaction = blocker.begin()
        try:
            blocker.execute(
                text("SELECT id FROM core.jobs WHERE id = :job_id FOR UPDATE"),
                {"job_id": job_id},
            ).scalar_one()
            await asyncio.to_thread(
                _wait_until_database_says_expired,
                ephemeral_postgres.engine,
                job_id,
            )
        finally:
            transaction.commit()
            blocker.close()

        await asyncio.wait_for(cancellation_started.wait(), timeout=2)
        try:
            # Pending item 18 is a semantic ordering requirement: callers may
            # not observe LeaseLostError while handler cleanup is unfinished.
            assert task.done() is False
        finally:
            allow_cleanup_to_finish.set()
        with pytest.raises(LeaseLostError):
            await asyncio.wait_for(task, timeout=2)
        assert cleanup_finished.is_set()
        assert side_effect_after_loss is False
        await async_engine.dispose()

    asyncio.run(exercise())


def test_M2_09A_pending_18_positive_control_early_propagation_beats_cleanup(
    ephemeral_postgres: PostgresFixture,
    tmp_path: Path,
) -> None:
    del ephemeral_postgres
    mutated = load_mutated_module(
        JOB_EXECUTOR_SOURCE,
        [
            (
                "                if not renewed:\n"
                "                    handler_task.cancel()\n"
                "                    with suppress(asyncio.CancelledError):\n"
                "                        await handler_task\n"
                "                    raise LeaseLostError()",
                "                if not renewed:\n"
                "                    handler_task.cancel()\n"
                "                    raise LeaseLostError()",
            )
        ],
        module_name="_m2_09a_early_lease_loss_executor",
        destination=tmp_path / "early_lease_loss_executor.py",
    )

    async def scenario() -> None:
        now = datetime.now(UTC)
        job = JobRecord(
            id=uuid4(),
            job_type="test.effect",
            payload={},
            target_object_id=uuid4(),
            target_object_version=1,
            status=JobStatus.RUNNING,
            priority=0,
            lease_owner="mutant-owner",
            lease_until=now + timedelta(seconds=5),
            attempt=1,
            max_attempts=3,
            run_after=now,
            last_error_code=None,
            created_at=now,
            updated_at=now,
            completed_at=None,
        )
        started = asyncio.Event()
        cancellation_started = asyncio.Event()
        allow_cleanup = asyncio.Event()
        cleanup_finished = asyncio.Event()

        async def handler(context: Any) -> None:
            del context
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_started.set()
                await allow_cleanup.wait()
                cleanup_finished.set()
                raise

        registry = mutated.JobHandlerRegistry()
        registry.register("test.effect", handler)

        class ForcedLeaseLoss(mutated.JobExecutor):  # type: ignore[misc, name-defined]
            async def _claim_one(self) -> JobRecord | None:
                return job

            async def _renew(self, selected: JobRecord, lease_lost: asyncio.Event) -> bool:
                assert selected is job
                await started.wait()
                lease_lost.set()
                return False

        executor = ForcedLeaseLoss(
            cast(Any, object()),
            registry,
            owner="mutant-owner",
            lease_seconds=2,
            heartbeat_seconds=1,
        )
        execution = asyncio.create_task(executor.run_once())
        await asyncio.wait_for(cancellation_started.wait(), 2)
        with pytest.raises(mutated.LeaseLostError):
            await execution
        assert cleanup_finished.is_set() is False
        allow_cleanup.set()
        await asyncio.wait_for(cleanup_finished.wait(), 2)
        print("M2-09A pending 18 control: early propagation observed before cleanup")

    asyncio.run(scenario())


def test_M1_04_retry_requeues_then_reaches_a_terminal_bound(
    ephemeral_postgres: PostgresFixture,
) -> None:
    policy = RetryPolicy(max_attempts=2, base_delay_seconds=0.02, max_delay_seconds=0.02)
    [job_id] = asyncio.run(_enqueue_jobs(ephemeral_postgres.engine.url, 1, policy=policy))

    async def claim_and_fail(owner: str) -> JobStatus:
        engine = create_database_engine(ephemeral_postgres.engine.url)
        factory = create_session_factory(engine)
        try:
            async with factory() as session, session.begin():
                repository = JobRepository(session)
                [claimed] = await repository.claim(owner=owner, lease_seconds=1)
                failed = await repository.fail(
                    claimed.id,
                    owner=owner,
                    error_code="fixture_failure",
                    retry_policy=policy,
                )
                assert failed is not None
                return failed.status
        finally:
            await engine.dispose()

    assert asyncio.run(claim_and_fail("retry-a")) is JobStatus.QUEUED
    with ephemeral_postgres.engine.connect() as connection:
        while not connection.execute(
            text("SELECT run_after <= clock_timestamp() FROM core.jobs WHERE id = :id"),
            {"id": job_id},
        ).scalar_one():
            connection.rollback()
            time.sleep(0.005)
    assert asyncio.run(claim_and_fail("retry-b")) is JobStatus.FAILED
    assert asyncio.run(_claim_once(ephemeral_postgres.engine.url, "retry-c", 1)) == []


def test_M1_04_heartbeat_upsert_and_age_use_database_time(
    ephemeral_postgres: PostgresFixture,
) -> None:
    async def exercise() -> None:
        engine = create_database_engine(ephemeral_postgres.engine.url)
        factory = create_session_factory(engine)
        try:
            async with factory() as session, session.begin():
                repository = ComponentHeartbeatRepository(session)
                first = await repository.write(
                    component=Component.DOWNLOAD_WORKER,
                    instance_id="download-1",
                    version="1.0.0",
                    status=ComponentStatus.HEALTHY,
                )
                await session.execute(text("SELECT pg_sleep(0.02)"))
                aged = await repository.read(
                    component=Component.DOWNLOAD_WORKER,
                    instance_id="download-1",
                )
                assert aged is not None
                assert aged.database_now >= aged.last_heartbeat_at
                assert aged.age_seconds >= 0.01
                second = await repository.write(
                    component=Component.DOWNLOAD_WORKER,
                    instance_id="download-1",
                    version="1.0.1",
                    status=ComponentStatus.DEGRADED,
                    reason="source unavailable",
                )
                assert second.version == "1.0.1"
                assert second.status is ComponentStatus.DEGRADED
                assert second.last_heartbeat_at > first.last_heartbeat_at
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_M1_04_database_fence_rejects_running_rows_without_a_lease(
    ephemeral_postgres: PostgresFixture,
) -> None:
    transaction = ephemeral_postgres.engine.connect()
    try:
        with pytest.raises(IntegrityError):
            transaction.execute(
                text(
                    """
                    INSERT INTO core.jobs (
                        id, job_type, payload, target_object_id, target_object_version,
                        status, priority, lease_owner, lease_until, attempt, max_attempts,
                        run_after, created_at, updated_at
                    ) VALUES (
                        :id, 'test.invalid', '{}'::jsonb, :target_id, 1,
                        'Running', 0, NULL, NULL, 0, 3,
                        clock_timestamp(), clock_timestamp(), clock_timestamp()
                    )
                    """
                ),
                {"id": new_uuid7(), "target_id": new_uuid7()},
            )
    finally:
        transaction.rollback()
        transaction.close()

"""TC-DL-13: real Download Worker process lease recovery and heartbeat wiring."""

from __future__ import annotations

import asyncio
import base64
import json
import multiprocessing
import os
import signal
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from multiprocessing.connection import Connection as PipeConnection
from multiprocessing.context import SpawnProcess
from pathlib import Path
from typing import Any, Final, cast
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from conftest import PROCESS_TIMEOUT_SECONDS, EphemeralPostgres
from fixtures.hugging_face import (
    PUBLIC_REPOSITORY_COMMIT,
    PUBLIC_REPOSITORY_ID,
    fake_hugging_face_hub,
)
from fixtures.source_mutation import load_mutated_module
from holm_contracts import VersionPayload
from holm_control_api import GrpcHostAgentClient, SystemStatusService
from holm_control_api.system_status import (
    SystemComponentId,
    SystemComponentsResponse,
    SystemComponentStatus,
)
from holm_domain import (
    DOWNLOAD_RESOLUTION_JOB_TYPE,
    Identified,
    JobExecutionKey,
    JobStatus,
    MetadataStatus,
    ModelOriginType,
    RemoteFile,
    ResolvedModel,
    RevisionImmutability,
)
from holm_download_worker import (
    DownloadResolutionJobHandler,
    DownloadWorker,
    ProductionResolutionProcess,
    ResolutionSubprocessError,
    ResolutionSubprocessRequest,
    ResolutionSubprocessResult,
    SubprocessModelResolver,
)
from holm_persistence import (
    JobDraft,
    JobHandlerContext,
    JobRepository,
    LeaseLostError,
    SystemStatusRepository,
    SystemStatusSnapshot,
    create_database_engine,
    create_session_factory,
)
from holm_persistence.model_domain import DOWNLOAD_TASKS, MODELS
from holm_persistence.model_sources import MODEL_SOURCES
from migration_test_support import upgrade_head
from sqlalchemy.engine import URL, Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

LEASE_SECONDS: Final[float] = 0.8
HEARTBEAT_SECONDS: Final[float] = 0.2
REPOSITORY_ID: Final[str] = "Owner/Lease-Recovery"
REQUESTED_REVISION: Final[str] = "main"
WORKER_SOURCE: Final[Path] = (
    Path(__file__).resolve().parents[2] / "apps/workers/download/src/holm_download_worker/worker.py"
)


@dataclass(frozen=True, slots=True)
class _ResolutionProbe:
    child_pid: int
    execution_key: JobExecutionKey


@dataclass(frozen=True, slots=True)
class _BlockingResolutionProcess:
    report: PipeConnection
    release: PipeConnection
    resolved: ResolvedModel

    def __call__(self, request: ResolutionSubprocessRequest) -> ResolvedModel:
        self.report.send(_ResolutionProbe(os.getpid(), request.execution_key))
        if not self.release.poll(PROCESS_TIMEOUT_SECONDS):
            raise RuntimeError("test release gate timed out")
        self.release.recv()
        return self.resolved


@dataclass(frozen=True, slots=True)
class _ReportingResolutionProcess:
    report: PipeConnection
    resolved: ResolvedModel

    def __call__(self, request: ResolutionSubprocessRequest) -> ResolvedModel:
        self.report.send(_ResolutionProbe(os.getpid(), request.execution_key))
        return self.resolved


@dataclass(frozen=True, slots=True)
class _NeverResolutionProcess:
    def __call__(self, request: ResolutionSubprocessRequest) -> ResolvedModel:
        del request
        raise AssertionError("heartbeat-only worker unexpectedly received a job")


class _ResolverReached(RuntimeError):
    pass


class _TeardownWatchdogExpired(AssertionError):
    pass


@contextmanager
def _teardown_watchdog(timeout_seconds: float = PROCESS_TIMEOUT_SECONDS) -> Iterator[None]:
    """Turn a deadlocked real-process finalizer into a loud test failure."""

    def expire(signum: int, frame: object) -> None:
        del signum, frame
        raise _TeardownWatchdogExpired("real-process teardown exceeded its watchdog")

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class _RejectingResolver:
    async def resolve(
        self,
        request: ResolutionSubprocessRequest,
    ) -> ResolutionSubprocessResult:
        del request
        raise _ResolverReached()


class _DatabaseReader:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def read(self) -> SystemStatusSnapshot:
        async with self._factory() as session, session.begin():
            return await SystemStatusRepository(session).read()


def _resolved(
    *,
    repository_id: str = REPOSITORY_ID,
    requested_revision: str = REQUESTED_REVISION,
    resolved_revision: str = "a" * 40,
) -> ResolvedModel:
    return ResolvedModel(
        source=ModelOriginType.HUGGING_FACE,
        repository_id=repository_id,
        requested_revision_norm=requested_revision,
        resolved_revision=resolved_revision,
        revision_immutability=RevisionImmutability.IMMUTABLE,
        files=Identified(
            (
                RemoteFile("config.json", 2),
                RemoteFile("tokenizer.json", 2),
                RemoteFile("model.bin", 4),
            )
        ),
        total_size_bytes=Identified(8),
        architecture=Identified("LeaseRecoveryArchitecture"),
        modalities=Identified(("text",)),
        precision=Identified("bf16"),
        quantization=Identified("none"),
        context_length=Identified(4096),
        candidate_runtimes=Identified(("vllm",)),
        license=Identified("apache-2.0"),
    )


def _async_url(fixture: EphemeralPostgres) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username="ubuntu",
        host=str(fixture.socket_directory),
        port=fixture.port,
        database="postgres",
    )


def _database_secret_url(fixture: EphemeralPostgres) -> str:
    return URL.create(
        "postgresql+psycopg",
        username="ubuntu",
        database="postgres",
        query={"host": str(fixture.socket_directory), "port": str(fixture.port)},
    ).render_as_string(hide_password=False)


def _clear_database(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "TRUNCATE core.domain_events, core.component_heartbeats, core.jobs, "
            "core.download_subscriptions, core.download_tasks, core.model_artifacts, "
            "core.models, core.model_sources CASCADE"
        )


def _seed_download(engine: Engine) -> tuple[UUID, UUID]:
    source_id = uuid4()
    model_id = uuid4()
    task_id = uuid4()
    with engine.begin() as connection:
        now = connection.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        connection.execute(
            sa.insert(MODEL_SOURCES).values(
                id=source_id,
                type=ModelOriginType.HUGGING_FACE.value,
                credential_key_id=None,
                credential_nonce=None,
                credential_ciphertext=None,
                credential_tag=None,
                verification_status=None,
                verified_at=None,
                verification_reason_code=None,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            sa.insert(MODELS).values(
                id=model_id,
                source_id=source_id,
                source_type=ModelOriginType.HUGGING_FACE.value,
                source_key=REPOSITORY_ID,
                display_name="Lease Recovery",
                repository_id=REPOSITORY_ID,
                external_path=None,
                architecture=None,
                modalities=None,
                precision=None,
                quantization=None,
                context_length=None,
                supported_runtimes=None,
                estimated_size_bytes=None,
                license=None,
                metadata_status=MetadataStatus.UNCONFIRMED.value,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            sa.insert(DOWNLOAD_TASKS).values(
                id=task_id,
                model_id=model_id,
                artifact_id=None,
                source_type=ModelOriginType.HUGGING_FACE.value,
                repository_id=REPOSITORY_ID,
                requested_revision_norm=REQUESTED_REVISION,
                resolved_revision=None,
                status="Queued",
                attempt=1,
                downloaded_bytes=0,
                total_bytes=None,
                lease_owner=None,
                lease_until=None,
                staging_path=None,
                error_code=None,
                created_at=now,
                updated_at=now,
                completed_at=None,
            )
        )

    async def enqueue() -> UUID:
        async_engine = create_database_engine(engine.url)
        try:
            factory = create_session_factory(async_engine)
            async with factory() as session, session.begin():
                job = await JobRepository(session).enqueue(
                    JobDraft(
                        job_type=DOWNLOAD_RESOLUTION_JOB_TYPE,
                        payload={},
                        target_object_id=task_id,
                        target_object_version=1,
                    )
                )
                return job.id
        finally:
            await async_engine.dispose()

    return task_id, asyncio.run(enqueue())


def _worker_process(
    url: URL,
    owner: str,
    resolver_process: Any,
    result_sender: PipeConnection,
    control: PipeConnection | None,
) -> None:
    async def run() -> None:
        engine = create_database_engine(url, connect_timeout_seconds=5)
        try:
            factory = create_session_factory(engine)
            handler = DownloadResolutionJobHandler(
                session_factory=factory,
                resolver=SubprocessModelResolver(resolver_process, timeout_seconds=30),
            )
            worker = DownloadWorker(
                session_factory=factory,
                handler=handler,
                owner=owner,
                build_version="m2-09a-test",
                lease_seconds=LEASE_SECONDS,
                heartbeat_seconds=HEARTBEAT_SECONDS,
                poll_seconds=0.02,
            )
            if control is not None:
                early = await worker.run_once()
                result_sender.send(("early", os.getpid(), early))
                if not control.poll(PROCESS_TIMEOUT_SECONDS):
                    raise RuntimeError("recovery worker control timed out")
                if control.recv() != "recover":
                    raise RuntimeError("recovery worker control was invalid")
            result = await worker.run_once()
            result_sender.send(("result", os.getpid(), result))
        except BaseException as error:
            result_sender.send(("error", os.getpid(), type(error).__name__))
            raise
        finally:
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        result_sender.close()
        if control is not None:
            control.close()


def _receive(receiver: PipeConnection, label: str) -> Any:
    if not receiver.poll(PROCESS_TIMEOUT_SECONDS):
        pytest.fail(f"{label} did not report before timeout")
    return receiver.recv()


def _wait_for_process_exit(pid: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not Path(f"/proc/{pid}").exists():
            return
        time.sleep(0.01)
    pytest.fail("resolution child survived its killed worker")


def _wait_for_database_expiry(engine: Engine, job_id: UUID) -> None:
    deadline = time.monotonic() + PROCESS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            expired = connection.execute(
                sa.text("SELECT lease_until <= clock_timestamp() FROM core.jobs WHERE id = :id"),
                {"id": job_id},
            ).scalar_one()
        if bool(expired):
            return
        time.sleep(0.01)
    pytest.fail("database clock did not observe the killed worker lease expiry")


def _assert_processes_reaped(processes: list[SpawnProcess]) -> None:
    alive = [process.pid for process in processes if process.is_alive()]
    if alive:
        raise AssertionError(f"teardown left worker processes alive: {alive}")


def _stop(processes: list[SpawnProcess]) -> None:
    for process in processes:
        process.join(timeout=0)
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
    _assert_processes_reaped(processes)


@pytest.fixture(autouse=True)
def _migrated_empty_database(ephemeral_postgres: EphemeralPostgres) -> Iterator[None]:
    upgrade_head(ephemeral_postgres.engine)
    _clear_database(ephemeral_postgres.engine)
    yield
    _clear_database(ephemeral_postgres.engine)


def test_TC_DL_13_killed_worker_is_reclaimed_after_expiry_without_double_resolution(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    task_id, job_id = _seed_download(ephemeral_postgres.engine)
    resolved = _resolved()
    context = multiprocessing.get_context("spawn")
    first_release_receiver, first_release_sender = context.Pipe(duplex=False)
    first_probe_receiver, first_probe_sender = context.Pipe(duplex=False)
    first_result_receiver, first_result_sender = context.Pipe(duplex=False)
    second_probe_receiver, second_probe_sender = context.Pipe(duplex=False)
    second_result_receiver, second_result_sender = context.Pipe(duplex=False)
    parent_control, worker_control = context.Pipe(duplex=True)
    first = context.Process(
        target=_worker_process,
        args=(
            _async_url(ephemeral_postgres),
            "worker-before-kill",
            _BlockingResolutionProcess(first_probe_sender, first_release_receiver, resolved),
            first_result_sender,
            None,
        ),
    )
    second = context.Process(
        target=_worker_process,
        args=(
            _async_url(ephemeral_postgres),
            "worker-after-kill",
            _ReportingResolutionProcess(second_probe_sender, resolved),
            second_result_sender,
            worker_control,
        ),
    )
    processes = [first, second]
    try:
        first.start()
        first_release_receiver.close()
        first_probe_sender.close()
        first_result_sender.close()
        first_probe = cast(_ResolutionProbe, _receive(first_probe_receiver, "first resolver"))
        assert first_probe.child_pid not in {os.getpid(), first.pid}

        second.start()
        second_probe_sender.close()
        second_result_sender.close()
        worker_control.close()
        early = _receive(second_result_receiver, "early recovery attempt")
        assert early[0] == "early" and early[2] is None
        assert not second_probe_receiver.poll(0.05)

        first.join(timeout=0)
        assert first.pid is not None and first.is_alive()
        with pytest.raises(AssertionError, match="teardown left worker processes alive"):
            _assert_processes_reaped([first])
        os.kill(first.pid, signal.SIGKILL)
        first.join(timeout=5)
        assert first.exitcode == -signal.SIGKILL
        _wait_for_process_exit(first_probe.child_pid)

        _wait_for_database_expiry(ephemeral_postgres.engine, job_id)
        parent_control.send("recover")
        second_probe = cast(
            _ResolutionProbe,
            _receive(second_probe_receiver, "recovery resolver"),
        )
        assert not Path(f"/proc/{first_probe.child_pid}").exists()
        assert second_probe.child_pid not in {os.getpid(), first.pid, second.pid}
        result = _receive(second_result_receiver, "recovery worker")
        assert result[0] == "result" and result[2].completed is True
        second.join(timeout=PROCESS_TIMEOUT_SECONDS)
        assert second.exitcode == 0

        assert first_probe.execution_key == second_probe.execution_key
        assert first_probe.execution_key.job_id == job_id
        with ephemeral_postgres.engine.connect() as connection:
            task = (
                connection.execute(
                    sa.text(
                        "SELECT status, resolved_revision FROM core.download_tasks WHERE id = :id"
                    ),
                    {"id": task_id},
                )
                .mappings()
                .one()
            )
            job = (
                connection.execute(
                    sa.text(
                        "SELECT status, attempt, lease_owner, lease_until "
                        "FROM core.jobs WHERE id = :id"
                    ),
                    {"id": job_id},
                )
                .mappings()
                .one()
            )
            events = (
                connection.execute(
                    sa.text(
                        "SELECT event, aggregate_id, version, payload "
                        "FROM core.domain_events ORDER BY id"
                    )
                )
                .mappings()
                .all()
            )
        assert dict(task) == {"status": "Downloading", "resolved_revision": "a" * 40}
        assert job["status"] == JobStatus.SUCCEEDED.value
        assert job["attempt"] == 2
        assert job["lease_owner"] is None and job["lease_until"] is None
        assert len(events) == 1
        assert events[0]["event"] == "download.resolution_started"
        assert events[0]["aggregate_id"] == task_id
        assert events[0]["version"] == 1
        assert events[0]["payload"] == {"status": "Resolving"}
        print(
            "TC-DL-13: killed child reaped; early claim denied; DB-expiry reclaim "
            "completed; unreaped-process teardown mutant failed"
        )
    finally:
        with _teardown_watchdog():
            try:
                first_release_sender.send(None)
            except (BrokenPipeError, EOFError, OSError):
                pass
            finally:
                first_release_sender.close()
                first_release_receiver.close()
            _stop(processes)
            for pipe_endpoint in (
                first_probe_receiver,
                first_result_receiver,
                second_probe_receiver,
                second_result_receiver,
                parent_control,
            ):
                pipe_endpoint.close()


def _hold_event_condition_lock(event: Any, ready: PipeConnection) -> None:
    event._cond._lock.acquire()  # noqa: SLF001 - deliberate deadlock regression probe
    ready.send(None)
    signal.pause()


def test_TC_DL_13_positive_control_event_gate_deadlock_trips_teardown_watchdog() -> None:
    context = multiprocessing.get_context("spawn")
    event = context.Event()
    ready_receiver, ready_sender = context.Pipe(duplex=False)
    waiter = context.Process(target=_hold_event_condition_lock, args=(event, ready_sender))
    try:
        waiter.start()
        ready_sender.close()
        _receive(ready_receiver, "event-lock waiter")
        waiter.kill()
        waiter.join(timeout=PROCESS_TIMEOUT_SECONDS)
        assert waiter.exitcode == -signal.SIGKILL
        with pytest.raises(_TeardownWatchdogExpired), _teardown_watchdog(0.1):
            event.set()
        print("TC-DL-13 positive control 11: Event-gate deadlock tripped watchdog")
    finally:
        _stop([waiter])
        ready_receiver.close()
        ready_sender.close()


def test_M2_09A_queued_transition_requires_the_database_owned_lease_and_mutant_fails(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    task_id, job_id = _seed_download(ephemeral_postgres.engine)

    async def exercise() -> None:
        engine = create_database_engine(_async_url(ephemeral_postgres))
        try:
            factory = create_session_factory(engine)
            async with factory() as session, session.begin():
                [claimed] = await JobRepository(session).claim(
                    owner="actual-owner",
                    lease_seconds=5,
                )
            assert claimed.id == job_id
            forged = replace(claimed, lease_owner="forged-owner")
            real_context = JobHandlerContext(job=forged, lease_lost=asyncio.Event())
            real_handler = DownloadResolutionJobHandler(
                session_factory=factory,
                resolver=_RejectingResolver(),
            )
            with pytest.raises(LeaseLostError):
                await real_handler.process(real_context)
            async with factory() as session:
                status = (
                    await session.execute(
                        sa.select(DOWNLOAD_TASKS.c.status).where(DOWNLOAD_TASKS.c.id == task_id)
                    )
                ).scalar_one()
            assert status == "Queued"

            mutated = load_mutated_module(
                WORKER_SOURCE,
                [
                    (
                        "            if not await JobRepository(session).lock_owned_lease("
                        "context.job.id, owner=owner):\n"
                        "                context.lease_lost.set()\n"
                        "                raise LeaseLostError()\n"
                        "            repository = DownloadTaskRepository(session)\n",
                        "            repository = DownloadTaskRepository(session)\n",
                    )
                ],
                module_name="_m2_09a_unfenced_transition_worker",
                destination=tmp_path / "unfenced_transition_worker.py",
            )
            mutant_context = JobHandlerContext(job=forged, lease_lost=asyncio.Event())
            mutant_handler = mutated.DownloadResolutionJobHandler(
                session_factory=factory,
                resolver=_RejectingResolver(),
            )
            with pytest.raises(_ResolverReached):
                await mutant_handler.process(mutant_context)
            async with factory() as session:
                mutated_status = (
                    await session.execute(
                        sa.select(DOWNLOAD_TASKS.c.status).where(DOWNLOAD_TASKS.c.id == task_id)
                    )
                ).scalar_one()
            with pytest.raises(AssertionError):
                assert mutated_status == "Queued"
            print("M2-09A lease-first control: forged owner rejected; unfenced mutant advanced")
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def _version() -> VersionPayload:
    return VersionPayload(
        build_version="1.0.0+m209a",
        commit="7" * 40,
        schema_version=1,
        runtime_compat_hash="6" * 64,
    )


def _download_manager_status(response: SystemComponentsResponse) -> SystemComponentStatus:
    return next(
        component.status
        for component in response.components
        if component.id is SystemComponentId.DOWNLOAD_MANAGER
    )


def test_M2_09A_worker_run_publishes_healthy_then_stopping_heartbeat(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        engine = create_database_engine(_async_url(ephemeral_postgres))
        try:
            factory = create_session_factory(engine)
            status = SystemStatusService(
                agent=GrpcHostAgentClient(tmp_path / "absent.sock", timeout_seconds=0.1),
                database=_DatabaseReader(factory),
                version=_version(),
            )
            assert _download_manager_status(await status.read_components()) is (
                SystemComponentStatus.NOT_DEPLOYED
            )
            handler = DownloadResolutionJobHandler(
                session_factory=factory,
                resolver=SubprocessModelResolver(_NeverResolutionProcess()),
            )
            worker = DownloadWorker(
                session_factory=factory,
                handler=handler,
                owner="heartbeat-worker",
                build_version="m2-09a-test",
                lease_seconds=1,
                heartbeat_seconds=0.2,
                poll_seconds=0.02,
            )
            stop = asyncio.Event()
            running = asyncio.create_task(worker.run(stop))
            try:
                deadline = asyncio.get_running_loop().time() + 3
                while _download_manager_status(await status.read_components()) is not (
                    SystemComponentStatus.READY
                ):
                    if asyncio.get_running_loop().time() >= deadline:
                        raise AssertionError("worker heartbeat did not become observable")
                    await asyncio.sleep(0.01)
            finally:
                stop.set()
                await asyncio.wait_for(running, 3)
            assert _download_manager_status(await status.read_components()) is (
                SystemComponentStatus.DEGRADED
            )

            async with factory() as session, session.begin():
                await session.execute(sa.text("DELETE FROM core.component_heartbeats"))
            mutated = load_mutated_module(
                WORKER_SOURCE,
                [
                    (
                        "        await self.write_heartbeat(ComponentStatus.HEALTHY)\n        try:",
                        "        try:",
                    ),
                    (
                        "            except TimeoutError:\n"
                        "                await self.write_heartbeat(ComponentStatus.HEALTHY)",
                        "            except TimeoutError:\n                pass",
                    ),
                ],
                module_name="_m2_09a_heartbeat_disconnected_worker",
                destination=tmp_path / "heartbeat_disconnected_worker.py",
            )
            mutant = mutated.DownloadWorker(
                session_factory=factory,
                handler=handler,
                owner="heartbeat-mutant",
                build_version="m2-09a-test",
                lease_seconds=1,
                heartbeat_seconds=0.2,
                poll_seconds=0.02,
            )
            mutant_stop = asyncio.Event()
            mutant_running = asyncio.create_task(mutant.run(mutant_stop))
            try:
                await asyncio.sleep(0.25)
                observed = _download_manager_status(await status.read_components())
                assert observed is SystemComponentStatus.NOT_DEPLOYED
                with pytest.raises(AssertionError):
                    assert cast(object, observed) is SystemComponentStatus.READY
            finally:
                mutant_stop.set()
                await asyncio.wait_for(mutant_running, 3)
            print(
                "M2-09A heartbeat: not_deployed -> ready -> stopping; "
                "disconnected mutant stayed not_deployed"
            )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def _write_secret(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o400)


def test_M2_09A_production_subprocess_uses_the_shipped_hugging_face_adapter(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    database_url_file = tmp_path / "database-url"
    keyring_file = tmp_path / "master-keyring.json"
    _write_secret(
        database_url_file,
        _database_secret_url(ephemeral_postgres),
    )
    _write_secret(
        keyring_file,
        json.dumps(
            {
                "version": 1,
                "keys": [
                    {
                        "id": "m2-09a-test",
                        "state": "writable",
                        "material": base64.b64encode(b"K" * 32).decode("ascii"),
                    }
                ],
            }
        ),
    )
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    external_root = tmp_path / "external"
    external_root.mkdir()
    with fake_hugging_face_hub() as hub:
        request = ResolutionSubprocessRequest(
            source=ModelOriginType.HUGGING_FACE,
            repository_id=PUBLIC_REPOSITORY_ID,
            requested_revision_norm="main",
            execution_key=JobExecutionKey(uuid4(), 1),
        )
        production_process = ProductionResolutionProcess(
            database_url_file=database_url_file,
            master_keyring_file=keyring_file,
            external_model_roots=(external_root,),
            hugging_face_endpoint=hub.endpoint,
            scratch_root=scratch_root,
        )
        resolver = SubprocessModelResolver(
            production_process,
            timeout_seconds=10,
        )
        try:
            result = asyncio.run(resolver.resolve(request))
        except ResolutionSubprocessError as error:
            pytest.fail(
                f"production resolution subprocess failed with {error.code}; "
                f"fake Hub observed {len(hub.requests)} requests"
            )
    assert result.child_pid != os.getpid()
    assert result.resolved.resolved_revision == PUBLIC_REPOSITORY_COMMIT
    assert hub.requests
    print("M2-09A subprocess: production registry reached the recorded fake Hub")

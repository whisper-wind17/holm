"""M2-05 real SDK resume and real child termination boundaries."""

from __future__ import annotations

import asyncio
import contextlib
import multiprocessing
import os
import re
import shutil
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from multiprocessing.connection import Connection as PipeConnection
from multiprocessing.context import SpawnProcess
from pathlib import Path
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from fixtures.hugging_face import (
    TINY_REPOSITORY,
    TINY_REPOSITORY_COMMIT,
    TINY_REPOSITORY_ID,
    FakeHub,
    ObservedRequest,
    fake_hugging_face_hub,
)
from holm_common.config import StorageConfig
from holm_domain import (
    DOWNLOAD_RESOLUTION_JOB_TYPE,
    JobExecutionKey,
    JobStatus,
    MetadataStatus,
    ModelOriginType,
    ModelSourceType,
)
from holm_download_worker import (
    DownloadProcessCompleted,
    DownloadResolutionJobHandler,
    DownloadStagingLifecycle,
    DownloadSubprocessError,
    DownloadSubprocessRequest,
    DownloadWorker,
    ProductionDownloadProcess,
    SubprocessModelDownloader,
)
from holm_persistence import (
    JobDraft,
    JobRepository,
    create_database_engine,
    create_session_factory,
)
from holm_persistence.model_domain import DOWNLOAD_TASKS, MODELS
from holm_persistence.model_sources import MODEL_SOURCES
from migration_test_support import upgrade_head
from sqlalchemy.engine import URL, Engine

PROCESS_TIMEOUT_SECONDS: Final[float] = 20.0
LEASE_SECONDS: Final[float] = 0.8
HEARTBEAT_SECONDS: Final[float] = 0.2
INTERRUPTED_FILE: Final[str] = "pytorch_model.bin"
RANDOM_INCOMPLETE_FILE: Final[re.Pattern[str]] = re.compile(r".+\.[0-9a-f]{8}\.incomplete")


@pytest.fixture(autouse=True)
def _migrated_empty_database(ephemeral_postgres: EphemeralPostgres) -> None:
    upgrade_head(ephemeral_postgres.engine)


def _seed_source(fixture: EphemeralPostgres) -> UUID:
    source_id = uuid4()
    with fixture.engine.begin() as connection:
        now = connection.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        existing_source_id = connection.execute(
            sa.select(MODEL_SOURCES.c.id).where(
                MODEL_SOURCES.c.type == ModelSourceType.HUGGING_FACE.value
            )
        ).scalar_one_or_none()
        if existing_source_id is None:
            connection.execute(
                sa.insert(MODEL_SOURCES).values(
                    id=source_id,
                    type=ModelSourceType.HUGGING_FACE.value,
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
        else:
            source_id = existing_source_id
    return source_id


def _database_url(fixture: EphemeralPostgres) -> URL:
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


def _write_secret(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o400)


def _tiny_total_size() -> int:
    sizes = tuple(file.size_bytes for file in TINY_REPOSITORY.files())
    assert all(size is not None for size in sizes)
    return sum(size for size in sizes if size is not None)


def _seed_downloading_job(
    fixture: EphemeralPostgres,
    *,
    model_root: Path,
) -> tuple[UUID, UUID, Path]:
    source_id = _seed_source(fixture)
    model_id = uuid4()
    task_id = uuid4()
    staging = model_root / ".staging" / str(task_id)
    with fixture.engine.begin() as connection:
        now = connection.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        connection.execute(
            sa.insert(MODELS).values(
                id=model_id,
                source_id=source_id,
                source_type=ModelOriginType.HUGGING_FACE.value,
                source_key=TINY_REPOSITORY_ID,
                display_name="M2-05 lease recovery",
                repository_id=TINY_REPOSITORY_ID,
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
                repository_id=TINY_REPOSITORY_ID,
                requested_revision_norm="main",
                resolved_revision=TINY_REPOSITORY_COMMIT,
                status="Downloading",
                attempt=1,
                downloaded_bytes=0,
                total_bytes=_tiny_total_size(),
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
        engine = create_database_engine(_database_url(fixture))
        try:
            factory = create_session_factory(engine)
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
            await engine.dispose()

    return task_id, asyncio.run(enqueue()), staging


def _request(staging: Path, endpoint: str) -> DownloadSubprocessRequest:
    return DownloadSubprocessRequest(
        source=ModelOriginType.HUGGING_FACE,
        repository_id=TINY_REPOSITORY_ID,
        resolved_revision=TINY_REPOSITORY_COMMIT,
        staging_path=staging,
        endpoint=endpoint,
        execution_key=JobExecutionKey(uuid4(), 1),
    )


class _NeverResolver:
    async def resolve(self, request: object) -> object:
        del request
        raise AssertionError("a Downloading task must not return to resolution")


def _download_worker_process(
    database_url: URL,
    database_url_file: Path,
    keyring_file: Path,
    model_root: Path,
    endpoint: str,
    owner: str,
    result_sender: PipeConnection,
    control: PipeConnection | None,
) -> None:
    async def run() -> None:
        engine = create_database_engine(database_url, connect_timeout_seconds=5)
        try:
            factory = create_session_factory(engine)
            handler = DownloadResolutionJobHandler(
                session_factory=factory,
                resolver=_NeverResolver(),  # type: ignore[arg-type]
                command_poll_seconds=0.05,
            )
            handler.attach_staging_lifecycle(
                DownloadStagingLifecycle(
                    session_factory=factory,
                    storage=StorageConfig(model_root=model_root),
                )
            )
            handler.attach_download_executor(
                SubprocessModelDownloader(
                    ProductionDownloadProcess(
                        database_url_file=database_url_file,
                        master_keyring_file=keyring_file,
                        max_workers=1,
                    ),
                    cooperative_stop_seconds=0.1,
                ),
                hugging_face_endpoint=endpoint,
                modelscope_endpoint="http://modelscope-fixture",
            )
            worker = DownloadWorker(
                session_factory=factory,
                handler=handler,
                owner=owner,
                build_version="m2-05-test",
                lease_seconds=LEASE_SECONDS,
                heartbeat_seconds=HEARTBEAT_SECONDS,
                poll_seconds=0.02,
            )
            if control is not None:
                early = await worker.run_once()
                result_sender.send(("early", os.getpid(), early is None))
                if not control.poll(PROCESS_TIMEOUT_SECONDS):
                    raise RuntimeError("recovery worker control timed out")
                if control.recv() != "recover":
                    raise RuntimeError("recovery worker control was invalid")
            result = await worker.run_once()
            result_sender.send(
                (
                    "result",
                    os.getpid(),
                    result is not None and result.completed,
                )
            )
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


def _child_pids(parent_pid: int) -> frozenset[int]:
    children: set[int] = set()
    for status in Path("/proc").glob("[0-9]*/status"):
        try:
            fields = dict(
                line.split(":", 1) for line in status.read_text(encoding="ascii").splitlines()
            )
        except (FileNotFoundError, PermissionError, ValueError):
            continue
        if int(fields.get("PPid", "-1").strip()) == parent_pid:
            children.add(int(status.parent.name))
    return frozenset(children)


def _wait_for_no_processes(process_ids: frozenset[int]) -> None:
    deadline = time.monotonic() + PROCESS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if all(not _process_exists(process_id) for process_id in process_ids):
            return
        time.sleep(0.01)
    pytest.fail("download child survived its killed worker")


def _stop_processes(processes: list[SpawnProcess]) -> None:
    for process in processes:
        process.join(timeout=0)
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
    alive = [process.pid for process in processes if process.is_alive()]
    assert not alive, f"teardown left worker processes alive: {alive}"


async def _wait_until(predicate: Callable[[], bool]) -> None:
    deadline = asyncio.get_running_loop().time() + PROCESS_TIMEOUT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("download subprocess condition was not reached")


def _process_exists(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def _downloaded_files(staging: Path) -> frozenset[str]:
    return frozenset(
        str(path.relative_to(staging))
        for path in staging.rglob("*")
        if path.is_file() and ".cache" not in path.relative_to(staging).parts
    )


def _file_gets(hub: FakeHub) -> tuple[ObservedRequest, ...]:
    return tuple(
        request
        for request in hub.requests
        if request.method == "GET" and "/resolve/" in request.path
    )


def test_TC_DL_05_real_sdk_reuses_completed_files_and_restarts_partial_file(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    _seed_source(ephemeral_postgres)
    database_url_file = tmp_path / "database-url"
    # This case has no configured credential, but the production callable still
    # reads the real source row through its child-only database path.
    database_url_file.write_text(
        URL.create(
            "postgresql+psycopg",
            username="ubuntu",
            database="postgres",
            query={
                "host": str(ephemeral_postgres.socket_directory),
                "port": str(ephemeral_postgres.port),
            },
        ).render_as_string(hide_password=False),
        encoding="utf-8",
    )
    database_url_file.chmod(0o400)
    keyring_file = tmp_path / "unused-keyring"
    staging = tmp_path / "staging"
    staging.mkdir()

    with fake_hugging_face_hub(
        serve_synthetic_file_bodies=True,
        response_chunk_bytes=64 * 1024,
        response_chunk_delay_seconds=0.005,
    ) as hub:
        downloader = SubprocessModelDownloader(
            ProductionDownloadProcess(
                database_url_file=database_url_file,
                master_keyring_file=keyring_file,
                max_workers=1,
            ),
            cooperative_stop_seconds=0.1,
        )
        transferred = 0

        async def interrupting_progress(increment: int) -> None:
            nonlocal transferred
            transferred += increment

        def large_file_is_transferring() -> bool:
            observed_gets = _file_gets(hub)
            return (
                hub.state.active_file_gets == 1
                and bool(observed_gets)
                and observed_gets[-1].path.endswith(f"/{INTERRUPTED_FILE}")
            )

        async def interrupt() -> int:
            running = asyncio.create_task(
                downloader.download(_request(staging, hub.endpoint), interrupting_progress)
            )
            await _wait_until(lambda: running.done() or large_file_is_transferring())
            if running.done():
                try:
                    running.result()
                except DownloadSubprocessError as error:
                    pytest.fail(f"download child failed with {error.code}")
                raise AssertionError("download completed before the interruption point")
            await asyncio.sleep(0.03)
            child_pid = downloader.last_child_pid
            assert child_pid is not None and _process_exists(child_pid)
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
            await _wait_until(lambda: not _process_exists(child_pid))
            return child_pid

        asyncio.run(interrupt())
        completed_before_resume = _downloaded_files(staging)
        assert completed_before_resume
        incomplete_files = tuple(staging.rglob("*.incomplete"))
        assert len(incomplete_files) == 1
        incomplete_relative = incomplete_files[0].relative_to(staging)
        assert incomplete_relative.parts[:3] == (".cache", "huggingface", "download")
        assert RANDOM_INCOMPLETE_FILE.fullmatch(incomplete_relative.name)

        truncated_staging = tmp_path / "truncated-staging"
        shutil.copytree(staging, truncated_staging)
        truncated_fragment = next(truncated_staging.rglob("*.incomplete"))
        if truncated_fragment.stat().st_size == 0:
            truncated_fragment.write_bytes(b"truncated-fragment")
        truncated_target = truncated_staging / INTERRUPTED_FILE
        truncated_target.parent.mkdir(parents=True, exist_ok=True)
        truncated_fragment.replace(truncated_target)
        truncated_size = truncated_target.stat().st_size
        expected_size = next(
            file.size_bytes for file in TINY_REPOSITORY.files() if file.path == INTERRUPTED_FILE
        )
        assert expected_size is not None and 0 < truncated_size < expected_size

        hub.wait_until_idle(PROCESS_TIMEOUT_SECONDS)
        hub.clear()
        asyncio.run(downloader.download(_request(truncated_staging, hub.endpoint), _no_op_progress))
        truncated_target_gets = tuple(
            request for request in _file_gets(hub) if request.path.endswith(f"/{INTERRUPTED_FILE}")
        )
        assert len(truncated_target_gets) == 1
        assert truncated_target_gets[0].range_header is None
        assert truncated_target.stat().st_size == expected_size

        hub.wait_until_idle(PROCESS_TIMEOUT_SECONDS)
        hub.clear()

        resumed_transfers = 0

        async def resumed_progress(increment: int) -> None:
            nonlocal resumed_transfers
            resumed_transfers += increment

        asyncio.run(downloader.download(_request(staging, hub.endpoint), resumed_progress))
        resumed_gets = _file_gets(hub)
        resumed_paths = {request.path for request in resumed_gets}
        assert all(
            not any(path.endswith(f"/{completed}") for path in resumed_paths)
            for completed in completed_before_resume
        )
        restarted_gets = tuple(
            request for request in resumed_gets if request.path.endswith(f"/{INTERRUPTED_FILE}")
        )
        assert len(restarted_gets) == 1
        assert restarted_gets[0].range_header is None
        assert resumed_transfers > 0

        shutil.rmtree(staging)
        staging.mkdir()
        hub.wait_until_idle(PROCESS_TIMEOUT_SECONDS)
        hub.clear()
        asyncio.run(downloader.download(_request(staging, hub.endpoint), resumed_progress))
        fresh_gets = _file_gets(hub)
        fresh_paths = {request.path for request in fresh_gets}
        assert all(
            any(path.endswith(f"/{completed}") for path in fresh_paths)
            for completed in completed_before_resume
        )
        assert all(request.range_header is None for request in fresh_gets)

    print(
        "TC-DL-05 requests: resumed completed files=0; interrupted file restarted "
        "without Range; truncated target re-fetched; cleared staging fetched completed "
        f"files again; orphan layout={incomplete_relative}"
    )


def test_TC_DL_13_killed_download_worker_is_reclaimed_without_double_execution(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    model_root = tmp_path / "models"
    model_root.mkdir()
    task_id, job_id, expected_staging = _seed_downloading_job(
        ephemeral_postgres,
        model_root=model_root,
    )
    database_url_file = tmp_path / "database-url"
    _write_secret(database_url_file, _database_secret_url(ephemeral_postgres))
    keyring_file = tmp_path / "unused-keyring"
    context = multiprocessing.get_context("spawn")
    first_result_receiver, first_result_sender = context.Pipe(duplex=False)
    second_result_receiver, second_result_sender = context.Pipe(duplex=False)
    parent_control, worker_control = context.Pipe(duplex=True)

    with fake_hugging_face_hub(
        serve_synthetic_file_bodies=True,
        response_chunk_bytes=64 * 1024,
        response_chunk_delay_seconds=0.02,
    ) as hub:
        first = context.Process(
            target=_download_worker_process,
            args=(
                _database_url(ephemeral_postgres),
                database_url_file,
                keyring_file,
                model_root,
                hub.endpoint,
                "download-worker-before-kill",
                first_result_sender,
                None,
            ),
        )
        second = context.Process(
            target=_download_worker_process,
            args=(
                _database_url(ephemeral_postgres),
                database_url_file,
                keyring_file,
                model_root,
                hub.endpoint,
                "download-worker-after-kill",
                second_result_sender,
                worker_control,
            ),
        )
        processes = [first, second]
        try:
            first.start()
            first_result_sender.close()
            asyncio.run(_wait_until(lambda: hub.state.active_file_gets == 1))

            second.start()
            second_result_sender.close()
            worker_control.close()
            early = _receive(second_result_receiver, "early recovery attempt")
            assert early == ("early", second.pid, True)
            assert hub.max_concurrent_file_gets == 1

            asyncio.run(
                _wait_until(
                    lambda: (
                        hub.state.active_file_gets == 1
                        and bool(_file_gets(hub))
                        and _file_gets(hub)[-1].path.endswith(f"/{INTERRUPTED_FILE}")
                    )
                )
            )
            assert first.pid is not None and first.is_alive()
            download_children = _child_pids(first.pid)
            assert download_children

            os.kill(first.pid, signal.SIGKILL)
            first.join(timeout=5)
            assert first.exitcode == -signal.SIGKILL
            _wait_for_no_processes(download_children)
            hub.wait_until_idle(PROCESS_TIMEOUT_SECONDS)

            completed_before_recovery = _downloaded_files(expected_staging)
            assert completed_before_recovery
            orphan_files = tuple(expected_staging.rglob("*.incomplete"))
            assert orphan_files
            orphan_layouts = tuple(path.relative_to(expected_staging) for path in orphan_files)
            assert all(
                layout.parts[:3] == (".cache", "huggingface", "download")
                and RANDOM_INCOMPLETE_FILE.fullmatch(layout.name)
                for layout in orphan_layouts
            )

            _wait_for_database_expiry(ephemeral_postgres.engine, job_id)
            hub.wait_until_idle(PROCESS_TIMEOUT_SECONDS)
            hub.clear()
            parent_control.send("recover")
            recovered = _receive(second_result_receiver, "recovery worker")
            assert recovered == ("result", second.pid, True)
            second.join(timeout=PROCESS_TIMEOUT_SECONDS)
            assert second.exitcode == 0

            recovered_gets = _file_gets(hub)
            recovered_paths = {request.path for request in recovered_gets}
            assert all(
                not any(path.endswith(f"/{completed}") for path in recovered_paths)
                for completed in completed_before_recovery
            )
            assert any(
                request.path.endswith(f"/{INTERRUPTED_FILE}") and request.range_header is None
                for request in recovered_gets
            )
            assert hub.max_concurrent_file_gets == 1
            assert all(path.exists() for path in orphan_files)

            with ephemeral_postgres.engine.connect() as connection:
                task = (
                    connection.execute(
                        sa.text(
                            "SELECT status, downloaded_bytes, total_bytes, staging_path "
                            "FROM core.download_tasks WHERE id = :id"
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
            assert task["status"] == "Verifying"
            assert task["downloaded_bytes"] == task["total_bytes"] == _tiny_total_size()
            assert task["staging_path"] == str(expected_staging)
            assert job["status"] == JobStatus.SUCCEEDED.value
            assert job["attempt"] == 2
            assert job["lease_owner"] is None and job["lease_until"] is None
            print(
                "TC-DL-13: second real Worker was denied before DB expiry, reclaimed "
                "after SIGKILL, reused staging, reached Verifying, and max concurrent "
                f"file GETs stayed 1; SIGKILL orphan layout={orphan_layouts[0]}"
            )
        finally:
            with contextlib.suppress(BrokenPipeError, EOFError, OSError):
                parent_control.send("recover")
            _stop_processes(processes)
            for endpoint in (
                first_result_receiver,
                second_result_receiver,
                parent_control,
                first_result_sender,
                second_result_sender,
                worker_control,
            ):
                endpoint.close()


@dataclass(frozen=True, slots=True)
class _UnfencedWritingDownload:
    active_directory: Path

    def __call__(
        self,
        request: DownloadSubprocessRequest,
        progress: Callable[[int], None],
    ) -> DownloadProcessCompleted:
        self.active_directory.mkdir(parents=True, exist_ok=True)
        marker = self.active_directory / str(os.getpid())
        marker.write_text("active", encoding="ascii")
        deadline = time.monotonic() + PROCESS_TIMEOUT_SECONDS
        while len(tuple(self.active_directory.iterdir())) < 2:
            if time.monotonic() >= deadline:
                raise RuntimeError("unfenced writer peer did not arrive")
            time.sleep(0.01)
        with (request.staging_path / "unfenced-mutant.bin").open("ab") as shared:
            shared.write(f"{os.getpid()}\n".encode("ascii"))
            shared.flush()
        progress(1)
        while True:
            time.sleep(0.05)


def test_TC_DL_13_positive_control_unfenced_workers_write_the_same_staging(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "unfenced-staging"
    staging.mkdir()
    active_directory = tmp_path / "active-writers"
    downloaders = (
        SubprocessModelDownloader(
            _UnfencedWritingDownload(active_directory),
            cooperative_stop_seconds=0.05,
        ),
        SubprocessModelDownloader(
            _UnfencedWritingDownload(active_directory),
            cooperative_stop_seconds=0.05,
        ),
    )

    async def exercise() -> None:
        tasks = tuple(
            asyncio.create_task(
                downloader.download(_request(staging, "http://fixture"), _no_op_progress)
            )
            for downloader in downloaders
        )
        try:
            await _wait_until(
                lambda: (
                    (staging / "unfenced-mutant.bin").exists()
                    and len((staging / "unfenced-mutant.bin").read_text().splitlines()) == 2
                )
            )
            child_pids = {downloader.last_child_pid for downloader in downloaders}
            assert None not in child_pids and len(child_pids) == 2
            assert {
                int(line) for line in (staging / "unfenced-mutant.bin").read_text().splitlines()
            } == child_pids
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with pytest.raises(asyncio.CancelledError):
                    await task

    asyncio.run(exercise())
    print("TC-DL-13 positive control: unfenced mutant exposed 2 concurrent writers")


@dataclass(frozen=True, slots=True)
class _BlockingDownload:
    ready: Path
    termination_seen: Path | None = None

    def __call__(
        self,
        request: DownloadSubprocessRequest,
        progress: Callable[[int], None],
    ) -> DownloadProcessCompleted:
        del request, progress
        if self.termination_seen is not None:
            marker = self.termination_seen

            def ignore_termination(signum: int, frame: object) -> None:
                del signum, frame
                marker.write_text("termination-seen", encoding="ascii")

            signal.signal(signal.SIGTERM, ignore_termination)
        self.ready.write_text("ready", encoding="ascii")
        while True:
            time.sleep(0.05)


def test_TC_DL_06_real_child_exits_cooperatively_without_a_zombie(tmp_path: Path) -> None:
    ready = tmp_path / "cooperative-ready"
    downloader = SubprocessModelDownloader(
        _BlockingDownload(ready),
        cooperative_stop_seconds=0.1,
    )

    async def exercise() -> None:
        running = asyncio.create_task(
            downloader.download(_request(tmp_path / "cooperative", "http://fixture"), _no_progress)
        )
        await _wait_until(ready.exists)
        child_pid = downloader.last_child_pid
        assert child_pid is not None and _process_exists(child_pid)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        await _wait_until(lambda: not _process_exists(child_pid))

    asyncio.run(exercise())


def test_TC_DL_06_uncooperative_real_child_is_killed_after_injected_boundary(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "uncooperative-ready"
    termination_seen = tmp_path / "termination-seen"
    downloader = SubprocessModelDownloader(
        _BlockingDownload(ready, termination_seen),
        cooperative_stop_seconds=0.05,
    )

    async def exercise() -> None:
        running = asyncio.create_task(
            downloader.download(
                _request(tmp_path / "uncooperative", "http://fixture"), _no_progress
            )
        )
        await _wait_until(ready.exists)
        child_pid = downloader.last_child_pid
        assert child_pid is not None and _process_exists(child_pid)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert termination_seen.read_text(encoding="ascii") == "termination-seen"
        await _wait_until(lambda: not _process_exists(child_pid))

    asyncio.run(exercise())


async def _no_progress(increment: int) -> None:
    raise AssertionError(f"blocking fixture reported {increment} bytes")


async def _no_op_progress(increment: int) -> None:
    assert increment > 0

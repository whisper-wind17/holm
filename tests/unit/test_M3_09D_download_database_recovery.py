"""M3-09D: download-worker heartbeat recovery from PostgreSQL interruptions."""

from __future__ import annotations

import asyncio
import logging
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast

import holm_download_worker.worker as worker_module
import pytest
from holm_domain import ComponentStatus
from holm_download_worker import DownloadWorker
from holm_persistence import AsyncSessionFactory
from sqlalchemy.exc import OperationalError


def _database_error() -> OperationalError:
    return OperationalError(
        "INSERT secret_sql_sentinel",
        {"password": "secret_password_sentinel"},
        OSError("postgresql://secret_dsn_sentinel/holm"),
    )


@dataclass
class _HeartbeatScript:
    outcomes: list[BaseException | None]
    stop: asyncio.Event
    stop_after_healthy: int | None = None
    statuses: list[ComponentStatus] = field(default_factory=list)
    attempts: int = 0
    healthy_writes: int = 0
    attempted: asyncio.Event = field(default_factory=asyncio.Event)

    def next(self, status: ComponentStatus) -> None:
        self.attempts += 1
        self.attempted.set()
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise outcome
        self.statuses.append(status)
        if status is ComponentStatus.HEALTHY:
            self.healthy_writes += 1
            if self.stop_after_healthy == self.healthy_writes:
                self.stop.set()


class _Transaction(AbstractAsyncContextManager[None]):
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Session(AbstractAsyncContextManager["_Session"]):
    def __init__(self, script: _HeartbeatScript) -> None:
        self.script = script

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self) -> AbstractAsyncContextManager[None]:
        return _Transaction()


class _SessionFactory:
    def __init__(self, script: _HeartbeatScript) -> None:
        self.script = script

    def __call__(self) -> _Session:
        return _Session(self.script)


class _HeartbeatRepository:
    def __init__(self, session: _Session) -> None:
        self._script = session.script

    async def write(self, *, status: ComponentStatus, **_kwargs: object) -> None:
        self._script.next(status)


class _Executor:
    def __init__(self, stop: asyncio.Event, *, stop_immediately: bool = False) -> None:
        self._stop = stop
        self._stop_immediately = stop_immediately
        self.calls = 0

    async def run_once(self) -> None:
        self.calls += 1
        if self._stop_immediately:
            self._stop.set()
            return None
        await self._stop.wait()
        return None


class _DatabaseFlakyExecutor:
    def __init__(self, stop: asyncio.Event) -> None:
        self._stop = stop
        self.calls = 0

    async def run_once(self) -> None:
        self.calls += 1
        if self.calls == 1:
            raise _database_error()
        self._stop.set()
        return None


def _worker(script: _HeartbeatScript, executor: _Executor) -> DownloadWorker:
    worker = object.__new__(DownloadWorker)
    # This heartbeat fixture supplies no file-verification handler.
    object.__setattr__(worker, "_launch_verification_enabled", False)
    object.__setattr__(
        worker, "_session_factory", cast(AsyncSessionFactory, _SessionFactory(script))
    )
    object.__setattr__(worker, "_instance_id", "m3-09d-worker")
    object.__setattr__(worker, "_build_version", "m3-09d")
    object.__setattr__(worker, "_heartbeat_seconds", 0.001)
    object.__setattr__(worker, "_poll_seconds", 0.001)
    object.__setattr__(worker, "_executor", executor)
    object.__setattr__(worker, "_stop_event", None)
    return worker


@pytest.fixture(autouse=True)
def _replace_heartbeat_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    class _DownloadTaskRepository:
        def __init__(self, session: _Session) -> None:
            pass

        async def fail_tasks_with_failed_jobs(self, *, now: datetime) -> int:
            return 0

    async def _database_utc_now(session: _Session) -> datetime:
        return datetime(2026, 10, 15, tzinfo=UTC)

    monkeypatch.setattr(worker_module, "ComponentHeartbeatRepository", _HeartbeatRepository)
    monkeypatch.setattr(worker_module, "DownloadTaskRepository", _DownloadTaskRepository)
    monkeypatch.setattr(worker_module, "database_utc_now", _database_utc_now)
    monkeypatch.setattr(worker_module, "_DATABASE_RETRY_BACKOFF_SECONDS", (0.001,))


def test_M3_09D_startup_heartbeat_database_failure_recovers_without_worker_exit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop = asyncio.Event()
    script = _HeartbeatScript([_database_error(), None, None], stop)
    executor = _Executor(stop, stop_immediately=True)
    caplog.set_level(logging.WARNING, logger="holm.download_worker")

    asyncio.run(_worker(script, executor).run(stop))

    assert executor.calls == 1
    assert script.statuses == [ComponentStatus.HEALTHY, ComponentStatus.STOPPING]
    record = next(record for record in caplog.records if record.name == "holm.download_worker")
    assert record.component == "download-worker"
    assert record.error_code == "heartbeat_failed"
    assert "secret_sql_sentinel" not in caplog.text
    assert "secret_password_sentinel" not in caplog.text
    assert "secret_dsn_sentinel" not in caplog.text


def test_M3_09D_periodic_heartbeat_database_failure_recovers_healthy() -> None:
    stop = asyncio.Event()
    script = _HeartbeatScript(
        [None, _database_error(), None, None],
        stop,
        stop_after_healthy=2,
    )
    executor = _Executor(stop)

    asyncio.run(_worker(script, executor).run(stop))

    assert script.statuses == [
        ComponentStatus.HEALTHY,
        ComponentStatus.HEALTHY,
        ComponentStatus.STOPPING,
    ]
    assert executor.calls == 1


def test_M3_09D_heartbeat_database_backoff_is_interrupted_by_stop_signal() -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        script = _HeartbeatScript([_database_error(), _database_error()], stop)
        worker = _worker(script, _Executor(stop))
        running = asyncio.create_task(worker.run(stop))
        await asyncio.wait_for(script.attempted.wait(), timeout=0.5)
        stop.set()
        await asyncio.wait_for(running, timeout=0.5)
        assert script.statuses == []

    asyncio.run(scenario())


def test_M3_09D_heartbeat_programming_error_is_not_swallowed() -> None:
    stop = asyncio.Event()
    script = _HeartbeatScript([ValueError("programming error")], stop)

    with pytest.raises(ValueError, match="programming error"):
        asyncio.run(_worker(script, _Executor(stop)).run(stop))


def test_M3_09D_work_loop_database_failure_retries_and_recovers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop = asyncio.Event()
    script = _HeartbeatScript([], stop)
    executor = _DatabaseFlakyExecutor(stop)
    worker = _worker(script, cast(_Executor, executor))
    caplog.set_level(logging.WARNING, logger="holm.download_worker")

    asyncio.run(worker._work_loop(stop))  # noqa: SLF001 - loop recovery boundary under test

    assert executor.calls == 2
    record = next(
        record
        for record in caplog.records
        if getattr(record, "error_code", None) == "database_unavailable"
    )
    assert record.component == "download-worker"
    assert record.getMessage() == "component=download-worker error_code=database_unavailable"

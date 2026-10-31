"""TC-DB-03: concurrent processes serialize Alembic with a PostgreSQL lock."""

from __future__ import annotations

import logging
import multiprocessing
import os
import time
from collections.abc import Sequence
from multiprocessing.connection import Connection as PipeConnection
from multiprocessing.context import SpawnProcess
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any, Final

import pytest
from holm_persistence.migrations import (
    MIGRATION_LOCK_CLASS_ID,
    MIGRATION_LOCK_OBJECT_ID,
    MigrationLockTimeout,
    migration_lock,
    upgrade_to_head,
)
from migration_test_support import (
    ALEMBIC_CONFIG_PATH,
    PostgresFixture,
    assert_head_structure,
    database_structure,
    revision_count,
)
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.pool import NullPool

PROCESS_WAIT_SECONDS: Final[float] = 30.0
LOCK_OBSERVATION_SECONDS: Final[float] = 5.0
MIGRATION_WAIT_SECONDS: Final[float] = 15.0


class _UpgradeLogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.revision_runs = 0

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage().startswith("Running upgrade "):
            self.revision_runs += 1


def _worker_engine(url: URL) -> Engine:
    return create_engine(
        url,
        connect_args={
            "connect_timeout": 5,
            "options": "-c timezone=UTC -c statement_timeout=10000 -c lock_timeout=5000",
        },
        poolclass=NullPool,
    )


def _run_concurrent_upgrade(
    url: URL,
    config_path: Path,
    barrier: Any,
    result_sender: PipeConnection,
) -> None:
    engine = _worker_engine(url)
    logger = logging.getLogger("alembic.runtime.migration")
    original_level = logger.level
    handler = _UpgradeLogHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        with engine.connect() as connection:
            backend_pid = int(connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
            connection.commit()
            process_pid = os.getpid()
            result_sender.send(("ready", process_pid, backend_pid))
            barrier.wait(timeout=PROCESS_WAIT_SECONDS)
            result_sender.send(("started", process_pid, backend_pid))
            upgrade_to_head(
                connection,
                config_path,
                lock_timeout_seconds=MIGRATION_WAIT_SECONDS,
            )
            result_sender.send(("completed", process_pid, backend_pid, handler.revision_runs))
    except BaseException as error:
        result_sender.send(("error", os.getpid(), type(error).__name__))
        raise
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)
        engine.dispose()
        result_sender.close()


def _receive(receiver: PipeConnection) -> tuple[Any, ...]:
    if not receiver.poll(PROCESS_WAIT_SECONDS):
        pytest.fail("concurrent Migration worker did not report before the timeout")
    message = receiver.recv()
    if not isinstance(message, tuple):
        pytest.fail("concurrent Migration worker returned an invalid result")
    return message


def _lock_rows(engine: Engine) -> dict[int, bool]:
    with engine.connect() as connection:
        return {
            int(pid): bool(granted)
            for pid, granted in connection.execute(
                text(
                    """
                    SELECT pid, granted
                    FROM pg_locks
                    WHERE locktype = 'advisory'
                      AND classid = :class_id
                      AND objid = :object_id
                    """
                ),
                {
                    "class_id": MIGRATION_LOCK_CLASS_ID,
                    "object_id": MIGRATION_LOCK_OBJECT_ID,
                },
            ).tuples()
        }


def _wait_for_both_workers_to_block(engine: Engine, backend_pids: set[int]) -> dict[int, bool]:
    deadline = time.monotonic() + LOCK_OBSERVATION_SECONDS
    rows: dict[int, bool] = {}
    while time.monotonic() < deadline:
        rows = _lock_rows(engine)
        waiting = {pid for pid, granted in rows.items() if not granted}
        if backend_pids <= waiting:
            return rows
        time.sleep(0.02)
    pytest.fail(f"both Migration backends did not wait on the database lock: {rows}")


def _stop_processes(processes: Sequence[BaseProcess]) -> None:
    for process in processes:
        process.join(timeout=0)
    for process in processes:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    for process in processes:
        if process.is_alive():
            process.kill()
            process.join(timeout=5)


def test_TC_DB_03_two_processes_share_one_database_migration_lock(
    ephemeral_postgres: PostgresFixture,
) -> None:
    engine = ephemeral_postgres.engine
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    processes: list[SpawnProcess] = []
    receivers: list[PipeConnection] = []

    try:
        with engine.connect() as blocker, migration_lock(blocker, timeout_seconds=10):
            blocker_pid = int(blocker.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
            for _ in range(2):
                receiver, sender = context.Pipe(duplex=False)
                process = context.Process(
                    target=_run_concurrent_upgrade,
                    args=(engine.url, ALEMBIC_CONFIG_PATH, barrier, sender),
                )
                process.start()
                sender.close()
                receivers.append(receiver)
                processes.append(process)

            ready = [_receive(receiver) for receiver in receivers]
            assert all(message[0] == "ready" for message in ready)
            process_pids = {int(message[1]) for message in ready}
            backend_pids = {int(message[2]) for message in ready}
            assert len(process_pids) == 2
            assert len(backend_pids) == 2

            barrier.wait(timeout=PROCESS_WAIT_SECONDS)
            started = [_receive(receiver) for receiver in receivers]
            assert all(message[0] == "started" for message in started)
            lock_rows = _wait_for_both_workers_to_block(engine, backend_pids)
            assert lock_rows[blocker_pid]
            assert all(not lock_rows[pid] for pid in backend_pids)

        completed = [_receive(receiver) for receiver in receivers]
        assert all(message[0] == "completed" for message in completed)
        # Exactly one worker ran the Migration; the other found head already
        # applied and ran nothing.
        assert sorted(int(message[3]) for message in completed) == [0, revision_count()]

        for process in processes:
            process.join(timeout=PROCESS_WAIT_SECONDS)
            assert process.exitcode == 0

        assert_head_structure(database_structure(engine))
        assert _lock_rows(engine) == {}
    finally:
        _stop_processes(processes)
        for receiver in receivers:
            receiver.close()


def test_TC_DB_03_lock_wait_has_a_recognizable_bounded_timeout(
    ephemeral_postgres: PostgresFixture,
) -> None:
    engine = ephemeral_postgres.engine
    with (
        engine.connect() as blocker,
        migration_lock(blocker, timeout_seconds=2),
        engine.connect() as contender,
    ):
        started_at = time.monotonic()
        with pytest.raises(MigrationLockTimeout):
            upgrade_to_head(
                contender,
                ALEMBIC_CONFIG_PATH,
                lock_timeout_seconds=0.05,
            )
        assert time.monotonic() - started_at < 2

    assert _lock_rows(engine) == {}

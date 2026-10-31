"""Real PostgreSQL fixtures shared by the M0-05 Integration tests."""

from __future__ import annotations

import multiprocessing
import os
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from multiprocessing.connection import Connection as PipeConnection
from multiprocessing.context import SpawnProcess
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any, Final

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Connection, Engine
from sqlalchemy.pool import NullPool

POSTGRESQL_BIN: Final[Path] = Path("/usr/lib/postgresql/16/bin")
RUNUSER: Final[Path] = Path("/usr/sbin/runuser")
CLUSTER_USER: Final[str] = "ubuntu"
CLUSTER_UID: Final[int] = 1000
CLUSTER_GID: Final[int] = 1000
CLUSTER_PORT: Final[int] = 55432
TASK_ROOT_PARENT: Final[Path] = Path("/tmp")
TASK_ROOT_PREFIX: Final[str] = "holm-m0-05-"
PROCESS_TIMEOUT_SECONDS: Final[int] = 30
TEST_TRANSACTION_APPLICATION_NAME: Final[str] = "holm_tc_db_04_transaction"
type TestTransactionFactory = Callable[[], AbstractContextManager[Connection]]
type TwoProcessDatabaseRace = Callable[
    [Sequence["ConcurrentStatement"]], tuple["ConcurrentStatementResult", ...]
]


@dataclass(frozen=True)
class EphemeralPostgres:
    """A task-owned PostgreSQL 16 cluster reachable only over its Unix socket."""

    engine: Engine
    data_directory: Path
    socket_directory: Path
    port: int


@dataclass(frozen=True)
class ConcurrentStatement:
    """One SQL statement executed by one side of a two-process database race."""

    sql: str
    parameters: Mapping[str, Any]


@dataclass(frozen=True)
class ConcurrentStatementResult:
    """Committed or database-rejected outcome from one spawned process."""

    process_pid: int
    backend_pid: int
    succeeded: bool
    error_type: str | None
    sqlstate: str | None


def _run_as_cluster_user(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(RUNUSER), "--user", CLUSTER_USER, "--", *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=PROCESS_TIMEOUT_SECONDS,
    )


def _assert_cluster_prerequisites() -> None:
    required = ("initdb", "pg_ctl", "pg_isready", "postgres")
    missing = [name for name in required if not (POSTGRESQL_BIN / name).is_file()]
    if missing:
        raise RuntimeError(f"PostgreSQL 16 test binaries are missing: {', '.join(missing)}")
    if not RUNUSER.is_file():
        raise RuntimeError("runuser is required to drop PostgreSQL fixture privileges")

    account = subprocess.run(
        ["id", "-u", CLUSTER_USER],
        check=True,
        capture_output=True,
        text=True,
        timeout=PROCESS_TIMEOUT_SECONDS,
    )
    if int(account.stdout.strip()) != CLUSTER_UID:
        raise RuntimeError(f"{CLUSTER_USER} must have uid {CLUSTER_UID}")

    version = subprocess.run(
        [str(POSTGRESQL_BIN / "postgres"), "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=PROCESS_TIMEOUT_SECONDS,
    )
    if "PostgreSQL) 16." not in version.stdout:
        raise RuntimeError("TC-DB requires PostgreSQL 16.x")


def _create_task_root() -> Path:
    root = Path(tempfile.mkdtemp(prefix=TASK_ROOT_PREFIX, dir=TASK_ROOT_PARENT))
    try:
        os.chown(root, CLUSTER_UID, CLUSTER_GID)
        root.chmod(0o700)
    except BaseException:
        _remove_task_root(root)
        raise
    return root


def _read_postmaster_pid(data_directory: Path) -> int | None:
    pid_file = data_directory / "postmaster.pid"
    try:
        first_line = pid_file.read_text(encoding="ascii").splitlines()[0]
    except (FileNotFoundError, IndexError, OSError, UnicodeError):
        return None
    try:
        return int(first_line)
    except ValueError:
        return None


def _is_task_postgres(pid: int | None, data_directory: Path) -> bool:
    if pid is None:
        return False
    try:
        command_line = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return False
    return str(data_directory).encode() in command_line


def _wait_for_process_exit(pid: int, data_directory: Path, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _is_task_postgres(pid, data_directory):
            return True
        time.sleep(0.05)
    return not _is_task_postgres(pid, data_directory)


def _force_stop_task_postgres(pid: int | None, data_directory: Path) -> None:
    if not _is_task_postgres(pid, data_directory) or pid is None:
        return
    os.kill(pid, signal.SIGTERM)
    if _wait_for_process_exit(pid, data_directory, 5.0):
        return
    os.kill(pid, signal.SIGKILL)
    if not _wait_for_process_exit(pid, data_directory, 5.0):
        raise RuntimeError("temporary PostgreSQL process did not terminate")


def _remove_task_root(root: Path) -> None:
    resolved_root = root.resolve()
    if resolved_root.parent != TASK_ROOT_PARENT.resolve() or not resolved_root.name.startswith(
        TASK_ROOT_PREFIX
    ):
        raise RuntimeError(f"refusing to remove unsafe PostgreSQL test path: {resolved_root}")
    shutil.rmtree(resolved_root)


def _stop_cluster(data_directory: Path) -> None:
    pid = _read_postmaster_pid(data_directory)
    stop_error: subprocess.SubprocessError | None = None
    if _is_task_postgres(pid, data_directory):
        try:
            _run_as_cluster_user(
                [
                    str(POSTGRESQL_BIN / "pg_ctl"),
                    "-D",
                    str(data_directory),
                    "-m",
                    "fast",
                    "-t",
                    str(PROCESS_TIMEOUT_SECONDS),
                    "-w",
                    "stop",
                ]
            )
        except subprocess.SubprocessError as error:
            stop_error = error
            _force_stop_task_postgres(pid, data_directory)
    if _is_task_postgres(pid, data_directory):
        raise RuntimeError("temporary PostgreSQL process remains after teardown")
    if stop_error is not None:
        raise RuntimeError(
            "pg_ctl failed while stopping the temporary PostgreSQL cluster"
        ) from stop_error


@contextmanager
def _rollback_test_transaction(engine: Engine) -> Iterator[Connection]:
    connection = engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(
            text("SELECT set_config('application_name', :name, true)"),
            {"name": TEST_TRANSACTION_APPLICATION_NAME},
        ).scalar_one()
        yield connection
    finally:
        try:
            if transaction.is_active:
                transaction.rollback()
            elif connection.in_transaction():
                connection.rollback()
        finally:
            connection.close()


def _concurrent_statement_worker(
    url: URL,
    statement: ConcurrentStatement,
    barrier: Any,
    sender: PipeConnection,
) -> None:
    engine = create_engine(
        url,
        connect_args={
            "connect_timeout": 5,
            "options": "-c timezone=UTC -c statement_timeout=10000 -c lock_timeout=5000",
        },
        poolclass=NullPool,
    )
    process_pid = os.getpid()
    backend_pid = -1
    try:
        with engine.connect() as connection:
            backend_pid = int(connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
            connection.commit()
            sender.send(("ready", process_pid, backend_pid))
            barrier.wait(timeout=PROCESS_TIMEOUT_SECONDS)
            try:
                connection.execute(text(statement.sql), dict(statement.parameters))
                connection.commit()
            except BaseException as error:
                connection.rollback()
                sender.send(
                    (
                        "result",
                        process_pid,
                        backend_pid,
                        False,
                        type(error).__name__,
                        getattr(getattr(error, "orig", None), "sqlstate", None),
                    )
                )
            else:
                sender.send(("result", process_pid, backend_pid, True, None, None))
    except BaseException as error:
        sender.send(
            (
                "worker_error",
                process_pid,
                backend_pid,
                False,
                type(error).__name__,
                getattr(getattr(error, "orig", None), "sqlstate", None),
            )
        )
        raise
    finally:
        engine.dispose()
        sender.close()


def _receive_process_message(receiver: PipeConnection) -> tuple[Any, ...]:
    if not receiver.poll(PROCESS_TIMEOUT_SECONDS):
        pytest.fail("two-process database worker did not report before timeout")
    message = receiver.recv()
    if not isinstance(message, tuple):
        pytest.fail("two-process database worker returned an invalid message")
    return message


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


def _run_two_process_database_race(
    url: URL,
    statements: Sequence[ConcurrentStatement],
) -> tuple[ConcurrentStatementResult, ...]:
    if len(statements) != 2:
        raise ValueError("a database race requires exactly two statements")

    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    processes: list[SpawnProcess] = []
    receivers: list[PipeConnection] = []
    try:
        for statement in statements:
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(
                target=_concurrent_statement_worker,
                args=(url, statement, barrier, sender),
            )
            process.start()
            sender.close()
            receivers.append(receiver)
            processes.append(process)

        ready = [_receive_process_message(receiver) for receiver in receivers]
        assert all(message[0] == "ready" for message in ready)
        assert len({int(message[1]) for message in ready}) == 2
        assert len({int(message[2]) for message in ready}) == 2
        barrier.wait(timeout=PROCESS_TIMEOUT_SECONDS)

        messages = [_receive_process_message(receiver) for receiver in receivers]
        assert all(message[0] == "result" for message in messages)
        results = tuple(
            ConcurrentStatementResult(
                process_pid=int(message[1]),
                backend_pid=int(message[2]),
                succeeded=bool(message[3]),
                error_type=None if message[4] is None else str(message[4]),
                sqlstate=None if message[5] is None else str(message[5]),
            )
            for message in messages
        )

        for process in processes:
            process.join(timeout=PROCESS_TIMEOUT_SECONDS)
            assert process.exitcode == 0
        return results
    finally:
        _stop_processes(processes)
        for receiver in receivers:
            receiver.close()


@contextmanager
def temporary_postgres() -> Iterator[EphemeralPostgres]:
    """FX-DB-M0-05: start PostgreSQL as ubuntu and remove all state afterward."""
    _assert_cluster_prerequisites()
    task_root = _create_task_root()
    data_directory = task_root / "data"
    socket_directory = task_root / "socket"
    log_file = task_root / "postgres.log"
    engine: Engine | None = None

    try:
        socket_directory.mkdir(mode=0o700)
        os.chown(socket_directory, CLUSTER_UID, CLUSTER_GID)
        _run_as_cluster_user(
            [
                str(POSTGRESQL_BIN / "initdb"),
                "-D",
                str(data_directory),
                "--auth-local=trust",
                "--auth-host=reject",
                "--encoding=UTF8",
                "--locale=C.UTF-8",
                "--no-sync",
            ]
        )
        server_options = " ".join(
            (
                "-F",
                f"-p {CLUSTER_PORT}",
                f"-k {socket_directory}",
                "-c listen_addresses=''",
                "-c max_connections=10",
            )
        )
        _run_as_cluster_user(
            [
                str(POSTGRESQL_BIN / "pg_ctl"),
                "-D",
                str(data_directory),
                "-l",
                str(log_file),
                "-o",
                server_options,
                "-t",
                str(PROCESS_TIMEOUT_SECONDS),
                "-w",
                "start",
            ]
        )
        _run_as_cluster_user(
            [
                str(POSTGRESQL_BIN / "pg_isready"),
                "-h",
                str(socket_directory),
                "-p",
                str(CLUSTER_PORT),
                "-d",
                "postgres",
                "-t",
                "5",
            ]
        )

        url = URL.create(
            "postgresql+psycopg",
            username=CLUSTER_USER,
            host=str(socket_directory),
            port=CLUSTER_PORT,
            database="postgres",
        )
        engine = create_engine(
            url,
            connect_args={
                "connect_timeout": 5,
                "options": "-c timezone=UTC -c statement_timeout=10000 -c lock_timeout=5000",
            },
            poolclass=NullPool,
        )
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1").scalar_one()

        yield EphemeralPostgres(
            engine=engine,
            data_directory=data_directory,
            socket_directory=socket_directory,
            port=CLUSTER_PORT,
        )
    finally:
        try:
            if engine is not None:
                engine.dispose()
        finally:
            try:
                _stop_cluster(data_directory)
            finally:
                if not _is_task_postgres(_read_postmaster_pid(data_directory), data_directory):
                    _remove_task_root(task_root)


@pytest.fixture(scope="module")
def ephemeral_postgres() -> Iterator[EphemeralPostgres]:
    with temporary_postgres() as database:
        yield database


@pytest.fixture
def database_transaction(ephemeral_postgres: EphemeralPostgres) -> Iterator[Connection]:
    """FX-DB-M0-05B: roll back every write made by one test case."""
    with _rollback_test_transaction(ephemeral_postgres.engine) as connection:
        yield connection


@pytest.fixture
def database_transaction_factory(
    ephemeral_postgres: EphemeralPostgres,
) -> TestTransactionFactory:
    """FX-DB-M0-05B: open isolated transactions, including exception paths."""

    def open_transaction() -> AbstractContextManager[Connection]:
        return _rollback_test_transaction(ephemeral_postgres.engine)

    return open_transaction


@pytest.fixture
def two_process_database_race(
    ephemeral_postgres: EphemeralPostgres,
) -> TwoProcessDatabaseRace:
    """FX-DB-M2-02: run exactly two spawned processes against one database."""

    def run(statements: Sequence[ConcurrentStatement]) -> tuple[ConcurrentStatementResult, ...]:
        return _run_two_process_database_race(ephemeral_postgres.engine.url, statements)

    return run

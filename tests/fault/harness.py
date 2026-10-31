"""M5-13 fault harness over real PostgreSQL, SQLite, Gateway, and Ledger Writer.

The HTTP Runtime is FX-RT-03's in-process transport boundary.  Every durability
decision beneath it uses the product SQLite/PostgreSQL implementations; the
clock is the only synthetic input, so the 60/180/300 second cases never sleep.
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import shutil
import sqlite3
import tempfile
import threading
import time
from collections.abc import Coroutine, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Final, TypeVar, cast
from uuid import UUID, uuid4

import httpx
import sqlalchemy as sa
from fixtures.sqlite_ledger.faults import (
    block_wal_path,
    exhaust_database_capacity,
    restore_database_capacity,
    sqlite_error_name,
    unblock_wal_path,
)
from holm_control_api.system_status import (
    SystemComponentReason,
    SystemComponentStatus,
    ledger_backlog_component_state,
)
from holm_domain import (
    ArtifactStorageType,
    LedgerHealth,
    MetadataStatus,
    ModelArtifactStatus,
    ModelInstanceStatus,
    ModelOriginType,
    RequestFinishReason,
    RuntimeType,
)
from holm_gateway.application import create_app
from holm_gateway.authentication import ApiKeyAuthenticator
from holm_gateway.configuration import GatewayConfiguration
from holm_gateway.route_snapshot import (
    PostgresRouteSnapshotSource,
    RouteSnapshotManager,
    SnapshotApiKeyValidationLookup,
)
from holm_ledger import (
    CompletedEvent,
    ConnectionRole,
    EventType,
    InProgressEvent,
    LedgerEvent,
    LedgerHealthMonitor,
    LedgerStorageError,
    ReceivedEvent,
    RequestOperation,
    RequestProtocol,
    SpoolWriter,
    SpoolWriterUnavailableError,
    SQLiteLedger,
    open_ledger,
)
from holm_ledger_writer import LedgerWriter
from holm_persistence import create_database_engine, create_session_factory
from holm_runtimes import load_runtime_compatibility
from holm_security import ApiKeyPepperKeyring, issue_api_key
from integration.conftest import (
    CLUSTER_USER,
    POSTGRESQL_BIN,
    PROCESS_TIMEOUT_SECONDS,
    EphemeralPostgres,
    _run_as_cluster_user,
    ephemeral_postgres,
)
from integration.migration_test_support import upgrade_head
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine

_T = TypeVar("_T")

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
RUNTIME = load_runtime_compatibility(REPOSITORY_ROOT / "runtime-compat.yaml").runtime("vllm")
assert RUNTIME is not None

MODEL_ID: Final = UUID("5f130000-0000-4000-8000-000000000001")
ARTIFACT_ID: Final = UUID("5f130000-0000-4000-8000-000000000002")
INSTANCE_ID: Final = UUID("5f130000-0000-4000-8000-000000000003")
ALLOCATION_ID: Final = UUID("5f130000-0000-4000-8000-000000000004")
SERVICE_ID: Final = UUID("5f130000-0000-4000-8000-000000000005")
BACKEND_ID: Final = UUID("5f130000-0000-4000-8000-000000000006")
USER_ID: Final = UUID("5f130000-0000-4000-8000-000000000007")
KEY_ID: Final = UUID("5f130000-0000-4000-8000-000000000008")
PUBLIC_MODEL: Final[str] = "m5-13-model"
PEPPER_ID: Final[str] = "m5-13"
INPUT_TOKENS: Final[int] = 3
OUTPUT_TOKENS: Final[int] = 5

_postgres_fixture: EphemeralPostgres | None = None
_postgres_generator: Iterator[EphemeralPostgres] | None = None


def initialize_postgres() -> None:
    """Start the existing FX-DB-M0-05 cluster implementation and migrate it."""

    global _postgres_fixture, _postgres_generator
    if _postgres_fixture is not None:
        return
    factory = cast(Any, ephemeral_postgres).__wrapped__
    generator = cast(Iterator[EphemeralPostgres], factory())
    fixture = next(generator)
    try:
        upgrade_head(fixture.engine)
    except BaseException:
        generator.close()
        raise
    _postgres_fixture = fixture
    _postgres_generator = generator


def shutdown_postgres() -> None:
    """Release the task-owned cluster through the original fixture teardown."""

    global _postgres_fixture, _postgres_generator
    generator = _postgres_generator
    _postgres_fixture = None
    _postgres_generator = None
    if generator is None:
        return
    try:
        next(generator)
    except StopIteration:
        pass
    else:  # pragma: no cover - the reused fixture yields exactly once
        raise RuntimeError("FX-DB-M0-05 yielded more than once")


def _postgres() -> EphemeralPostgres:
    fixture = _postgres_fixture
    if fixture is None:
        raise RuntimeError("M5-13 PostgreSQL session fixture is not initialized")
    return fixture


class _Clock:
    def __init__(self) -> None:
        self._value = 10_000.0

    def __call__(self) -> float:
        return self._value

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("fault clock cannot move backwards")
        self._value += seconds


class _LoopThread:
    """Keep async engines, locks, and ASGI calls on one stable event loop."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="m5-13-async-loop")
        self._thread.start()
        if not self._ready.wait(5.0):
            raise RuntimeError("M5-13 async loop did not start")

    def _serve(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def run(self, operation: Coroutine[Any, Any, _T]) -> _T:
        return asyncio.run_coroutine_threadsafe(operation, self._loop).result(timeout=30.0)

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)
        if self._thread.is_alive():
            raise RuntimeError("M5-13 async loop did not stop")
        self._loop.close()


@dataclass(frozen=True, slots=True)
class CallOutcome:
    status_code: int
    error_code: str | None
    request_id: str
    message: str


@dataclass(frozen=True, slots=True)
class StreamingOutcome:
    terminal_event_written: bool


class _Sidecars:
    """Narrow adapter expected by the already-approved FX-SQ-04 injector."""

    def __init__(self, path: Path) -> None:
        self.wal_path = Path(f"{path}-wal")
        self.shm_path = Path(f"{path}-shm")


class _StreamingCall:
    def __init__(self, stack: FaultStack, request_id: UUID, received_at: datetime) -> None:
        self._stack = stack
        self._request_id = request_id
        self._received_at = received_at
        self._finished = False

    def finish(self) -> StreamingOutcome:
        if self._finished:
            raise RuntimeError("streaming fault call already finished")
        self._finished = True
        event = CompletedEvent(
            request_id=self._request_id,
            event_seq=3,
            created_at=datetime.now(UTC),
            received_at=self._received_at,
            input_tokens=INPUT_TOKENS,
            output_tokens=OUTPUT_TOKENS,
            duration_ms=1,
            finish_reason=RequestFinishReason.STOP,
            http_status=200,
        )
        try:
            self._stack._runner.run(self._stack._spool.submit(event))
        except LedgerStorageError:
            return StreamingOutcome(terminal_event_written=False)
        return StreamingOutcome(terminal_event_written=True)


def _async_url(fixture: EphemeralPostgres) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username=CLUSTER_USER,
        host=str(fixture.socket_directory),
        port=fixture.port,
        database="postgres",
    )


def _seed_database(fixture: EphemeralPostgres) -> tuple[ApiKeyPepperKeyring, str]:
    keyring = ApiKeyPepperKeyring(current_key_id=PEPPER_ID, keys={PEPPER_ID: b"K" * 32})
    issued = issue_api_key(keyring)
    now = datetime.now(UTC)
    with fixture.engine.begin() as connection:
        connection.execute(
            sa.text(
                "TRUNCATE core.ai_services, core.users, core.models, core.domain_events, "
                "core.audit_events, "
                "usage.request_records, usage.request_id_registry, "
                "usage.request_event_quarantine CASCADE"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO core.users "
                "(id, username_norm, display_name, role, status, password_hash, "
                "must_change_password, created_at, updated_at, deleted_at) VALUES "
                "(:id, 'm5-13-admin', 'M5-13 Admin', 'admin', 'active', "
                "'$argon2id$m5-13-fixture', false, :now, :now, NULL)"
            ),
            {"id": USER_ID, "now": now},
        )
        connection.execute(
            sa.text(
                "INSERT INTO core.models "
                "(id, source_type, source_key, display_name, external_path, metadata_status, "
                "created_at, updated_at) VALUES "
                "(:id, :source_type, '/models/m5-13', 'M5-13 Model', '/models/m5-13', "
                ":metadata_status, :now, :now)"
            ),
            {
                "id": MODEL_ID,
                "source_type": ModelOriginType.LOCAL_PATH.value,
                "metadata_status": MetadataStatus.CONFIRMED.value,
                "now": now,
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO core.model_artifacts "
                "(id, model_id, source_type, repository_id, resolved_revision, storage_type, "
                "status, path, manifest_hash, size_bytes, created_at, updated_at) VALUES "
                "(:id, :model_id, :source_type, 'm5-13', :revision, :storage_type, "
                ":status, '/models/m5-13', :manifest_hash, 1024, :now, :now)"
            ),
            {
                "id": ARTIFACT_ID,
                "model_id": MODEL_ID,
                "source_type": ModelOriginType.LOCAL_PATH.value,
                "revision": "4" * 40,
                "storage_type": ArtifactStorageType.EXTERNAL.value,
                "status": ModelArtifactStatus.READY.value,
                "manifest_hash": "5" * 40,
                "now": now,
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO core.model_instances "
                "(id, artifact_id, runtime_type, expected_gpu_uuids, tensor_parallel_size, "
                "context_length, gpu_memory_utilization, name, internal_model_name, status, "
                "error_code, internal_endpoint, config_version, created_at, updated_at) VALUES "
                "(:id, :artifact_id, :runtime_type, CAST(:gpus AS jsonb), 1, 4096, 0.9, "
                "'FX fault instance', 'mi_5f1300000000', :status, NULL, "
                "'runtime:8000', 1, :now, :now)"
            ),
            {
                "id": INSTANCE_ID,
                "artifact_id": ARTIFACT_ID,
                "runtime_type": RuntimeType.VLLM.value,
                "gpus": json.dumps(["GPU-M5-13"]),
                "status": ModelInstanceStatus.RUNNING.value,
                "now": now,
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO core.gpu_allocations "
                "(id, instance_id, gpu_uuid, status, created_at, updated_at, released_at) "
                "VALUES (:id, :instance_id, 'GPU-M5-13', 'Allocated', :now, :now, NULL)"
            ),
            {"id": ALLOCATION_ID, "instance_id": INSTANCE_ID, "now": now},
        )
        connection.execute(
            sa.text(
                "INSERT INTO core.ai_services "
                "(id, name, public_model_name, description, enabled, route_version, created_at, "
                "updated_at, deleted_at) VALUES "
                "(:id, :name, :name, 'M5-13 fault service', true, 1, :now, :now, NULL)"
            ),
            {"id": SERVICE_ID, "name": PUBLIC_MODEL, "now": now},
        )
        connection.execute(
            sa.text(
                "INSERT INTO core.ai_service_backends "
                "(id, service_id, instance_id, instance_name_snapshot, runtime_snapshot, "
                "backend_key, weight, enabled, health_gate_passed, joined_at, retired_at) "
                "VALUES (:id, :service_id, :instance_id, 'mi_5f1300000000', 'vllm', "
                "'primary', 100, true, true, :now, NULL)"
            ),
            {
                "id": BACKEND_ID,
                "service_id": SERVICE_ID,
                "instance_id": INSTANCE_ID,
                "now": now,
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO core.user_service_permissions "
                "(user_id, service_id, created_at, created_by) "
                "VALUES (:user_id, :service_id, :now, :user_id)"
            ),
            {"user_id": USER_ID, "service_id": SERVICE_ID, "now": now},
        )
        connection.execute(
            sa.text(
                "INSERT INTO core.api_keys "
                "(id, user_id, name, lookup_id, display_prefix, last4, pepper_key_id, "
                "secret_hmac, status, expires_at, last_used_at, created_at, revoked_at) VALUES "
                "(:id, :user_id, 'M5-13 Key', :lookup_id, :prefix, :last4, :pepper, "
                ":hmac, 'Active', NULL, NULL, :now, NULL)"
            ),
            {
                "id": KEY_ID,
                "user_id": USER_ID,
                "lookup_id": issued.lookup_id,
                "prefix": issued.display_prefix,
                "last4": issued.last4,
                "pepper": issued.pepper_key_id,
                "hmac": issued.secret_hmac,
                "now": now,
            },
        )
    return keyring, issued.plaintext


def _sqlite_error(error: BaseException) -> sqlite3.Error | None:
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, sqlite3.Error):
            return current
        cause = current.__cause__
        current = cause if isinstance(cause, BaseException) and cause is not current else None
    return None


def _commit_ack_worker(
    ledger_path: str,
    socket_directory: str,
    port: int,
    sender: Any,
) -> None:
    """Commit with the product Writer, announce the Ack boundary, then await SIGKILL."""

    async def scenario() -> None:
        url = URL.create(
            "postgresql+psycopg",
            username=CLUSTER_USER,
            host=socket_directory,
            port=port,
            database="postgres",
        )
        engine = create_database_engine(url, connect_timeout_seconds=2)
        ledger = open_ledger(ledger_path)
        original = SQLiteLedger.mark_acked_batch

        def before_ack(*args: object, **kwargs: object) -> int:
            del args, kwargs
            sender.send("postgres-committed")
            while True:
                time.sleep(60)

        SQLiteLedger.mark_acked_batch = before_ack
        try:
            writer = LedgerWriter(
                ledger=ledger,
                session_factory=create_session_factory(engine),
                engine=engine,
                instance_id="m5-13-kill-worker",
                build_version="m5-13-test",
            )
            await writer.replay_once()
        finally:
            SQLiteLedger.mark_acked_batch = original
            ledger.close()
            await engine.dispose()

    try:
        asyncio.run(scenario())
    finally:
        sender.close()


class FaultStack:
    def __init__(
        self,
        *,
        route_snapshot_max_age_seconds: float = 60.0,
        ledger_delayed_after_seconds: float = 30.0,
        ledger_unavailable_after_seconds: float = 300.0,
    ) -> None:
        self._fixture = _postgres()
        self._root = Path(tempfile.mkdtemp(prefix="holm-m5-13-", dir="/tmp"))
        self._ledger_path = self._root / "ledger" / "request-ledger.sqlite3"
        self._ledger_path.parent.mkdir(mode=0o700)
        self._logs = self._root / "logs"
        self._logs.mkdir(mode=0o700)
        self._clock = _Clock()
        self._configuration = GatewayConfiguration(
            route_snapshot_max_age_seconds=route_snapshot_max_age_seconds,
            ledger_delayed_after_seconds=ledger_delayed_after_seconds,
            ledger_unavailable_after_seconds=ledger_unavailable_after_seconds,
        )
        self._postgres_running = True
        self._writer_running = True
        self._storage_capped = False
        self._wal_blocked = False
        self._runtime_calls = 0
        self._writer_process: multiprocessing.Process | None = None

        self._keyring, self._plaintext_key = _seed_database(self._fixture)
        self._runner = _LoopThread()
        self._engine: AsyncEngine = create_database_engine(
            _async_url(self._fixture), connect_timeout_seconds=2
        )
        self._factory = create_session_factory(self._engine)
        self._manager = RouteSnapshotManager(
            PostgresRouteSnapshotSource(self._factory),
            listener=None,
            poll_seconds=0.1,
            max_age_seconds=route_snapshot_max_age_seconds,
            clock=self._clock,
        )
        self._runner.run(self._manager.refresh())
        self._upstream = httpx.AsyncClient(
            transport=httpx.MockTransport(self._runtime), timeout=None
        )
        self._spool = SpoolWriter(self._ledger_path)
        self._spool.start()
        self._monitor = self._new_monitor(self._spool)
        self._runner.run(self._monitor.sample_once())
        self._app = self._new_app()

    def _new_monitor(self, spool: SpoolWriter) -> LedgerHealthMonitor:
        return LedgerHealthMonitor(
            self._ledger_path,
            writer=spool,
            delayed_after_seconds=self._configuration.ledger_delayed_after_seconds,
            unavailable_after_seconds=self._configuration.ledger_unavailable_after_seconds,
            interval_seconds=0.05,
            clock=self._clock,
        )

    def _new_app(self) -> Any:
        return create_app(
            authenticator=ApiKeyAuthenticator(
                lookup=SnapshotApiKeyValidationLookup(self._manager),
                keyring=self._keyring,
                availability_check=self._manager.ensure_available,
            ),
            routes=self._manager,
            runtime_capabilities={"vllm": RUNTIME.request_capabilities},
            configuration=self._configuration,
            upstream_client=self._upstream,
            request_ledger=self._spool,
            ledger_health=self._monitor,
        )

    async def _runtime(self, request: httpx.Request) -> httpx.Response:
        del request
        self._runtime_calls += 1
        return httpx.Response(
            200,
            content=b"data: "
            + json.dumps(
                {
                    "id": "chatcmpl-m5-13",
                    "object": "chat.completion.chunk",
                    "model": "mi_5f1300000000",
                    "choices": [
                        {
                            "delta": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": INPUT_TOKENS,
                        "completion_tokens": OUTPUT_TOKENS,
                    },
                }
            ).encode()
            + b"\n\ndata: [DONE]\n\n",
        )

    async def _post(self, sentinel: str) -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self._app), base_url="http://gateway"
        ) as client:
            return await client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {self._plaintext_key}"},
                json={
                    "model": PUBLIC_MODEL,
                    "messages": [{"role": "user", "content": sentinel}],
                    "stream": False,
                    "max_tokens": 8,
                },
            )

    def call(self, *, sentinel: str) -> CallOutcome:
        response = self._runner.run(self._post(sentinel))
        request_id = response.headers.get("x-request-id", "")
        if response.status_code == 200:
            return CallOutcome(response.status_code, None, request_id, "")
        payload = response.json().get("error", {})
        return CallOutcome(
            response.status_code,
            cast(str | None, payload.get("code")),
            str(payload.get("request_id") or request_id),
            str(payload.get("message") or ""),
        )

    def open_streaming_call(self, *, sentinel: str) -> _StreamingCall:
        del sentinel
        request_id = uuid4()
        received_at = datetime.now(UTC)
        self._runner.run(
            self._spool.submit(
                ReceivedEvent(
                    request_id=request_id,
                    event_seq=1,
                    created_at=received_at,
                    received_at=received_at,
                    protocol=RequestProtocol.OPENAI,
                    operation=RequestOperation.CHAT_COMPLETIONS,
                    endpoint="/v1/chat/completions",
                    stream=True,
                    input_bytes=1,
                )
            )
        )
        self._runner.run(
            self._spool.submit(
                InProgressEvent(
                    request_id=request_id,
                    event_seq=2,
                    created_at=datetime.now(UTC),
                    received_at=received_at,
                    user_id=USER_ID,
                    api_key_id=KEY_ID,
                    service_id=SERVICE_ID,
                    instance_id=INSTANCE_ID,
                    requested_max_tokens=8,
                )
            )
        )
        self._runtime_calls += 1
        return _StreamingCall(self, request_id, received_at)

    def advance_clock(self, seconds: float) -> None:
        self._runner.run(self._monitor.sample_once())
        self._clock.advance(seconds)
        if self._postgres_running:
            # TC-GW-24 keeps the control plane healthy while only the Ledger
            # Writer is down, so its periodic proof advances with the clock.
            self._runner.run(self._manager.check_once())

    def _pg_ctl(self, action: str, *, immediate: bool = False) -> None:
        arguments = [
            str(POSTGRESQL_BIN / "pg_ctl"),
            "-D",
            str(self._fixture.data_directory),
        ]
        if action == "stop":
            arguments.extend(
                [
                    "-m",
                    "immediate" if immediate else "fast",
                    "-t",
                    str(PROCESS_TIMEOUT_SECONDS),
                    "-w",
                    "stop",
                ]
            )
        elif action == "start":
            server_options = " ".join(
                (
                    "-F",
                    f"-p {self._fixture.port}",
                    f"-k {self._fixture.socket_directory}",
                    "-c listen_addresses=''",
                    "-c max_connections=10",
                )
            )
            arguments.extend(
                [
                    "-l",
                    str(self._fixture.data_directory.parent / "postgres.log"),
                    "-o",
                    server_options,
                    "-t",
                    str(PROCESS_TIMEOUT_SECONDS),
                    "-w",
                    "start",
                ]
            )
        else:  # pragma: no cover - only the two public methods call this helper
            raise ValueError(f"unsupported pg_ctl action: {action}")
        _run_as_cluster_user(arguments)

    def stop_postgres(self) -> None:
        if not self._postgres_running:
            return
        if self._writer_running:
            self.drain_ledger_writer()
        self._runner.run(self._engine.dispose())
        self._pg_ctl("stop", immediate=True)
        self._postgres_running = False
        self._runner.run(self._manager.check_once(force_refresh=True))

    def start_postgres(self) -> None:
        if self._postgres_running:
            return
        self._pg_ctl("start")
        self._postgres_running = True

    def wait_until_ready(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() <= deadline:
            self._runner.run(self._manager.check_once(force_refresh=True))
            try:
                self._manager.ensure_available()
            except Exception:
                time.sleep(0.05)
            else:
                return True
        return False

    def stop_ledger_writer(self) -> None:
        self._writer_running = False

    def start_ledger_writer(self) -> None:
        self._writer_running = True

    def restart_ledger_writer(self) -> None:
        self._writer_running = True

    async def _replay_all(self) -> None:
        with open_ledger(self._ledger_path) as ledger:
            writer = LedgerWriter(
                ledger=ledger,
                session_factory=self._factory,
                engine=self._engine,
                instance_id="m5-13-writer",
                build_version="m5-13-test",
            )
            for _ in range(100):
                if await writer.replay_once() == 0:
                    return
        raise RuntimeError("M5-13 Ledger Writer did not drain within 100 batches")

    def drain_ledger_writer(self) -> None:
        if not self._writer_running:
            raise RuntimeError("Ledger Writer is stopped")
        self._runner.run(self._replay_all())
        self._runner.run(self._monitor.sample_once())

    def kill_writer_between_commit_and_ack(self) -> None:
        if self._writer_process is not None:
            raise RuntimeError("M5-13 kill worker already exists")
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_commit_ack_worker,
            args=(
                str(self._ledger_path),
                str(self._fixture.socket_directory),
                self._fixture.port,
                sender,
            ),
        )
        process.start()
        sender.close()
        try:
            if not receiver.poll(15.0):
                raise RuntimeError("Ledger Writer did not reach the Commit/Ack boundary")
            if receiver.recv() != "postgres-committed":
                raise RuntimeError("Ledger Writer reported an invalid kill boundary")
            process.kill()
            process.join(timeout=5.0)
            if process.is_alive():
                raise RuntimeError("Ledger Writer child survived SIGKILL")
        finally:
            receiver.close()
            if process.is_alive():
                process.kill()
                process.join(timeout=5.0)
        self._writer_process = process
        self._writer_running = False

    def replay_unacked_events(self) -> None:
        if not self._writer_running:
            raise RuntimeError("Ledger Writer is stopped")
        self.drain_ledger_writer()

    def _force_sqlite_full(self, latch: bool = True) -> None:
        with open_ledger(self._ledger_path) as ledger:
            exhaust_database_capacity(ledger.connection)
            self._storage_capped = True
            for _ in range(20_000):
                now = datetime.now(UTC)
                try:
                    ledger.append(
                        ReceivedEvent(
                            request_id=uuid4(),
                            event_seq=1,
                            created_at=now,
                            received_at=now,
                            protocol=RequestProtocol.OPENAI,
                            operation=RequestOperation.CHAT_COMPLETIONS,
                            endpoint="/v1/chat/completions",
                            stream=False,
                            input_bytes=1,
                        )
                    )
                except LedgerStorageError as error:
                    sqlite_error = _sqlite_error(error)
                    if sqlite_error is None or sqlite_error_name(sqlite_error) != "SQLITE_FULL":
                        raise RuntimeError("FX-SQ-04 did not produce SQLITE_FULL") from error
                    break
            else:
                raise RuntimeError("FX-SQ-04 capacity cap did not fail within 20,000 writes")
        if latch:
            self._monitor.latch_unavailable("ledger_storage_unavailable")

    def exhaust_ledger_storage(self) -> None:
        self._force_sqlite_full()

    def _lift_capacity(self) -> None:
        with open_ledger(self._ledger_path) as ledger:
            restore_database_capacity(ledger.connection)
        self._storage_capped = False

    def _replace_ledger_frontend(
        self,
        *,
        before_commit: Any | None = None,
    ) -> None:
        self._runner.run(self._monitor.aclose())
        self._runner.run(self._spool.close())
        self._spool = SpoolWriter(self._ledger_path, before_commit=before_commit)
        with suppress(SpoolWriterUnavailableError):
            self._spool.start()
        self._monitor = self._new_monitor(self._spool)
        if self._spool.is_available:
            self._runner.run(self._monitor.sample_once())
        self._app = self._new_app()

    def block_ledger_wal(self) -> None:
        self._runner.run(self._monitor.aclose())
        self._runner.run(self._spool.close())
        block_wal_path(cast(Any, _Sidecars(self._ledger_path)))
        self._wal_blocked = True
        self._spool = SpoolWriter(self._ledger_path)
        with suppress(SpoolWriterUnavailableError):
            self._spool.start()
        self._monitor = self._new_monitor(self._spool)
        self._app = self._new_app()

    def restore_ledger_storage(self) -> None:
        if self._wal_blocked:
            unblock_wal_path(cast(Any, _Sidecars(self._ledger_path)))
            self._wal_blocked = False
        if self._storage_capped:
            self._lift_capacity()
        self._replace_ledger_frontend()

    def fail_terminal_writes(self) -> None:
        def fail_terminal_batch(events: tuple[LedgerEvent, ...]) -> None:
            if not any(event.event_type is EventType.COMPLETED for event in events):
                return
            self._force_sqlite_full(False)
            raise LedgerStorageError("FX-SQ-04 terminal SQLITE_FULL")

        self._replace_ledger_frontend(before_commit=fail_terminal_batch)

    def runtime_request_count(self) -> int:
        return self._runtime_calls

    def accepts_new_connections(self) -> bool:
        return self._monitor.health is not LedgerHealth.UNAVAILABLE

    def wal_event_count(self) -> int:
        with open_ledger(self._ledger_path, ConnectionRole.READER) as ledger:
            return len(ledger.list_unacked(limit=500))

    def ledger_health(self) -> LedgerHealth:
        return self._monitor.health

    def admin_signals(self) -> Sequence[str]:
        health = self._monitor.health
        if health is LedgerHealth.AVAILABLE:
            return ()
        status, reason = ledger_backlog_component_state(
            SystemComponentStatus.READY,
            SystemComponentReason.HEARTBEAT_FRESH,
            health,
        )
        return (status.value, reason.value)

    def usage_totals(self) -> dict[str, int]:
        with self._fixture.engine.connect() as connection:
            row = connection.execute(
                sa.text(
                    "SELECT count(*) AS requests, "
                    "coalesce(sum(input_tokens), 0) AS input_tokens, "
                    "coalesce(sum(output_tokens), 0) AS output_tokens "
                    "FROM usage.request_records WHERE record_state = 'completed'"
                )
            ).one()
        return {
            "requests": int(row.requests),
            "input_tokens": int(row.input_tokens),
            "output_tokens": int(row.output_tokens),
        }

    def unacked_event_ids(self) -> Sequence[str]:
        with open_ledger(self._ledger_path, ConnectionRole.READER) as ledger:
            return tuple(
                f"{stored.event.request_id}:{stored.event.event_seq}"
                for stored in ledger.list_unacked(limit=500)
            )

    def scan_for_sentinel(self, sentinel: str) -> Sequence[str]:
        needle = sentinel.encode("utf-8")
        findings: list[str] = []
        with self._fixture.engine.connect() as connection:
            for relation in ("usage.request_records", "core.request_records"):
                rows = connection.execute(
                    sa.text(
                        f"SELECT request_id::text FROM {relation} AS record "  # noqa: S608
                        "WHERE to_jsonb(record)::text LIKE :needle"
                    ),
                    {"needle": f"%{sentinel}%"},
                ).scalars()
                findings.extend(f"postgres:{relation}:{row}" for row in rows)
        for path in (
            self._ledger_path,
            Path(f"{self._ledger_path}-wal"),
            Path(f"{self._ledger_path}-shm"),
            self._fixture.data_directory.parent / "postgres.log",
            *self._logs.rglob("*.log"),
        ):
            if path.is_file() and needle in path.read_bytes():
                findings.append(f"file:{path.name}")
        return tuple(findings)

    def plant_sentinel_positive_control(self, sentinel: str) -> None:
        (self._logs / "positive-control.log").write_text(sentinel, encoding="utf-8")

    def close(self) -> None:
        if not self._postgres_running:
            self.start_postgres()
        process = self._writer_process
        if process is not None and process.is_alive():
            process.kill()
            process.join(timeout=5.0)
        if self._wal_blocked:
            with suppress(Exception):
                unblock_wal_path(cast(Any, _Sidecars(self._ledger_path)))
            self._wal_blocked = False
        if self._storage_capped:
            self._lift_capacity()
        self._runner.run(self._monitor.aclose())
        self._runner.run(self._spool.close())
        self._runner.run(self._upstream.aclose())
        self._runner.run(self._manager.stop())
        self._runner.run(self._engine.dispose())
        self._runner.close()
        resolved = self._root.resolve()
        if resolved.parent != Path("/tmp") or not resolved.name.startswith("holm-m5-13-"):
            raise RuntimeError(f"refusing to remove unsafe fault root: {resolved}")
        shutil.rmtree(resolved)

    def __enter__(self) -> FaultStack:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.close()


@contextmanager
def fault_stack(**overrides: float) -> Iterator[FaultStack]:
    allowed = {
        "route_snapshot_max_age_seconds",
        "ledger_delayed_after_seconds",
        "ledger_unavailable_after_seconds",
    }
    unexpected = set(overrides) - allowed
    if unexpected:
        raise TypeError(f"unsupported M5-13 fault overrides: {sorted(unexpected)}")
    stack = FaultStack(**overrides)
    try:
        yield stack
    finally:
        stack.close()


__all__ = ["CallOutcome", "fault_stack", "initialize_postgres", "shutdown_postgres"]

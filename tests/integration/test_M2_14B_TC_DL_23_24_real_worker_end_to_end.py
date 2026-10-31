"""M2-14B: TC-DL-23/24 driven through the shipped Download Worker entry point.

Every other download test wires ``DownloadResolutionJobHandler`` by hand.  This
module starts ``python -m holm_download_worker`` instead, so the assertions
below fail when the *shipped* wiring in ``__main__._run`` drifts -- which is the
one thing the hand-wired suites structurally cannot see.

Concurrency is process-level throughout: two coroutines on
one event loop share a connection pool and prove nothing about the unique
constraints and row locks that actually do the merging.
"""

from __future__ import annotations

import asyncio
import base64
import json
import multiprocessing
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Final

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from fixtures.asgi_client import (
    NO_CONFIGURED_CREDENTIAL_ROWS,
    AsgiResponse,
    AsgiTestClient,
    SessionCookies,
)
from fixtures.hugging_face import (
    PUBLIC_REPOSITORY_ID,
    TINY_REPOSITORY_COMMIT,
    TINY_REPOSITORY_ID,
    fake_hugging_face_hub,
)
from holm_contracts import VersionPayload
from holm_control_api import (
    SESSION_COOKIE_NAME,
    AuthenticationService,
    DependencyCheck,
    ModelRegistrationService,
    ReadinessReason,
    create_app,
)
from holm_control_api.download_creation_service import DownloadCreationService
from holm_control_api.model_acquisition_read_service import ModelAcquisitionReadService
from holm_control_api.model_resolution_registry import create_default_resolution_registry
from holm_control_api.model_resolution_service import ModelResolutionService
from holm_control_api.modelscope_source import MODELSCOPE_OFFICIAL_ENDPOINT
from holm_domain import ModelOriginType, UserRole
from holm_persistence import (
    UserRepository,
    create_database_engine,
    create_session_factory,
    database_utc_now,
)
from holm_security import (
    OWASP_MINIMUM_MEMORY_COST_KIB,
    OWASP_MINIMUM_TIME_COST,
    Argon2idParameters,
    MasterKey,
    MasterKeyring,
    MasterKeyState,
    PasswordHasher,
)
from migration_test_support import upgrade_head
from sqlalchemy.engine import URL, Engine

SOURCE: Final[ModelOriginType] = ModelOriginType.HUGGING_FACE
REPOSITORY_ID: Final[str] = TINY_REPOSITORY_ID
DEFAULT_BRANCH: Final[str] = "main"
RESOLVED_COMMIT: Final[str] = TINY_REPOSITORY_COMMIT
OTHER_REPOSITORY_ID: Final[str] = PUBLIC_REPOSITORY_ID

PASSWORD: Final[str] = "Correct-Horse-Battery-9"
FIRST_ADMIN: Final[str] = "dl23-admin-one"
SECOND_ADMIN: Final[str] = "dl23-admin-two"
THIRD_ADMIN: Final[str] = "dl23-admin-three"
HMAC_SECRET: Final[bytes] = b"m2-14b-idempotency-secret-padding-0123456789"
KEY_ID: Final[str] = "m2-14b-test"
KEY_MATERIAL: Final[bytes] = b"K" * 32
KEYRING: Final[MasterKeyring] = MasterKeyring(
    (MasterKey(KEY_ID, MasterKeyState.WRITABLE, KEY_MATERIAL),)
)
CHEAP: Final = Argon2idParameters(
    memory_cost_kib=OWASP_MINIMUM_MEMORY_COST_KIB,
    time_cost=OWASP_MINIMUM_TIME_COST,
    parallelism=1,
)

# The tiny repository is ~12.5 MiB across ten files.  Chunked and delayed, one
# run transfers for several seconds, which is what makes an in-flight progress
# sample observable at all.
CHUNK_BYTES: Final[int] = 32 * 1024
CHUNK_DELAY_SECONDS: Final[float] = 0.01

WORKER_TIMEOUT_SECONDS: Final[float] = 240.0
SUBMIT_TIMEOUT_SECONDS: Final[float] = 120.0
POLL_SECONDS: Final[float] = 0.1

TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({"Completed", "Failed", "Cancelled", "Paused"})
# The CI fixtures serve synthetic file bodies, so validation legitimately
# rejects the artifact.  The point of pinning the exact
# pair is that the end-to-end outcome has to be determinate, not "any of these".
TERMINAL_STATUS: Final[str] = "Failed"
TERMINAL_ERROR_CODE: Final[str] = "artifact_required_files_invalid"


# --------------------------------------------------------------------------
# database plumbing
# --------------------------------------------------------------------------


def _async_url(fixture: EphemeralPostgres) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username="ubuntu",
        host=str(fixture.socket_directory),
        port=fixture.port,
        database="postgres",
    )


def _database_secret_url(socket_directory: str, port: int) -> str:
    return URL.create(
        "postgresql+psycopg",
        username="ubuntu",
        database="postgres",
        query={"host": socket_directory, "port": str(port)},
    ).render_as_string(hide_password=False)


def _clear_database(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql(
            # login_protection is included deliberately: these tests log in
            # repeatedly through the real endpoint, and leaving principals behind
            # would hand the next module a database this one shaped.
            "TRUNCATE core.domain_events, core.component_heartbeats, core.jobs, "
            "core.idempotency_records, core.login_protection, core.sessions, core.users, "
            "core.download_subscriptions, core.download_tasks, core.model_artifacts, "
            "core.models, core.model_sources CASCADE"
        )


@pytest.fixture(autouse=True)
def _migrated_empty_database(ephemeral_postgres: EphemeralPostgres) -> Iterator[None]:
    upgrade_head(ephemeral_postgres.engine)
    _clear_database(ephemeral_postgres.engine)
    yield
    _clear_database(ephemeral_postgres.engine)


def _rows(engine: Engine, table: str) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                sa.text(f"SELECT * FROM core.{table} ORDER BY created_at, id")
            ).mappings()
        ]


def _seed_model_sources(engine: Engine) -> None:
    """Registration upserts a Model against an existing Model Source row."""

    with engine.begin() as connection:
        for source_type in ("Hugging Face", "ModelScope"):
            connection.execute(
                sa.text(
                    "INSERT INTO core.model_sources (id, type, created_at, updated_at) "
                    "VALUES (gen_random_uuid(), :type, clock_timestamp(), clock_timestamp())"
                ),
                {"type": source_type},
            )


def _secret_files(tmp_path: Path, fixture: EphemeralPostgres) -> tuple[Path, Path]:
    database_url_file = tmp_path / "database-url"
    keyring_file = tmp_path / "master-keyring.json"
    database_url_file.write_text(
        _database_secret_url(str(fixture.socket_directory), fixture.port), encoding="utf-8"
    )
    database_url_file.chmod(0o400)
    keyring_file.write_text(
        json.dumps(
            {
                "version": 1,
                "keys": [
                    {
                        "id": KEY_ID,
                        "state": "writable",
                        "material": base64.b64encode(KEY_MATERIAL).decode("ascii"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    keyring_file.chmod(0o400)
    return database_url_file, keyring_file


# --------------------------------------------------------------------------
# the Control API, assembled from the production resolution registry
# --------------------------------------------------------------------------


async def _ready() -> ReadinessReason:
    return ReadinessReason.OK


class _NoStorageProbe:
    async def available_bytes(self) -> int | None:
        return None


def _build_api(
    url: URL,
    *,
    hub_endpoint: str,
    modelscope_endpoint: str = MODELSCOPE_OFFICIAL_ENDPOINT,
    scratch_root: Path,
    external_root: Path,
) -> tuple[Any, AsgiTestClient]:
    engine = create_database_engine(url, connect_timeout_seconds=5)
    factory = create_session_factory(engine)
    resolution = ModelResolutionService(
        session_factory=factory,
        keyring=KEYRING,
        registry=create_default_resolution_registry(
            external_model_roots=(external_root,),
            hugging_face_endpoint=hub_endpoint,
            modelscope_endpoint=modelscope_endpoint,
            scratch_root=scratch_root,
        ),
    )
    registration = ModelRegistrationService(
        session_factory=factory, resolution=resolution, idempotency_hmac_secret=HMAC_SECRET
    )
    creation = DownloadCreationService(
        session_factory=factory, registration=registration, idempotency_hmac_secret=HMAC_SECRET
    )
    read_service = ModelAcquisitionReadService(
        session_factory=factory, storage_availability=_NoStorageProbe()
    )
    app = create_app(
        version_payload=VersionPayload(
            build_version="2.2.0+m214b0001",
            commit="e" * 40,
            schema_version=1,
            runtime_compat_hash="f" * 64,
        ),
        readiness_checks=[DependencyCheck(name="database", probe=_ready)],
        credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
        authentication=AuthenticationService(
            session_factory=factory,
            password_hasher=PasswordHasher(CHEAP),
            minimum_login_failure_seconds=0,
        ),
        model_registration=registration,
        download_creation=creation,
        model_acquisition_read=read_service,
    )
    return engine, AsgiTestClient(app)


async def _seed_admins(url: URL) -> None:
    engine = create_database_engine(url, connect_timeout_seconds=5)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            now = await database_utc_now(session)
            hasher = PasswordHasher(CHEAP)
            password_hash = await asyncio.to_thread(hasher.hash, PASSWORD)
            users = UserRepository(session)
            for username in (FIRST_ADMIN, SECOND_ADMIN, THIRD_ADMIN):
                await users.create(
                    username=username,
                    display_name=username,
                    role=UserRole.ADMIN,
                    password_hash=password_hash,
                    now=now,
                    must_change_password=False,
                )
            await session.commit()
    finally:
        await engine.dispose()


def _cookie(response: AsgiResponse, name: str) -> str:
    values: list[str] = []
    for key, raw in response.headers:
        if key.casefold() != "set-cookie":
            continue
        parsed = SimpleCookie()
        parsed.load(raw)
        if name in parsed:
            values.append(parsed[name].value)
    assert len(values) == 1
    return values[0]


async def _login(client: AsgiTestClient, username: str) -> Mapping[str, str]:
    response = await client.request(
        "POST",
        "/api/v1/auth/login",
        json_body={"username": username, "password": PASSWORD},
    )
    assert response.status == 200, response.body
    return SessionCookies(
        {SESSION_COOKIE_NAME: _cookie(response, SESSION_COOKIE_NAME)},
        csrf_token=str(response.json()["csrf_token"]),
    )


# --------------------------------------------------------------------------
# one submitting process: real login, real POST /api/v1/downloads
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _SubmitSpec:
    socket_directory: str
    port: int
    hub_endpoint: str
    scratch_root: str
    external_root: str
    username: str
    idempotency_key: str
    revision: str
    repository_id: str


def _submit_process(
    spec: _SubmitSpec,
    barrier: Any,
    sender: Any,
) -> None:
    """Log in and submit one download from a process of its very own."""

    async def run() -> tuple[int, dict[str, Any]]:
        engine, client = _build_api(
            URL.create(
                "postgresql+psycopg",
                username="ubuntu",
                host=spec.socket_directory,
                port=spec.port,
                database="postgres",
            ),
            hub_endpoint=spec.hub_endpoint,
            scratch_root=Path(spec.scratch_root),
            external_root=Path(spec.external_root),
        )
        try:
            async with client.lifespan():
                cookies = await _login(client, spec.username)
                barrier.wait(SUBMIT_TIMEOUT_SECONDS)
                response = await client.request(
                    "POST",
                    "/api/v1/downloads",
                    json_body={
                        "source": SOURCE.value,
                        "repository_id": spec.repository_id,
                        "revision": spec.revision,
                    },
                    headers={"Idempotency-Key": spec.idempotency_key},
                    cookies=cookies,
                )
                body = json.loads(response.body) if response.body else {}
                return response.status, body
        finally:
            await engine.dispose()

    try:
        sender.send(("ok", os.getpid(), asyncio.run(run())))
    except BaseException as error:  # pragma: no cover - reported to the parent
        sender.send(("error", os.getpid(), f"{type(error).__name__}: {error}"))
        raise
    finally:
        sender.close()


def _submit_concurrently(
    fixture: EphemeralPostgres,
    *,
    hub_endpoint: str,
    scratch_root: Path,
    external_root: Path,
    submissions: tuple[tuple[str, str, str], ...],
    repository_id: str = REPOSITORY_ID,
) -> tuple[tuple[int, dict[str, Any]], ...]:
    """Run every submission in its own process, released by one barrier."""

    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(len(submissions))
    processes = []
    receivers = []
    for username, idempotency_key, revision in submissions:
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_submit_process,
            args=(
                _SubmitSpec(
                    socket_directory=str(fixture.socket_directory),
                    port=fixture.port,
                    hub_endpoint=hub_endpoint,
                    scratch_root=str(scratch_root),
                    external_root=str(external_root),
                    username=username,
                    idempotency_key=idempotency_key,
                    revision=revision,
                    repository_id=repository_id,
                ),
                barrier,
                sender,
            ),
        )
        process.start()
        sender.close()
        processes.append(process)
        receivers.append(receiver)

    results: list[tuple[int, dict[str, Any]]] = []
    try:
        for receiver in receivers:
            if not receiver.poll(SUBMIT_TIMEOUT_SECONDS):
                pytest.fail("a download submission process never reported")
            label, _pid, payload = receiver.recv()
            if label != "ok":
                pytest.fail(f"download submission process failed: {payload}")
            results.append(payload)
    finally:
        for process in processes:
            process.join(timeout=30)
            if process.is_alive():  # pragma: no cover - defensive reaping
                process.kill()
                process.join(timeout=10)
        for receiver in receivers:
            receiver.close()
    assert all(process.exitcode == 0 for process in processes)
    return tuple(results)


# --------------------------------------------------------------------------
# the shipped Worker entry point, started as its own OS process
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _WorkerHandle:
    process: subprocess.Popen[bytes]
    log_path: Path
    instance_id: str


def _start_shipped_worker(
    *,
    database_url_file: Path,
    keyring_file: Path,
    hub_endpoint: str,
    modelscope_endpoint: str,
    model_root: Path,
    tokenizer_root: Path,
    scratch_root: Path,
    external_root: Path,
    log_path: Path,
    instance_id: str,
    extra_environment: Mapping[str, str] | None = None,
    module: str = "holm_download_worker",
) -> _WorkerHandle:
    """Start ``python -m holm_download_worker`` -- the shipped wiring."""

    environment = dict(os.environ)
    environment.update(extra_environment or {})
    log_handle = log_path.open("wb")
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [
                sys.executable,
                "-m",
                module,
                "--database-url-file",
                str(database_url_file),
                "--master-keyring-file",
                str(keyring_file),
                "--instance-id",
                instance_id,
                "--build-version",
                "2.2.0+m214b0001",
                "--hugging-face-endpoint",
                hub_endpoint,
                "--modelscope-endpoint",
                modelscope_endpoint,
                "--model-root",
                str(model_root),
                "--tokenizer-root",
                str(tokenizer_root),
                "--scratch-root",
                str(scratch_root),
                "--external-model-root",
                str(external_root),
                "--lease-seconds",
                "30",
                "--heartbeat-seconds",
                "2",
                "--poll-seconds",
                "0.05",
                "--resolution-timeout-seconds",
                "60",
            ],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=environment,
        )
    finally:
        log_handle.close()
    return _WorkerHandle(process=process, log_path=log_path, instance_id=instance_id)


def _stop_worker(handle: _WorkerHandle) -> int | None:
    process = handle.process
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive reaping
            process.kill()
            process.wait(timeout=10)
    return process.returncode


def _task_rows(engine: Engine) -> list[dict[str, Any]]:
    return _rows(engine, "download_tasks")


def _wait_until(
    predicate: Any,
    *,
    timeout_seconds: float,
    description: str,
    workers: tuple[_WorkerHandle, ...] = (),
) -> Any:
    deadline = time.monotonic() + timeout_seconds
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        for worker in workers:
            if worker.process.poll() is not None:
                pytest.fail(
                    f"{description}: worker {worker.instance_id} exited "
                    f"{worker.process.returncode}\n"
                    f"{worker.log_path.read_text(encoding='utf-8', errors='replace')[-4000:]}"
                )
        time.sleep(POLL_SECONDS)
    logs = "\n".join(
        f"--- {worker.instance_id} ---\n"
        f"{worker.log_path.read_text(encoding='utf-8', errors='replace')[-4000:]}"
        for worker in workers
    )
    pytest.fail(f"timed out waiting for {description}; last={last!r}\n{logs}")


def _all_terminal(engine: Engine, expected_tasks: int) -> Any:
    tasks = _task_rows(engine)
    if len(tasks) != expected_tasks:
        return None
    if not all(task["status"] in TERMINAL_STATUSES for task in tasks):
        return None
    return tasks


# --------------------------------------------------------------------------
# reading the same task back as each subscriber, over real HTTP
# --------------------------------------------------------------------------


async def _list_downloads_as(
    url: URL,
    *,
    hub_endpoint: str,
    modelscope_endpoint: str = MODELSCOPE_OFFICIAL_ENDPOINT,
    scratch_root: Path,
    external_root: Path,
    usernames: tuple[str, ...],
) -> dict[str, list[dict[str, Any]]]:
    engine, client = _build_api(
        url,
        hub_endpoint=hub_endpoint,
        modelscope_endpoint=modelscope_endpoint,
        scratch_root=scratch_root,
        external_root=external_root,
    )
    try:
        async with client.lifespan():
            observed: dict[str, list[dict[str, Any]]] = {}
            for username in usernames:
                cookies = await _login(client, username)
                response = await client.request("GET", "/api/v1/downloads", cookies=cookies)
                assert response.status == 200, response.body
                observed[username] = json.loads(response.body)["items"]
            return observed
    finally:
        await engine.dispose()


async def _observe_shared_progress(
    url: URL,
    *,
    hub_endpoint: str,
    scratch_root: Path,
    external_root: Path,
    worker: _WorkerHandle,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """Sample progress through one long-lived API session.

    Rebuilding the app and logging in per poll costs ~1s, which leaves only a
    handful of samples inside a transfer that lasts a few seconds -- a test that
    can miss its own window is a timeout waiting to happen.  One session, many
    reads.
    """

    api_engine, client = _build_api(
        url,
        hub_endpoint=hub_endpoint,
        scratch_root=scratch_root,
        external_root=external_root,
    )
    try:
        async with client.lifespan():
            cookies = {
                username: await _login(client, username) for username in (FIRST_ADMIN, SECOND_ADMIN)
            }

            async def items(username: str) -> list[dict[str, Any]]:
                response = await client.request(
                    "GET", "/api/v1/downloads", cookies=cookies[username]
                )
                assert response.status == 200, response.body
                return list(json.loads(response.body)["items"])

            sample: dict[str, Any] | None = None
            deadline = time.monotonic() + WORKER_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                observed = await items(FIRST_ADMIN)
                if len(observed) == 1:
                    candidate = observed[0]
                    total = candidate.get("total_bytes")
                    downloaded = candidate.get("downloaded_bytes") or 0
                    if total is not None and 0 < downloaded < total:
                        sample = candidate
                        break
                if worker.process.poll() is not None:
                    break
                await asyncio.sleep(0.05)
            if sample is None:
                pytest.fail(
                    "no in-flight progress sample was ever served by GET /api/v1/downloads\n"
                    + worker.log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
                )
            # Read the other subscriber straight away: same task, same series.
            second = await items(SECOND_ADMIN)

            while time.monotonic() < deadline:
                current = await items(FIRST_ADMIN)
                if current and all(entry["status"] in TERMINAL_STATUSES for entry in current):
                    break
                if worker.process.poll() is not None:
                    break
                await asyncio.sleep(POLL_SECONDS)
            terminal = {
                FIRST_ADMIN: await items(FIRST_ADMIN),
                SECOND_ADMIN: await items(SECOND_ADMIN),
            }
            assert len(second) == 1
            return sample, second[0], terminal
    finally:
        await api_engine.dispose()


@dataclass(frozen=True, slots=True)
class _Bench:
    fixture: EphemeralPostgres
    hub_endpoint: str
    modelscope_endpoint: str
    model_root: Path
    tokenizer_root: Path
    scratch_root: Path
    external_root: Path
    database_url_file: Path
    keyring_file: Path
    log_root: Path

    @property
    def url(self) -> URL:
        return _async_url(self.fixture)

    def submit(
        self,
        submissions: tuple[tuple[str, str, str], ...],
        *,
        repository_id: str = REPOSITORY_ID,
    ) -> Any:
        return _submit_concurrently(
            self.fixture,
            hub_endpoint=self.hub_endpoint,
            scratch_root=self.scratch_root,
            external_root=self.external_root,
            submissions=submissions,
            repository_id=repository_id,
        )

    def worker(
        self,
        instance_id: str,
        *,
        module: str = "holm_download_worker",
        extra_environment: Mapping[str, str] | None = None,
    ) -> _WorkerHandle:
        return _start_shipped_worker(
            database_url_file=self.database_url_file,
            keyring_file=self.keyring_file,
            hub_endpoint=self.hub_endpoint,
            modelscope_endpoint=self.modelscope_endpoint,
            model_root=self.model_root,
            tokenizer_root=self.tokenizer_root,
            scratch_root=self.scratch_root,
            external_root=self.external_root,
            log_path=self.log_root / f"{instance_id}.log",
            instance_id=instance_id,
            module=module,
            extra_environment=extra_environment,
        )

    def read_as(self, usernames: tuple[str, ...]) -> dict[str, list[dict[str, Any]]]:
        return asyncio.run(
            _list_downloads_as(
                self.url,
                hub_endpoint=self.hub_endpoint,
                modelscope_endpoint=self.modelscope_endpoint,
                scratch_root=self.scratch_root,
                external_root=self.external_root,
                usernames=usernames,
            )
        )


def _bench(
    fixture: EphemeralPostgres,
    tmp_path: Path,
    hub_endpoint: str,
    *,
    modelscope_endpoint: str = MODELSCOPE_OFFICIAL_ENDPOINT,
) -> _Bench:
    model_root = tmp_path / "models"
    tokenizer_root = tmp_path / "tokenizers"
    scratch_root = tmp_path / "scratch"
    external_root = tmp_path / "external"
    log_root = tmp_path / "logs"
    for directory in (model_root, tokenizer_root, scratch_root, external_root, log_root):
        directory.mkdir(parents=True, exist_ok=True)
    database_url_file, keyring_file = _secret_files(tmp_path, fixture)
    _seed_model_sources(fixture.engine)
    asyncio.run(_seed_admins(_async_url(fixture)))
    return _Bench(
        fixture=fixture,
        hub_endpoint=hub_endpoint,
        modelscope_endpoint=modelscope_endpoint,
        model_root=model_root,
        tokenizer_root=tokenizer_root,
        scratch_root=scratch_root,
        external_root=external_root,
        database_url_file=database_url_file,
        keyring_file=keyring_file,
        log_root=log_root,
    )


# --------------------------------------------------------------------------
# TC-DL-23: concurrent subscribers on one revision
# --------------------------------------------------------------------------


def _single_task(engine: Engine) -> dict[str, Any]:
    tasks = _task_rows(engine)
    assert len(tasks) == 1, [task["requested_revision_norm"] for task in tasks]
    return tasks[0]


def test_TC_DL_23_concurrent_subscribers_share_one_task_and_its_live_progress(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    """One active task, one resolution job, one progress series -- two subscribers."""

    engine = ephemeral_postgres.engine
    with fake_hugging_face_hub(
        serve_synthetic_file_bodies=True,
        response_chunk_bytes=CHUNK_BYTES,
        response_chunk_delay_seconds=CHUNK_DELAY_SECONDS,
    ) as hub:
        bench = _bench(ephemeral_postgres, tmp_path, hub.endpoint)
        results = bench.submit(
            (
                (FIRST_ADMIN, "dl23-key-one", DEFAULT_BRANCH),
                (SECOND_ADMIN, "dl23-key-two", DEFAULT_BRANCH),
            )
        )
        assert [status for status, _ in results] == [200, 200], results
        # One submission created the task; the other joined it.  Two "created"
        # dispositions would mean two tasks even if a later count hid it.
        assert sorted(body["disposition"] for _status, body in results) == [
            "created",
            "joined_existing",
        ], results

        # three separate counts, not one.
        task = _single_task(engine)
        subscriptions = _rows(engine, "download_subscriptions")
        assert len(subscriptions) == 2
        assert {row["task_id"] for row in subscriptions} == {task["id"]}
        assert len({row["subscriber_id"] for row in subscriptions}) == 2
        jobs = [row for row in _rows(engine, "jobs") if row["target_object_id"] == task["id"]]
        assert len(jobs) == 1
        assert {body["id"] for _status, body in results} == {str(task["id"])}

        worker = bench.worker("m2-14b-dl23")
        try:
            sample, second, terminal_view = asyncio.run(
                _observe_shared_progress(
                    bench.url,
                    hub_endpoint=bench.hub_endpoint,
                    scratch_root=bench.scratch_root,
                    external_root=bench.external_root,
                    worker=worker,
                )
            )
        finally:
            _stop_worker(worker)

        # The read API served a live, partial value -- not a single write at the
        # end -- and the second subscriber saw the same task and the same series.
        assert 0 < sample["downloaded_bytes"] < sample["total_bytes"]
        assert second["id"] == sample["id"]
        assert second["total_bytes"] == sample["total_bytes"]
        assert (second["downloaded_bytes"] or 0) >= sample["downloaded_bytes"]

        terminal = _wait_until(
            lambda: _all_terminal(engine, expected_tasks=1),
            timeout_seconds=WORKER_TIMEOUT_SECONDS,
            description="the single task reaching a terminal status",
        )
        assert terminal[0]["resolved_revision"] == RESOLVED_COMMIT

        # Terminal reads are stable, so the two subscribers must agree exactly.
        observed = bench.read_as((FIRST_ADMIN, SECOND_ADMIN))
        assert len(observed[FIRST_ADMIN]) == 1
        assert observed[FIRST_ADMIN] == observed[SECOND_ADMIN]
        assert observed[FIRST_ADMIN][0]["status"] == TERMINAL_STATUS
        assert observed[FIRST_ADMIN][0]["error_code"] == TERMINAL_ERROR_CODE
        assert terminal_view[FIRST_ADMIN] == terminal_view[SECOND_ADMIN]
        assert hub.requests


def test_TC_DL_23_control_distinct_identities_are_not_merged(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    """The row counts above discriminate: two identities really do make two tasks.

    Without this control ``len(tasks) == 1`` would also hold for an
    implementation that merged everything onto whatever task existed first.
    """

    engine = ephemeral_postgres.engine
    with fake_hugging_face_hub() as hub:
        bench = _bench(ephemeral_postgres, tmp_path, hub.endpoint)
        results = _submit_concurrently(
            ephemeral_postgres,
            hub_endpoint=hub.endpoint,
            scratch_root=bench.scratch_root,
            external_root=bench.external_root,
            submissions=(
                (FIRST_ADMIN, "dl23-control-one", DEFAULT_BRANCH),
                (SECOND_ADMIN, "dl23-control-two", DEFAULT_BRANCH),
            ),
        )
        assert [status for status, _ in results] == [200, 200]
    assert len(_task_rows(engine)) == 1

    # A different repository is a different identity and must not be merged.
    with fake_hugging_face_hub() as hub:
        other = _submit_concurrently(
            ephemeral_postgres,
            hub_endpoint=hub.endpoint,
            scratch_root=bench.scratch_root,
            external_root=bench.external_root,
            submissions=((FIRST_ADMIN, "dl23-control-three", DEFAULT_BRANCH),),
            repository_id=OTHER_REPOSITORY_ID,
        )
        assert other[0][0] == 200
    tasks = _task_rows(engine)
    assert len(tasks) == 2
    assert {task["repository_id"] for task in tasks} == {REPOSITORY_ID, OTHER_REPOSITORY_ID}
    assert len(_rows(engine, "download_subscriptions")) == 3


# --------------------------------------------------------------------------
# TC-DL-24: pre-resolution concurrency, then a second merge at the Commit
# --------------------------------------------------------------------------


def _by_requested_revision(engine: Engine) -> dict[str, dict[str, Any]]:
    return {task["requested_revision_norm"]: task for task in _task_rows(engine)}


def test_TC_DL_24_pre_resolution_merge_then_second_merge_at_the_resolved_revision(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    """Phase 1 dedups the Requested Revision; phase 2 merges on the Commit."""

    engine = ephemeral_postgres.engine
    with fake_hugging_face_hub(
        serve_synthetic_file_bodies=True,
        response_chunk_bytes=CHUNK_BYTES,
        response_chunk_delay_seconds=CHUNK_DELAY_SECONDS,
    ) as hub:
        bench = _bench(ephemeral_postgres, tmp_path, hub.endpoint)

        # Phase 1 -- same Source/Repository/Requested Revision, two processes.
        phase_one = bench.submit(
            (
                (FIRST_ADMIN, "dl24-branch-one", DEFAULT_BRANCH),
                (SECOND_ADMIN, "dl24-branch-two", DEFAULT_BRANCH),
            )
        )
        assert [status for status, _ in phase_one] == [200, 200], phase_one
        branch_task = _single_task(engine)
        assert len(_rows(engine, "download_subscriptions")) == 2

        # Phase 2 -- a different Requested Revision that resolves to the very
        # same immutable Commit.  It must NOT be merged before resolution.
        phase_two = bench.submit(((THIRD_ADMIN, "dl24-commit", RESOLVED_COMMIT),))
        assert phase_two[0][0] == 200, phase_two
        tasks = _by_requested_revision(engine)
        assert set(tasks) == {DEFAULT_BRANCH, RESOLVED_COMMIT}
        assert len(_rows(engine, "download_subscriptions")) == 3

        workers = (bench.worker("m2-14b-dl24-a"), bench.worker("m2-14b-dl24-b"))
        try:
            _wait_until(
                lambda: _all_terminal(engine, expected_tasks=2),
                timeout_seconds=WORKER_TIMEOUT_SECONDS,
                description="both tasks reaching a terminal status after the second merge",
                workers=workers,
            )
        finally:
            for handle in workers:
                _stop_worker(handle)

        final = _task_rows(engine)
        cancelled = [task for task in final if task["status"] == "Cancelled"]
        survivors = [task for task in final if task["status"] != "Cancelled"]
        assert len(cancelled) == 1, [(t["requested_revision_norm"], t["status"]) for t in final]
        assert len(survivors) == 1
        loser = cancelled[0]
        winner = survivors[0]
        assert loser["error_code"] == "duplicate_resolved_revision"
        assert loser["resolved_revision"] == RESOLVED_COMMIT
        assert winner["resolved_revision"] == RESOLVED_COMMIT
        assert winner["status"] == TERMINAL_STATUS
        assert winner["error_code"] == TERMINAL_ERROR_CODE

        # No subscriber is dropped by the merge, and all three land on the winner.
        subscriptions = _rows(engine, "download_subscriptions")
        assert len(subscriptions) == 3
        assert {row["task_id"] for row in subscriptions} == {winner["id"]}
        assert len({row["subscriber_id"] for row in subscriptions}) == 3

        observed = bench.read_as((FIRST_ADMIN, SECOND_ADMIN, THIRD_ADMIN))
        winners = [
            next(item for item in items if item["id"] == str(winner["id"]))
            for items in observed.values()
        ]
        assert winners[0] == winners[1] == winners[2]
        # The phase-1 task is one of the two, and whichever role it ended up in,
        # the three subscribers are all reading the survivor.
        assert branch_task["id"] in {winner["id"], loser["id"]}
        assert winner["id"] != loser["id"]


# --------------------------------------------------------------------------
# positive control: the judgment really does ride on __main__
# --------------------------------------------------------------------------


_NO_PUBLISHER_SHIM: Final[str] = '''\
"""Positive control: the shipped entry point with the publisher attach removed."""

from holm_download_worker import worker as _worker
from holm_download_worker.__main__ import main


def _skip_attach(self, publisher):  # noqa: ANN001, ANN202 - control shim
    return None


_worker.DownloadResolutionJobHandler.attach_artifact_publisher = _skip_attach

if __name__ == "__main__":
    main()
'''


def test_M2_14B_positive_control_dropping_the_publisher_attach_is_detected(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    """with ``attach_artifact_publisher`` neutered the run cannot finish.

    This is what makes the end-to-end tests above claims about the *shipped*
    wiring.  Neutering one attach and still reaching the same terminal state
    would mean the assertions never depended on ``__main__`` at all.
    """

    shim_root = tmp_path / "shims"
    shim_root.mkdir()
    (shim_root / "m2_14b_no_publisher_shim.py").write_text(_NO_PUBLISHER_SHIM, encoding="utf-8")

    engine = ephemeral_postgres.engine
    with fake_hugging_face_hub(
        serve_synthetic_file_bodies=True,
        response_chunk_bytes=CHUNK_BYTES,
        response_chunk_delay_seconds=CHUNK_DELAY_SECONDS,
    ) as hub:
        bench = _bench(ephemeral_postgres, tmp_path, hub.endpoint)
        assert bench.submit(((FIRST_ADMIN, "dl24-control", DEFAULT_BRANCH),))[0][0] == 200

        existing = os.environ.get("PYTHONPATH")
        worker = bench.worker(
            "m2-14b-no-publisher",
            module="m2_14b_no_publisher_shim",
            extra_environment={
                "PYTHONPATH": f"{shim_root}{os.pathsep}{existing}" if existing else str(shim_root)
            },
        )
        try:
            # The download itself still runs; only publication is missing.
            _wait_until(
                lambda: _single_task(engine)["status"] == "Verifying",
                timeout_seconds=WORKER_TIMEOUT_SECONDS,
                description="the publisher-less run stalling at Verifying",
                workers=(worker,),
            )
            # And it must stay there: no terminal state without the publisher.
            time.sleep(3.0)
            assert _single_task(engine)["status"] == "Verifying"
        finally:
            _stop_worker(worker)
        assert hub.requests


_NO_MERGE_SHIM: Final[str] = '''\
"""Positive control: the shipped entry point with subscription merging removed."""

from holm_persistence import download_task_repository as _repository
from holm_download_worker.__main__ import main


async def _skip_merge(self, task_id, winner_id, resolved_revision, now):  # noqa: ANN001, ANN202
    return None


_repository.DownloadTaskRepository.merge_subscriptions = _skip_merge

if __name__ == "__main__":
    main()
'''


def test_TC_DL_24_positive_control_without_the_second_merge_both_identities_survive(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    """The merge assertions discriminate: remove the merge and they go red.

    ``merge_subscriptions`` is the one statement that both cancels the loser and
    moves its subscribers.  With it neutered the loser must stay uncancelled and
    the subscriptions must stay split -- exactly what the TC-DL-24 test forbids.
    """

    shim_root = tmp_path / "shims"
    shim_root.mkdir()
    (shim_root / "m2_14b_no_merge_shim.py").write_text(_NO_MERGE_SHIM, encoding="utf-8")

    engine = ephemeral_postgres.engine
    with fake_hugging_face_hub(
        serve_synthetic_file_bodies=True,
        response_chunk_bytes=CHUNK_BYTES,
        response_chunk_delay_seconds=CHUNK_DELAY_SECONDS,
    ) as hub:
        bench = _bench(ephemeral_postgres, tmp_path, hub.endpoint)
        assert bench.submit(((FIRST_ADMIN, "dl24-nm-branch", DEFAULT_BRANCH),))[0][0] == 200
        assert bench.submit(((SECOND_ADMIN, "dl24-nm-commit", RESOLVED_COMMIT),))[0][0] == 200
        assert len(_task_rows(engine)) == 2

        existing = os.environ.get("PYTHONPATH")
        environment = {
            "PYTHONPATH": f"{shim_root}{os.pathsep}{existing}" if existing else str(shim_root)
        }
        workers = tuple(
            bench.worker(
                f"m2-14b-no-merge-{index}",
                module="m2_14b_no_merge_shim",
                extra_environment=environment,
            )
            for index in range(2)
        )
        try:
            # The winner still runs to completion; only the loser's merge is gone,
            # so it is left behind in Resolving instead of being cancelled.
            _wait_until(
                lambda: [
                    task for task in _task_rows(engine) if task["status"] in TERMINAL_STATUSES
                ],
                timeout_seconds=WORKER_TIMEOUT_SECONDS,
                description="the winner finishing while the merge is neutered",
                workers=workers,
            )
            time.sleep(3.0)
            tasks = _task_rows(engine)
            assert [task for task in tasks if task["status"] == "Cancelled"] == [], [
                (task["requested_revision_norm"], task["status"]) for task in tasks
            ]
            subscriptions = _rows(engine, "download_subscriptions")
            assert len({row["task_id"] for row in subscriptions}) == 2
        finally:
            for handle in workers:
                _stop_worker(handle)
        assert hub.requests

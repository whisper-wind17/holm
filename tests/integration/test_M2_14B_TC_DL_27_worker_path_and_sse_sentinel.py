"""M2-14B: TC-DL-27 over the whole shipped Worker path, SSE included.

The two existing TC-DL-27 suites scan the *download child* only
(``test_M2_06B_TC_DL_20_27_modelscope_download.py``,
``tests/security/test_M2_04A_TC_DL_26_27_hugging_face_boundaries.py``).  TST's
判定标准 also names the SSE event stream, and nothing scanned it.  This module
runs ``python -m holm_download_worker`` against the recorded fake Hub with
a sentinel credential in the database and scans every sink named there.

Two rules shape the assertions:

* a sink is asserted non-empty *before* it is asserted clean -- scanning zero
  bytes is indistinguishable from finding no leak;
* the guard is two-part (the scan, and the list of sinks it scans), so it gets
  two positive controls: one that proves the scan finds a real leak, and one
  that proves the finding came from the SSE sink rather than somewhere else.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from credential_scan import database_bytes
from fixtures.asgi_client import NO_CONFIGURED_CREDENTIAL_ROWS, AsgiTestClient
from fixtures.hugging_face import CredentialPresence, fake_hugging_face_hub
from holm_contracts import VersionPayload
from holm_control_api import (
    EVENTS_PATH,
    AuthenticationService,
    DependencyCheck,
    EventStreamService,
    ModelRegistrationService,
    ReadinessReason,
    create_app,
)
from holm_control_api.download_creation_service import DownloadCreationService
from holm_control_api.model_acquisition_read_service import ModelAcquisitionReadService
from holm_control_api.model_resolution_registry import create_default_resolution_registry
from holm_control_api.model_resolution_service import ModelResolutionService
from holm_domain import (
    CredentialType,
    DomainEventType,
    ModelOriginType,
)
from holm_domain.production_events import PRODUCTION_EVENT_REGISTRY
from holm_persistence import (
    DomainEventRepository,
    create_database_engine,
    create_session_factory,
)
from holm_persistence.model_sources import MODEL_SOURCES
from holm_security import PasswordHasher
from migration_test_support import upgrade_head
from sqlalchemy.engine import Engine
from test_M2_14B_TC_DL_23_24_real_worker_end_to_end import (
    CHEAP,
    CHUNK_BYTES,
    CHUNK_DELAY_SECONDS,
    DEFAULT_BRANCH,
    FIRST_ADMIN,
    HMAC_SECRET,
    KEYRING,
    POLL_SECONDS,
    REPOSITORY_ID,
    SOURCE,
    TERMINAL_STATUSES,
    WORKER_TIMEOUT_SECONDS,
    _async_url,
    _bench,
    _clear_database,
    _login,
    _rows,
    _stop_worker,
    _WorkerHandle,
)

SINK_NAMES: Final[tuple[str, ...]] = (
    "sse_frames",
    "process_command_lines",
    "process_environments",
    "worker_logs",
    "files_on_disk",
    "database_plaintext",
)


@pytest.fixture(autouse=True)
def _migrated_empty_database(ephemeral_postgres: EphemeralPostgres) -> Iterator[None]:
    upgrade_head(ephemeral_postgres.engine)
    _clear_database(ephemeral_postgres.engine)
    yield
    _clear_database(ephemeral_postgres.engine)


def _sentinel() -> str:
    return f"PWS_M214B_TCDL27_{os.urandom(12).hex().upper()}"


def _seed_credential(engine: Engine, source: ModelOriginType, sentinel: str) -> UUID:
    """Store the sentinel the way the platform stores a real source token."""

    with engine.begin() as connection:
        source_id = cast(
            UUID,
            connection.execute(
                sa.select(MODEL_SOURCES.c.id).where(MODEL_SOURCES.c.type == source.value)
            ).scalar_one(),
        )
        envelope = KEYRING.encrypt(
            sentinel.encode("utf-8"),
            source_id=source_id,
            credential_type=CredentialType.ACCESS_TOKEN.value,
        )
        connection.execute(
            sa.update(MODEL_SOURCES)
            .where(MODEL_SOURCES.c.id == source_id)
            .values(
                credential_key_id=envelope.key_id,
                credential_nonce=envelope.nonce,
                credential_ciphertext=envelope.ciphertext,
                credential_tag=envelope.tag,
                updated_at=sa.func.clock_timestamp(),
            )
        )
    return source_id


# --------------------------------------------------------------------------
# collected surfaces
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ScannedFile:
    """One file as it was when it was read, not as it is afterwards.

    Staging is cleaned up when publication fails, so a scan that ran only after
    the worker exited would find an empty tree and call it clean.  Files are
    therefore read *during* the run, and what is kept here is the verdict.
    """

    path: Path
    size_bytes: int
    carries_sentinel: bool


@dataclass(frozen=True, slots=True)
class _Surfaces:
    sse_frames: tuple[bytes, ...]
    process_command_lines: tuple[str, ...]
    process_environments: tuple[str, ...]
    worker_logs: tuple[str, ...]
    files_on_disk: tuple[_ScannedFile, ...]
    database_plaintext: bytes

    def sizes(self) -> Mapping[str, int]:
        return {
            "sse_frames": len(self.sse_frames),
            "process_command_lines": len(self.process_command_lines),
            "process_environments": len(self.process_environments),
            "worker_logs": len(self.worker_logs),
            "files_on_disk": len(self.files_on_disk),
            "database_plaintext": len(self.database_plaintext),
        }

    def non_empty_files(self) -> tuple[_ScannedFile, ...]:
        return tuple(entry for entry in self.files_on_disk if entry.size_bytes > 0)


def _scan(
    surfaces: _Surfaces, sentinel: str, *, sinks: Sequence[str] = SINK_NAMES
) -> tuple[str, ...]:
    """Return the sinks that carry the sentinel -- empty means no leak found."""

    needle = sentinel.encode("utf-8")
    found: list[str] = []
    for sink in sinks:
        if sink not in SINK_NAMES:
            raise AssertionError(f"unknown sink {sink!r}")
        if sink == "sse_frames":
            hit = any(needle in frame for frame in surfaces.sse_frames)
        elif sink == "process_command_lines":
            hit = any(sentinel in value for value in surfaces.process_command_lines)
        elif sink == "process_environments":
            hit = any(sentinel in value for value in surfaces.process_environments)
        elif sink == "worker_logs":
            hit = any(sentinel in value for value in surfaces.worker_logs)
        elif sink == "files_on_disk":
            hit = any(entry.carries_sentinel for entry in surfaces.files_on_disk)
        else:
            hit = needle in surfaces.database_plaintext
        if hit:
            found.append(sink)
    return tuple(found)


def _file_contains(path: Path, needle: bytes) -> bool:
    try:
        with path.open("rb") as stream:
            tail = b""
            while chunk := stream.read(1 << 20):
                if needle in tail + chunk:
                    return True
                tail = chunk[-len(needle) :]
    except OSError:
        return False
    return False


def _scan_new_files(
    roots: Sequence[Path],
    sentinel: str,
    seen: dict[Path, _ScannedFile],
) -> None:
    """Read every file not read before, right now, while it still exists."""

    needle = sentinel.encode("utf-8")
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path in seen or path.is_symlink() or not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            seen[path] = _ScannedFile(
                path=path,
                size_bytes=size,
                carries_sentinel=_file_contains(path, needle),
            )


def _descendants(pid: int) -> tuple[int, ...]:
    """Every live descendant of ``pid``, read straight out of ``/proc``."""

    children: dict[int, list[int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in status.splitlines():
            if line.startswith("PPid:"):
                children.setdefault(int(line.split()[1]), []).append(int(entry.name))
                break
    collected: list[int] = []
    frontier = [pid]
    while frontier:
        current = frontier.pop()
        for child in children.get(current, ()):
            collected.append(child)
            frontier.append(child)
    return tuple(collected)


def _sample_processes(pid: int) -> tuple[tuple[str, ...], tuple[str, ...]]:
    command_lines: list[str] = []
    environments: list[str] = []
    for candidate in (pid, *_descendants(pid)):
        base = Path("/proc") / str(candidate)
        for name, sink in (("cmdline", command_lines), ("environ", environments)):
            try:
                raw = (base / name).read_bytes()
            except OSError:
                continue
            if raw:
                sink.append(raw.replace(b"\x00", b" ").decode("utf-8", errors="replace"))
    return tuple(command_lines), tuple(environments)


# --------------------------------------------------------------------------
# the API used here carries the SSE endpoint as well
# --------------------------------------------------------------------------


async def _ready() -> ReadinessReason:
    return ReadinessReason.OK


class _NoStorageProbe:
    async def available_bytes(self) -> int | None:
        return None


def _app_with_events(factory: Any, bench: Any, event_stream: EventStreamService) -> Any:
    resolution = ModelResolutionService(
        session_factory=factory,
        keyring=KEYRING,
        registry=create_default_resolution_registry(
            external_model_roots=(bench.external_root,),
            hugging_face_endpoint=bench.hub_endpoint,
            scratch_root=bench.scratch_root,
        ),
    )
    registration = ModelRegistrationService(
        session_factory=factory, resolution=resolution, idempotency_hmac_secret=HMAC_SECRET
    )
    return create_app(
        version_payload=VersionPayload(
            build_version="2.2.0+m214b0027",
            commit="a" * 40,
            schema_version=1,
            runtime_compat_hash="b" * 64,
        ),
        readiness_checks=[DependencyCheck(name="database", probe=_ready)],
        credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
        authentication=AuthenticationService(
            session_factory=factory,
            password_hasher=PasswordHasher(CHEAP),
            minimum_login_failure_seconds=0,
        ),
        event_stream=event_stream,
        model_registration=registration,
        download_creation=DownloadCreationService(
            session_factory=factory,
            registration=registration,
            idempotency_hmac_secret=HMAC_SECRET,
        ),
        model_acquisition_read=ModelAcquisitionReadService(
            session_factory=factory, storage_availability=_NoStorageProbe()
        ),
    )


async def _read_frames(stream: Any, frames: list[bytes]) -> None:
    while True:
        frames.append(await stream.read_chunk())


async def _exercise_shipped_worker(
    bench: Any,
    fixture: EphemeralPostgres,
    *,
    worker_name: str,
    sentinel: str,
    source: ModelOriginType = SOURCE,
    repository_id: str = REPOSITORY_ID,
    revision: str = DEFAULT_BRANCH,
    timeout_seconds: float | None = WORKER_TIMEOUT_SECONDS,
) -> tuple[_Surfaces, dict[str, Any]]:
    """Run one full download while an SSE client and /proc sampler watch."""

    engine = create_database_engine(_async_url(fixture), connect_timeout_seconds=5)
    frames: list[bytes] = []
    command_lines: list[str] = []
    environments: list[str] = []
    scanned_files: dict[Path, _ScannedFile] = {}
    roots = (
        bench.model_root,
        bench.tokenizer_root,
        bench.scratch_root,
        bench.external_root,
        bench.log_root,
    )
    worker: _WorkerHandle | None = None
    try:
        factory = create_session_factory(engine)
        event_stream = EventStreamService(
            factory,
            PRODUCTION_EVENT_REGISTRY,
            listener=None,
            poll_seconds=0.05,
            keepalive_seconds=0.5,
        )
        client = AsgiTestClient(_app_with_events(factory, bench, event_stream))
        async with client.lifespan():
            cookies = await _login(client, FIRST_ADMIN)
            stream = await client.stream("GET", EVENTS_PATH, cookies=cookies)
            reader = asyncio.create_task(_read_frames(stream, frames))
            try:
                created = await client.request(
                    "POST",
                    "/api/v1/downloads",
                    json_body={
                        "source": source.value,
                        "repository_id": repository_id,
                        "revision": revision,
                    },
                    headers={"Idempotency-Key": "dl27-key"},
                    cookies=cookies,
                )
                assert created.status == 200, created.body

                worker = bench.worker(worker_name)
                deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
                terminal: dict[str, Any] | None = None
                while deadline is None or time.monotonic() < deadline:
                    sampled_commands, sampled_environments = _sample_processes(worker.process.pid)
                    command_lines.extend(sampled_commands)
                    environments.extend(sampled_environments)
                    # Read staging while it still exists: a failed publication
                    # removes it, and a post-mortem scan of an empty tree would
                    # report "clean" without having read a single weight byte.
                    _scan_new_files(roots, sentinel, scanned_files)
                    tasks = _rows(fixture.engine, "download_tasks")
                    if tasks and all(row["status"] in TERMINAL_STATUSES for row in tasks):
                        terminal = tasks[0]
                        break
                    if worker.process.poll() is not None:
                        break
                    await asyncio.sleep(POLL_SECONDS)
                if terminal is None:
                    pytest.fail(
                        "the shipped Worker never reached a terminal status\n"
                        + worker.log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
                    )
                # Give the stream a moment to deliver anything published last.
                await asyncio.sleep(1.0)
                _scan_new_files(roots, sentinel, scanned_files)
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
                await stream.close()
    finally:
        if worker is not None:
            _stop_worker(worker)
        await engine.dispose()

    assert worker is not None
    surfaces = _Surfaces(
        sse_frames=tuple(frames),
        process_command_lines=tuple(command_lines),
        process_environments=tuple(environments),
        worker_logs=(worker.log_path.read_text(encoding="utf-8", errors="replace"),),
        files_on_disk=tuple(scanned_files.values()),
        database_plaintext=database_bytes(fixture.engine),
    )
    return surfaces, terminal


# --------------------------------------------------------------------------
# TC-DL-27
# --------------------------------------------------------------------------


def test_TC_DL_27_sentinel_credential_survives_the_shipped_worker_path_and_no_sink(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    """One sentinel, one full Worker run, every sink named by TST 5.2."""

    sentinel = _sentinel()
    with fake_hugging_face_hub(
        valid_token=sentinel,
        serve_synthetic_file_bodies=True,
        response_chunk_bytes=CHUNK_BYTES,
        response_chunk_delay_seconds=CHUNK_DELAY_SECONDS,
    ) as hub:
        bench = _bench(ephemeral_postgres, tmp_path, hub.endpoint)
        _seed_credential(ephemeral_postgres.engine, SOURCE, sentinel)
        surfaces, terminal = asyncio.run(
            _exercise_shipped_worker(
                bench,
                ephemeral_postgres,
                worker_name="m2-14b-dl27",
                sentinel=sentinel,
            )
        )
        # The sentinel is only worth scanning for if it actually travelled.
        assert hub.requests
        assert CredentialPresence.VALID in {request.credential for request in hub.requests}

    # Every sink is proven non-empty before it is declared clean.
    sizes = surfaces.sizes()
    assert sizes["sse_frames"] > 0, sizes
    assert any(
        b"event: " + DomainEventType.DOWNLOAD_RESOLUTION_STARTED.value.encode("utf-8") in frame
        for frame in surfaces.sse_frames
    ), [frame[:120] for frame in surfaces.sse_frames]
    assert any("holm_download_worker" in value for value in surfaces.process_command_lines)
    # The download and resolution children must have been sampled too, or the
    # argv/environ sink only ever looked at the parent.
    assert len({value for value in surfaces.process_command_lines}) > 1, (
        surfaces.process_command_lines
    )
    assert sizes["process_environments"] > 0
    # The log sink is scanned but carries no evidence today, and saying so is
    # the point: "no sentinel in an empty file" is indistinguishable from "no
    # ability to find one".  TDD 17.2 asks for structured JSON logs, yet no
    # component in this build configures logging at all, and the Download
    # Worker's three ``_LOGGER`` call sites (worker.py:706/744/753) are all
    # failure paths that a successful run never reaches.  Pin the emptiness so
    # that the day the Worker does log, this assertion fails and the sink has
    # to be certified instead of silently staying vacuous.  Registered as a
    # handover in TASKS.md.
    assert surfaces.worker_logs == ("",), surfaces.worker_logs
    # Not just "some files existed": real, non-empty payload files were read
    # while the download was on disk.
    assert len(surfaces.non_empty_files()) >= 5, [
        (entry.path.name, entry.size_bytes) for entry in surfaces.files_on_disk
    ]
    assert max(entry.size_bytes for entry in surfaces.non_empty_files()) > 100_000
    assert sizes["database_plaintext"] > 0

    assert _scan(surfaces, sentinel) == ()
    assert terminal["status"] in TERMINAL_STATUSES


def test_TC_DL_27_positive_control_a_sentinel_in_an_sse_payload_is_found(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    """Two controls in one run: the scan finds a real leak, and the SSE sink is
    where the finding comes from.

    The first half proves the判定逻辑 works.  The second half mutates the *sink
    list* -- the configuration half of the guard -- and shows the same leak then
    goes unnoticed, which is what makes "no leak" above a statement about the
    SSE stream rather than about whatever else happened to be scanned.
    """

    sentinel = _sentinel()
    with fake_hugging_face_hub() as hub:
        bench = _bench(ephemeral_postgres, tmp_path, hub.endpoint)
        surfaces = asyncio.run(_leaky_sse_surfaces(ephemeral_postgres, bench, sentinel))

    assert surfaces.sse_frames
    assert _scan(surfaces, sentinel) == ("sse_frames",)
    remaining = tuple(name for name in SINK_NAMES if name != "sse_frames")
    assert _scan(surfaces, sentinel, sinks=remaining) == ()


async def _leaky_sse_surfaces(
    fixture: EphemeralPostgres,
    bench: Any,
    sentinel: str,
) -> _Surfaces:
    """Publish one production event whose declared payload field carries the token."""

    engine = create_database_engine(_async_url(fixture), connect_timeout_seconds=5)
    frames: list[bytes] = []
    try:
        factory = create_session_factory(engine)
        event_stream = EventStreamService(
            factory,
            PRODUCTION_EVENT_REGISTRY,
            listener=None,
            poll_seconds=0.05,
            keepalive_seconds=0.5,
        )
        client = AsgiTestClient(_app_with_events(factory, bench, event_stream))
        async with client.lifespan():
            cookies = await _login(client, FIRST_ADMIN)
            stream = await client.stream("GET", EVENTS_PATH, cookies=cookies)
            reader = asyncio.create_task(_read_frames(stream, frames))
            try:
                draft = PRODUCTION_EVENT_REGISTRY.build_draft(
                    DomainEventType.DOWNLOAD_RESOLUTION_STARTED.value,
                    aggregate_id=uuid4(),
                    version=1,
                    candidates={"status": sentinel},
                )
                async with factory() as session, session.begin():
                    await DomainEventRepository(session).append(draft)
                deadline = time.monotonic() + 20.0
                while time.monotonic() < deadline:
                    if any(sentinel.encode("utf-8") in frame for frame in frames):
                        break
                    await asyncio.sleep(POLL_SECONDS)
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
                await stream.close()
    finally:
        await engine.dispose()
    return _Surfaces(
        sse_frames=tuple(frames),
        process_command_lines=("no leak here",),
        process_environments=("no leak here",),
        worker_logs=("no leak here",),
        files_on_disk=(),
        database_plaintext=b"no leak here",
    )

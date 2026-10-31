"""M2-06B: real source SDK downloads, ModelScope transport, and TC-DL-27."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from credential_scan import database_bytes
from fixtures.hugging_face import (
    MISSING_REPOSITORY_ID as HF_MISSING_REPOSITORY_ID,
)
from fixtures.hugging_face import (
    TINY_REPOSITORY_COMMIT,
    TINY_REPOSITORY_ID,
    fake_hugging_face_hub,
)
from fixtures.hugging_face import (
    CredentialPresence as HuggingFaceCredentialPresence,
)
from fixtures.hugging_face import (
    FakeHub as HuggingFaceFakeHub,
)
from fixtures.modelscope import (
    CONFIG_FILENAME,
    PUBLIC_DEFAULT_BRANCH,
    PUBLIC_LARGE_FILE,
    PUBLIC_REPOSITORY_ID,
    assert_recorded_lfs_redirect_agrees_with_listing,
    fake_modelscope_hub,
)
from fixtures.modelscope import (
    MISSING_REPOSITORY_ID as MS_MISSING_REPOSITORY_ID,
)
from fixtures.modelscope import (
    CredentialPresence as ModelScopeCredentialPresence,
)
from fixtures.modelscope import (
    FakeHub as ModelScopeFakeHub,
)
from fixtures.source_mutation import load_mutated_module
from holm_domain import (
    CredentialType,
    JobExecutionKey,
    ModelOriginType,
    ModelSourceType,
)
from holm_download_worker import (
    DownloadFilterRules,
    DownloadProcessCompleted,
    DownloadSubprocessError,
    DownloadSubprocessRequest,
    ProductionDownloadProcess,
    SubprocessModelDownloader,
)
from holm_persistence.model_sources import MODEL_SOURCES
from holm_security import MasterKey, MasterKeyring, MasterKeyState
from migration_test_support import upgrade_head
from sqlalchemy.engine import URL

PROCESS_TIMEOUT_SECONDS: Final[float] = 20.0
REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
DOWNLOAD_SUBPROCESS_SOURCE: Final[Path] = (
    REPOSITORY_ROOT / "apps/workers/download/src/holm_download_worker/download_subprocess.py"
)
KEY_ID: Final[str] = "m2-06b-test"
KEY_MATERIAL: Final[bytes] = b"M" * 32
KEYRING: Final[MasterKeyring] = MasterKeyring(
    (MasterKey(KEY_ID, MasterKeyState.WRITABLE, KEY_MATERIAL),)
)


@pytest.fixture(autouse=True)
def _migrated_empty_database(ephemeral_postgres: EphemeralPostgres) -> None:
    upgrade_head(ephemeral_postgres.engine)


def _database_url(fixture: EphemeralPostgres) -> str:
    return URL.create(
        "postgresql+psycopg",
        username="ubuntu",
        database="postgres",
        query={"host": str(fixture.socket_directory), "port": str(fixture.port)},
    ).render_as_string(hide_password=False)


def _write_secret(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o400)


def _secret_files(tmp_path: Path, fixture: EphemeralPostgres) -> tuple[Path, Path]:
    database_url_file = tmp_path / "database-url"
    keyring_file = tmp_path / "master-keyring.json"
    _write_secret(database_url_file, _database_url(fixture))
    _write_secret(
        keyring_file,
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
    )
    return database_url_file, keyring_file


def _seed_credential(
    fixture: EphemeralPostgres,
    source: ModelOriginType,
    sentinel: str,
) -> UUID:
    with fixture.engine.begin() as connection:
        now = connection.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        source_type = ModelSourceType(source.value)
        source_id = connection.execute(
            sa.select(MODEL_SOURCES.c.id).where(MODEL_SOURCES.c.type == source_type.value)
        ).scalar_one_or_none()
        if source_id is None:
            source_id = uuid4()
        envelope = KEYRING.encrypt(
            sentinel.encode("utf-8"),
            source_id=source_id,
            credential_type=CredentialType.ACCESS_TOKEN.value,
        )
        values = {
            "credential_key_id": envelope.key_id,
            "credential_nonce": envelope.nonce,
            "credential_ciphertext": envelope.ciphertext,
            "credential_tag": envelope.tag,
            "updated_at": now,
        }
        if (
            connection.execute(
                sa.select(MODEL_SOURCES.c.id).where(MODEL_SOURCES.c.id == source_id)
            ).scalar_one_or_none()
            is None
        ):
            connection.execute(
                sa.insert(MODEL_SOURCES).values(
                    id=source_id,
                    type=source_type.value,
                    verification_status=None,
                    verified_at=None,
                    verification_reason_code=None,
                    created_at=now,
                    **values,
                )
            )
        else:
            connection.execute(
                sa.update(MODEL_SOURCES).where(MODEL_SOURCES.c.id == source_id).values(**values)
            )
    return source_id


def _request(
    *,
    source: ModelOriginType,
    repository_id: str,
    revision: str,
    staging: Path,
    endpoint: str,
) -> DownloadSubprocessRequest:
    staging.mkdir(parents=True, exist_ok=True)
    return DownloadSubprocessRequest(
        source=source,
        repository_id=repository_id,
        resolved_revision=revision,
        staging_path=staging,
        endpoint=endpoint,
        execution_key=JobExecutionKey(uuid4(), 1),
    )


async def _wait_until(predicate: Callable[[], bool]) -> None:
    deadline = asyncio.get_running_loop().time() + PROCESS_TIMEOUT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("download subprocess condition was not reached")


def _read_process_argv(pid: int) -> str:
    return (Path("/proc") / str(pid) / "cmdline").read_bytes().decode("utf-8", errors="replace")


def _small_file_surfaces(*roots: Path) -> tuple[str, ...]:
    surfaces: list[str] = []
    for root in roots:
        candidates: tuple[Path, ...]
        if root.is_file():
            candidates = (root,)
        elif root.is_dir():
            candidates = tuple(path for path in root.rglob("*") if path.is_file())
        else:
            candidates = ()
        for path in candidates:
            surfaces.append(str(path))
            if path.stat().st_size <= 1 << 20:
                surfaces.append(path.read_text(encoding="utf-8", errors="replace"))
    return tuple(surfaces)


async def _execute_and_sample(
    downloader: SubprocessModelDownloader,
    request: DownloadSubprocessRequest,
) -> tuple[object, tuple[str, ...], int]:
    transferred = 0
    environment_snapshots: list[tuple[tuple[str, str], ...]] = []

    async def progress(increment: int) -> None:
        nonlocal transferred
        transferred += increment

    running = asyncio.create_task(
        downloader.download(
            request,
            progress,
            observe_child_environment=environment_snapshots.append,
        )
    )
    await _wait_until(lambda: downloader.last_child_pid is not None or running.done())
    pid = downloader.last_child_pid
    assert pid is not None
    process_surfaces: list[str] = []
    while not running.done():
        with suppress(FileNotFoundError, ProcessLookupError):
            process_surfaces.append(_read_process_argv(pid))
        await asyncio.sleep(0.001)
    try:
        outcome: object = await running
    except DownloadSubprocessError as error:
        outcome = error
    assert len(environment_snapshots) == 1
    assert ("HOME", "/nonexistent") in environment_snapshots[0]
    process_surfaces.extend(f"{name}={value}" for name, value in environment_snapshots[0])
    return outcome, tuple(process_surfaces), transferred


def _sentinel(source: ModelOriginType) -> str:
    return f"PWS_{source.value.upper()}_TCDL27_{os.urandom(8).hex().upper()}"


@pytest.mark.parametrize("source", [ModelOriginType.HUGGING_FACE, ModelOriginType.MODELSCOPE])
def test_TC_DL_20_TC_DL_27_real_child_success_and_failure_keep_token_in_memory(
    source: ModelOriginType,
    caplog: pytest.LogCaptureFixture,
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    sentinel = _sentinel(source)
    _seed_credential(ephemeral_postgres, source, sentinel)
    database_url_file, keyring_file = _secret_files(tmp_path, ephemeral_postgres)
    caplog.set_level(logging.DEBUG)
    hub_context: AbstractContextManager[HuggingFaceFakeHub | ModelScopeFakeHub]

    if source is ModelOriginType.HUGGING_FACE:
        hub_context = fake_hugging_face_hub(
            valid_token=sentinel,
            serve_synthetic_file_bodies=True,
            response_chunk_bytes=1024,
            response_chunk_delay_seconds=0.002,
        )
        success_identity = (TINY_REPOSITORY_ID, TINY_REPOSITORY_COMMIT)
        failure_identity = (HF_MISSING_REPOSITORY_ID, "main")
        rules = DownloadFilterRules()
        expected_presence: object = HuggingFaceCredentialPresence.VALID
    else:
        hub_context = fake_modelscope_hub(
            valid_token=sentinel,
            serve_synthetic_file_bodies=True,
            response_chunk_bytes=1,
            response_chunk_delay_seconds=0.002,
        )
        success_identity = (PUBLIC_REPOSITORY_ID, PUBLIC_DEFAULT_BRANCH)
        failure_identity = (MS_MISSING_REPOSITORY_ID, PUBLIC_DEFAULT_BRANCH)
        # The production value remains the asserted empty Runtime-derived set.
        # This internal-only test rule selects the one byte-faithful recorded
        # body so TC-DL-27 does not turn a 15 GB model into a CI artifact.
        rules = DownloadFilterRules(modelscope_allow_patterns=(CONFIG_FILENAME,))
        expected_presence = ModelScopeCredentialPresence.VALID

    with hub_context as hub:
        process = ProductionDownloadProcess(
            database_url_file=database_url_file,
            master_keyring_file=keyring_file,
            max_workers=1,
            filter_rules=rules,
        )
        success_downloader = SubprocessModelDownloader(process)
        success_request = _request(
            source=source,
            repository_id=success_identity[0],
            revision=success_identity[1],
            staging=tmp_path / "successful-download",
            endpoint=hub.endpoint,
        )
        if source is ModelOriginType.MODELSCOPE:
            (success_request.staging_path / f"{CONFIG_FILENAME}.incomplete").write_bytes(
                b"already-downloaded"
            )
        success, success_process_surfaces, transferred = asyncio.run(
            _execute_and_sample(success_downloader, success_request)
        )
        assert not isinstance(success, DownloadSubprocessError)
        if source is ModelOriginType.MODELSCOPE:
            assert transferred == 663
        else:
            assert transferred > 0

        failure_downloader = SubprocessModelDownloader(process)
        failure_request = _request(
            source=source,
            repository_id=failure_identity[0],
            revision=failure_identity[1],
            staging=tmp_path / "failed-download",
            endpoint=hub.endpoint,
        )
        failure, failure_process_surfaces, _ = asyncio.run(
            _execute_and_sample(failure_downloader, failure_request)
        )
        assert isinstance(failure, DownloadSubprocessError)
        assert failure.code == "download_process_failed"
        assert hub.requests
        source_requests = tuple(
            request for request in hub.requests if request.path.startswith("/api/")
        )
        assert source_requests
        assert expected_presence in {request.credential for request in source_requests}

    rendered_logs = "\n".join(
        f"{record.getMessage()} {record.__dict__!r}" for record in caplog.records
    )
    surfaces = (
        rendered_logs,
        *success_process_surfaces,
        *failure_process_surfaces,
        *_small_file_surfaces(
            tmp_path / "successful-download",
            tmp_path / "failed-download",
            Path("/nonexistent"),
        ),
        database_bytes(ephemeral_postgres.engine).decode("latin-1"),
        repr(success_request),
        repr(failure_request),
        str(failure),
        repr(failure),
    )
    assert all(sentinel not in surface for surface in surfaces)


@dataclass(frozen=True, slots=True)
class _CompatibleMutatedProductionDownload:
    process: Callable[[DownloadSubprocessRequest, Callable[[int], None]], object]

    def __call__(
        self,
        request: DownloadSubprocessRequest,
        progress: Callable[[int], None],
    ) -> DownloadProcessCompleted:
        outcome = self.process(request, progress)
        if type(outcome).__name__ != DownloadProcessCompleted.__name__:
            raise RuntimeError("mutated production download did not complete")
        return DownloadProcessCompleted()


@dataclass(frozen=True, slots=True)
class _CacheLeakingDownload:
    sentinel: str
    ready: Path
    cache_file: Path

    def __call__(
        self,
        request: DownloadSubprocessRequest,
        progress: Callable[[int], None],
    ) -> DownloadProcessCompleted:
        del request, progress
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.cache_file.write_text(self.sentinel, encoding="utf-8")
        self.ready.write_text("ready", encoding="ascii")
        while True:
            time.sleep(0.05)


@pytest.mark.parametrize("source", [ModelOriginType.HUGGING_FACE, ModelOriginType.MODELSCOPE])
def test_TC_DL_27_positive_control_production_child_environment_leak_is_caught(
    source: ModelOriginType,
    monkeypatch: pytest.MonkeyPatch,
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    sentinel = _sentinel(source)
    _seed_credential(ephemeral_postgres, source, sentinel)
    database_url_file, keyring_file = _secret_files(tmp_path, ephemeral_postgres)
    monkeypatch.syspath_prepend(str(tmp_path))
    module_name = f"_tc_dl_27_{source.value}_environment_leak"
    if source is ModelOriginType.HUGGING_FACE:
        anchor = (
            "        snapshot_download(\n"
            "            request.repository_id,\n"
            "            revision=request.resolved_revision,\n"
            "            local_dir=request.staging_path,\n"
        )
        environment_name = "HF_TOKEN"
        hub_context: AbstractContextManager[HuggingFaceFakeHub | ModelScopeFakeHub] = (
            fake_hugging_face_hub(
                valid_token=sentinel,
                serve_synthetic_file_bodies=True,
                response_chunk_bytes=1024,
                response_chunk_delay_seconds=0.002,
            )
        )
        repository_id, revision = TINY_REPOSITORY_ID, TINY_REPOSITORY_COMMIT
        rules = DownloadFilterRules()
    else:
        anchor = (
            "        snapshot_download(\n"
            "            request.repository_id,\n"
            "            revision=request.resolved_revision,\n"
            "            local_dir=str(request.staging_path),\n"
        )
        environment_name = "MODELSCOPE_API_TOKEN"
        hub_context = fake_modelscope_hub(
            serve_synthetic_file_bodies=True,
            response_chunk_bytes=1,
            response_chunk_delay_seconds=0.002,
        )
        repository_id, revision = PUBLIC_REPOSITORY_ID, PUBLIC_DEFAULT_BRANCH
        rules = DownloadFilterRules(modelscope_allow_patterns=(CONFIG_FILENAME,))
    mutated = load_mutated_module(
        DOWNLOAD_SUBPROCESS_SOURCE,
        [
            (
                anchor,
                f'        os.environ["{environment_name}"] = str(token)\n' + anchor,
            )
        ],
        module_name=module_name,
        destination=tmp_path / f"{module_name}.py",
    )

    with hub_context as hub:
        downloader = SubprocessModelDownloader(
            _CompatibleMutatedProductionDownload(
                mutated.ProductionDownloadProcess(
                    database_url_file=database_url_file,
                    master_keyring_file=keyring_file,
                    max_workers=1,
                    filter_rules=rules,
                )
            )
        )
        request = _request(
            source=source,
            repository_id=repository_id,
            revision=revision,
            staging=tmp_path / "staging",
            endpoint=hub.endpoint,
        )
        outcome, leaked_surfaces, _ = asyncio.run(_execute_and_sample(downloader, request))

    assert not isinstance(outcome, DownloadSubprocessError)
    with pytest.raises(AssertionError):
        assert all(sentinel not in surface for surface in leaked_surfaces)


@pytest.mark.parametrize("source", [ModelOriginType.HUGGING_FACE, ModelOriginType.MODELSCOPE])
def test_TC_DL_27_positive_control_sdk_cache_config_leaks_are_caught(
    source: ModelOriginType,
    tmp_path: Path,
) -> None:
    sentinel = _sentinel(source)
    ready = tmp_path / "ready"
    cache_file = tmp_path / source.value / "sdk-cache" / "credentials.json"
    downloader = SubprocessModelDownloader(
        _CacheLeakingDownload(sentinel, ready, cache_file),
        cooperative_stop_seconds=0.05,
    )
    request = _request(
        source=source,
        repository_id="fixture/repository",
        revision="fixture-revision",
        staging=tmp_path / "staging",
        endpoint="http://fixture.invalid",
    )

    async def scenario() -> tuple[str, ...]:
        async def no_progress(increment: int) -> None:
            raise AssertionError(f"leaking child unexpectedly reported {increment} bytes")

        running = asyncio.create_task(downloader.download(request, no_progress))
        await _wait_until(ready.exists)
        pid = downloader.last_child_pid
        assert pid is not None
        surfaces = (_read_process_argv(pid), *_small_file_surfaces(cache_file))
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        return surfaces

    leaked_surfaces = asyncio.run(scenario())
    with pytest.raises(AssertionError):
        assert all(sentinel not in surface for surface in leaked_surfaces)


def test_TC_DL_20_modelscope_fixture_bounds_synthetic_bodies_and_lfs_redirect() -> None:
    recorded_location = urlsplit(assert_recorded_lfs_redirect_agrees_with_listing())
    file_url_suffix = f"/api/v1/models/{PUBLIC_REPOSITORY_ID}/repo"
    with fake_modelscope_hub() as hub:
        missing_body = httpx.get(
            hub.endpoint + file_url_suffix,
            params={"Revision": PUBLIC_DEFAULT_BRANCH, "FilePath": ".gitattributes"},
        )
        assert missing_body.status_code == 599

    with fake_modelscope_hub(serve_synthetic_file_bodies=True) as hub:
        unknown = httpx.get(
            hub.endpoint + file_url_suffix,
            params={"Revision": PUBLIC_DEFAULT_BRANCH, "FilePath": "not-recorded.bin"},
        )
        assert unknown.status_code == 599

        generated = httpx.get(
            hub.endpoint + file_url_suffix,
            params={"Revision": PUBLIC_DEFAULT_BRANCH, "FilePath": ".gitattributes"},
        )
        assert generated.status_code == 200
        assert len(generated.content) == 1519
        assert generated.headers["etag"] == '"synthetic-.gitattributes"'

        redirect = httpx.get(
            hub.endpoint + file_url_suffix,
            params={"Revision": PUBLIC_DEFAULT_BRANCH, "FilePath": PUBLIC_LARGE_FILE},
            follow_redirects=False,
        )
        assert redirect.status_code == 302
        derived_location = urlsplit(redirect.headers["location"])
        assert derived_location.scheme == "http"
        assert derived_location.netloc == urlsplit(hub.endpoint).netloc
        assert (derived_location.path, derived_location.query) == (
            recorded_location.path,
            recorded_location.query,
        )

        partial = httpx.get(redirect.headers["location"], headers={"Range": "bytes=0-127"})
        assert partial.status_code == 206
        assert len(partial.content) == 128
        assert partial.headers["content-range"].startswith("bytes 0-127/")

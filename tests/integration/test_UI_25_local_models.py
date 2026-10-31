"""UI-25: FX-UI-25 derives safe model/filesystem fixtures from TC-DL-06/10/14."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from fixtures.asgi_client import SessionCookies
from holm_common.config import StorageConfig
from holm_control_api.artifact_deletion_service import ArtifactDeletionService
from holm_control_api.download_command_service import DownloadCommand, DownloadCommandService
from holm_domain import (
    DOWNLOAD_CLEANUP_JOB_TYPE,
    ArtifactStorageType,
    DownloadFailureReason,
    DownloadTaskStatus,
)
from holm_domain.artifact_manifest import deserialize_artifact_manifest
from holm_download_worker import DownloadStagingLifecycle
from holm_download_worker.artifact_deletion import ArtifactDeletionHandler
from holm_download_worker.artifact_publishing import ArtifactRevalidator
from holm_persistence import (
    JobHandlerContext,
    JobRepository,
    create_database_engine,
    create_session_factory,
)
from holm_persistence.model_domain import DOWNLOAD_TASKS, MODEL_ARTIFACTS
from migration_test_support import upgrade_head
from test_M2_09B_TC_DL_06_download_commands import _seed_task
from test_M2_10B_TC_DL_09_10_11_25_artifact_publishing import (
    _components,
    _enqueue,
    _resolved,
    _seed_downloading,
    _valid_payloads,
    _WritingDownloader,
)
from test_M2_12A_download_artifact_read_api import (
    ADMIN_USERNAME,
    MEMBER_USERNAME,
    PASSWORD,
    _cookie,
    _create_accounts,
    _environment,
)
from test_M7_10A_TC_DL_14_artifact_deletion import _seed as _seed_artifact


@pytest.fixture(autouse=True)
def _database(ephemeral_postgres: EphemeralPostgres) -> None:
    upgrade_head(ephemeral_postgres.engine)
    with ephemeral_postgres.engine.begin() as connection:
        connection.exec_driver_sql(
            "TRUNCATE core.jobs, core.audit_events, core.models, core.model_sources, "
            "core.users CASCADE"
        )


def test_UI_25_download_api_filters_counts_delete_and_permissions(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    tasks = {status: _seed_task(ephemeral_postgres, status) for status in DownloadTaskStatus}
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(
            sa.update(DOWNLOAD_TASKS)
            .where(DOWNLOAD_TASKS.c.id == tasks[DownloadTaskStatus.FAILED])
            .values(error_code=DownloadFailureReason.ARTIFACT_PUBLISH_FAILED.value)
        )

    async def run() -> None:
        async with _environment(ephemeral_postgres, model_root=tmp_path) as env:
            await _create_accounts(env)

            async def login(username: str) -> SessionCookies:
                response = await env.client.request(
                    "POST",
                    "/api/v1/auth/login",
                    json_body={"username": username, "password": PASSWORD},
                )
                return SessionCookies(
                    {"__Host-pws_session": _cookie(response, "__Host-pws_session")},
                    csrf_token=str(response.json()["csrf_token"]),
                )

            admin, member = await login(ADMIN_USERNAME), await login(MEMBER_USERNAME)
            base = "/api/v1/downloads"
            counts = await env.client.request("GET", f"{base}/status-counts", cookies=admin)
            assert counts.status == 200
            assert counts.json() == {"counts": {status.value: 1 for status in DownloadTaskStatus}}
            first = await env.client.request(
                "GET", f"{base}?status=Failed&status=Cancelled&page_size=1", cookies=admin
            )
            assert first.status == 200 and len(first.json()["items"]) == 1
            cursor = first.json()["next_cursor"]
            second = await env.client.request(
                "GET",
                f"{base}?status=Failed&status=Cancelled&page_size=1&cursor={cursor}",
                cookies=admin,
            )
            assert second.status == 200, second.text
            assert {first.json()["items"][0]["status"], second.json()["items"][0]["status"]} == {
                "Failed",
                "Cancelled",
            }
            assert second.json()["next_cursor"] is None
            invalid = await env.client.request("GET", f"{base}?status=invalid", cookies=admin)
            assert invalid.status == 400 and invalid.json()["error"]["code"] == "invalid_request"
            for status in (
                DownloadTaskStatus.COMPLETED,
                DownloadTaskStatus.FAILED,
                DownloadTaskStatus.CANCELLED,
            ):
                response = await env.client.request(
                    "DELETE", f"{base}/{tasks[status]}", cookies=admin
                )
                assert response.status == 204, response.text
            denied = await env.client.request(
                "DELETE", f"{base}/{tasks[DownloadTaskStatus.DOWNLOADING]}", cookies=admin
            )
            assert (
                denied.status == 409
                and denied.json()["error"]["code"] == "download_command_not_allowed"
            )
            missing = await env.client.request("DELETE", f"{base}/{uuid4()}", cookies=admin)
            assert missing.status == 404
            for method, path in (
                ("GET", f"{base}/status-counts"),
                ("DELETE", f"{base}/{tasks[DownloadTaskStatus.QUEUED]}"),
                ("GET", f"/api/v1/artifacts/{uuid4()}"),
            ):
                assert (await env.client.request(method, path, cookies=member)).status == 403
                assert (await env.client.request(method, path)).status == 401
            missing_detail = await env.client.request(
                "GET", f"/api/v1/artifacts/{uuid4()}", cookies=admin
            )
            assert missing_detail.status == 404

    asyncio.run(run())
    with ephemeral_postgres.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text("SELECT count(*) FROM core.download_subscriptions WHERE task_id = :id"),
                {"id": tasks[DownloadTaskStatus.COMPLETED]},
            )
            == 0
        )
        assert (
            connection.scalar(
                sa.text(
                    "SELECT count(*) FROM core.audit_events WHERE details->>'operation'='delete'"
                )
            )
            == 3
        )


@pytest.mark.parametrize(
    "cancel_first,cleanup", [(False, False), (False, True), (True, False), (True, True)]
)
def test_UI_25_TC_DL_14_deleted_task_cleanup_interleaving(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    cancel_first: bool,
    cleanup: bool,
) -> None:
    task = _seed_task(
        ephemeral_postgres,
        DownloadTaskStatus.DOWNLOADING if cancel_first else DownloadTaskStatus.FAILED,
    )
    root = tmp_path / "models"
    path = root / ".staging" / str(task)
    path.mkdir(parents=True)
    (path / "partial").write_bytes(b"FX-UI-25-partial")
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(
            sa.update(DOWNLOAD_TASKS)
            .where(DOWNLOAD_TASKS.c.id == task)
            .values(staging_path=str(path))
        )

    async def run() -> None:
        engine = create_database_engine(ephemeral_postgres.engine.url)
        factory = create_session_factory(engine)
        service = DownloadCommandService(session_factory=factory)
        try:
            if cancel_first:
                await service.execute(
                    task_id=task,
                    command=DownloadCommand.CANCEL,
                    actor_id=uuid4(),
                    request_id=uuid4(),
                )
            # Deterministic interleaving: cancellation commits its queued cleanup,
            # deletion commits, only then does the Worker claim the cleanup.
            await service.delete(
                task_id=task, actor_id=uuid4(), request_id=uuid4(), cleanup_staging=cleanup
            )
            async with factory() as session, session.begin():
                jobs = await JobRepository(session).claim(owner="FX-UI-25")
            assert bool(jobs) is (cancel_first or cleanup)
            lifecycle = DownloadStagingLifecycle(
                session_factory=factory, storage=StorageConfig(model_root=root)
            )
            for job in jobs:
                assert job.job_type == DOWNLOAD_CLEANUP_JOB_TYPE
                await lifecycle.cleanup(
                    JobHandlerContext(job, asyncio.Event()),
                    expected_status=DownloadTaskStatus.CANCELLED,
                )
                async with factory() as session, session.begin():
                    assert await JobRepository(session).complete(job.id, owner="FX-UI-25")
            assert path.exists() is not (cancel_first or cleanup)
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("status", ["Corrupted", "Missing"])
def test_UI_25_TC_DL_14_unhealthy_managed_deletion(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    status: str,
) -> None:
    artifact = uuid4()
    root, tokenizers = tmp_path / "models", tmp_path / "tokenizers"
    source, bundle = root / "managed" / str(artifact), tokenizers / str(artifact)
    source.parent.mkdir(parents=True)
    if status != "Missing":
        source.mkdir()
        (source / "weights").write_bytes(b"FX-UI-25-corrupted")
    bundle.mkdir(parents=True)
    (bundle / "tokenizer.json").write_bytes(b"FX-UI-25-tokenizer")
    _seed_artifact(ephemeral_postgres, artifact, source, ArtifactStorageType.MANAGED, bundle)
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(
            sa.update(MODEL_ARTIFACTS).where(MODEL_ARTIFACTS.c.id == artifact).values(status=status)
        )

    async def run() -> None:
        engine = create_database_engine(ephemeral_postgres.engine.url)
        factory = create_session_factory(engine)
        try:
            assert await ArtifactDeletionService(factory).delete(
                artifact_id=artifact, actor_id=uuid4(), request_id=uuid4()
            )
            async with factory() as session, session.begin():
                job = (await JobRepository(session).claim(owner="FX-UI-25"))[0]
            await ArtifactDeletionHandler(
                session_factory=factory,
                storage=StorageConfig(model_root=root, tokenizer_root=tokenizers),
            ).delete(JobHandlerContext(job, asyncio.Event()))
            assert not source.exists() and not bundle.exists()
            assert (tokenizers / ".trash" / str(artifact) / "tokenizer.json").is_file()
            async with factory() as session:
                assert (
                    await session.scalar(
                        sa.select(MODEL_ARTIFACTS.c.id).where(MODEL_ARTIFACTS.c.id == artifact)
                    )
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_UI_25_publication_detail_backfill_and_delete_preserves_artifact(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    task = _seed_downloading(ephemeral_postgres.engine)
    payloads = _valid_payloads()
    root = tmp_path / "models"
    engine, worker, _ = _components(
        ephemeral_postgres, root, _resolved(payloads), _WritingDownloader(payloads)
    )

    async def run() -> None:
        factory = create_session_factory(engine)
        try:
            await _enqueue(factory, task)
            result = await worker.run_once()
            assert result is not None and result.completed
            async with factory() as session:
                artifact = (await session.execute(sa.select(MODEL_ARTIFACTS))).mappings().one()
            files = [
                asdict(entry)
                for entry in deserialize_artifact_manifest(
                    (Path(artifact["path"]) / "manifest.json").read_bytes()
                ).files
            ]
            assert artifact["manifest_files"] == files
            async with _environment(ephemeral_postgres, model_root=root) as env:
                await _create_accounts(env)
                response = await env.client.request(
                    "POST",
                    "/api/v1/auth/login",
                    json_body={"username": ADMIN_USERNAME, "password": PASSWORD},
                )
                cookies = SessionCookies(
                    {"__Host-pws_session": _cookie(response, "__Host-pws_session")},
                    csrf_token=str(response.json()["csrf_token"]),
                )
                url = f"/api/v1/artifacts/{artifact['id']}"
                detail = await env.client.request("GET", url, cookies=cookies)
                assert detail.status == 200, detail.text
                assert detail.json()["files"] == files
                assert set(detail.json()["model"]) == {
                    "architecture",
                    "modalities",
                    "precision",
                    "quantization",
                    "context_length",
                    "license",
                }
                assert detail.json()["download_tasks"][0]["id"] == str(task)
                assert detail.json()["instances"] == []
                async with factory() as session, session.begin():
                    await session.execute(sa.update(MODEL_ARTIFACTS).values(manifest_files=None))
                assert (await env.client.request("GET", url, cookies=cookies)).json()[
                    "files"
                ] is None
                await ArtifactRevalidator(session_factory=factory).revalidate(
                    artifact["id"], full=False
                )
                assert (await env.client.request("GET", url, cookies=cookies)).json()[
                    "files"
                ] is None
                await ArtifactRevalidator(session_factory=factory).revalidate(
                    artifact["id"], full=True
                )
                assert (await env.client.request("GET", url, cookies=cookies)).json()[
                    "files"
                ] == files
                deleted = await env.client.request(
                    "DELETE", f"/api/v1/downloads/{task}", cookies=cookies
                )
                assert deleted.status == 204
                retained = (await env.client.request("GET", url, cookies=cookies)).json()
                assert retained["status"] == "Ready" and retained["files"] == files
                assert retained["download_tasks"] == []
                assert Path(artifact["path"]).is_dir()
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("cleanup", [False, True])
def test_UI_25_cancel_optionally_keeps_staging(
    ephemeral_postgres: EphemeralPostgres,
    cleanup: bool,
) -> None:
    task = _seed_task(ephemeral_postgres, DownloadTaskStatus.PAUSED, staging=True)

    async def run() -> None:
        engine = create_database_engine(ephemeral_postgres.engine.url)
        try:
            result = await DownloadCommandService(
                session_factory=create_session_factory(engine)
            ).cancel(task_id=task, actor_id=uuid4(), request_id=uuid4(), cleanup_staging=cleanup)
            assert result.task.status is DownloadTaskStatus.CANCELLED
            assert result.staging_cleanup_requested is cleanup
        finally:
            await engine.dispose()

    asyncio.run(run())
    with ephemeral_postgres.engine.connect() as connection:
        row = connection.execute(
            sa.select(DOWNLOAD_TASKS.c.status, DOWNLOAD_TASKS.c.staging_path).where(
                DOWNLOAD_TASKS.c.id == task
            )
        ).one()
        jobs = connection.scalar(
            sa.text("SELECT count(*) FROM core.jobs WHERE job_type = :t"),
            {"t": DOWNLOAD_CLEANUP_JOB_TYPE},
        )
    assert row.status == "Cancelled"
    assert jobs == int(cleanup)
    if not cleanup:
        assert row.staging_path == "opaque-staging-token"

"""M7-10A / TC-DL-14: real PostgreSQL deletion, retained history and filesystem guard."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from fixtures.asgi_client import SessionCookies
from holm_common.config import StorageConfig
from holm_control_api.artifact_deletion_service import (
    ArtifactDeleteRejected,
    ArtifactDeletionService,
)
from holm_control_api.http_constants import SESSION_COOKIE_NAME
from holm_domain import ArtifactStorageType
from holm_download_worker.artifact_deletion import (
    ArtifactDeletionFailed,
    ArtifactDeletionHandler,
)
from holm_persistence import (
    AsyncSessionFactory,
    JobExecutor,
    JobHandlerContext,
    JobHandlerRegistry,
    JobRepository,
    create_database_engine,
    create_session_factory,
)
from holm_persistence.artifact_deletion_repository import (
    ARTIFACT_DELETE_JOB_TYPE,
    ArtifactDeletionRepository,
)
from migration_test_support import upgrade_head
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine
from test_M2_12A_download_artifact_read_api import (
    ADMIN_USERNAME,
    MEMBER_USERNAME,
    PASSWORD,
    _cookie,
    _create_accounts,
    _environment,
)


@pytest.fixture(autouse=True)
def _migrated(ephemeral_postgres: EphemeralPostgres) -> None:
    upgrade_head(ephemeral_postgres.engine)
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(
            sa.text(
                "TRUNCATE core.model_sources, core.models, core.model_artifacts, "
                "core.download_tasks, core.model_instances, core.jobs, core.audit_events CASCADE"
            )
        )


def _seed(
    database: EphemeralPostgres,
    artifact_id: UUID,
    path: Path,
    storage_type: ArtifactStorageType,
    bundle: Path | None = None,
) -> None:
    model_id = uuid4()
    with database.engine.begin() as connection:
        source_id = uuid4()
        connection.execute(
            sa.text(
                "INSERT INTO core.model_sources (id, type, created_at, updated_at) "
                "VALUES (:id, 'Hugging Face', now(), now())"
            ),
            {"id": source_id},
        )
        connection.execute(
            sa.text(
                "INSERT INTO core.models (id, source_id, source_type, source_key, "
                "display_name, repository_id, metadata_status, created_at, updated_at) "
                "VALUES (:id, :source_id, 'Hugging Face', :source, 'sentinel-model', "
                ":repository, 'CONFIRMED', now(), now())"
            ),
            {
                "id": model_id,
                "source_id": source_id,
                "source": str(model_id),
                "repository": str(model_id),
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO core.model_artifacts (id, model_id, source_type, repository_id, "
                "resolved_revision, storage_type, status, path, manifest_hash, "
                "tokenizer_bundle_hash, tokenizer_bundle_path, size_bytes, created_at, updated_at) "
                "VALUES (:id, :model_id, 'Hugging Face', :repository, :revision, "
                ":storage_type, 'Ready', :path, :hash, :bundle_hash, :bundle_path, "
                "1, now(), now())"
            ),
            {
                "id": artifact_id,
                "model_id": model_id,
                "repository": str(model_id),
                "revision": str(artifact_id),
                "storage_type": storage_type.value,
                "path": str(path),
                "hash": "a" * 64,
                "bundle_hash": "b" * 64 if bundle else None,
                "bundle_path": str(bundle) if bundle else None,
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO core.download_tasks (id, model_id, artifact_id, source_type, "
                "repository_id, requested_revision_norm, status, attempt, downloaded_bytes, "
                "created_at, updated_at) VALUES (:id, :model_id, :artifact_id, "
                "'Hugging Face', :repository, 'main', 'Completed', 1, 1, now(), now())"
            ),
            {
                "id": uuid4(),
                "model_id": model_id,
                "artifact_id": artifact_id,
                "repository": str(model_id),
            },
        )


async def _factory(database: EphemeralPostgres) -> tuple[AsyncEngine, AsyncSessionFactory]:
    engine = create_database_engine(
        URL.create(
            "postgresql+psycopg",
            username="ubuntu",
            host=str(database.socket_directory),
            port=database.port,
            database="postgres",
        )
    )
    return engine, create_session_factory(engine)


def _value(database: EphemeralPostgres, sql: str, artifact_id: UUID) -> int:
    with database.engine.connect() as connection:
        return int(connection.scalar(sa.text(sql), {"id": artifact_id}))


async def _external_unregister(ephemeral_postgres: EphemeralPostgres, tmp_path: Path) -> None:
    artifact_id = uuid4()
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel"
    sentinel.write_bytes(b"external-file-must-not-change")
    before = (sentinel.read_bytes(), sentinel.stat().st_mtime_ns, external.stat().st_mtime_ns)
    _seed(ephemeral_postgres, artifact_id, external, ArtifactStorageType.EXTERNAL)
    engine, factory = await _factory(ephemeral_postgres)
    try:
        queued = await ArtifactDeletionService(factory).delete(
            artifact_id=artifact_id,
            actor_id=uuid4(),
            request_id=uuid4(),
        )
        assert not queued
        assert (
            _value(
                ephemeral_postgres,
                "SELECT count(*) FROM core.audit_events WHERE object_id=:id",
                artifact_id,
            )
            == 1
        )
        assert (
            sentinel.read_bytes(),
            sentinel.stat().st_mtime_ns,
            external.stat().st_mtime_ns,
        ) == before
        assert (
            _value(
                ephemeral_postgres,
                "SELECT count(*) FROM core.model_artifacts WHERE id=:id",
                artifact_id,
            )
            == 0
        )
        assert (
            _value(
                ephemeral_postgres,
                "SELECT count(*) FROM core.download_tasks WHERE artifact_id IS NULL",
                artifact_id,
            )
            == 1
        )
    finally:
        await engine.dispose()


async def _managed_deletion(ephemeral_postgres: EphemeralPostgres, tmp_path: Path) -> None:
    artifact_id = uuid4()
    root = tmp_path / "models"
    tokenizers = tmp_path / "tokenizers"
    source = root / "managed" / "source" / str(artifact_id)
    source.mkdir(parents=True)
    (source / "weights").write_bytes(b"model")
    bundle = tokenizers / str(artifact_id)
    bundle.mkdir(parents=True)
    (bundle / "tokenizer").write_bytes(b"tokenizer")
    _seed(ephemeral_postgres, artifact_id, source, ArtifactStorageType.MANAGED, bundle)
    engine, factory = await _factory(ephemeral_postgres)
    try:
        assert await ArtifactDeletionService(factory).delete(
            artifact_id=artifact_id, actor_id=uuid4(), request_id=uuid4()
        )
        assert (
            _value(
                ephemeral_postgres,
                "SELECT count(*) FROM core.audit_events WHERE object_id=:id",
                artifact_id,
            )
            == 0
        )
        async with factory() as session:
            job = await JobRepository(session).get(
                (
                    await session.execute(
                        sa.text(
                            "SELECT id FROM core.jobs WHERE target_object_id=:id "
                            "AND job_type='artifact.delete'"
                        ),
                        {"id": artifact_id},
                    )
                ).scalar_one()
            )
        assert job is not None
        handler = ArtifactDeletionHandler(
            session_factory=factory,
            storage=StorageConfig(model_root=root, tokenizer_root=tokenizers),
        )
        await handler.delete(JobHandlerContext(job, asyncio.Event()))
        assert (
            _value(
                ephemeral_postgres,
                "SELECT count(*) FROM core.audit_events WHERE object_id=:id",
                artifact_id,
            )
            == 1
        )
        assert not source.exists() and not bundle.exists()
        assert (root / ".trash" / str(artifact_id) / "weights").read_bytes() == b"model"
        assert (tokenizers / ".trash" / str(artifact_id) / "tokenizer").read_bytes() == b"tokenizer"
        assert (
            _value(
                ephemeral_postgres,
                "SELECT count(*) FROM core.model_artifacts WHERE id=:id",
                artifact_id,
            )
            == 0
        )
        assert (
            _value(
                ephemeral_postgres,
                "SELECT count(*) FROM core.download_tasks WHERE artifact_id IS NULL",
                artifact_id,
            )
            == 1
        )
        async with factory() as session:
            cleanup_job = await JobRepository(session).get(
                (
                    await session.execute(
                        sa.text(
                            "SELECT id FROM core.jobs WHERE target_object_id=:id "
                            "AND job_type='artifact.trash_cleanup'"
                        ),
                        {"id": artifact_id},
                    )
                ).scalar_one()
            )
        assert cleanup_job is not None
        await handler.cleanup(JobHandlerContext(cleanup_job, asyncio.Event()))
        assert not (root / ".trash" / str(artifact_id)).exists()
        assert not (tokenizers / ".trash" / str(artifact_id)).exists()
    finally:
        await engine.dispose()


async def _reference_guard(ephemeral_postgres: EphemeralPostgres, tmp_path: Path) -> None:
    artifact_id = uuid4()
    source = tmp_path / "external"
    source.mkdir()
    marker = source / "sentinel"
    marker.write_bytes(b"unchanged")
    before = (marker.read_bytes(), marker.stat().st_mtime_ns)
    _seed(ephemeral_postgres, artifact_id, source, ArtifactStorageType.EXTERNAL)
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO core.model_instances (id, artifact_id, runtime_type, "
                "expected_gpu_uuids, tensor_parallel_size, context_length, "
                "gpu_memory_utilization, name, internal_model_name, status, config_version, "
                "created_at, updated_at) VALUES (:id, :artifact_id, 'vllm', "
                "'[\"GPU-test\"]'::jsonb, 1, 4096, 0.9, :name, :name, 'Stopped', 1, now(), now())"
            ),
            {"id": uuid4(), "artifact_id": artifact_id, "name": "mi_123456789abc"},
        )
    engine, factory = await _factory(ephemeral_postgres)
    try:
        with pytest.raises(ArtifactDeleteRejected, match="artifact deletion rejected") as error:
            await ArtifactDeletionService(factory).delete(
                artifact_id=artifact_id, actor_id=uuid4(), request_id=uuid4()
            )
        assert error.value.code == "artifact_referenced"
        assert (
            _value(
                ephemeral_postgres,
                "SELECT count(*) FROM core.model_artifacts WHERE id=:id",
                artifact_id,
            )
            == 1
        )
        assert (
            _value(
                ephemeral_postgres,
                "SELECT count(*) FROM core.jobs WHERE target_object_id=:id",
                artifact_id,
            )
            == 0
        )
        assert (marker.read_bytes(), marker.stat().st_mtime_ns) == before
    finally:
        await engine.dispose()


async def _symlink_guard(ephemeral_postgres: EphemeralPostgres, tmp_path: Path) -> None:
    artifact_id = uuid4()
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "sentinel"
    marker.write_bytes(b"safe")
    root = tmp_path / "models"
    (root / "managed").mkdir(parents=True)
    os.symlink(outside, root / "managed" / "escaped")
    _seed(
        ephemeral_postgres, artifact_id, root / "managed" / "escaped", ArtifactStorageType.MANAGED
    )
    engine, factory = await _factory(ephemeral_postgres)
    try:
        assert await ArtifactDeletionService(factory).delete(
            artifact_id=artifact_id, actor_id=uuid4(), request_id=uuid4()
        )
        async with factory() as session:
            job = await JobRepository(session).get(
                (
                    await session.execute(
                        sa.text("SELECT id FROM core.jobs WHERE target_object_id=:id"),
                        {"id": artifact_id},
                    )
                ).scalar_one()
            )
        assert job is not None
        handler = ArtifactDeletionHandler(
            session_factory=factory,
            storage=StorageConfig(model_root=root, tokenizer_root=tmp_path / "tokenizers"),
        )
        with pytest.raises(ArtifactDeletionFailed) as error:
            await handler.delete(JobHandlerContext(job, asyncio.Event()))
        assert error.value.code == "artifact_delete_unsafe_path"
        assert marker.read_bytes() == b"safe"
        assert (
            _value(
                ephemeral_postgres,
                "SELECT count(*) FROM core.model_artifacts WHERE id=:id",
                artifact_id,
            )
            == 1
        )
    finally:
        await engine.dispose()


async def _move_failure(ephemeral_postgres: EphemeralPostgres, tmp_path: Path) -> None:
    artifact_id = uuid4()
    root = tmp_path / "models"
    source = root / "managed" / "source" / str(artifact_id)
    source.mkdir(parents=True)
    marker = source / "sentinel"
    marker.write_bytes(b"never moved")
    _seed(ephemeral_postgres, artifact_id, source, ArtifactStorageType.MANAGED)
    engine, factory = await _factory(ephemeral_postgres)
    try:
        assert await ArtifactDeletionService(factory).delete(
            artifact_id=artifact_id, actor_id=uuid4(), request_id=uuid4()
        )
        async with factory() as session:
            deletion = ArtifactDeletionRepository(session)
            job = await deletion.latest_job(artifact_id)
        assert job is not None
        handler = ArtifactDeletionHandler(
            session_factory=factory,
            storage=StorageConfig(model_root=root, tokenizer_root=tmp_path / "tokenizers"),
        )
        handlers = JobHandlerRegistry()
        handlers.register(ARTIFACT_DELETE_JOB_TYPE, handler.delete)
        executor = JobExecutor(factory, handlers, owner="tc-m7-10a")
        with (
            patch("holm_download_worker.artifact_deletion.os.rename", side_effect=PermissionError),
            pytest.raises(ArtifactDeletionFailed) as error,
        ):
            await executor.run_once()
        assert error.value.code == "artifact_delete_move_failed"
        persisted = await ArtifactDeletionService(factory).status(artifact_id)
        assert persisted is not None
        assert persisted.last_error_code == "artifact_delete_move_failed"
        assert (
            _value(
                ephemeral_postgres,
                "SELECT count(*) FROM core.audit_events WHERE object_id=:id",
                artifact_id,
            )
            == 0
        )
        assert marker.read_bytes() == b"never moved"
        assert not (root / ".trash" / str(artifact_id)).exists()
        assert (
            _value(
                ephemeral_postgres,
                "SELECT count(*) FROM core.model_artifacts WHERE id=:id",
                artifact_id,
            )
            == 1
        )
        assert (
            _value(
                ephemeral_postgres,
                "SELECT count(*) FROM core.download_tasks WHERE artifact_id=:id",
                artifact_id,
            )
            == 1
        )
    finally:
        await engine.dispose()


def test_M7_10A_TC_DL_14_external_unregister_does_not_touch_source(
    ephemeral_postgres: EphemeralPostgres, tmp_path: Path
) -> None:
    asyncio.run(_external_unregister(ephemeral_postgres, tmp_path))


def test_M7_10A_TC_DL_14_managed_moves_then_cleans_bundle(
    ephemeral_postgres: EphemeralPostgres, tmp_path: Path
) -> None:
    asyncio.run(_managed_deletion(ephemeral_postgres, tmp_path))


def test_M7_10A_TC_DL_14_reference_has_no_effect(
    ephemeral_postgres: EphemeralPostgres, tmp_path: Path
) -> None:
    asyncio.run(_reference_guard(ephemeral_postgres, tmp_path))


def test_M7_10A_TC_DL_14_symlink_escape_rejected(
    ephemeral_postgres: EphemeralPostgres, tmp_path: Path
) -> None:
    asyncio.run(_symlink_guard(ephemeral_postgres, tmp_path))


def test_M7_10A_TC_DL_14_failed_move_retains_records_and_files(
    ephemeral_postgres: EphemeralPostgres, tmp_path: Path
) -> None:
    asyncio.run(_move_failure(ephemeral_postgres, tmp_path))


def test_M7_10A_TC_DL_14_api_rejects_non_admin_and_referenced_artifact(
    ephemeral_postgres: EphemeralPostgres, tmp_path: Path
) -> None:
    artifact_id = uuid4()
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel"
    sentinel.write_bytes(b"untouched")
    _seed(ephemeral_postgres, artifact_id, external, ArtifactStorageType.EXTERNAL)
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO core.model_instances (id, artifact_id, runtime_type, "
                "expected_gpu_uuids, tensor_parallel_size, context_length, "
                "gpu_memory_utilization, name, internal_model_name, status, config_version, "
                "created_at, updated_at) VALUES (:id, :artifact_id, 'vllm', "
                "'[\"GPU-test\"]'::jsonb, 1, 4096, 0.9, :name, :name, 'Stopped', 1, now(), now())"
            ),
            {"id": uuid4(), "artifact_id": artifact_id, "name": "mi_123456789abc"},
        )

    async def scenario() -> None:
        model_root = tmp_path / "models"
        model_root.mkdir()
        async with _environment(ephemeral_postgres, model_root=model_root) as environment:
            await _create_accounts(environment)
            unauthenticated = await environment.client.request(
                "DELETE", f"/api/v1/artifacts/{artifact_id}"
            )
            assert unauthenticated.status == 401

            async def cookies_for(username: str) -> SessionCookies:
                login = await environment.client.request(
                    "POST",
                    "/api/v1/auth/login",
                    json_body={"username": username, "password": PASSWORD},
                )
                assert login.status == 200
                return SessionCookies(
                    {SESSION_COOKIE_NAME: _cookie(login, SESSION_COOKIE_NAME)},
                    csrf_token=str(login.json()["csrf_token"]),
                )

            forbidden = await environment.client.request(
                "DELETE",
                f"/api/v1/artifacts/{artifact_id}",
                cookies=await cookies_for(MEMBER_USERNAME),
            )
            assert forbidden.status == 403
            conflict = await environment.client.request(
                "DELETE",
                f"/api/v1/artifacts/{artifact_id}",
                cookies=await cookies_for(ADMIN_USERNAME),
            )
            assert conflict.status == 409
            assert conflict.json()["error"]["code"] == "artifact_referenced"

    asyncio.run(scenario())
    assert sentinel.read_bytes() == b"untouched"
    assert (
        _value(
            ephemeral_postgres,
            "SELECT count(*) FROM core.model_artifacts WHERE id=:id",
            artifact_id,
        )
        == 1
    )
    assert (
        _value(
            ephemeral_postgres,
            "SELECT count(*) FROM core.jobs WHERE target_object_id=:id",
            artifact_id,
        )
        == 0
    )

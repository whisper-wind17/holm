"""M2-10C / TC-DL-09, TC-RT-07: FX-M2-10C tiny real files, PostgreSQL and roles.

The manifest fixture uses domain canonical serialization; files are synthetic,
contain no model content or credentials, and need no Runtime or GPU.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from contextlib import AbstractContextManager
from dataclasses import replace
from pathlib import Path
from typing import Any
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
from holm_control_api.artifact_verification_service import (
    ArtifactVerificationService,
)
from holm_control_api.http_constants import SESSION_COOKIE_NAME
from holm_control_api.model_acquisition_read_service import ModelAcquisitionReadService
from holm_control_api.model_instance_service import ModelInstanceService
from holm_domain import (
    ArtifactManifestEntry,
    ArtifactStorageType,
    build_artifact_manifest,
    serialize_artifact_manifest,
)
from holm_download_worker.artifact_verification import ArtifactVerificationHandler
from holm_download_worker.worker import DownloadResolutionJobHandler, DownloadWorker
from holm_persistence import ModelInstanceRepository, database_utc_now
from holm_persistence.job_executor import JobFailure
from holm_persistence.model_instance_repository import ModelInstanceArtifactNotReady
from test_M2_12A_download_artifact_read_api import (
    ADMIN_USERNAME,
    MEMBER_USERNAME,
    PASSWORD,
    _cookie,
    _create_accounts,
    _environment,
)
from test_M7_10A_TC_DL_14_artifact_deletion import _factory, _seed
from test_M7_10A_TC_DL_14_artifact_deletion import _migrated as _migrated
from test_PROTO_03_instance_controller_postgres import GPU_0, _InventoryAgent, _value

# Import the established migration fixture instead of duplicating bootstrap.
pytestmark = pytest.mark.usefixtures("_migrated")


@pytest.fixture(autouse=True)
def _clean_accounts(ephemeral_postgres: EphemeralPostgres, _migrated: None) -> None:
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(sa.text("TRUNCATE core.users CASCADE"))


def _files(
    database: EphemeralPostgres,
    tmp_path: Path,
    storage_type: ArtifactStorageType = ArtifactStorageType.MANAGED,
) -> tuple[UUID, Path, StorageConfig]:
    artifact_id = uuid4()
    root = tmp_path / str(artifact_id)
    path = (
        root / "managed" / "artifact"
        if storage_type is ArtifactStorageType.MANAGED
        else root / "external"
    )
    path.mkdir(parents=True)
    payloads = {"config.json": b"{}", "model.safetensors": b"12345678", "notes.txt": b"fixture"}
    for name, value in payloads.items():
        (path / name).write_bytes(value)
    raw = serialize_artifact_manifest(
        build_artifact_manifest(
            ArtifactManifestEntry(name, len(value), hashlib.sha256(value).hexdigest())
            for name, value in payloads.items()
        )
    )
    (path / "manifest.json").write_bytes(raw)
    _seed(database, artifact_id, path, storage_type)
    with database.engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE core.model_artifacts SET manifest_hash=:hash WHERE id=:id"),
            {"id": artifact_id, "hash": hashlib.sha256(raw).hexdigest()},
        )
    return (
        artifact_id,
        path,
        StorageConfig(
            model_root=root, tokenizer_root=root / "tokenizers", external_model_roots=(path,)
        ),
    )


async def _role_factory(database: EphemeralPostgres, role: str) -> Any:
    engine, factory = await _factory(database)

    @sa.event.listens_for(engine.sync_engine, "connect")
    def set_role(connection: Any, record: Any) -> None:
        cursor = connection.cursor()
        cursor.execute(f"SET ROLE {role}")
        cursor.close()

    return engine, factory


class _UnusedResolver:
    async def resolve(self, request: Any) -> Any:
        raise AssertionError("manual verification must not resolve or download")


def _worker(factory: Any, storage: StorageConfig) -> DownloadWorker:
    return DownloadWorker(
        session_factory=factory,
        handler=DownloadResolutionJobHandler(session_factory=factory, resolver=_UnusedResolver()),
        artifact_verification=ArtifactVerificationHandler(session_factory=factory, storage=storage),
        owner="m2-10c-worker",
        build_version="m2-10c",
        lease_seconds=5,
        heartbeat_seconds=1,
    )


async def _cookies(environment: Any, username: str) -> SessionCookies:
    login = await environment.client.request(
        "POST", "/api/v1/auth/login", json_body={"username": username, "password": PASSWORD}
    )
    assert login.status == 200
    return SessionCookies(
        {SESSION_COOKIE_NAME: _cookie(login, SESSION_COOKIE_NAME)},
        csrf_token=str(login.json()["csrf_token"]),
    )


@pytest.mark.parametrize("storage_type", list(ArtifactStorageType))
def test_M2_10C_TC_DL_09_api_worker_full_hash_and_recovery(
    ephemeral_postgres: EphemeralPostgres, tmp_path: Path, storage_type: ArtifactStorageType
) -> None:
    artifact_id, path, storage = _files(ephemeral_postgres, tmp_path, storage_type)

    async def scenario() -> None:
        control_engine, control = await _role_factory(ephemeral_postgres, "holm_control")
        worker_engine, download = await _role_factory(ephemeral_postgres, "holm_download_worker")

        class RoleReadService(ModelAcquisitionReadService):
            def __init__(self, **kwargs: Any) -> None:
                super().__init__(**{**kwargs, "session_factory": control})

        try:
            async with _environment(
                ephemeral_postgres, model_root=storage.model_root, read_service_type=RoleReadService
            ) as env:
                await _create_accounts(env)
                url = f"/api/v1/artifacts/{artifact_id}:verify"
                assert (await env.client.request("POST", url)).status == 401
                assert (
                    await env.client.request(
                        "POST", url, cookies=await _cookies(env, MEMBER_USERNAME)
                    )
                ).status == 403
                admin = await _cookies(env, ADMIN_USERNAME)

                async def status() -> dict[str, Any]:
                    response = await env.client.request(
                        "GET", f"/api/v1/artifacts/{artifact_id}/verification", cookies=admin
                    )
                    assert response.status == 200
                    return dict(response.json())

                async def listed() -> dict[str, Any]:
                    response = await env.client.request("GET", "/api/v1/artifacts", cookies=admin)
                    assert response.status == 200
                    return dict(response.json()["items"][0])

                assert (await status())["status"] is None
                before = (await listed())["last_verified_at"]
                assert (await env.client.request("POST", url, cookies=admin)).status == 202
                assert (await env.client.request("POST", url, cookies=admin)).status == 202
                assert (await status())["status"] == "Queued"
                assert (await listed())["last_verified_at"] == before
                with ephemeral_postgres.engine.connect() as connection:
                    assert (
                        connection.scalar(
                            sa.text(
                                "SELECT count(*) FROM core.jobs WHERE job_type='artifact.verify'"
                            )
                        )
                        == 1
                    )
                with pytest.raises(ArtifactDeleteRejected) as rejected:
                    await ArtifactDeletionService(control).delete(
                        artifact_id=artifact_id, actor_id=uuid4(), request_id=uuid4()
                    )
                assert rejected.value.code == "artifact_verify_in_progress"
                deletion = await env.client.request(
                    "DELETE", f"/api/v1/artifacts/{artifact_id}", cookies=admin
                )
                assert deletion.status == 409
                assert "等待校验完成" in deletion.json()["error"]["message"]
                worker = _worker(download, storage)
                assert await worker.run_once() is not None
                assert (await status())["status"] == "Succeeded"
                assert (await listed())["status"] == "Ready"
                assert (await listed())["last_verified_at"] != before
                # Same-size corruption requires a full hash, not only metadata.
                weight = path / "model.safetensors"
                weight.write_bytes(b"87654321")
                before_fs = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in path.iterdir()}
                assert (await env.client.request("POST", url, cookies=admin)).status == 202
                assert await worker.run_once() is not None
                assert (await listed())["status"] == "Corrupted"
                assert before_fs == {
                    p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in path.iterdir()
                }
                weight.write_bytes(b"12345678")
                assert (await env.client.request("POST", url, cookies=admin)).status == 202
                assert await worker.run_once() is not None
                assert (await listed())["status"] == "Ready"
                weight.unlink()
                assert (await env.client.request("POST", url, cookies=admin)).status == 202
                assert await worker.run_once() is not None
                assert (await listed())["status"] == "Missing"
                # UI-30 2a (PDD 7.3): Missing is re-verifiable, so the request is queued.
                assert (await env.client.request("POST", url, cookies=admin)).status == 202
                assert (
                    await env.client.request(
                        "POST", f"/api/v1/artifacts/{uuid4()}:verify", cookies=admin
                    )
                ).status == 404
        finally:
            await control_engine.dispose()
            await worker_engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("unsafe", ["outside", "parent_symlink", "fifo", "nested_symlink"])
def test_M2_10C_TC_DL_09_unsafe_paths_and_nonblocking_files(
    ephemeral_postgres: EphemeralPostgres, tmp_path: Path, unsafe: str
) -> None:
    artifact_id, path, storage = _files(ephemeral_postgres, tmp_path)
    if unsafe == "outside":
        storage = StorageConfig(
            model_root=tmp_path / "other", tokenizer_root=tmp_path / "tokenizers"
        )
    elif unsafe == "parent_symlink":
        link = tmp_path / "link"
        link.symlink_to(path.parent, target_is_directory=True)
        with ephemeral_postgres.engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE core.model_artifacts SET path=:path WHERE id=:id"),
                {"id": artifact_id, "path": str(link / path.name)},
            )
    elif unsafe == "fifo":
        (path / "notes.txt").unlink()
        os.mkfifo(path / "notes.txt")
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "notes.txt").write_bytes(b"fixture")
        (path / "notes.txt").unlink()
        (path / "notes.txt").symlink_to(outside / "notes.txt")

    async def scenario() -> None:
        engine, factory = await _factory(ephemeral_postgres)
        try:
            job = await ArtifactVerificationService(factory).verify(artifact_id)
            worker = _worker(factory, storage)
            if unsafe in {"outside", "parent_symlink"}:
                with pytest.raises(JobFailure) as failure:
                    await asyncio.wait_for(worker.run_once(), 3)
                assert failure.value.code == "artifact_verify_unsafe_path"
                persisted = await ArtifactVerificationService(factory).status(artifact_id)
                assert (
                    persisted is not None
                    and persisted.last_error_code == "artifact_verify_unsafe_path"
                )
                assert str(tmp_path) not in str(failure.value)
            else:
                assert await asyncio.wait_for(worker.run_once(), 3) is not None
                with ephemeral_postgres.engine.connect() as connection:
                    assert (
                        connection.scalar(
                            sa.text("SELECT status FROM core.model_artifacts WHERE id=:id"),
                            {"id": artifact_id},
                        )
                        == "Corrupted"
                    )
            assert job.id is not None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["Missing", "Corrupted"])
def test_M2_10C_TC_RT_07_known_bad_inventory_blocks_reservations(
    ephemeral_postgres: EphemeralPostgres, tmp_path: Path, status: str
) -> None:
    artifact_id, _, _ = _files(ephemeral_postgres, tmp_path)
    instance_id = uuid4()
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE core.model_artifacts SET status=:status WHERE id=:id"),
            {"id": artifact_id, "status": status},
        )
        connection.execute(
            sa.text(
                "INSERT INTO core.model_instances (id, artifact_id, runtime_type, "
                "expected_gpu_uuids, tensor_parallel_size, context_length, "
                "gpu_memory_utilization, name, internal_model_name, status, config_version, "
                "created_at, updated_at) VALUES (:id, :artifact, 'vllm', CAST(:gpus AS "
                "jsonb), 1, 4096, 0.9, :name, :name, 'Stopped', 1, now(), now())"
            ),
            {
                "id": instance_id,
                "artifact": artifact_id,
                "name": "mi_" + instance_id.hex[:12],
                "gpus": json.dumps([GPU_0]),
            },
        )

    async def scenario() -> None:
        for role in ("holm_control", "holm_instance_controller"):
            engine, factory = await _role_factory(ephemeral_postgres, role)
            try:
                if role == "holm_control":
                    service = ModelInstanceService(session_factory=factory, agent=_InventoryAgent())
                    with pytest.raises(ModelInstanceArtifactNotReady):
                        await service.create(replace(_value(), artifact_id=artifact_id))
                    for state, operation in (
                        ("Stopped", service.start),
                        ("Failed", service.retry),
                        ("WaitingForResource", service.start),
                    ):
                        with ephemeral_postgres.engine.begin() as connection:
                            connection.execute(
                                sa.text(
                                    "UPDATE core.model_instances SET status=:state WHERE id=:id"
                                ),
                                {"id": instance_id, "state": state},
                            )
                        with pytest.raises(ModelInstanceArtifactNotReady):
                            await operation(instance_id)
                    with ephemeral_postgres.engine.begin() as connection:
                        connection.execute(
                            sa.text(
                                "UPDATE core.model_instances SET status='Stopped' WHERE id=:id"
                            ),
                            {"id": instance_id},
                        )
                async with factory() as session, session.begin():
                    repository = ModelInstanceRepository(session)
                    instance = await repository.get(instance_id, for_update=True)
                    assert instance is not None
                    with pytest.raises(ModelInstanceArtifactNotReady):
                        await repository.reserve_or_wait(
                            instance, now=await database_utc_now(session)
                        )
            finally:
                await engine.dispose()

    asyncio.run(scenario())
    with ephemeral_postgres.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text("SELECT count(*) FROM core.gpu_allocations WHERE instance_id=:id"),
                {"id": instance_id},
            )
            == 0
        )
        assert (
            connection.scalar(
                sa.text("SELECT status FROM core.model_instances WHERE id=:id"), {"id": instance_id}
            )
            == "Stopped"
        )


def test_M2_10C_TC_RT_07_bad_waiter_does_not_block_healthy_and_running_is_untouched(
    ephemeral_postgres: EphemeralPostgres, tmp_path: Path
) -> None:
    from holm_domain import RuntimeType
    from holm_persistence.model_instance_repository import ModelInstanceCreate

    bad, path, storage = _files(ephemeral_postgres, tmp_path)
    from holm_persistence.model_domain import MODEL_ARTIFACTS

    good = uuid4()
    with ephemeral_postgres.engine.begin() as connection:
        record = dict(
            connection.execute(sa.select(MODEL_ARTIFACTS).where(MODEL_ARTIFACTS.c.id == bad))
            .mappings()
            .one()
        )
        record.update(id=good, resolved_revision=str(good))
        connection.execute(sa.insert(MODEL_ARTIFACTS).values(**record))

    async def scenario() -> None:
        engine, factory = await _factory(ephemeral_postgres)
        controller_engine, controller = await _role_factory(
            ephemeral_postgres, "holm_instance_controller"
        )
        try:
            async with factory() as session, session.begin():
                repo = ModelInstanceRepository(session)
                value = ModelInstanceCreate(
                    artifact_id=bad,
                    runtime_type=RuntimeType.VLLM,
                    expected_gpu_uuids=(GPU_0,),
                    tensor_parallel_size=1,
                    context_length=4096,
                    gpu_memory_utilization=0.8,
                )
                blocked = await repo.create(
                    value,
                    now=await database_utc_now(session),
                )
                healthy = await repo.create(
                    replace(value, artifact_id=good),
                    now=await database_utc_now(session),
                )
            with ephemeral_postgres.engine.begin() as connection:
                connection.execute(
                    sa.text("UPDATE core.model_instances SET status='WaitingForResource'")
                )
            (path / "model.safetensors").write_bytes(b"87654321")
            await ArtifactVerificationService(factory).verify(bad)
            await _worker(factory, storage).run_once()
            async with controller() as session, session.begin():
                started = await ModelInstanceRepository(session).retry_waiting_after_release(
                    now=await database_utc_now(session)
                )
                assert started == (healthy.id,)
            with ephemeral_postgres.engine.begin() as connection:
                assert (
                    connection.scalar(
                        sa.text("SELECT count(*) FROM core.gpu_allocations WHERE instance_id=:id"),
                        {"id": blocked.id},
                    )
                    == 0
                )
                connection.execute(
                    sa.text(
                        "UPDATE core.model_instances SET status='Running', "
                        "artifact_id=:artifact WHERE id=:id"
                    ),
                    {"id": healthy.id, "artifact": bad},
                )
            await ArtifactVerificationService(factory).verify(bad)
            await _worker(factory, storage).run_once()
            with ephemeral_postgres.engine.connect() as connection:
                assert (
                    connection.scalar(
                        sa.text("SELECT status FROM core.model_instances WHERE id=:id"),
                        {"id": healthy.id},
                    )
                    == "Running"
                )
        finally:
            await engine.dispose()
            await controller_engine.dispose()

    asyncio.run(scenario())


def test_M2_10C_delete_in_progress_and_terminal_execution_failure_are_visible(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    from holm_control_api.artifact_verification_service import ArtifactVerifyRejected
    from holm_persistence.artifact_verification_repository import (
        ArtifactVerificationRepository,
    )

    artifact_id, _, storage = _files(ephemeral_postgres, tmp_path)

    async def scenario() -> None:
        engine, factory = await _factory(ephemeral_postgres)
        try:
            await ArtifactDeletionService(factory).delete(
                artifact_id=artifact_id,
                actor_id=uuid4(),
                request_id=uuid4(),
            )
            with pytest.raises(ArtifactVerifyRejected) as rejected:
                await ArtifactVerificationService(factory).verify(artifact_id)
            assert rejected.value.code == "artifact_delete_in_progress"
            with ephemeral_postgres.engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "UPDATE core.jobs SET status='Failed', completed_at=now() "
                        "WHERE target_object_id=:id"
                    ),
                    {"id": artifact_id},
                )
            job = await ArtifactVerificationService(factory).verify(artifact_id)
            # Exercise the final bounded retry, without waiting for backoff in a test.
            with ephemeral_postgres.engine.begin() as connection:
                connection.execute(
                    sa.text("UPDATE core.jobs SET attempt=max_attempts-1 WHERE id=:id"),
                    {"id": job.id},
                )
            unsafe = storage.model_copy(update={"model_root": tmp_path / "not-allowed"})
            with pytest.raises(JobFailure):
                await _worker(factory, unsafe).run_once()
            record = await ArtifactVerificationService(factory).status(artifact_id)
            assert record is not None and record.status.value == "Failed"
            assert record.last_error_code == "artifact_verify_unsafe_path"
            async with factory() as session:
                assert (
                    await ArtifactVerificationRepository(session).last_completed_at(artifact_id)
                    is None
                )
            with ephemeral_postgres.engine.connect() as connection:
                assert (
                    connection.scalar(
                        sa.text("SELECT status FROM core.model_artifacts WHERE id=:id"),
                        {"id": artifact_id},
                    )
                    == "Ready"
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("read_stage", ["manifest", "hash", "metadata"])
def test_M2_10C_read_denied_is_execution_failure_not_inventory_damage(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    read_stage: str,
) -> None:
    from unittest.mock import patch

    artifact_id, path, storage = _files(ephemeral_postgres, tmp_path)
    original_open = os.open
    original_read = Path.read_bytes
    original_lstat = Path.lstat

    # The container test runner can bypass chmod permissions. Inject only the
    # target read syscall's PermissionError; all files, jobs and DB roles remain real.
    def denied_open(candidate: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if candidate == path / "model.safetensors":
            raise PermissionError("FX-M2-10C sensitive source path")
        return original_open(candidate, flags, *args, **kwargs)

    def denied_read(candidate: Path) -> bytes:
        if candidate == path / "manifest.json":
            raise PermissionError("FX-M2-10C sensitive source path")
        return original_read(candidate)

    def denied_metadata(candidate: Path) -> os.stat_result:
        if candidate == path / "model.safetensors":
            raise PermissionError("FX-M2-10C sensitive source path")
        return original_lstat(candidate)

    async def scenario() -> None:
        engine, factory = await _role_factory(ephemeral_postgres, "holm_download_worker")
        control_engine, control = await _role_factory(ephemeral_postgres, "holm_control")
        try:
            await ArtifactVerificationService(control).verify(artifact_id)
            replacement: AbstractContextManager[Any]
            if read_stage == "manifest":
                replacement = patch.object(Path, "read_bytes", denied_read)
            elif read_stage == "metadata":
                replacement = patch.object(Path, "lstat", denied_metadata)
            else:
                replacement = patch("holm_download_worker.artifact_publishing.os.open", denied_open)
            with replacement, pytest.raises(JobFailure) as failure:
                await _worker(factory, storage).run_once()
            assert failure.value.code == "artifact_verify_failed"
            assert "sensitive" not in str(failure.value)
            record = await ArtifactVerificationService(control).status(artifact_id)
            assert record is not None and record.last_error_code == "artifact_verify_failed"
            with ephemeral_postgres.engine.connect() as connection:
                assert (
                    connection.scalar(
                        sa.text("SELECT status FROM core.model_artifacts WHERE id=:id"),
                        {"id": artifact_id},
                    )
                    == "Ready"
                )
        finally:
            await engine.dispose()
            await control_engine.dispose()

    asyncio.run(scenario())

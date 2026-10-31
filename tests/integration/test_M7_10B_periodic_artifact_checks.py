"""FX-M7-10B: existing FX-M2-10C canonical tiny files and isolated real PostgreSQL.

No actual model, Runtime, GPU, production database or credentials are used.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from holm_common.config import StorageConfig
from holm_control_api.artifact_verification_service import ArtifactVerificationService
from holm_control_api.model_acquisition_read_service import ModelAcquisitionReadService
from holm_control_api.model_instance_service import ModelInstanceService
from holm_domain import ArtifactStorageType
from holm_download_worker.artifact_publishing import ArtifactRevalidator
from holm_download_worker.worker import DownloadWorker
from holm_maintenance_worker.worker import MaintenanceWorker
from holm_persistence.artifact_check_repository import ArtifactCheckRepository
from holm_persistence.artifact_verification_repository import ArtifactVerificationRepository
from holm_persistence.job_executor import JobFailure
from holm_persistence.model_instance_repository import ModelInstanceArtifactNotReady
from holm_persistence.role_grants import MAINTENANCE, RUNTIME_ROLES
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from test_M2_10C_artifact_reverification import _cookies, _files, _role_factory, _worker
from test_M2_12A_download_artifact_read_api import ADMIN_USERNAME, _create_accounts, _environment
from test_M7_10A_TC_DL_14_artifact_deletion import _factory
from test_M7_10A_TC_DL_14_artifact_deletion import _migrated as _migrated
from test_PROTO_03_instance_controller_postgres import _InventoryAgent, _value

pytestmark = pytest.mark.usefixtures("_cleanup")


@pytest.fixture
def _cleanup(ephemeral_postgres: EphemeralPostgres, _migrated: None) -> Any:
    yield
    # Do not leak deliberately overdue fixtures into subsequent process smoke tests.
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(sa.text("TRUNCATE core.models, core.jobs, core.users CASCADE"))


def _age(database: EphemeralPostgres, artifact_id: UUID) -> None:
    with database.engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE core.model_artifacts SET created_at=now()-interval '25 hours' WHERE id=:id"
            ),
            {"id": artifact_id},
        )


def _row(database: EphemeralPostgres, artifact_id: UUID) -> Any:
    with database.engine.connect() as connection:
        return connection.execute(
            sa.text(
                "SELECT status, light_check_requested_at, light_checked_at FROM "
                "core.model_artifacts WHERE id=:id"
            ),
            {"id": artifact_id},
        ).one()


async def _completed(worker: DownloadWorker) -> None:
    result = await worker.run_once()
    assert result is not None and result.completed


async def _schedule(factory: Any) -> Any:
    async with factory() as session, session.begin():
        return await ArtifactCheckRepository(session).schedule_due()


@pytest.mark.parametrize("storage_type", list(ArtifactStorageType))
def test_M7_10B_TC_DL_09_TC_DP_08_due_restart_readonly_metadata_and_full_check(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    storage_type: ArtifactStorageType,
) -> None:
    artifact_id, path, storage = _files(ephemeral_postgres, tmp_path, storage_type)
    weight = path / "model.safetensors"
    # A light check should not hash this same-size modification; full verification must.
    weight.write_bytes(b"87654321")
    snapshot = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in path.iterdir()}
    directory_mtime = path.stat().st_mtime_ns

    async def scenario() -> None:
        me, maintenance = await _role_factory(ephemeral_postgres, MAINTENANCE)
        de, download = await _role_factory(ephemeral_postgres, "holm_download_worker")
        ce, control = await _role_factory(ephemeral_postgres, "holm_control")
        try:
            assert (await _schedule(maintenance)).scheduled == 0
            _age(ephemeral_postgres, artifact_id)
            first = MaintenanceWorker(
                backup_root=tmp_path,
                session_factory=maintenance,
                instance_id="FX-M7-10B",
                build_version="fixture",
            )
            assert (await first.schedule_artifact_checks()).scheduled == 1
            restarted = MaintenanceWorker(
                backup_root=tmp_path,
                session_factory=maintenance,
                instance_id="FX-M7-10B",
                build_version="fixture",
            )
            assert (await restarted.schedule_artifact_checks()).scheduled == 0
            assert _row(ephemeral_postgres, artifact_id).light_checked_at is None
            async with control() as session:
                repo = ArtifactVerificationRepository(session)
                assert await repo.last_completed_at(artifact_id) is None
                assert await repo.latest_job(artifact_id) is None
                assert not await repo.pending(artifact_id)
            original_open = Path.open

            def guarded_open(p: Path, *args: Any, **kwargs: Any) -> Any:
                assert p != weight, "light check read the complete weight file"
                return original_open(p, *args, **kwargs)

            original_os_open = os.open

            def guarded_os_open(name: Any, *args: Any, **kwargs: Any) -> Any:
                assert Path(name) != weight, "light check opened the weight file"
                return original_os_open(name, *args, **kwargs)

            with monkeypatch.context() as guard:
                guard.setattr(Path, "open", guarded_open)
                guard.setattr(os, "open", guarded_os_open)
                await _completed(_worker(download, storage))
            row = _row(ephemeral_postgres, artifact_id)
            assert row.status == "Ready"
            assert row.light_checked_at > row.light_check_requested_at
            assert not (await _schedule(maintenance)).pending
            assert (await _schedule(maintenance)).scheduled == 0
            async with control() as session:
                assert (
                    await ArtifactVerificationRepository(session).last_completed_at(artifact_id)
                    == row.light_checked_at
                )
            await ArtifactVerificationService(control).verify(artifact_id)
            await _completed(_worker(download, storage))
            assert _row(ephemeral_postgres, artifact_id).status == "Corrupted"
            async with control() as session:
                assert (
                    await ArtifactVerificationRepository(session).last_completed_at(artifact_id)
                    > row.light_checked_at
                )
        finally:
            for engine in (me, de, ce):
                await engine.dispose()

    asyncio.run(scenario())
    assert snapshot == {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in path.iterdir()}
    assert path.stat().st_mtime_ns == directory_mtime


@pytest.mark.parametrize(
    "damage,expected", [("missing", "Missing"), ("manifest", "Corrupted"), ("symlink", "Corrupted")]
)
def test_M7_10B_TC_DL_09_TC_RT_07_actual_damage_completed_and_blocks_start(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    damage: str,
    expected: str,
) -> None:
    artifact_id, path, storage = _files(ephemeral_postgres, tmp_path)
    if damage == "missing":
        (path / "model.safetensors").unlink()
    elif damage == "manifest":
        (path / "manifest.json").write_bytes(b"invalid-manifest")
    else:
        outside = tmp_path / "outside"
        outside.write_bytes(b"12345678")
        (path / "model.safetensors").unlink()
        (path / "model.safetensors").symlink_to(outside)
    _age(ephemeral_postgres, artifact_id)

    async def scenario() -> None:
        me, maintenance = await _role_factory(ephemeral_postgres, MAINTENANCE)
        de, download = await _role_factory(ephemeral_postgres, "holm_download_worker")
        ce, control = await _role_factory(ephemeral_postgres, "holm_control")
        try:
            assert (await _schedule(maintenance)).scheduled == 1
            await _completed(_worker(download, storage))
            row = _row(ephemeral_postgres, artifact_id)
            assert row.status == expected and row.light_checked_at is not None
            service = ModelInstanceService(session_factory=control, agent=_InventoryAgent())
            with pytest.raises(ModelInstanceArtifactNotReady):
                await service.create(replace(_value(), artifact_id=artifact_id))
            assert (await _schedule(maintenance)).scheduled == 0
        finally:
            for engine in (me, de, ce):
                await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["Missing", "Corrupted"])
@pytest.mark.parametrize("when", ["before", "during"])
def test_M7_10B_TC_DL_09_skipped_light_check_cannot_revive_or_record_completion(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    when: str,
) -> None:
    artifact_id, _, storage = _files(ephemeral_postgres, tmp_path)
    _age(ephemeral_postgres, artifact_id)

    def mark_bad() -> None:
        with ephemeral_postgres.engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE core.model_artifacts SET status=:state WHERE id=:id"),
                {"state": status, "id": artifact_id},
            )

    original = ArtifactRevalidator._judge_record

    def judge(record: Any, *, full: bool) -> Any:
        value = original(record, full=full)
        mark_bad()
        return value

    async def scenario() -> None:
        engine, factory = await _factory(ephemeral_postgres)
        try:
            assert (await _schedule(factory)).scheduled == 1
            if when == "before":
                mark_bad()
            else:
                monkeypatch.setattr(ArtifactRevalidator, "_judge_record", staticmethod(judge))
            await _completed(_worker(factory, storage))
            row = _row(ephemeral_postgres, artifact_id)
            assert row.status == status and row.light_checked_at is None
            async with factory() as session:
                assert (
                    await ArtifactVerificationRepository(session).last_completed_at(artifact_id)
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("problem", ["root", "parent_symlink", "permission"])
def test_M7_10B_TC_DP_08_failures_sanitized_finite_retries_no_false_completion(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    problem: str,
) -> None:
    artifact_id, path, storage = _files(ephemeral_postgres, tmp_path)
    if problem == "root":
        storage = StorageConfig(
            model_root=tmp_path / "wrong", tokenizer_root=tmp_path / "tokenizers"
        )
    elif problem == "parent_symlink":
        link = tmp_path / "link"
        link.symlink_to(path.parent)
        with ephemeral_postgres.engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE core.model_artifacts SET path=:path WHERE id=:id"),
                {"path": str(link / path.name), "id": artifact_id},
            )
    else:
        original = Path.read_bytes

        def denied(p: Path) -> bytes:
            if p == path / "manifest.json":
                raise PermissionError("FX-M7-10B-SECRET-BODY " + str(path))
            return original(p)

        monkeypatch.setattr(Path, "read_bytes", denied)
    _age(ephemeral_postgres, artifact_id)

    async def scenario() -> None:
        engine, factory = await _factory(ephemeral_postgres)
        try:
            await _schedule(factory)
            worker = _worker(factory, storage)
            for attempt in range(1, 4):
                with pytest.raises(JobFailure) as failure:
                    await worker.run_once()
                assert str(path) not in str(failure.value)
                with ephemeral_postgres.engine.begin() as connection:
                    row = connection.execute(
                        sa.text(
                            "SELECT status, attempt, max_attempts, last_error_code, run_after > "
                            "updated_at AS backed_off FROM core.jobs "
                            "WHERE job_type='artifact.light_verify'"
                        )
                    ).one()
                    assert row.attempt == attempt and row.max_attempts == 3
                    assert row.last_error_code == (
                        "artifact_verify_failed"
                        if problem == "permission"
                        else "artifact_verify_unsafe_path"
                    )
                    if attempt < 3:
                        assert row.status == "Queued" and row.backed_off
                        connection.execute(sa.text("UPDATE core.jobs SET run_after=now()"))
                    else:
                        assert row.status == "Failed"
                assert _row(ephemeral_postgres, artifact_id).light_checked_at is None
            result = await _schedule(factory)
            assert result.scheduled == 0 and result.failed and not result.pending
            assert await worker.run_once() is None
            async with factory() as session:
                assert (
                    await ArtifactVerificationRepository(session).last_completed_at(artifact_id)
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_M7_10B_TC_DP_13_restricted_function_real_logins(
    ephemeral_postgres: EphemeralPostgres, tmp_path: Path
) -> None:
    artifact_id, _, _ = _files(ephemeral_postgres, tmp_path)
    _age(ephemeral_postgres, artifact_id)
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(sa.text("CREATE ROLE m710b_maintenance_login LOGIN"))
        connection.execute(sa.text("GRANT holm_maintenance_worker TO m710b_maintenance_login"))
        connection.execute(sa.text("CREATE ROLE m710b_public_login LOGIN"))
        connection.execute(sa.text("GRANT USAGE ON SCHEMA core TO m710b_public_login"))
    engine = sa.create_engine(ephemeral_postgres.engine.url.set(username="m710b_maintenance_login"))
    try:
        with engine.begin() as connection:
            assert connection.scalar(sa.text("SELECT current_user")) == "m710b_maintenance_login"
            assert (
                connection.execute(sa.text("SELECT * FROM core.holm_schedule_artifact_checks()"))
                .one()
                .scheduled
                == 1
            )
        for sql in (
            "SELECT * FROM core.model_artifacts",
            "SELECT * FROM core.jobs",
            "UPDATE core.model_artifacts SET status='Ready' WHERE false",
            "INSERT INTO core.jobs SELECT * FROM core.jobs WHERE false",
            "UPDATE core.jobs SET job_type='artifact.delete' WHERE false",
            "SET ROLE holm_control",
            "SELECT secret_hmac FROM core.api_keys LIMIT 0",
            "CREATE TABLE core.forbidden (id int)",
        ):
            with pytest.raises(DBAPIError) as failure, engine.begin() as connection:
                connection.execute(sa.text(sql))
            assert getattr(failure.value.orig, "sqlstate", None) == "42501"
    finally:
        engine.dispose()
    for role in (*[r for r in RUNTIME_ROLES if r != MAINTENANCE], "m710b_public_login"):
        with ephemeral_postgres.engine.begin() as connection:
            if role != "m710b_public_login":
                login = "m710b_" + role.removeprefix("holm_")
                connection.execute(sa.text(f"CREATE ROLE {login} LOGIN"))
                connection.execute(sa.text(f"GRANT {role} TO {login}"))
            else:
                login = role
        denied = sa.create_engine(ephemeral_postgres.engine.url.set(username=login))
        try:
            with pytest.raises(DBAPIError) as failure, denied.begin() as connection:
                connection.execute(sa.text("SELECT * FROM core.holm_schedule_artifact_checks()"))
            assert getattr(failure.value.orig, "sqlstate", None) == "42501"
        finally:
            denied.dispose()


def test_M7_10B_TC_DL_09_api_time_tracks_actual_completion(
    ephemeral_postgres: EphemeralPostgres, tmp_path: Path
) -> None:
    artifact_id, _, storage = _files(ephemeral_postgres, tmp_path)
    _age(ephemeral_postgres, artifact_id)

    async def scenario() -> None:
        engine, control = await _role_factory(ephemeral_postgres, "holm_control")
        de, download = await _role_factory(ephemeral_postgres, "holm_download_worker")
        me, maintenance = await _role_factory(ephemeral_postgres, MAINTENANCE)

        class RoleReadService(ModelAcquisitionReadService):
            def __init__(self, **kwargs: Any) -> None:
                super().__init__(**{**kwargs, "session_factory": control})

        try:
            async with _environment(
                ephemeral_postgres, model_root=storage.model_root, read_service_type=RoleReadService
            ) as env:
                await _create_accounts(env)
                admin = await _cookies(env, ADMIN_USERNAME)

                async def listed() -> Any:
                    response = await env.client.request("GET", "/api/v1/artifacts", cookies=admin)
                    assert response.status == 200
                    return response.json()["items"][0]

                before = (await listed())["last_verified_at"]
                await _schedule(maintenance)
                assert (await listed())["last_verified_at"] == before
                await _worker(download, storage).run_once()
                assert (await listed())["last_verified_at"] != before
                # Periodic jobs do not replace the manual-operation status.
                response = await env.client.request(
                    "GET", f"/api/v1/artifacts/{artifact_id}/verification", cookies=admin
                )
                assert response.status == 200 and response.json()["status"] is None
        finally:
            for e in (engine, de, me):
                await e.dispose()

    asyncio.run(scenario())


def test_M7_10B_TC_DP_13_production_entry_schedules_and_keeps_retention_failure(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    artifact_id, path, storage = _files(ephemeral_postgres, tmp_path)
    _age(ephemeral_postgres, artifact_id)
    backups = tmp_path / "backups"
    backups.mkdir()
    bad = backups / "unknown"
    bad.write_bytes(b"FX-M7-10B-PRIVATE")
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(sa.text("CREATE ROLE m710b_process LOGIN"))
        connection.execute(sa.text("GRANT holm_maintenance_worker TO m710b_process"))
    credential = tmp_path / "database-url"
    url = URL.create(
        "postgresql+psycopg",
        username="m710b_process",
        database="postgres",
        query={
            "host": str(ephemeral_postgres.socket_directory),
            "port": str(ephemeral_postgres.port),
        },
    )
    credential.write_text(url.render_as_string(hide_password=False))
    credential.chmod(0o400)
    output = tmp_path / "worker.log"

    def heartbeat() -> Any:
        with ephemeral_postgres.engine.connect() as connection:
            return connection.execute(
                sa.text(
                    "SELECT status, reason FROM core.component_heartbeats WHERE "
                    "instance_id='FX-M7-10B-process'"
                )
            ).one_or_none()

    def wait_for(predicate: Any, process: Any) -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            assert process.poll() is None
            if predicate():
                return
            time.sleep(0.05)
        raise AssertionError("production check did not converge")

    with output.open("wb") as log:
        process = subprocess.Popen(
            [
                str(Path(sys.executable).parent / "holm-maintenance-worker"),
                "--database-url-file",
                str(credential),
                "--backup-root",
                str(backups),
                "--model-root",
                str(storage.model_root),
                "--external-model-root",
                str(path),
                "--instance-id",
                "FX-M7-10B-process",
                "--retention-seconds",
                "0.1",
                "--heartbeat-seconds",
                "0.1",
                "--artifact-check-seconds",
                "0.1",
            ],
            stdout=log,
            stderr=log,
        )
        try:
            wait_for(
                lambda: _row(ephemeral_postgres, artifact_id).light_check_requested_at is not None,
                process,
            )
            wait_for(lambda: heartbeat() == ("Degraded", "invalid_backup_directory"), process)

            async def execute() -> None:
                engine, factory = await _role_factory(ephemeral_postgres, "holm_download_worker")
                try:
                    await _completed(_worker(factory, storage))
                finally:
                    await engine.dispose()

            asyncio.run(execute())
            assert _row(ephemeral_postgres, artifact_id).light_checked_at is not None
            assert heartbeat() == ("Degraded", "invalid_backup_directory")
            bad.unlink()
            wait_for(lambda: heartbeat() == ("Healthy", None), process)
            # A missing function privilege is a real scheduler failure, not healthy work.
            with ephemeral_postgres.engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "REVOKE EXECUTE ON FUNCTION core.holm_schedule_artifact_checks() "
                        "FROM holm_maintenance_worker"
                    )
                )
            wait_for(lambda: heartbeat() == ("Degraded", "artifact_check_schedule_failed"), process)
            with ephemeral_postgres.engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "GRANT EXECUTE ON FUNCTION core.holm_schedule_artifact_checks() TO "
                        "holm_maintenance_worker"
                    )
                )
            wait_for(lambda: heartbeat() == ("Healthy", None), process)
            process.terminate()
            assert process.wait(timeout=12) == 0
            assert heartbeat() == ("Stopping", "maintenance_stopping")
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=12)
            with ephemeral_postgres.engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "GRANT EXECUTE ON FUNCTION core.holm_schedule_artifact_checks() TO "
                        "holm_maintenance_worker"
                    )
                )
    assert b"FX-M7-10B-PRIVATE" not in output.read_bytes()
    assert str(tmp_path).encode() not in output.read_bytes()


def test_M7_10B_TC_RT_07_running_service_and_allocation_unchanged(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    from fixtures.model_domain_snapshot import model_domain_snapshot
    from holm_domain import RuntimeType
    from holm_persistence import ModelInstanceRepository, database_utc_now
    from holm_persistence.model_instance_repository import ModelInstanceCreate

    artifact_id, path, storage = _files(ephemeral_postgres, tmp_path)
    _age(ephemeral_postgres, artifact_id)

    async def scenario() -> None:
        engine, factory = await _factory(ephemeral_postgres)
        try:
            # Real persisted instance/allocation/service metadata, no Runtime or GPU access.
            async with factory() as session, session.begin():
                repository = ModelInstanceRepository(session)
                now = await database_utc_now(session)
                instance = await repository.create(
                    ModelInstanceCreate(
                        artifact_id=artifact_id,
                        runtime_type=RuntimeType.VLLM,
                        expected_gpu_uuids=("GPU-FX-M7-10B",),
                        tensor_parallel_size=1,
                        context_length=4096,
                        gpu_memory_utilization=0.8,
                    ),
                    now=now,
                )
                await repository.reserve_or_wait(instance, now=now)
            service_id = uuid4()
            with ephemeral_postgres.engine.begin() as connection:
                connection.execute(
                    sa.text("UPDATE core.model_instances SET status='Running' WHERE id=:id"),
                    {"id": instance.id},
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO core.ai_services (id, name, public_model_name, description, "
                        "enabled, route_version, created_at, updated_at) "
                        "VALUES (:id, 'FX-M7-10B-service', 'FX-M7-10B-service', "
                        "'', true, 1, now(), now())"
                    ),
                    {"id": service_id},
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO core.ai_service_backends (id, service_id, instance_id, "
                        "instance_name_snapshot, runtime_snapshot, "
                        "backend_key, weight, enabled, health_gate_passed, joined_at) VALUES "
                        "(:id, :service, :instance, 'FX-M7-10B-instance', 'vllm', 'primary', "
                        "100, true, true, now())"
                    ),
                    {"id": uuid4(), "service": service_id, "instance": instance.id},
                )
            tables = ("model_instances", "gpu_allocations", "ai_services", "ai_service_backends")
            before = model_domain_snapshot(ephemeral_postgres.engine, tables)
            (path / "model.safetensors").unlink()
            await _schedule(factory)
            await _completed(_worker(factory, storage))
            assert _row(ephemeral_postgres, artifact_id).status == "Missing"
            assert model_domain_snapshot(ephemeral_postgres.engine, tables) == before
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_M7_10B_TC_RT_07_recent_light_check_never_replaces_launch_full_check(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    from holm_domain.launch_verification import LaunchVerificationResult
    from holm_instance_controller import PostgresControllerStore

    artifact_id, path, storage = _files(ephemeral_postgres, tmp_path)
    _age(ephemeral_postgres, artifact_id)
    (path / "model.safetensors").write_bytes(b"87654321")

    async def scenario() -> None:
        engine, factory = await _factory(ephemeral_postgres)
        try:
            await _schedule(factory)
            worker = _worker(factory, storage)
            await _completed(worker)
            assert _row(ephemeral_postgres, artifact_id).status == "Ready"
            created = await ModelInstanceService(
                session_factory=factory, agent=_InventoryAgent()
            ).create(replace(_value(), artifact_id=artifact_id))
            await _completed(worker)
            result = await PostgresControllerStore(factory).launch_verification(created.id)
            assert result is not None and result.result is LaunchVerificationResult.CORRUPTED
            assert _row(ephemeral_postgres, artifact_id).status == "Corrupted"
        finally:
            await engine.dispose()

    asyncio.run(scenario())

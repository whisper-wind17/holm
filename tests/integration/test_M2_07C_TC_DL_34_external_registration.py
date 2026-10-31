"""FX-M2-07C: tiny safetensors from TC-DL-09; real PG, no Hub or GPU required."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from fixtures.asgi_client import SessionCookies
from holm_common.config import StorageConfig
from holm_contracts import CursorPageParams
from holm_control_api.artifact_deletion_service import (
    ArtifactDeleteRejected,
    ArtifactDeletionService,
)
from holm_control_api.download_command_service import DownloadCommand, DownloadCommandService
from holm_control_api.download_creation_service import DownloadCreationSuccess
from holm_control_api.model_acquisition_read_routes import (
    DownloadTaskItemResponse,
    ModelArtifactItemResponse,
)
from holm_control_api.model_acquisition_read_service import (
    ModelAcquisitionReadService,
    ModelRootAvailability,
)
from holm_control_api.model_instance_service import CreateModelInstance, ModelInstanceService
from holm_domain import (
    EXTERNAL_REGISTRATION_JOB_TYPE,
    ModelArtifactStatus,
    ModelOriginType,
    RuntimeType,
)
from holm_download_worker import (
    ArtifactRevalidator,
    DownloadArtifactPublisher,
    DownloadResolutionJobHandler,
    DownloadWorker,
    ResolutionSubprocessRequest,
    ResolutionSubprocessResult,
)
from holm_download_worker.agent_client import PathValidator
from holm_persistence.job_executor import JobFailure
from holm_persistence.model_acquisition_read_repository import ModelArtifactFilters
from test_M2_10B_TC_DL_09_10_11_25_artifact_publishing import _valid_payloads
from test_PROTO_01_model_instances import _StaticAgent
from test_TC_DL_01_download_creation import Environment, environment
from test_TC_DL_01_download_creation import empty_database as empty_database
from test_TC_MDL_01_10_11_13_model_catalog_api import PASSWORD, _cookie

# Import the existing real-PG cleanup fixture explicitly; do not seed External rows.
pytestmark = pytest.mark.usefixtures("empty_database")


class NeverResolve:
    async def resolve(self, request: ResolutionSubprocessRequest) -> ResolutionSubprocessResult:
        raise AssertionError("External registration entered Resolution")


class FakePathValidator:
    def __init__(self, result: bool | None, *, unavailable: bool = False) -> None:
        self.result = result
        self.unavailable = unavailable

    async def readable(self, path: str) -> bool | None:
        assert Path(path).is_absolute()
        if self.unavailable:
            raise JobFailure("external_path_agent_unavailable")
        return self.result


def source(root: Path, *, missing_config: bool = False) -> Path:
    result = root / "external" / "model"
    result.mkdir(parents=True)
    payloads = _valid_payloads()
    payloads["config.json"] = (
        b'{"architectures":["FixtureForCausalLM"],"num_attention_heads":8,"num_key_value_heads":2}'
    )
    for name, content in payloads.items():
        if name == "config.json" and missing_config:
            continue
        (result / name).write_bytes(content)
    return result


def snapshot(path: Path) -> dict[str, tuple[bytes | None, int, int]]:
    return {
        str(p.relative_to(path)): (
            p.read_bytes() if p.is_file() else None,
            p.stat().st_mtime_ns,
            p.stat().st_mode,
        )
        for p in (path, *sorted(path.rglob("*")))
    }


def rows(database: EphemeralPostgres, table: str) -> list[dict[str, Any]]:
    assert table in {"download_tasks", "model_artifacts", "models", "jobs", "audit_events"}
    with database.engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(sa.text(f"SELECT * FROM core.{table}")).mappings()
        ]


async def register(env: Environment, path: Path, key: str = "external") -> DownloadCreationSuccess:
    result = await env.service.create(
        actor_id=env.actor_id,
        request_id=uuid4(),
        idempotency_key=key,
        source=ModelOriginType.LOCAL_PATH,
        repository_id=None,
        external_path=str(path),
        revision=None,
    )
    assert isinstance(result, DownloadCreationSuccess)
    return result


def worker(
    env: Environment, root: Path, validator: PathValidator | None = None
) -> tuple[DownloadWorker, StorageConfig]:
    storage = StorageConfig(
        model_root=root / "managed-storage",
        tokenizer_root=root / "bundles",
        external_model_roots=(root / "external",),
    )
    handler = DownloadResolutionJobHandler(session_factory=env.factory, resolver=NeverResolve())
    handler.attach_artifact_publisher(
        DownloadArtifactPublisher(
            session_factory=env.factory, storage=storage, path_validator=validator
        )
    )
    return DownloadWorker(
        session_factory=env.factory, handler=handler, owner="M2-07C", build_version="test"
    ), storage


async def process(
    env: Environment, root: Path, validator: PathValidator | None = None
) -> StorageConfig:
    runner, storage = worker(env, root, validator)
    # Use the real worker's registered executor, including its accepted job types.
    result = await runner.run_once()
    assert result is not None
    return storage


def test_M2_07C_TC_DL_34_register_publish_deploy_unregister_readonly(
    ephemeral_postgres: EphemeralPostgres, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = source(tmp_path)
    before = snapshot(path)

    async def run() -> None:
        async with environment(ephemeral_postgres, path.parent) as env:
            first = await register(env, path)
            assert first.task.status.value == "Verifying"
            same = await register(env, path / ".." / "model", "joined")
            replay = await register(env, path)
            assert same.task.id == replay.task.id == first.task.id
            assert same.disposition == replay.disposition == "joined_existing"
            task = rows(ephemeral_postgres, "download_tasks")[0]
            assert task["downloaded_bytes"] == 0 and task["staging_path"] is None
            assert rows(ephemeral_postgres, "jobs")[0]["job_type"] == EXTERNAL_REGISTRATION_JOB_TYPE
            assert rows(ephemeral_postgres, "models")[0]["source_key"] == str(path)
            storage = await process(env, tmp_path)
            artifact = rows(ephemeral_postgres, "model_artifacts")[0]
            assert artifact["storage_type"] == "External" and artifact["path"] == str(path)
            assert artifact["status"] == "Ready" and artifact["manifest_files"]
            assert artifact["attention_heads"] == 8 and artifact["kv_heads"] == 2
            assert artifact["tokenizer_bundle_hash"] is artifact["tokenizer_bundle_path"] is None
            assert rows(ephemeral_postgres, "download_tasks")[0]["status"] == "Completed"
            assert (await register(env, path, "completed")).task.id == first.task.id
            assert (
                len(rows(ephemeral_postgres, "download_tasks"))
                == len(rows(ephemeral_postgres, "model_artifacts"))
                == 1
            )
            read = ModelAcquisitionReadService(
                session_factory=env.factory,
                storage_availability=ModelRootAvailability(storage.model_root),
            )
            downloads = await read.list_download_tasks(CursorPageParams())
            artifacts = await read.list_artifacts(
                CursorPageParams(), filters=ModelArtifactFilters()
            )
            assert DownloadTaskItemResponse.from_item(downloads.items[0]).revision is None
            assert ModelArtifactItemResponse.from_item(artifacts.items[0]).resolved_revision is None
            judgement = await ArtifactRevalidator(session_factory=env.factory).revalidate(
                artifact["id"], full=True, storage=storage
            )
            assert judgement.status is ModelArtifactStatus.READY
            instances = ModelInstanceService(
                session_factory=env.factory, agent=_StaticAgent(("GPU-M2-07C",))
            )
            instance = await instances.create(
                CreateModelInstance(
                    artifact_id=artifact["id"],
                    runtime_type=RuntimeType.VLLM,
                    gpu_uuids=("GPU-M2-07C",),
                    tensor_parallel_size=1,
                    context_length=4096,
                    gpu_memory_utilization=0.9,
                )
            )
            deletion = ArtifactDeletionService(env.factory)
            with pytest.raises(ArtifactDeleteRejected) as exc:
                await deletion.delete(
                    artifact_id=artifact["id"], actor_id=env.actor_id, request_id=uuid4()
                )
            assert exc.value.code == "artifact_referenced"
            # Fixture teardown only: release the test instance's references before unregistering.
            async with env.factory() as session, session.begin():
                await session.execute(
                    sa.text("DELETE FROM core.gpu_allocations WHERE instance_id=:id"),
                    {"id": instance.id},
                )
                await session.execute(
                    sa.text("DELETE FROM core.model_instances WHERE id=:id"), {"id": instance.id}
                )
            await deletion.delete(
                artifact_id=artifact["id"], actor_id=env.actor_id, request_id=uuid4()
            )
            assert rows(ephemeral_postgres, "model_artifacts") == []
            assert snapshot(path) == before
            assert not storage.tokenizer_root.exists()
            assert str(path) not in str(rows(ephemeral_postgres, "audit_events"))

    with caplog.at_level(logging.INFO):
        asyncio.run(run())
    assert "运行身份可读性未验证" in caplog.text
    assert str(path) not in caplog.text


def test_M2_07C_TC_DL_34_missing_config_resume(
    ephemeral_postgres: EphemeralPostgres, tmp_path: Path
) -> None:
    path = source(tmp_path, missing_config=True)
    before = snapshot(path)

    async def run() -> None:
        async with environment(ephemeral_postgres, path.parent) as env:
            task = await register(env, path)
            await process(env, tmp_path, FakePathValidator(True))
            failed = rows(ephemeral_postgres, "download_tasks")[0]
            assert (
                failed["status"] == "Failed"
                and failed["error_code"] == "artifact_required_files_invalid"
            )
            assert rows(ephemeral_postgres, "model_artifacts") == [] and snapshot(path) == before
            (path / "config.json").write_bytes(_valid_payloads()["config.json"])
            repaired = snapshot(path)
            resumed = await DownloadCommandService(session_factory=env.factory).execute(
                task_id=task.task.id,
                command=DownloadCommand.RESUME,
                actor_id=env.actor_id,
                request_id=uuid4(),
            )
            assert (
                resumed.task.status.value == "Verifying" and not resumed.staging_cleanup_requested
            )
            await process(env, tmp_path, FakePathValidator(True))
            assert rows(ephemeral_postgres, "download_tasks")[0]["status"] == "Completed"
            assert snapshot(path) == repaired

    asyncio.run(run())


@pytest.mark.parametrize(
    "readable,unavailable,expected",
    [
        (True, False, "Completed"),
        (False, False, "Failed"),
        (None, False, "Completed"),
        (None, True, "Verifying"),
    ],
)
def test_M2_07C_TC_DL_34_runtime_readability(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    readable: bool | None,
    unavailable: bool,
    expected: str,
) -> None:
    path = source(tmp_path)
    before = snapshot(path)

    async def run() -> None:
        async with environment(ephemeral_postgres, path.parent) as env:
            await register(env, path)
            if unavailable:
                with pytest.raises(JobFailure) as exc:
                    await process(env, tmp_path, FakePathValidator(readable, unavailable=True))
                assert exc.value.code == "external_path_agent_unavailable"
            else:
                await process(env, tmp_path, FakePathValidator(readable))
            task = rows(ephemeral_postgres, "download_tasks")[0]
            assert task["status"] == expected
            if readable is False:
                assert task["error_code"] == "external_path_unreadable"
            if expected != "Completed":
                assert rows(ephemeral_postgres, "model_artifacts") == []
            if unavailable:
                job = rows(ephemeral_postgres, "jobs")[0]
                assert (
                    job["status"] == "Queued"
                    and job["last_error_code"] == "external_path_agent_unavailable"
                )
            assert snapshot(path) == before

    with caplog.at_level(logging.INFO):
        asyncio.run(run())
    if readable is None and not unavailable:
        assert "运行身份可读性未验证" in caplog.text
    assert str(path) not in caplog.text


def test_M2_07C_TC_DL_34_api_rejections_and_authorization(
    ephemeral_postgres: EphemeralPostgres, tmp_path: Path
) -> None:
    path = source(tmp_path)

    async def run() -> None:
        async with environment(ephemeral_postgres, path.parent) as env, env.client.lifespan():

            async def login(username: str) -> Any:
                response = await env.client.request(
                    "POST",
                    "/api/v1/auth/login",
                    json_body={"username": username, "password": PASSWORD},
                )
                return SessionCookies(
                    {"__Host-pws_session": _cookie(response, "__Host-pws_session")},
                    csrf_token=str(response.json()["csrf_token"]),
                )

            admin = await login("download-admin")
            denied = await env.client.request(
                "POST",
                "/api/v1/downloads",
                json_body={"source": "Local Path", "external_path": str(path)},
                cookies=await login("download-member"),
                headers={"Idempotency-Key": "denied"},
            )
            assert denied.status == 403
            for rejected in (str(tmp_path), str(path / "missing"), "relative-sentinel"):
                result = await env.client.request(
                    "POST",
                    "/api/v1/downloads",
                    json_body={"source": "Local Path", "external_path": rejected},
                    cookies=admin,
                    headers={"Idempotency-Key": str(uuid4())},
                )
                assert result.status == 200 and result.json()["reason_code"] == "source_not_found"
                assert rejected not in result.text
                assert rejected not in str(rows(ephemeral_postgres, "audit_events"))
            assert (
                rows(ephemeral_postgres, "models")
                == rows(ephemeral_postgres, "download_tasks")
                == []
            )
            success = await env.client.request(
                "POST",
                "/api/v1/downloads",
                json_body={"source": "Local Path", "external_path": str(path)},
                cookies=admin,
                headers={"Idempotency-Key": "created"},
            )
            assert success.status == 200 and success.json()["status"] == "Verifying"

    asyncio.run(run())


@pytest.mark.parametrize(
    "defect,reason",
    [
        ("remote_code", "remote_code_not_allowed"),
        ("symlink_escape", "artifact_unsafe_file"),
        ("safetensors", "artifact_safetensors_invalid"),
    ],
)
def test_M2_07C_TC_DL_34_unsafe_content_is_rejected_readonly(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    defect: str,
    reason: str,
) -> None:
    path = source(tmp_path)
    if defect == "remote_code":
        (path / "config.json").write_text(
            '{"trust_remote_code":true,"architectures":["FixtureForCausalLM"]}'
        )
    elif defect == "symlink_escape":
        outside = tmp_path / "outside-sentinel"
        outside.write_bytes(b"must not be touched")
        (path / "escaped").symlink_to(outside)
    else:
        (path / "model.safetensors").write_bytes(b"invalid header")
    before = snapshot(path)

    async def run() -> None:
        async with environment(ephemeral_postgres, path.parent) as env:
            await register(env, path)
            await process(env, tmp_path, FakePathValidator(True))
            task = rows(ephemeral_postgres, "download_tasks")[0]
            assert task["status"] == "Failed" and task["error_code"] == reason
            assert rows(ephemeral_postgres, "model_artifacts") == []
            assert snapshot(path) == before

    asyncio.run(run())

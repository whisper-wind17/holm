"""FIX-23: PostgreSQL-backed registration, early Worker gate and command parity."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from fixtures.model_layout import IMAGE_REPOSITORY, TEXT_REPOSITORY, resolved_layout
from holm_contracts import CursorPageParams
from holm_control_api.download_command_routes import (
    create_download_command_router,
    handle_download_command_error,
)
from holm_control_api.download_command_service import (
    DownloadCommand,
    DownloadCommandNotAllowed,
    DownloadCommandService,
    available_commands_for,
)
from holm_control_api.download_creation_routes import create_download_creation_router
from holm_control_api.download_creation_service import DownloadCreationService
from holm_control_api.model_acquisition_read_service import (
    ModelAcquisitionReadService,
    ModelRootAvailability,
)
from holm_control_api.model_registration_service import ModelRegistrationService
from holm_control_api.model_resolution import (
    ModelResolutionRegistry,
    ResolutionRequest,
    ResolutionSucceeded,
)
from holm_domain import DownloadFailureReason, DownloadTaskStatus, ModelOriginType
from holm_download_worker import DownloadResolutionJobHandler, DownloadWorker
from holm_persistence import create_database_engine, create_session_factory
from holm_persistence.model_domain import DOWNLOAD_TASKS
from test_M2_09B_TC_DL_06_download_commands import _seed_task, _task_row
from test_M2_10B_TC_DL_09_10_11_25_artifact_publishing import (
    _components,
    _enqueue,
    _reset,
    _seed_downloading,
    _StaticResolver,
    _WritingDownloader,
)
from test_TC_RES_models_resolve_api import (
    _create_accounts,
    _environment,
    _login,
    _model_domain_snapshot,
    _register,
    _resolve,
    _truncate,
)


@dataclass
class _LayoutAdapter:
    unknown: bool = False
    calls: int = 0

    async def resolve(
        self, request: ResolutionRequest, *, credential: bytes | None
    ) -> ResolutionSucceeded:
        self.calls += 1
        return ResolutionSucceeded(resolved_layout(request.repository_id, unknown=self.unknown))


@pytest.mark.parametrize(
    ("repository", "unknown", "layout"),
    [
        (IMAGE_REPOSITORY, False, "unsupported"),
        (TEXT_REPOSITORY, False, "supported"),
        (IMAGE_REPOSITORY, True, "unknown"),
    ],
)
def test_FIX_23_TC_RES_11_preview_read_only_registration_rechecks(
    ephemeral_postgres: EphemeralPostgres, repository: str, unknown: bool, layout: str
) -> None:
    _reset(ephemeral_postgres.engine)
    _truncate(ephemeral_postgres.engine)
    adapter = _LayoutAdapter(unknown=unknown)

    async def scenario() -> None:
        registry = ModelResolutionRegistry({ModelOriginType.MODELSCOPE: adapter})
        async with _environment(ephemeral_postgres, registry=registry) as env:
            await _create_accounts(env)
            cookies = await _login(env)
            body = {"source": "ModelScope", "repository_id": repository}
            before = _model_domain_snapshot(ephemeral_postgres.engine)
            preview = await _resolve(env, cookies, body)
            assert preview.status == 200
            assert preview.json()["deployable_layout"] == layout
            assert _model_domain_snapshot(ephemeral_postgres.engine) == before
            response = await _register(env, cookies, body, idempotency_key=str(uuid4()))
            assert adapter.calls == 2  # Registration does not trust the earlier preview.
            if layout == "unsupported":
                assert response.status == 422
                assert response.json()["error"] == {
                    "code": "model_layout_unsupported",
                    "message": "该模型类型暂不支持，未加载到模型目录",
                    "details": {},
                    "request_id": dict(response.headers)["x-request-id"],
                }
                assert _model_domain_snapshot(ephemeral_postgres.engine) == before
            else:
                assert response.status == 200, response.text
                assert response.json()["metadata_status"] == "CONFIRMED"

    asyncio.run(scenario())


def test_FIX_23_TC_DL_33_worker_rejects_before_merge_staging_or_download(
    ephemeral_postgres: EphemeralPostgres, tmp_path: Path
) -> None:
    _reset(ephemeral_postgres.engine)
    _truncate(ephemeral_postgres.engine)
    task_id = _seed_downloading(ephemeral_postgres.engine)
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(
            sa.update(DOWNLOAD_TASKS)
            .where(DOWNLOAD_TASKS.c.id == task_id)
            .values(
                status="Queued",
                resolved_revision=None,
                source_type="ModelScope",
                repository_id=IMAGE_REPOSITORY,
                requested_revision_norm="@default",
            )
        )
    model_root = tmp_path / "models"
    downloader = _WritingDownloader({})

    async def scenario() -> None:
        engine, worker, resolver = _components(
            ephemeral_postgres, model_root, resolved_layout(IMAGE_REPOSITORY), downloader
        )
        try:
            factory = create_session_factory(engine)
            await _enqueue(factory, task_id)
            await worker.run_once()
            assert resolver.calls == 1
            assert downloader.calls == 0
            row = _task_row(ephemeral_postgres, task_id)
            assert row["status"] == "Failed"
            assert row["error_code"] == "model_layout_unsupported"
            assert row["downloaded_bytes"] == 0
            assert row["resolved_revision"] is None  # Merge did not run.
            assert row["staging_path"] is None
            assert not (model_root / ".staging" / str(task_id)).exists()
            read = ModelAcquisitionReadService(
                session_factory=factory, storage_availability=ModelRootAvailability(model_root)
            )
            page = await read.list_download_tasks(CursorPageParams())
            item = next(item for item in page.items if item.record.id == task_id)
            assert item.failure_stage is not None and item.failure_stage.value == "Resolving"
            assert item.available_commands == ()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_FIX_23_TC_DL_33_every_failure_code_read_commands_equal_enforced_commands(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _reset(ephemeral_postgres.engine)
    _truncate(ephemeral_postgres.engine)

    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as env:
            service = DownloadCommandService(session_factory=env.session_factory)
            env.app.include_router(create_download_command_router(service))
            env.app.add_exception_handler(DownloadCommandNotAllowed, handle_download_command_error)
            read = ModelAcquisitionReadService(
                session_factory=env.session_factory,
                storage_availability=ModelRootAvailability(Path("/nonexistent-fix23")),
            )
            await _create_accounts(env)
            cookies = await _login(env)
            for reason in DownloadFailureReason:
                for command in DownloadCommand:
                    task_id = _seed_task(ephemeral_postgres, DownloadTaskStatus.FAILED)
                    with ephemeral_postgres.engine.begin() as connection:
                        connection.execute(
                            sa.update(DOWNLOAD_TASKS)
                            .where(DOWNLOAD_TASKS.c.id == task_id)
                            .values(error_code=reason.value)
                        )
                    page = await read.list_download_tasks(CursorPageParams(page_size=100))
                    item = next(item for item in page.items if item.record.id == task_id)
                    deterministic = reason in {
                        DownloadFailureReason.MODEL_LAYOUT_UNSUPPORTED,
                        DownloadFailureReason.ARTIFACT_REQUIRED_FILES_INVALID,
                    }
                    expected = (
                        () if deterministic else (DownloadCommand.RESUME, DownloadCommand.RETRY)
                    )
                    assert item.available_commands == expected
                    assert item.available_commands == available_commands_for(
                        item.record.status, reason.value
                    )
                    before = _task_row(ephemeral_postgres, task_id)
                    response = await env.client.request(
                        "POST", f"/api/v1/downloads/{task_id}/{command.value}", cookies=cookies
                    )
                    if command in expected:
                        assert response.status == 200, response.text
                    else:
                        assert response.status == 409, response.text
                        assert response.json()["error"]["code"] == "download_command_not_allowed"
                        assert _task_row(ephemeral_postgres, task_id) == before

    asyncio.run(scenario())


@pytest.mark.parametrize("unknown", [False, True])
def test_FIX_23_TC_DL_33_supported_and_unknown_listings_reach_merge(
    ephemeral_postgres: EphemeralPostgres, unknown: bool
) -> None:
    _reset(ephemeral_postgres.engine)
    _truncate(ephemeral_postgres.engine)
    task_id = _seed_downloading(ephemeral_postgres.engine)
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(
            sa.update(DOWNLOAD_TASKS)
            .where(DOWNLOAD_TASKS.c.id == task_id)
            .values(
                status="Queued",
                resolved_revision=None,
                source_type="ModelScope",
                repository_id=TEXT_REPOSITORY,
                requested_revision_norm="@default",
            )
        )

    async def scenario() -> None:
        engine = create_database_engine(ephemeral_postgres.engine.url)
        try:
            factory = create_session_factory(engine)
            resolver = _StaticResolver(resolved_layout(TEXT_REPOSITORY, unknown=unknown))
            handler = DownloadResolutionJobHandler(session_factory=factory, resolver=resolver)
            worker = DownloadWorker(
                session_factory=factory,
                handler=handler,
                owner="fix-23-layout",
                build_version="fix-23",
                lease_seconds=5,
                heartbeat_seconds=1,
            )
            await _enqueue(factory, task_id)
            await worker.run_once()
            row = _task_row(ephemeral_postgres, task_id)
            assert resolver.calls == 1
            assert row["status"] == "Downloading"
            assert row["error_code"] is None
            assert row["resolved_revision"] == "master"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_FIX_23_TC_RES_11_download_creation_registration_rejects_without_writes(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _reset(ephemeral_postgres.engine)
    _truncate(ephemeral_postgres.engine)

    async def scenario() -> None:
        registry = ModelResolutionRegistry({ModelOriginType.MODELSCOPE: _LayoutAdapter()})
        async with _environment(ephemeral_postgres, registry=registry) as env:
            registration = ModelRegistrationService(
                session_factory=env.session_factory,
                resolution=env.resolution,
                idempotency_hmac_secret=b"FX-MS-FIX-23-LAYOUT-test-only",
            )
            creation = DownloadCreationService(
                session_factory=env.session_factory,
                registration=registration,
                idempotency_hmac_secret=b"FX-MS-FIX-23-LAYOUT-test-only",
            )
            env.app.include_router(create_download_creation_router(creation))
            await _create_accounts(env)
            cookies = await _login(env)
            before = _model_domain_snapshot(ephemeral_postgres.engine)
            response = await env.client.request(
                "POST",
                "/api/v1/downloads",
                cookies=cookies,
                headers={"Idempotency-Key": str(uuid4())},
                json_body={"source": "ModelScope", "repository_id": IMAGE_REPOSITORY},
            )
            assert response.status == 422, response.text
            assert response.json()["error"]["code"] == "model_layout_unsupported"
            assert _model_domain_snapshot(ephemeral_postgres.engine) == before

    asyncio.run(scenario())

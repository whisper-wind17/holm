"""UI-30 2a / PDD 7.3: a Missing local model can be re-verified (Managed and External).

Reuses the FX-M2-10C tiny real files, PostgreSQL roles and Worker wiring from
the M2-10C re-verification test; no Runtime, GPU or model content is involved.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from holm_control_api.model_acquisition_read_service import ModelAcquisitionReadService
from holm_domain import ArtifactStorageType
from test_M2_10C_artifact_reverification import (
    _clean_accounts as _clean_accounts,
)
from test_M2_10C_artifact_reverification import (
    _cookies,
    _files,
    _role_factory,
    _worker,
)
from test_M2_12A_download_artifact_read_api import ADMIN_USERNAME, _create_accounts, _environment
from test_M7_10A_TC_DL_14_artifact_deletion import _migrated as _migrated

pytestmark = pytest.mark.usefixtures("_migrated")


@pytest.mark.parametrize("storage_type", list(ArtifactStorageType))
def test_UI_30_missing_artifact_is_queued_restored_and_stays_missing_when_absent(
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
                admin = await _cookies(env, ADMIN_USERNAME)
                url = f"/api/v1/artifacts/{artifact_id}:verify"
                worker = _worker(download, storage)

                def job_count() -> int:
                    with ephemeral_postgres.engine.connect() as connection:
                        return int(
                            connection.scalar(
                                sa.text(
                                    "SELECT count(*) FROM core.jobs "
                                    "WHERE job_type='artifact.verify'"
                                )
                            )
                            or 0
                        )

                async def status() -> str:
                    response = await env.client.request("GET", "/api/v1/artifacts", cookies=admin)
                    assert response.status == 200
                    return str(response.json()["items"][0]["status"])

                async def verify_once() -> None:
                    response = await env.client.request("POST", url, cookies=admin)
                    assert response.status == 202
                    assert await worker.run_once() is not None

                # A required file disappears: full verification records Missing.
                weight = path / "model.safetensors"
                weight.unlink()
                await verify_once()
                assert await status() == "Missing"
                queued_before = job_count()

                # Missing is no longer refused with 409 artifact_not_ready: the request queues.
                response = await env.client.request("POST", url, cookies=admin)
                assert response.status == 202
                assert job_count() == queued_before + 1
                # The queued job re-checks the disk; nothing was restored, so it stays Missing.
                assert await worker.run_once() is not None
                assert await status() == "Missing"

                # The file is restored: the same re-verification turns the record Ready.
                weight.write_bytes(b"12345678")
                await verify_once()
                assert await status() == "Ready"
        finally:
            await control_engine.dispose()
            await worker_engine.dispose()

    asyncio.run(scenario())

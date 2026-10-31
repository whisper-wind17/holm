"""FX-FIX-17: schedule outcomes from FIX-17 scenarios."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from holm_domain import ComponentStatus
from holm_maintenance_worker.worker import MaintenanceWorker
from holm_persistence.artifact_check_repository import ArtifactCheckSchedule


@pytest.mark.parametrize(
    ("failed", "expected_status", "expected_reason"),
    [
        (False, ComponentStatus.HEALTHY, None),
        (True, ComponentStatus.DEGRADED, "artifact_check_failed"),
    ],
    ids=["queued-without-failure", "failed-current-check"],
)
def test_FIX_17_pending_check_heartbeat_after_scheduling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed: bool,
    expected_status: ComponentStatus,
    expected_reason: str | None,
) -> None:
    worker = MaintenanceWorker(
        backup_root=tmp_path,
        session_factory=MagicMock(),
        instance_id="FX-FIX-17",
        build_version="test",
    )
    # Isolate artifact coordination after successful backup retention.
    worker._retention_reason = None

    async def run_once() -> None:
        stop = asyncio.Event()

        async def schedule() -> ArtifactCheckSchedule:
            stop.set()
            return ArtifactCheckSchedule(scheduled=0, pending=True, failed=failed)

        scheduler = AsyncMock(side_effect=schedule)
        monkeypatch.setattr(worker, "schedule_artifact_checks", scheduler)
        await worker._artifact_loop(stop)
        scheduler.assert_awaited_once_with()

    asyncio.run(run_once())
    assert worker._status is expected_status
    assert worker._reason == expected_reason

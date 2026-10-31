"""M2-09A Download Worker result dispatch and fail-loud controls."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final
from uuid import uuid4

import pytest
from fixtures.source_mutation import load_mutated_module
from holm_domain.download_merge import (
    DownloadCompleted,
    DownloadMerged,
    DownloadMergeResult,
    DownloadStarted,
)
from holm_download_worker import (
    DownloadResolutionJobHandler,
    DownloadWorkDisposition,
    DownloadWorkerError,
)

ROOT: Final[Path] = Path(__file__).resolve().parents[2]
WORKER_SOURCE: Final[Path] = ROOT / "apps/workers/download/src/holm_download_worker/worker.py"


def _context(task_id: Any) -> Any:
    return SimpleNamespace(job=SimpleNamespace(target_object_id=task_id))


def _assert_three_outcomes(
    dispatch: Callable[[Any, DownloadMergeResult], DownloadWorkDisposition],
) -> None:
    task_id = uuid4()
    artifact_id = uuid4()
    assert dispatch(_context(task_id), DownloadStarted(task_id)).value == "started"
    assert dispatch(_context(task_id), DownloadCompleted(task_id, artifact_id)).value == "completed"
    assert dispatch(_context(task_id), DownloadMerged(task_id)).value == "merged"


def test_M2_09A_worker_dispatches_all_three_resolution_merge_outcomes() -> None:
    _assert_three_outcomes(DownloadResolutionJobHandler._merge_disposition)


def test_M2_09A_positive_control_only_started_merge_outcome_fails_loudly(
    tmp_path: Path,
) -> None:
    mutated = load_mutated_module(
        WORKER_SOURCE,
        [
            (
                "        if isinstance(result, DownloadCompleted):\n"
                "            return DownloadWorkDisposition.COMPLETED\n"
                "        if isinstance(result, DownloadMerged):\n"
                "            return DownloadWorkDisposition.MERGED\n",
                "",
            )
        ],
        module_name="_m2_09a_started_only_worker",
        destination=tmp_path / "started_only_worker.py",
    )
    with pytest.raises(AssertionError):
        _assert_three_outcomes(mutated.DownloadResolutionJobHandler._merge_disposition)


def test_M2_09A_worker_rejects_a_merge_result_for_another_task() -> None:
    expected = uuid4()
    with pytest.raises(
        DownloadWorkerError,
        match="download worker operation failed",
    ) as captured:
        DownloadResolutionJobHandler._merge_disposition(
            _context(expected),
            DownloadStarted(uuid4()),
        )
    assert getattr(captured.value, "code", None) == "download_merge_identity_mismatch"

"""M2-10A unit controls for staging paths, capacity, and progress policy."""

from __future__ import annotations

import ast
import inspect
import os
from pathlib import Path
from typing import Final
from uuid import uuid4

import pytest
from fixtures.source_mutation import load_mutated_module
from holm_common import (
    DOWNLOAD_HEADROOM_BYTES,
    ProgressWriteThrottle,
    StagingFilesystemMismatch,
    StagingLayout,
    StagingPathRejected,
    assert_same_filesystem,
    check_download_capacity,
)
from holm_domain import (
    DOWNLOAD_FAILURE_CATEGORIES,
    DownloadFailureReason,
    ErrorCategory,
)

ROOT: Final[Path] = Path(__file__).resolve().parents[2]
ACQUISITION_SOURCE: Final[Path] = ROOT / "packages/domain/src/holm_domain/model_acquisition.py"


def test_TC_DL_07_known_size_capacity_accepts_exact_boundary_and_rejects_one_less() -> None:
    total = 9 * 1024**3
    exact = total + DOWNLOAD_HEADROOM_BYTES
    assert check_download_capacity(total_bytes=total, free_bytes=exact).accepted is True
    rejected = check_download_capacity(total_bytes=total, free_bytes=exact - 1)
    assert rejected.accepted is False
    assert rejected.additional_bytes_required == 1


def test_TC_DL_07_ten_percent_margin_uses_ceiling_bytes_above_fifty_gib() -> None:
    total = 50 * 1024**3 + 1
    expected_margin = (total * 10 + 99) // 100
    result = check_download_capacity(
        total_bytes=total,
        free_bytes=total + expected_margin,
    )
    assert result.accepted is True
    assert result.required_free_bytes == total + expected_margin


def test_TC_DL_07_staging_path_uses_only_platform_uuid_and_rejects_symlink_escape(
    tmp_path: Path,
) -> None:
    model_root = tmp_path / "models"
    outside = tmp_path / "outside"
    outside.mkdir()
    model_root.mkdir()
    (model_root / ".staging").symlink_to(outside, target_is_directory=True)
    layout = StagingLayout(model_root)

    with pytest.raises(StagingPathRejected):
        layout.prepare(uuid4(), total_bytes=None)


def test_TC_DL_07_source_identifiers_have_no_staging_path_input_channel(tmp_path: Path) -> None:
    layout = StagingLayout((tmp_path / "models").resolve())
    task_id = uuid4()
    path = layout.task_path(task_id)
    hostile_identifiers = ("../escape", "/absolute", "nul\x00suffix", "Ａ/../A")

    assert tuple(inspect.signature(StagingLayout.task_path).parameters) == ("self", "task_id")
    assert path == (layout.staging_root / str(task_id)).resolve()
    assert all(identifier not in str(path) for identifier in hostile_identifiers)


def test_TC_DL_07_task_symlink_cannot_alias_another_staging_directory(tmp_path: Path) -> None:
    layout = StagingLayout((tmp_path / "models").resolve())
    layout.staging_root.mkdir(parents=True)
    first_task = uuid4()
    second_task = uuid4()
    (layout.staging_root / str(second_task)).mkdir()
    (layout.staging_root / str(first_task)).symlink_to(
        layout.staging_root / str(second_task),
        target_is_directory=True,
    )

    with pytest.raises(StagingPathRejected):
        layout.task_path(first_task)


def test_TC_DL_14_parent_symlink_swap_cannot_redirect_cleanup(tmp_path: Path) -> None:
    layout = StagingLayout((tmp_path / "models").resolve())
    task_id = uuid4()
    task_path, _ = layout.prepare(task_id, total_bytes=None)
    displaced_staging = layout.model_root / ".staging-before-swap"
    layout.staging_root.rename(displaced_staging)
    outside = tmp_path / "outside"
    outside_task = outside / str(task_id)
    outside_task.mkdir(parents=True)
    (outside_task / "must-survive").write_bytes(b"fixture")
    layout.staging_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(StagingPathRejected):
        layout.cleanup(task_id, str(task_path))
    assert (outside_task / "must-survive").read_bytes() == b"fixture"


def test_TC_DL_07_same_filesystem_guard_fails_on_real_cross_device_roots(
    tmp_path: Path,
) -> None:
    other_device = Path("/dev/shm")
    assert other_device.is_dir()
    assert os.stat(tmp_path).st_dev != os.stat(other_device).st_dev
    with pytest.raises(StagingFilesystemMismatch):
        assert_same_filesystem(tmp_path, other_device)


def test_TC_DL_08_unknown_total_never_fabricates_a_percentage() -> None:
    throttle = ProgressWriteThrottle(total_bytes=None)
    assert throttle.percentage(0) is None
    assert throttle.percentage(987_654_321) is None


def test_TC_DL_12_progress_writes_on_either_one_percent_or_one_second() -> None:
    throttle = ProgressWriteThrottle(total_bytes=10_000)
    assert throttle.is_due(0, monotonic_now=10.0) is True
    throttle.mark_written(0, monotonic_now=10.0)

    assert throttle.is_due(99, monotonic_now=10.9) is False
    assert throttle.is_due(100, monotonic_now=10.2) is True
    throttle.mark_written(100, monotonic_now=10.2)
    assert throttle.is_due(150, monotonic_now=11.19) is False
    assert throttle.is_due(150, monotonic_now=11.2) is True


def _persisted_reason_assignments_use_error_code(source: str) -> bool:
    tree = ast.parse(source)
    reason_classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name in {"DownloadCancellationReason", "DownloadFailureReason"}
    ]
    assignments = [
        statement
        for class_node in reason_classes
        for statement in class_node.body
        if isinstance(statement, ast.Assign)
    ]
    return bool(assignments) and all(
        isinstance(node.value, ast.Attribute)
        and node.value.attr == "value"
        and isinstance(node.value.value, ast.Attribute)
        and isinstance(node.value.value.value, ast.Name)
        and node.value.value.value.id == "ErrorCode"
        for node in assignments
    )


def test_TC_DL_07_failure_registry_is_exact_and_uses_one_literal_source() -> None:
    assert DOWNLOAD_FAILURE_CATEGORIES == {
        DownloadFailureReason.INSUFFICIENT_STORAGE: ErrorCategory.CAPACITY,
        DownloadFailureReason.DOWNLOAD_TASK_STATE_MISMATCH: ErrorCategory.CONFLICT,
        DownloadFailureReason.DOWNLOAD_TASK_IDENTITY_MISMATCH: ErrorCategory.CONFLICT,
        DownloadFailureReason.DOWNLOAD_PROCESS_FAILED: ErrorCategory.DEPENDENCY,
        DownloadFailureReason.STAGING_FILESYSTEM_MISMATCH: ErrorCategory.DEPENDENCY,
        DownloadFailureReason.STAGING_CLEANUP_FAILED: ErrorCategory.DEPENDENCY,
        DownloadFailureReason.ARTIFACT_FILE_LIST_INVALID: ErrorCategory.VALIDATION,
        DownloadFailureReason.ARTIFACT_MANIFEST_HASH_FAILED: ErrorCategory.VALIDATION,
        DownloadFailureReason.MODEL_LAYOUT_UNSUPPORTED: ErrorCategory.VALIDATION,
        DownloadFailureReason.ARTIFACT_REQUIRED_FILES_INVALID: ErrorCategory.VALIDATION,
        DownloadFailureReason.ARTIFACT_SAFETENSORS_INVALID: ErrorCategory.VALIDATION,
        DownloadFailureReason.ARTIFACT_RUNTIME_INCOMPATIBLE: ErrorCategory.VALIDATION,
        DownloadFailureReason.REMOTE_CODE_NOT_ALLOWED: ErrorCategory.VALIDATION,
        DownloadFailureReason.ARTIFACT_UNSAFE_FILE: ErrorCategory.VALIDATION,
        DownloadFailureReason.EXTERNAL_PATH_UNREADABLE: ErrorCategory.VALIDATION,
        DownloadFailureReason.ARTIFACT_PUBLISH_FAILED: ErrorCategory.DEPENDENCY,
    }
    source = ACQUISITION_SOURCE.read_text(encoding="utf-8")
    assert _persisted_reason_assignments_use_error_code(source) is True


def test_TC_DL_07_positive_control_unregistered_failure_reason_fails_loudly(
    tmp_path: Path,
) -> None:
    marker = "    STAGING_CLEANUP_FAILED = ErrorCode.STAGING_CLEANUP_FAILED.value"
    with pytest.raises(AssertionError):
        load_mutated_module(
            ACQUISITION_SOURCE,
            [(marker, marker + "\n    UNREGISTERED = ErrorCode.MODEL_NOT_FOUND.value")],
            module_name="_m2_10a_unregistered_download_failure",
            destination=tmp_path / "unregistered_download_failure.py",
        )


@pytest.mark.parametrize(
    "code_name",
    [
        "DUPLICATE_RESOLVED_REVISION",
        "INSUFFICIENT_STORAGE",
        "DOWNLOAD_TASK_STATE_MISMATCH",
        "DOWNLOAD_TASK_IDENTITY_MISMATCH",
        "DOWNLOAD_PROCESS_FAILED",
        "STAGING_FILESYSTEM_MISMATCH",
        "STAGING_CLEANUP_FAILED",
    ],
)
def test_TC_DL_07_positive_control_bare_persisted_reason_literal_is_detected(
    code_name: str,
) -> None:
    source = ACQUISITION_SOURCE.read_text(encoding="utf-8")
    mutated = source.replace(
        f"ErrorCode.{code_name}.value",
        f'"{code_name.lower()}"',
        1,
    )
    assert _persisted_reason_assignments_use_error_code(mutated) is False

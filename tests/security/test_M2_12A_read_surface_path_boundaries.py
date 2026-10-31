"""M2-12A (``TC-DL-17`` scan shape): paths stop at the response body.

PDD 9.3 puts the local path and the temporary file *in* these two tables, so the
response body is the one place they belong.  The four sinks named by
``TC-DL-17`` -- structured log, audit detail, exception text and Metric label --
must stay clean, and the mechanism that keeps them clean is checked here rather
than assumed: the two record dataclasses redact their path in ``repr``, which is
what a log record and a chained exception would otherwise print.
"""

from __future__ import annotations

import ast
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from uuid import UUID

import pytest
from fixtures.source_mutation import load_mutated_module
from holm_control_api.model_acquisition_read_routes import (
    DownloadTaskItemResponse,
    ModelArtifactItemResponse,
)
from holm_control_api.model_acquisition_read_service import (
    DownloadTaskItem,
    ModelArtifactItem,
    ModelRootAvailability,
    available_commands_for,
)
from holm_domain import (
    ArtifactStorageType,
    DownloadTaskStatus,
    ModelArtifactStatus,
    ModelOriginType,
    download_error_stage,
)
from holm_persistence import DownloadTaskListRecord, ModelArtifactListRecord

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
ROUTES_SOURCE: Final[Path] = REPOSITORY_ROOT / (
    "apps/control_api/src/holm_control_api/model_acquisition_read_routes.py"
)
SERVICE_SOURCE: Final[Path] = REPOSITORY_ROOT / (
    "apps/control_api/src/holm_control_api/model_acquisition_read_service.py"
)
READ_REPOSITORY_SOURCE: Final[Path] = REPOSITORY_ROOT / (
    "packages/persistence/src/holm_persistence/model_acquisition_read_repository.py"
)
STAGING_SENTINEL: Final[str] = "/srv/STAGING_PATH_SENTINEL_M2_12A/partial"
ARTIFACT_SENTINEL: Final[str] = "/srv/ARTIFACT_PATH_SENTINEL_M2_12A/managed"
MOMENT: Final[datetime] = datetime(2026, 9, 10, 8, 0, tzinfo=UTC)
ROW_ID: Final[UUID] = UUID("70000000-0000-4000-8000-00000000000b")


def _download_item() -> DownloadTaskItem:
    record = DownloadTaskListRecord(
        id=ROW_ID,
        model_id=ROW_ID,
        display_name="path-boundary-model",
        source_type=ModelOriginType.HUGGING_FACE,
        repository_id="owner/path-boundary",
        resolved_revision="a" * 40,
        status=DownloadTaskStatus.DOWNLOADING,
        attempt=1,
        downloaded_bytes=1,
        total_bytes=2,
        staging_path=STAGING_SENTINEL,
        error_code=None,
        created_at=MOMENT,
        updated_at=MOMENT,
        completed_at=None,
    )
    return DownloadTaskItem(
        record=record,
        failure_stage=download_error_stage(record.error_code),
        available_commands=available_commands_for(record.status),
    )


def _artifact_item() -> ModelArtifactItem:
    return ModelArtifactItem(
        record=ModelArtifactListRecord(
            id=ROW_ID,
            model_id=ROW_ID,
            display_name="path-boundary-artifact",
            source_type=ModelOriginType.HUGGING_FACE,
            repository_id="owner/path-boundary",
            resolved_revision="b" * 40,
            status=ModelArtifactStatus.READY,
            storage_type=ArtifactStorageType.MANAGED,
            size_bytes=1,
            path=ARTIFACT_SENTINEL,
            created_at=MOMENT,
            updated_at=MOMENT,
        ),
        referencing_instances=None,
    )


def _assert_paths_reach_only_the_body(
    routes_module: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Serialize both rows, then scan every sink other than the response body."""

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        task = routes_module.DownloadTaskItemResponse.from_item(_download_item())
        artifact = routes_module.ModelArtifactItemResponse.from_item(_artifact_item())
        body = f"{task.model_dump_json()}{artifact.model_dump_json()}"
    item = _download_item()
    sinks = (
        caplog.text,
        f"{item.record}{item.record!r}",
        f"{_artifact_item().record}{_artifact_item().record!r}",
        f"{item}{item!r}",
    )
    for sentinel in (STAGING_SENTINEL, ARTIFACT_SENTINEL):
        assert sentinel in body, "the path belongs in the response body (PDD 9.3)"
        for sink in sinks:
            assert sentinel not in sink, "a server path escaped into a non-body sink"


def test_M2_12A_ruling_9_TC_DL_17_paths_reach_the_body_and_no_other_sink(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import holm_control_api.model_acquisition_read_routes as routes

    _assert_paths_reach_only_the_body(routes, caplog)


def test_M2_12A_ruling_9_positive_control_a_logged_path_is_detected(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    mutated = load_mutated_module(
        ROUTES_SOURCE,
        [
            (
                "    @classmethod\n"
                "    def from_item(cls, item: DownloadTaskItem) -> DownloadTaskItemResponse:\n"
                "        record = item.record\n",
                "    @classmethod\n"
                "    def from_item(cls, item: DownloadTaskItem) -> DownloadTaskItemResponse:\n"
                "        record = item.record\n"
                "        __import__('logging').getLogger('m2-12a-leak')"
                ".error(record.staging_path)\n",
            )
        ],
        module_name="_mutated_m2_12a_routes_logged_path",
        destination=tmp_path / "mutated_routes_logged_path.py",
    )
    with pytest.raises(AssertionError, match="escaped into a non-body sink"):
        _assert_paths_reach_only_the_body(mutated, caplog)


def test_M2_12A_ruling_9_positive_control_an_unredacted_repr_is_detected(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Delete the redacting ``__repr__`` and the same scan must catch it."""

    mutated = load_mutated_module(
        READ_REPOSITORY_SOURCE,
        [
            (
                '            "staging_path=<redacted>)"',
                '            f"staging_path={self.staging_path!r})"',
            )
        ],
        module_name="_mutated_m2_12a_repository_repr",
        destination=tmp_path / "mutated_read_repository_repr.py",
    )
    leaked = mutated.DownloadTaskListRecord(
        id=ROW_ID,
        model_id=ROW_ID,
        display_name="path-boundary-model",
        source_type=ModelOriginType.HUGGING_FACE,
        repository_id="owner/path-boundary",
        resolved_revision="a" * 40,
        status=DownloadTaskStatus.DOWNLOADING,
        attempt=1,
        downloaded_bytes=1,
        total_bytes=2,
        staging_path=STAGING_SENTINEL,
        error_code=None,
        created_at=MOMENT,
        updated_at=MOMENT,
        completed_at=None,
    )
    assert STAGING_SENTINEL in f"{leaked!r}"
    assert STAGING_SENTINEL not in f"{_download_item().record!r}"


def test_M2_12A_ruling_9_no_metric_label_or_logger_touches_the_read_surface() -> None:
    """The fourth sink cannot leak a path if the surface emits nothing at all."""

    for source in (ROUTES_SOURCE, SERVICE_SOURCE, READ_REPOSITORY_SOURCE):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        } | {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert not (
            called
            & {
                "getLogger",
                "debug",
                "info",
                "warning",
                "error",
                "exception",
                "labels",
                "observe",
                "inc",
            }
        ), f"{source.name} emits to a sink that could carry a path"
        assert "logging" not in {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }


def test_M2_12A_ruling_9_free_space_probe_never_renders_its_path() -> None:
    probe = ModelRootAvailability(Path("/srv/PROBE_PATH_SENTINEL_M2_12A"))
    assert "PROBE_PATH_SENTINEL_M2_12A" not in f"{probe}{probe!r}"


def test_M2_12A_no_credential_bearing_name_exists_on_either_response() -> None:
    """Secrets are not in these tables; this pins that rather than assuming it."""

    forbidden = {"credential", "credential_key_id", "secret", "token", "password", "pepper"}
    for model in (DownloadTaskItemResponse, ModelArtifactItemResponse):
        for field in model.model_fields:
            assert not any(word in field for word in forbidden)

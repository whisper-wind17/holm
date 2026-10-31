"""M2-12A contracts for the two read-only acquisition endpoints.

Inherited judgements: ``TC-UI-08`` (three missing-value semantics must stay
distinguishable, and ``0`` may not stand in for a failed collection),
``TC-DL-08`` (an unknown total size shows indeterminate progress, never a
fabricated percentage) and ``TC-UI-21`` (an unrecognized value rejects the
response instead of being passed through).

Every guard here is exercised twice: once against the production source, and
once against a mutated copy of that same source, so a guard that stopped
detecting anything would itself fail.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Final
from uuid import UUID

import pytest
from fixtures.source_mutation import load_mutated_module, mutate_source
from holm_contracts import CursorPage, CursorPageParams
from holm_control_api.download_command_service import (
    DOWNLOAD_COMMAND_MATRIX,
    DownloadCommand,
)
from holm_control_api.model_acquisition_read_routes import (
    ArtifactPageParams,
    DownloadTaskItemResponse,
    ModelArtifactItemResponse,
    ModelArtifactPageResponse,
    ModelArtifactSummaryResponse,
)
from holm_control_api.model_acquisition_read_service import (
    ARTIFACTS_CURSOR_KIND,
    DOWNLOADS_CURSOR_KIND,
    DownloadTaskItem,
    ModelAcquisitionCursorInvalid,
    ModelArtifactItem,
    ModelArtifactSummary,
    ModelRootAvailability,
    available_commands_for,
    decode_cursor,
    encode_cursor,
)
from holm_domain import (
    DOWNLOAD_ERROR_STAGES,
    ArtifactStorageType,
    DownloadCancellationReason,
    DownloadFailureReason,
    DownloadFailureStage,
    DownloadTaskStatus,
    ModelArtifactStatus,
    ModelOriginType,
    UnknownDownloadErrorCode,
    assert_download_error_stage_registry_complete,
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
DOMAIN_SOURCE: Final[Path] = REPOSITORY_ROOT / (
    "packages/domain/src/holm_domain/model_acquisition.py"
)
COMMAND_SOURCE: Final[Path] = REPOSITORY_ROOT / (
    "apps/control_api/src/holm_control_api/download_command_service.py"
)
READ_SURFACE_SOURCES: Final[tuple[Path, ...]] = (ROUTES_SOURCE, SERVICE_SOURCE)

DOWNLOAD_RESPONSE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "id",
        "model_id",
        "display_name",
        "source",
        "repository_id",
        "revision",
        "status",
        "attempt",
        "downloaded_bytes",
        "total_bytes",
        "staging_path",
        "error_code",
        "failure_stage",
        "available_commands",
        "created_at",
        "updated_at",
        "completed_at",
    }
)
ARTIFACT_RESPONSE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "id",
        "model_id",
        "display_name",
        "source",
        "repository_id",
        "resolved_revision",
        "status",
        "storage_type",
        "size_bytes",
        "path",
        "referencing_instances",
        "last_verified_at",
        "created_at",
        "updated_at",
    }
)
ARTIFACT_FILTER_FIELDS: Final[frozenset[str]] = frozenset(
    {"search", "source", "status", "storage_type"}
)
RENDERED_DASH: Final[str] = "—"
MOMENT: Final[datetime] = datetime(2026, 9, 10, 8, 0, tzinfo=UTC)
ROW_ID: Final[UUID] = UUID("40000000-0000-4000-8000-00000000000a")


def _download_record(
    *,
    status: DownloadTaskStatus = DownloadTaskStatus.QUEUED,
    resolved_revision: str | None = None,
    total_bytes: int | None = None,
    staging_path: str | None = None,
    error_code: str | None = None,
) -> DownloadTaskListRecord:
    return DownloadTaskListRecord(
        id=ROW_ID,
        model_id=ROW_ID,
        display_name="unknown-supply-model",
        source_type=ModelOriginType.HUGGING_FACE,
        repository_id="owner/unknown-supply",
        resolved_revision=resolved_revision,
        status=status,
        attempt=1,
        downloaded_bytes=0,
        total_bytes=total_bytes,
        staging_path=staging_path,
        error_code=error_code,
        created_at=MOMENT,
        updated_at=MOMENT,
        completed_at=None,
    )


def _artifact_record() -> ModelArtifactListRecord:
    return ModelArtifactListRecord(
        id=ROW_ID,
        model_id=ROW_ID,
        display_name="unknown-supply-artifact",
        source_type=ModelOriginType.HUGGING_FACE,
        repository_id="owner/unknown-supply",
        resolved_revision="a" * 40,
        status=ModelArtifactStatus.READY,
        storage_type=ArtifactStorageType.MANAGED,
        size_bytes=1024,
        path="/var/lib/holm/models/managed/artifact",
        created_at=MOMENT,
        updated_at=MOMENT,
    )


def _download_item() -> DownloadTaskItem:
    record = _download_record()
    return DownloadTaskItem(
        record=record,
        failure_stage=download_error_stage(record.error_code),
        available_commands=available_commands_for(record.status),
    )


def _artifact_item() -> ModelArtifactItem:
    return ModelArtifactItem(record=_artifact_record(), referencing_instances=None)


def _unknown_summary() -> ModelArtifactSummary:
    return ModelArtifactSummary(
        managed_bytes=0,
        storage_available_bytes=None,
        active_download_tasks=0,
    )


# --------------------------------------------------------------------------
# absent supply is JSON ``null``, never ``"—"`` and never ``0``
# --------------------------------------------------------------------------


def _assert_absent_supply_is_null(routes_module: Any) -> None:
    """Judged on the response value, not on anything a client renders."""

    task = routes_module.DownloadTaskItemResponse.from_item(_download_item()).model_dump(
        mode="json"
    )
    for field in ("revision", "total_bytes", "staging_path", "error_code", "failure_stage"):
        assert task[field] is None, f"{field} must be null, not {task[field]!r}"
    artifact = routes_module.ModelArtifactItemResponse.from_item(_artifact_item()).model_dump(
        mode="json"
    )
    assert artifact["referencing_instances"] is None
    summary = routes_module.ModelArtifactSummaryResponse.from_summary(
        _unknown_summary()
    ).model_dump(mode="json")
    assert summary["storage_available_bytes"] is None
    rendered = f"{task}\n{artifact}\n{summary}"
    assert RENDERED_DASH not in rendered


def test_M2_12A_ruling_2_3_TC_UI_08_absent_supply_serializes_as_null() -> None:
    import holm_control_api.model_acquisition_read_routes as routes

    _assert_absent_supply_is_null(routes)
    for source in READ_SURFACE_SOURCES:
        # Skip the module docstring: it names the rejected precedent on purpose.
        body = source.read_text(encoding="utf-8").split('"""', 2)[-1]
        assert "REVISION_UNAVAILABLE" not in body
        assert RENDERED_DASH not in body, "a display dash leaked into the read surface"


def test_M2_12A_ruling_2_positive_control_dash_instead_of_null_fails_loudly(
    tmp_path: Path,
) -> None:
    mutated = load_mutated_module(
        ROUTES_SOURCE,
        [
            (
                "            revision=None\n"
                "            if record.source_type is ModelOriginType.LOCAL_PATH\n"
                "            else record.resolved_revision,",
                '            revision=record.resolved_revision or "—",',
            )
        ],
        module_name="_mutated_m2_12a_routes_dash_revision",
        destination=tmp_path / "mutated_routes_dash_revision.py",
    )
    with pytest.raises(AssertionError):
        _assert_absent_supply_is_null(mutated)


def test_M2_12A_ruling_3_positive_control_zero_reference_count_fails_loudly(
    tmp_path: Path,
) -> None:
    mutated = load_mutated_module(
        ROUTES_SOURCE,
        [
            (
                "            referencing_instances=item.referencing_instances,",
                "            referencing_instances=item.referencing_instances or 0,",
            )
        ],
        module_name="_mutated_m2_12a_routes_zero_references",
        destination=tmp_path / "mutated_routes_zero_references.py",
    )
    with pytest.raises(AssertionError):
        _assert_absent_supply_is_null(mutated)


def test_M2_12A_ruling_3_positive_control_null_downgraded_to_zero_fails_loudly(
    tmp_path: Path,
) -> None:
    mutated = load_mutated_module(
        ROUTES_SOURCE,
        [
            (
                "    storage_available_bytes: Annotated[int, Field(ge=0)] | None",
                "    storage_available_bytes: Annotated[int, Field(ge=0)] = 0",
            ),
            (
                "            storage_available_bytes=summary.storage_available_bytes,",
                "            storage_available_bytes=summary.storage_available_bytes or 0,",
            ),
        ],
        module_name="_mutated_m2_12a_routes_zero_free_space",
        destination=tmp_path / "mutated_routes_zero_free_space.py",
    )
    with pytest.raises(AssertionError):
        _assert_absent_supply_is_null(mutated)


def _assert_missing_model_root_is_unknown(probe: Any) -> None:
    import asyncio

    assert asyncio.run(probe.available_bytes()) is None


def test_M2_12A_ruling_3_TC_UI_08_missing_model_root_is_unknown_not_zero(
    tmp_path: Path,
) -> None:
    # The development container runs as root, which bypasses POSIX mode bits, so
    # the unreadable branch is produced with a path that does not exist.
    _assert_missing_model_root_is_unknown(ModelRootAvailability(tmp_path / "absent-model-root"))


def test_M2_12A_ruling_3_positive_control_zero_free_space_probe_fails_loudly(
    tmp_path: Path,
) -> None:
    mutated = load_mutated_module(
        SERVICE_SOURCE,
        [
            (
                "        if snapshot is None:\n",
                "        if snapshot is None:\n"
                "            return 0\n"
                "        if snapshot is None:\n",
            )
        ],
        module_name="_mutated_m2_12a_service_zero_free_space",
        destination=tmp_path / "mutated_service_zero_free_space.py",
    )
    with pytest.raises(AssertionError):
        _assert_missing_model_root_is_unknown(
            mutated.ModelRootAvailability(tmp_path / "absent-model-root")
        )


# --------------------------------------------------------------------------
# available_commands is derived from DOWNLOAD_COMMAND_MATRIX
# --------------------------------------------------------------------------


def _enum_members_referenced(source: str, enum_name: str) -> set[str]:
    return {
        node.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == enum_name
        and node.attr.isupper()
    }


def _assert_no_second_state_table(sources: tuple[str, ...]) -> None:
    """No restated status vocabulary anywhere on the read surface.

    Value equality with the matrix would not settle this: a hand-copied table
    that happens to agree today is the failure mode to prevent, and
    it stays green on both sides until one of them changes.
    """

    for source in sources:
        for enum_name in ("DownloadCommand", "DownloadTaskStatus", "ModelArtifactStatus"):
            referenced = _enum_members_referenced(source, enum_name)
            assert not referenced, f"the read surface restated {enum_name}: {sorted(referenced)}"


def test_M2_12A_ruling_4_7_available_commands_are_derived_not_restated() -> None:
    sources = tuple(path.read_text(encoding="utf-8") for path in READ_SURFACE_SOURCES)
    _assert_no_second_state_table(sources)
    signature = inspect.signature(available_commands_for)
    assert signature.parameters["matrix"].default is DOWNLOAD_COMMAND_MATRIX
    for (status, error_code), commands in DOWNLOAD_COMMAND_MATRIX.items():
        assert available_commands_for(status, error_code) == tuple(
            sorted(commands, key=lambda command: command.value)
        )


def test_M2_12A_ruling_4_positive_control_restated_command_table_fails_loudly() -> None:
    mutated = mutate_source(
        ROUTES_SOURCE,
        [
            (
                "            available_commands=item.available_commands,",
                "            available_commands=(DownloadCommand.CANCEL,),",
            )
        ],
    )
    with pytest.raises(AssertionError, match="restated DownloadCommand"):
        _assert_no_second_state_table((mutated,))


def test_M2_12A_ruling_4_positive_control_matrix_cell_change_reaches_the_response(
    tmp_path: Path,
) -> None:
    """Change one matrix cell; the projected command set must change with it."""

    mutated = load_mutated_module(
        COMMAND_SOURCE,
        [
            (
                "        (DownloadTaskStatus.PUBLISHING, None): frozenset(),",
                "        (DownloadTaskStatus.PUBLISHING, None): "
                "frozenset({DownloadCommand.CANCEL}),",
            )
        ],
        module_name="_mutated_m2_12a_command_matrix",
        destination=tmp_path / "mutated_download_command_service.py",
    )
    assert available_commands_for(DownloadTaskStatus.PUBLISHING) == ()
    projected = available_commands_for(
        DownloadTaskStatus.PUBLISHING,
        matrix=mutated.DOWNLOAD_COMMAND_MATRIX,
    )
    assert [command.value for command in projected] == [DownloadCommand.CANCEL.value]


# --------------------------------------------------------------------------
# the failure stage table covers every persisted error code
# --------------------------------------------------------------------------


def test_M2_12A_ruling_5_stage_table_covers_every_persisted_error_code() -> None:
    assert_download_error_stage_registry_complete()
    assert {member.value for member in DOWNLOAD_ERROR_STAGES} == {
        member.value for member in DownloadFailureReason
    } | {member.value for member in DownloadCancellationReason}
    assert all(isinstance(stage, DownloadFailureStage) for stage in DOWNLOAD_ERROR_STAGES.values())
    assert download_error_stage(None) is None
    assert (
        download_error_stage(DownloadFailureReason.ARTIFACT_PUBLISH_FAILED.value)
        is DownloadFailureStage.PUBLISHING
    )
    assert (
        download_error_stage(DownloadCancellationReason.DUPLICATE_RESOLVED_REVISION.value)
        is DownloadFailureStage.RESOLVING
    )


def test_M2_12A_ruling_5_positive_control_unclassified_reason_fails_loudly() -> None:
    class ShadowFailureReason(StrEnum):
        SHADOW_UNCLASSIFIED = "shadow_unclassified"

    with pytest.raises(AssertionError, match="stage registry drift"):
        assert_download_error_stage_registry_complete(
            reasons=ShadowFailureReason,
        )


def test_M2_12A_ruling_5_positive_control_new_member_fails_at_import(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="stage registry drift"):
        load_mutated_module(
            DOMAIN_SOURCE,
            [
                (
                    "class DownloadCancellationReason(StrEnum):\n"
                    "    DUPLICATE_RESOLVED_REVISION = "
                    "ErrorCode.DUPLICATE_RESOLVED_REVISION.value",
                    "class DownloadCancellationReason(StrEnum):\n"
                    "    DUPLICATE_RESOLVED_REVISION = "
                    "ErrorCode.DUPLICATE_RESOLVED_REVISION.value\n"
                    '    SHADOW_CANCELLATION = "shadow_cancellation"',
                )
            ],
            module_name="_mutated_m2_12a_domain_stage_drift",
            destination=tmp_path / "mutated_model_acquisition.py",
        )


UNCLASSIFIED_CODE: Final[str] = "UNCLASSIFIED_ERROR_CODE_SENTINEL"


def _assert_unknown_code_is_rejected(classifier: Any) -> None:
    raised: BaseException | None = None
    try:
        classifier(UNCLASSIFIED_CODE)
    except LookupError as error:
        raised = error
    assert raised is not None, "an unclassified error code was passed through"
    assert UNCLASSIFIED_CODE not in _exception_chain_text(raised)


def _exception_chain_text(error: BaseException) -> str:
    rendered: list[str] = []
    current: BaseException | None = error
    while current is not None:
        rendered.append(f"{current}{current!r}")
        current = current.__cause__ or current.__context__
    return "\n".join(rendered)


def test_M2_12A_ruling_5_TC_UI_21_unknown_persisted_code_is_rejected_not_passed_through() -> None:
    _assert_unknown_code_is_rejected(download_error_stage)
    with pytest.raises(UnknownDownloadErrorCode):
        download_error_stage(UNCLASSIFIED_CODE)


def test_M2_12A_ruling_5_positive_control_pass_through_of_unknown_code_fails_loudly(
    tmp_path: Path,
) -> None:
    mutated = load_mutated_module(
        DOMAIN_SOURCE,
        [("    raise UnknownDownloadErrorCode()", "    return None")],
        module_name="_mutated_m2_12a_domain_pass_through",
        destination=tmp_path / "mutated_model_acquisition_pass_through.py",
    )
    with pytest.raises(AssertionError, match="passed through"):
        _assert_unknown_code_is_rejected(mutated.download_error_stage)


# --------------------------------------------------------------------------
# no speed, no ETA and no fabricated percentage
# --------------------------------------------------------------------------


def _assert_no_fabricated_rate(routes_module: Any, sources: tuple[str, ...]) -> None:
    fields = frozenset(routes_module.DownloadTaskItemResponse.model_fields)
    assert fields == DOWNLOAD_RESPONSE_FIELDS, f"unexpected fields: {sorted(fields)}"
    body = routes_module.DownloadTaskItemResponse.from_item(_download_item()).model_dump(
        mode="json"
    )
    assert body["total_bytes"] is None
    assert set(body) == set(DOWNLOAD_RESPONSE_FIELDS)
    tracked = {"downloaded_bytes", "total_bytes", "created_at", "updated_at"}
    for source in sources:
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.BinOp) or not isinstance(node.op, (ast.Div, ast.Sub)):
                continue
            names = {
                inner.attr if isinstance(inner, ast.Attribute) else inner.id
                for inner in ast.walk(node)
                if isinstance(inner, (ast.Attribute, ast.Name))
            }
            assert not (names & tracked), f"the read surface derived a rate from {names & tracked}"


def test_M2_12A_ruling_6_TC_DL_08_no_speed_eta_or_percentage_is_produced() -> None:
    import holm_control_api.model_acquisition_read_routes as routes

    _assert_no_fabricated_rate(
        routes,
        tuple(path.read_text(encoding="utf-8") for path in READ_SURFACE_SOURCES),
    )


def test_M2_12A_ruling_6_positive_control_zero_progress_field_fails_loudly(
    tmp_path: Path,
) -> None:
    mutated = load_mutated_module(
        ROUTES_SOURCE,
        [
            (
                "    total_bytes: Annotated[int, Field(ge=0)] | None\n    staging_path: str | None",
                "    total_bytes: Annotated[int, Field(ge=0)] | None\n"
                "    progress: int = 0\n"
                "    staging_path: str | None",
            )
        ],
        module_name="_mutated_m2_12a_routes_progress",
        destination=tmp_path / "mutated_routes_progress.py",
    )
    with pytest.raises(AssertionError):
        _assert_no_fabricated_rate(mutated, ())


def test_M2_12A_ruling_6_positive_control_average_rate_formula_fails_loudly() -> None:
    mutated = mutate_source(
        SERVICE_SOURCE,
        [
            (
                "    record: DownloadTaskListRecord\n"
                "    failure_stage: DownloadFailureStage | None\n"
                "    available_commands: tuple[DownloadCommand, ...]",
                "    record: DownloadTaskListRecord\n"
                "    failure_stage: DownloadFailureStage | None\n"
                "    available_commands: tuple[DownloadCommand, ...]\n"
                "    rate = lambda record: record.downloaded_bytes / 60",
            )
        ],
    )
    import holm_control_api.model_acquisition_read_routes as routes

    with pytest.raises(AssertionError, match="derived a rate"):
        _assert_no_fabricated_rate(routes, (mutated,))


# --------------------------------------------------------------------------
# the existing pagination contract, extended only by filters
# --------------------------------------------------------------------------


def _assert_pagination_contract_is_inherited(routes_module: Any, sources: tuple[str, ...]) -> None:
    params = routes_module.ArtifactPageParams
    assert issubclass(params, CursorPageParams)
    assert frozenset(params.model_fields) - frozenset(CursorPageParams.model_fields) == (
        ARTIFACT_FILTER_FIELDS
    )
    for field in CursorPageParams.model_fields:
        assert params.model_fields[field].annotation is (
            CursorPageParams.model_fields[field].annotation
        )
    page = routes_module.ModelArtifactPageResponse
    assert issubclass(page, CursorPage)
    assert frozenset(page.model_fields) == frozenset({"items", "next_cursor", "summary"})
    for source in sources:
        tree = ast.parse(source)
        identifiers = (
            {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
            | {node.arg for node in ast.walk(tree) if isinstance(node, ast.arg)}
            | {
                node.target.id
                for node in ast.walk(tree)
                if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
            }
        )
        assert not (identifiers & {"limit", "offset", "per_page"})


def test_M2_12A_ruling_8_pagination_reuses_the_single_contract() -> None:
    import holm_control_api.model_acquisition_read_routes as routes

    _assert_pagination_contract_is_inherited(
        routes,
        tuple(path.read_text(encoding="utf-8") for path in READ_SURFACE_SOURCES),
    )
    assert frozenset(ArtifactPageParams.model_fields) > frozenset(CursorPageParams.model_fields)
    assert frozenset(ModelArtifactPageResponse.model_fields) == frozenset(
        {"items", "next_cursor", "summary"}
    )
    assert frozenset(DownloadTaskItemResponse.model_fields) == DOWNLOAD_RESPONSE_FIELDS
    assert frozenset(ModelArtifactItemResponse.model_fields) == ARTIFACT_RESPONSE_FIELDS
    assert frozenset(ModelArtifactSummaryResponse.model_fields) == frozenset(
        {"managed_bytes", "storage_available_bytes", "active_download_tasks"}
    )


def test_M2_12A_ruling_8_positive_control_second_pagination_parameter_fails_loudly(
    tmp_path: Path,
) -> None:
    mutated = load_mutated_module(
        ROUTES_SOURCE,
        [
            (
                "    search: Annotated[str, Field(min_length=1, max_length=200)] | None = None",
                "    limit: int | None = None\n"
                "    search: Annotated[str, Field(min_length=1, max_length=200)] | None = None",
            )
        ],
        module_name="_mutated_m2_12a_routes_second_pagination",
        destination=tmp_path / "mutated_routes_second_pagination.py",
    )
    mutated_text = (tmp_path / "mutated_routes_second_pagination.py").read_text(encoding="utf-8")
    with pytest.raises(AssertionError):
        _assert_pagination_contract_is_inherited(mutated, (mutated_text,))


@pytest.mark.parametrize(
    "foreign",
    [
        "",
        "not-a-cursor",
        "40000000-0000-4000-8000-00000000000a",
        "eyJ2IjoxfQ",
    ],
)
def test_M2_12A_ruling_8_foreign_cursor_is_rejected_not_silently_reset(foreign: str) -> None:
    with pytest.raises(ModelAcquisitionCursorInvalid):
        decode_cursor(DOWNLOADS_CURSOR_KIND, foreign)


def test_M2_12A_ruling_8_a_cursor_from_the_other_list_is_rejected() -> None:
    issued = encode_cursor(ARTIFACTS_CURSOR_KIND, created_at=MOMENT, row_id=ROW_ID)
    assert decode_cursor(ARTIFACTS_CURSOR_KIND, issued).id == ROW_ID
    with pytest.raises(ModelAcquisitionCursorInvalid):
        decode_cursor(DOWNLOADS_CURSOR_KIND, issued)


def test_M2_12A_ruling_8_rejected_cursor_never_survives_in_the_exception_chain() -> None:
    sentinel = "CURSOR_SENTINEL_/srv/models/private"
    with pytest.raises(ModelAcquisitionCursorInvalid) as failure:
        decode_cursor(DOWNLOADS_CURSOR_KIND, sentinel)
    rendered: list[str] = []
    current: BaseException | None = failure.value
    while current is not None:
        rendered.append(f"{current}{current!r}")
        current = current.__cause__ or current.__context__
    assert sentinel not in "\n".join(rendered)


def test_M2_12A_ruling_8_positive_control_untagged_cursor_would_be_accepted(
    tmp_path: Path,
) -> None:
    """Drop the endpoint tag and the other list's cursor is silently accepted."""

    issued = encode_cursor(ARTIFACTS_CURSOR_KIND, created_at=MOMENT, row_id=ROW_ID)
    mutated = load_mutated_module(
        SERVICE_SOURCE,
        [
            (
                '    if decoded["v"] != CURSOR_VERSION or decoded["k"] != kind:',
                '    if decoded["v"] != CURSOR_VERSION:',
            )
        ],
        module_name="_mutated_m2_12a_service_untagged_cursor",
        destination=tmp_path / "mutated_service_untagged_cursor.py",
    )
    assert mutated.decode_cursor(mutated.DOWNLOADS_CURSOR_KIND, issued).id == ROW_ID
    with pytest.raises(ModelAcquisitionCursorInvalid):
        decode_cursor(DOWNLOADS_CURSOR_KIND, issued)


# --------------------------------------------------------------------------
# M2-10C: server defines latest completed verification, publication fallback
# --------------------------------------------------------------------------


def _assert_verification_time_is_defined_on_the_server(source: str) -> None:
    builders = [
        ast.unparse(node)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "from_item"
    ]
    artifact_builders = [text for text in builders if "last_verified_at" in text]
    assert len(artifact_builders) == 1
    assert "last_verified_at=item.last_verified_at or record.created_at" in artifact_builders[0]


def test_M2_12A_ruling_10_last_verified_at_is_derived_on_the_server() -> None:
    _assert_verification_time_is_defined_on_the_server(ROUTES_SOURCE.read_text(encoding="utf-8"))
    item = ModelArtifactItemResponse.from_item(_artifact_item()).model_dump(mode="json")
    assert item["last_verified_at"] == item["created_at"]
    verified_at = MOMENT + timedelta(hours=1)
    verified = ModelArtifactItemResponse.from_item(
        replace(_artifact_item(), last_verified_at=verified_at)
    )
    assert verified.last_verified_at == verified_at
    assert verified.created_at == MOMENT


def test_M2_12A_ruling_10_positive_control_absent_server_semantics_fails_loudly() -> None:
    mutated = mutate_source(
        ROUTES_SOURCE,
        [
            (
                "            last_verified_at=item.last_verified_at or record.created_at,",
                "            last_verified_at=None,",
            )
        ],
    )
    with pytest.raises(AssertionError):
        _assert_verification_time_is_defined_on_the_server(mutated)

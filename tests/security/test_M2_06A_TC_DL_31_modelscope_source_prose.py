"""TC-DL-31: judge on the numeric ``Code``; the source's prose crosses nothing.

ModelScope answers failures with Chinese prose in ``Message`` -- two different
sentences for the same numeric ``Code``, depending on which endpoint refused.
The conclusion is drawn from that number together with the HTTP status, and the
prose reaches no response body, no log record, no audit detail and no exception
text (the privacy policy).

``Code`` and HTTP status are deliberately read as two layers: ``Code: 200``
appears inside a 200 response that carries no file listing, so a judgement made
on either number alone would be wrong on a case this suite actually runs.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Final

import pytest
from fixtures.modelscope import (
    MISSING_REPOSITORY_ID,
    PUBLIC_REPOSITORY_ID,
    PUBLIC_UNKNOWN_BRANCH,
    fake_modelscope_hub,
    load_recording,
)
from fixtures.source_mutation import load_mutated_module
from holm_control_api.model_resolution import (
    ResolutionFailed,
    ResolutionOutcome,
    ResolutionRequest,
)
from holm_control_api.model_resolution_routes import render_preview
from holm_control_api.model_resolution_service import ResolutionPreview
from holm_control_api.modelscope_resolution import (
    ModelScopeResolutionAdapter,
    RepositoryNotFound,
    RevisionNotFound,
    UnmappedSourceAnswer,
    classify_reply,
    render_source_failure,
)
from holm_control_api.modelscope_source import ModelScopeCode, ModelScopeReply
from holm_domain import (
    AuditAction,
    ModelOriginType,
    ModelSourceValidationAction,
    ModelSourceValidationReason,
    ModelSourceValidationStatus,
)
from holm_persistence import DEFAULT_REQUESTED_REVISION
from holm_security import build_audit_details

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
SOURCE_READ_SOURCE: Final[Path] = (
    REPOSITORY_ROOT / "apps/control_api/src/holm_control_api/modelscope_source.py"
)
ADAPTER_SOURCE: Final[Path] = (
    REPOSITORY_ROOT / "apps/control_api/src/holm_control_api/modelscope_resolution.py"
)

# PROVENANCE.md lists this code as seen while recording (参数错误) but no
# recording retains it, so it is deliberately unmapped -- and it is a number the
# source really emits, not one invented for this test.
UNMAPPED_SOURCE_CODE: Final[int] = 10010102001


def recorded_prose(name: str) -> str:
    message = load_recording(name).json_body()["Message"]
    assert isinstance(message, str) and message
    return message


def resolve(
    adapter: Any,
    repository_id: str,
    revision: str = DEFAULT_REQUESTED_REVISION,
) -> ResolutionOutcome:
    return asyncio.run(
        adapter.resolve(
            ResolutionRequest(
                source=ModelOriginType.MODELSCOPE,
                repository_id=repository_id,
                requested_revision_norm=revision,
            ),
            credential=None,
        )
    )


def rendered_logs(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(f"{record.getMessage()} {record.__dict__!r}" for record in caplog.records)


def test_TC_DL_31_the_conclusion_comes_from_the_code_and_the_prose_reaches_no_sink(
    caplog: pytest.LogCaptureFixture,
) -> None:
    metadata_prose = recorded_prose("missing-repo-metadata-anonymous")
    listing_prose = recorded_prose("missing-repo-files-anonymous")
    # Same numeric Code, two different sentences: whatever the platform judged
    # on, it was not the sentence.
    assert metadata_prose != listing_prose
    assert (
        load_recording("missing-repo-metadata-anonymous").json_body()["Code"]
        == load_recording("missing-repo-files-anonymous").json_body()["Code"]
        == ModelScopeCode.RECORD_NOT_FOUND
    )
    caplog.set_level(logging.DEBUG)

    with fake_modelscope_hub() as hub:
        outcome = resolve(
            ModelScopeResolutionAdapter(endpoint=hub.endpoint),
            MISSING_REPOSITORY_ID,
        )
        assert [request.path for request in hub.requests] == [
            f"/api/v1/models/{MISSING_REPOSITORY_ID}"
        ]
    assert isinstance(outcome, ResolutionFailed)
    assert outcome.result.status is ModelSourceValidationStatus.NOT_FOUND

    response_body = render_preview(
        ResolutionPreview(outcome=outcome, local_availability=None)
    ).model_dump_json()
    audit_details = build_audit_details(
        AuditAction.MODEL_SOURCE_CREDENTIAL_CHANGE.value,
        {
            "reason_code": outcome.result.reason_code.value,
            "raw_source_response": metadata_prose,
        },
    )
    surfaces = (
        response_body,
        rendered_logs(caplog),
        str(outcome),
        repr(outcome),
        repr(audit_details),
    )
    for surface in surfaces:
        for prose in (metadata_prose, listing_prose):
            assert prose not in surface
    assert "raw_source_response" not in audit_details


def test_TC_DL_31_positive_control_source_prose_in_a_log_is_detected(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Log the ``Message`` inside the production read and require a hit.

    The production reply type has no field for the prose at all, so the leak has
    to be planted where the raw body still exists.  The Adapter is then rebuilt
    against that mutated read, which is what makes this a control over the path
    the real case walks rather than over a hand-built string.
    """

    prose = recorded_prose("missing-repo-metadata-anonymous")
    load_mutated_module(
        SOURCE_READ_SOURCE,
        [
            (
                '    code = document.get("Code")\n',
                "    __import__('logging').getLogger('ms-source-leak').error(\n"
                '        document.get("Message")\n'
                "    )\n"
                '    code = document.get("Code")\n',
            )
        ],
        module_name="_modelscope_source_prose_leak",
        destination=tmp_path / "modelscope_source_prose_leak.py",
    )
    module = load_mutated_module(
        ADAPTER_SOURCE,
        [
            (
                "from holm_control_api.modelscope_source import (",
                "from _modelscope_source_prose_leak import (",
            )
        ],
        module_name="_modelscope_adapter_prose_leak",
        destination=tmp_path / "modelscope_adapter_prose_leak.py",
    )
    caplog.set_level(logging.DEBUG)
    with fake_modelscope_hub() as hub:
        outcome = resolve(
            module.ModelScopeResolutionAdapter(endpoint=hub.endpoint),
            MISSING_REPOSITORY_ID,
        )
        assert hub.requests
    assert isinstance(outcome, ResolutionFailed)
    assert prose in rendered_logs(caplog)


def test_TC_DL_31_the_status_and_the_code_are_read_as_two_layers() -> None:
    """A success-looking envelope and a real success differ only in the body."""

    unknown = load_recording("public-repo-files-unknown-branch")
    master = load_recording("public-repo-files-master")
    assert unknown.status == master.status == 200
    assert unknown.json_body()["Code"] == master.json_body()["Code"] == ModelScopeCode.SUCCESS

    with fake_modelscope_hub() as hub:
        failed = resolve(
            ModelScopeResolutionAdapter(endpoint=hub.endpoint),
            PUBLIC_REPOSITORY_ID,
            PUBLIC_UNKNOWN_BRANCH,
        )
    assert isinstance(failed, ResolutionFailed)
    # HTTP status alone would have called it a success; the pair plus the
    # listing is what produced a failure.
    assert failed.result.reason_code is ModelSourceValidationReason.SOURCE_NOT_FOUND


def test_TC_DL_31_an_unmapped_code_is_a_stated_conclusion_not_a_fallthrough() -> None:
    """A ``Code`` no recording carries reaches a named conclusion of its own."""

    assert UNMAPPED_SOURCE_CODE not in {member.value for member in ModelScopeCode}
    unmapped = classify_reply(
        ModelScopeReply(http_status=400, code=UNMAPPED_SOURCE_CODE, data=None)
    )
    assert unmapped == UnmappedSourceAnswer()
    rendered = render_source_failure(unmapped)
    assert rendered.result.status is None
    assert rendered.result.reason_code is ModelSourceValidationReason.VALIDATION_FAILED
    assert rendered.result.next_action is ModelSourceValidationAction.RETRY_LATER

    # The mapped pairs stay mapped, and each is its own member rather than a
    # shared "something went wrong".
    assert (
        classify_reply(
            ModelScopeReply(http_status=404, code=ModelScopeCode.RECORD_NOT_FOUND, data=None)
        )
        == RepositoryNotFound()
    )
    assert classify_reply(
        ModelScopeReply(http_status=200, code=ModelScopeCode.SUCCESS, data={"Files": []})
    ) == {"Files": []}
    # Neither number decides alone: the recorded 404 is classified only when the
    # recorded Code comes with it, and that Code outside its recorded status is
    # not classified either.
    assert classify_reply(ModelScopeReply(http_status=404, code=None, data=None)) == (
        UnmappedSourceAnswer()
    )
    assert (
        classify_reply(
            ModelScopeReply(http_status=200, code=ModelScopeCode.RECORD_NOT_FOUND, data=None)
        )
        == UnmappedSourceAnswer()
    )
    # A 401 on a read path is not evidence about a credential: it must never
    # become "credential invalid" (TC-DL-18 collapse (c)).
    assert (
        classify_reply(
            ModelScopeReply(http_status=401, code=ModelScopeCode.USER_NOT_LOGGED_IN, data=None)
        )
        == UnmappedSourceAnswer()
    )
    assert render_source_failure(RevisionNotFound()).result.status is (
        ModelSourceValidationStatus.NOT_FOUND
    )

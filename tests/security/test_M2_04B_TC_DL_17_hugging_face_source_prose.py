"""TC-DL-17: source prose never crosses the Hugging Face Adapter boundary."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Final

import pytest
from fixtures.hugging_face import (
    GATED_REPOSITORY_ID,
    CredentialPresence,
    fake_hugging_face_hub,
    load_recording,
)
from fixtures.source_mutation import load_mutated_module
from holm_control_api.hugging_face_resolution import HuggingFaceResolutionAdapter
from holm_control_api.model_resolution import ResolutionFailed, ResolutionRequest
from holm_control_api.model_resolution_routes import render_preview
from holm_control_api.model_resolution_service import ResolutionPreview
from holm_domain import (
    AuditAction,
    ModelOriginType,
    ModelSourceValidationReason,
    ModelSourceValidationStatus,
)
from holm_persistence import DEFAULT_REQUESTED_REVISION
from holm_security import build_audit_details

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
ADAPTER_SOURCE: Final[Path] = (
    REPOSITORY_ROOT / "apps/control_api/src/holm_control_api/hugging_face_resolution.py"
)


def _resolve_gated(adapter: HuggingFaceResolutionAdapter) -> ResolutionFailed:
    outcome = asyncio.run(
        adapter.resolve(
            ResolutionRequest(
                source=ModelOriginType.HUGGING_FACE,
                repository_id=GATED_REPOSITORY_ID,
                requested_revision_norm=DEFAULT_REQUESTED_REVISION,
            ),
            credential=None,
        )
    )
    assert isinstance(outcome, ResolutionFailed)
    return outcome


def _rendered_logs(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(f"{record.getMessage()} {record.__dict__!r}" for record in caplog.records)


def test_TC_DL_17_gated_401_uses_error_code_and_source_prose_reaches_no_sink(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source_prose = load_recording("gated-repo-file-denied").header("x-error-message")
    assert source_prose is not None
    caplog.set_level(logging.DEBUG)

    with fake_hugging_face_hub() as hub:
        outcome = _resolve_gated(HuggingFaceResolutionAdapter(endpoint=hub.endpoint))
        denied_requests = [
            request
            for request in hub.requests
            if request.credential is CredentialPresence.ABSENT
            and GATED_REPOSITORY_ID in request.path
            and "/resolve/" in request.path
        ]
        assert denied_requests, "the recorded GatedRepo refusal branch was not reached"

    assert outcome.result.status is ModelSourceValidationStatus.PERMISSION_DENIED
    assert outcome.result.reason_code is ModelSourceValidationReason.SOURCE_PERMISSION_DENIED
    response_body = render_preview(
        ResolutionPreview(outcome=outcome, local_availability=None)
    ).model_dump_json()
    audit_details = build_audit_details(
        AuditAction.MODEL_SOURCE_CREDENTIAL_CHANGE.value,
        {
            "reason_code": outcome.result.reason_code.value,
            "raw_source_response": source_prose,
        },
    )
    surfaces = (
        response_body,
        _rendered_logs(caplog),
        str(outcome),
        repr(outcome),
        repr(audit_details),
    )
    for surface in surfaces:
        assert source_prose not in surface
    assert "raw_source_response" not in audit_details


def test_TC_DL_17_positive_control_source_prose_in_a_log_is_detected(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    source_prose = load_recording("gated-repo-file-denied").header("x-error-message")
    assert source_prose is not None
    module = load_mutated_module(
        ADAPTER_SOURCE,
        [
            (
                "    status_code = error.response.status_code\n",
                "    __import__('logging').getLogger('hf-source-leak').error(\n"
                "        error.response.headers.get('x-error-message')\n"
                "    )\n"
                "    status_code = error.response.status_code\n",
            )
        ],
        module_name="_source_prose_logging_mutation",
        destination=tmp_path / "source_prose_logging_mutation.py",
    )
    caplog.set_level(logging.DEBUG)
    with fake_hugging_face_hub() as hub:
        outcome = _resolve_gated(module.HuggingFaceResolutionAdapter(endpoint=hub.endpoint))
        assert outcome.result.status is ModelSourceValidationStatus.PERMISSION_DENIED
        assert any(
            request.credential is CredentialPresence.ABSENT
            and GATED_REPOSITORY_ID in request.path
            and "/resolve/" in request.path
            for request in hub.requests
        )
    assert source_prose in _rendered_logs(caplog)

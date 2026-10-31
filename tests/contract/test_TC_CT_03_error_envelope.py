"""TC-CT-03 stable Error Envelope and OpenAPI snapshot tests."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest
from holm_contracts.errors import (
    ErrorBody,
    ErrorEnvelope,
    error_envelope_openapi_components,
)
from holm_domain.errors import ERROR_CATEGORIES, ErrorCode, classify_error
from pydantic import ValidationError

SNAPSHOT = Path(__file__).parent / "snapshots" / "m0_04_error_envelope.json"
REQUEST_ID = UUID("018f47f2-8c00-7000-8000-000000000002")


def test_TC_CT_03_error_envelope_shape_and_error_code_single_source() -> None:
    envelope = ErrorEnvelope(
        error=ErrorBody(
            code=ErrorCode.INVALID_REQUEST,
            message="Request is invalid.",
            details={"field": "page_size"},
            request_id=REQUEST_ID,
        )
    )

    assert envelope.model_dump(mode="json") == {
        "error": {
            "code": "invalid_request",
            "message": "Request is invalid.",
            "details": {"field": "page_size"},
            "request_id": str(REQUEST_ID),
        }
    }
    assert set(ERROR_CATEGORIES) == set(ErrorCode)
    assert all(classify_error(code) is ERROR_CATEGORIES[code] for code in ErrorCode)


def test_TC_CT_03_openapi_error_envelope_snapshot_is_stable() -> None:
    rendered = json.dumps(
        error_envelope_openapi_components(),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    assert f"{rendered}\n" == SNAPSHOT.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "payload",
    [
        {
            "error": {
                "code": "unknown_error",
                "message": "Safe.",
                "details": {},
                "request_id": str(REQUEST_ID),
            }
        },
        {
            "error": {
                "code": "invalid_request",
                "message": "Safe.",
                "details": {},
                "request_id": "not-a-uuid",
            }
        },
        {
            "error": {
                "code": "invalid_request",
                "message": "Safe.",
                "details": {},
                "request_id": str(REQUEST_ID),
                "exception": "must-not-exist",
            }
        },
        {
            "error": {
                "code": "invalid_request",
                "message": "Safe.",
                "details": {"exception": ValueError("raw")},
                "request_id": str(REQUEST_ID),
            }
        },
    ],
)
def test_TC_CT_03_error_envelope_rejects_unknown_or_unsafe_values(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ErrorEnvelope.model_validate(payload)

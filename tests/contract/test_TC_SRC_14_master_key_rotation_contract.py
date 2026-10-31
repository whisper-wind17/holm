"""TC-SRC-14 Master Key rotation is an operation, never Key management."""

from __future__ import annotations

import pytest
from holm_control_api.master_key_rotation_routes import (
    MASTER_KEY_ROTATION_ADVANCE_PATH,
    MASTER_KEY_ROTATION_PATH,
    MasterKeyRotationAdvanceRequest,
)
from pydantic import BaseModel, ConfigDict, ValidationError


def _assert_no_key_management_fields(schema: type[BaseModel]) -> None:
    forbidden = {
        "key",
        "key_id",
        "key_material",
        "key_state",
        "master_key",
        "new_key_id",
        "old_key_id",
        "state",
    }
    assert not (set(schema.model_fields) & forbidden)


def test_TC_SRC_14_rotation_request_schema_has_no_key_management_surface() -> None:
    assert MASTER_KEY_ROTATION_PATH == "/api/v1/system/master-key-rotation"
    assert MASTER_KEY_ROTATION_ADVANCE_PATH == ("/api/v1/system/master-key-rotation:advance")
    assert set(MasterKeyRotationAdvanceRequest.model_fields) == {"current_password"}
    _assert_no_key_management_fields(MasterKeyRotationAdvanceRequest)

    with pytest.raises(ValidationError):
        MasterKeyRotationAdvanceRequest.model_validate(
            {"current_password": "admin-password", "new_key_id": "not-accepted"}
        )


def test_TC_SRC_14_positive_control_adding_a_key_field_fails_the_guard() -> None:
    class MutatedRotationRequest(BaseModel):
        model_config = ConfigDict(extra="forbid")

        current_password: str | None = None
        new_key_id: str | None = None

    with pytest.raises(AssertionError):
        _assert_no_key_management_fields(MutatedRotationRequest)

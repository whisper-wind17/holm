"""M4-17 / TC-GW-39 OpenAPI-compatible authorization schema artifact."""

import json
from pathlib import Path

from holm_control_api.user_management_routes import (
    GrantedServiceResponse,
    ReplaceServicePermissionsRequest,
    ReplaceUserPermissionsRequest,
)


def permission_schemas() -> dict[str, object]:
    return {
        model.__name__: model.model_json_schema()
        for model in (
            GrantedServiceResponse,
            ReplaceServicePermissionsRequest,
            ReplaceUserPermissionsRequest,
        )
    }


def test_M4_17_TC_GW_39_permission_openapi_schema_snapshot() -> None:
    path = Path(__file__).parent / "snapshots/m4_17_permission_limits.json"
    assert permission_schemas() == json.loads(path.read_text())

"""M2-10C: schema snapshots bind the administrator verification contract."""

import json
from pathlib import Path

import pytest
from holm_control_api.model_acquisition_read_routes import (
    ArtifactVerificationStatusResponse,
    ArtifactVerifyResponse,
)


@pytest.mark.parametrize(
    "model,name",
    [
        (ArtifactVerifyResponse, "m2_10c_artifact_verify.json"),
        (ArtifactVerificationStatusResponse, "m2_10c_artifact_verification_status.json"),
    ],
)
def test_M2_10C_verification_contract_snapshot(model: type, name: str) -> None:
    assert model.model_json_schema() == json.loads(
        (Path(__file__).parent / "snapshots" / name).read_text()
    )

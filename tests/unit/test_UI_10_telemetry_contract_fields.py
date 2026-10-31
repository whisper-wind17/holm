"""FX-UI-10: field snapshot from telemetry_routes response models (TC-TM-09)."""

import json
from pathlib import Path

import pytest
from holm_control_api import telemetry_routes
from pydantic import BaseModel, create_model

SNAPSHOT = Path(__file__).parents[1] / "fixtures/contracts/telemetry-response-fields.json"


def response_models() -> dict[str, type[BaseModel]]:
    return {
        name: model
        for name, model in vars(telemetry_routes).items()
        if isinstance(model, type)
        and issubclass(model, BaseModel)
        and model.__module__ == telemetry_routes.__name__
        and not name.startswith("_")
    }


def assert_snapshot_matches(models: dict[str, type[BaseModel]]) -> None:
    actual = {name: sorted(model.model_fields) for name, model in models.items()}
    expected = json.loads(SNAPSHOT.read_text())
    assert actual == expected, "UI-10 telemetry response field drift"


def test_UI_10_TC_TM_09_response_fields_match_snapshot() -> None:
    assert_snapshot_matches(response_models())


def test_UI_10_guard_rejects_backend_field_added() -> None:
    models = response_models()
    models["InstanceTelemetryResponse"] = create_model(
        "DriftedInstanceResponse",
        __base__=models["InstanceTelemetryResponse"],
        unexpected_field=(str, ...),
    )
    with pytest.raises(AssertionError, match="UI-10 telemetry response field drift"):
        assert_snapshot_matches(models)

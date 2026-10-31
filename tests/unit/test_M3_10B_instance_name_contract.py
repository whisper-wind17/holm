"""M3-10B request omission and public name Schema agree with API validation."""

from holm_control_api.model_instance_routes import (
    CreateModelInstanceRequest,
    ModelInstanceResponse,
)


def test_M3_10B_name_schema_is_optional_nonnullable_and_bounded() -> None:
    request = CreateModelInstanceRequest.model_json_schema()
    response = ModelInstanceResponse.model_json_schema()
    for schema in (request, response):
        name = schema["properties"]["name"]
        assert name["type"] == "string"
        assert name["minLength"] == 2
        assert name["maxLength"] == 64
        assert "anyOf" not in name
        assert "default" not in name
    assert "name" not in request["required"]
    assert "name" in response["required"]

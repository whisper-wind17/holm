"""TC-MDL-10/12: single vocabularies, pagination, and M3-01 connection point."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any, Final, cast
from urllib.parse import urlencode

import pytest
from fastapi.routing import APIRoute
from fixtures.source_mutation import load_mutated_module, mutate_source
from holm_control_api.model_catalog_routes import (
    ModelCatalogCursorInvalid,
    ModelCatalogItemResponse,
    _catalog_page_params,
    create_model_catalog_router,
)
from holm_control_api.model_catalog_service import ModelCatalogService
from holm_domain import LocalAvailability
from starlette.requests import Request

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
ROUTES_SOURCE: Final[Path] = REPOSITORY_ROOT / (
    "apps/control_api/src/holm_control_api/model_catalog_routes.py"
)
SERVICE_SOURCE: Final[Path] = REPOSITORY_ROOT / (
    "apps/control_api/src/holm_control_api/model_catalog_service.py"
)
CATALOG_REQUEST_PATH: Final[tuple[Path, ...]] = (ROUTES_SOURCE, SERVICE_SOURCE)
CATALOG_RESPONSE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "id",
        "source",
        "display_name",
        "repository_id",
        "architecture",
        "modalities",
        "estimated_size_bytes",
        "revision",
        "local_availability",
        "references",
    }
)


def _assert_availability_contract(response_model: Any) -> None:
    assert frozenset(response_model.model_fields) == CATALOG_RESPONSE_FIELDS
    field = response_model.model_fields["local_availability"]
    assert field.is_required()
    assert field.annotation is LocalAvailability


def _assert_no_second_catalog_enum(source_texts: tuple[str, ...]) -> None:
    found: list[str] = []
    for source in source_texts:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = {base.id for base in node.bases if isinstance(base, ast.Name)}
            if bases & {"Enum", "StrEnum"}:
                found.append(node.name)
    assert not found, f"catalog request path defined another enum: {found}"


def _get_catalog_route(router: object) -> APIRoute:
    routes = router.routes
    matches = [
        route for route in routes if isinstance(route, APIRoute) and route.methods == {"GET"}
    ]
    assert len(matches) == 1
    return matches[0]


def _assert_existing_pagination_is_the_only_query_contract(router: object) -> None:
    route = _get_catalog_route(router)
    signature = inspect.signature(route.endpoint)
    assert tuple(signature.parameters) == ("request", "params")
    annotation = str(signature.parameters["params"].annotation)
    assert "CursorPageParams" in annotation
    assert "Depends" in annotation


def _assert_transport_adapter_delegates_the_complete_query(source_texts: tuple[str, ...]) -> None:
    route_tree = ast.parse(source_texts[0])
    functions = {
        node.name: node
        for node in ast.walk(route_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_catalog_page_params" in functions
    adapter = functions["_catalog_page_params"]
    explicit_query_fields = {
        argument.value
        for node in ast.walk(adapter)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        for argument in node.args[:1]
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
    }
    assert explicit_query_fields == {"page_size"}
    raw_calls = {
        node.func.attr
        for node in ast.walk(adapter)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "raw"
    }
    assert raw_calls == {"get"}
    raw_subscript_keys = {
        node.slice.value
        for node in ast.walk(adapter)
        if isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "raw"
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    }
    assert raw_subscript_keys == {"page_size"}
    rendered_adapter = ast.unparse(adapter)
    assert "CursorPageParams.model_validate(raw)" in rendered_adapter
    for source in source_texts:
        tree = ast.parse(source)
        identifiers = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
            node.arg for node in ast.walk(tree) if isinstance(node, ast.arg)
        }
        forbidden_names = identifiers & {
            "limit",
            "offset",
        }
        assert not forbidden_names


def _assert_instance_reference_connection(source: str) -> None:
    tree = ast.parse(source)
    methods = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_instance_reference_count" in methods
    probe_method = ast.unparse(methods["_instance_reference_count"])
    delete_method = ast.unparse(methods["delete"])
    assert "self._instance_references is None" in probe_method
    assert "self._instance_references.count" in probe_method
    assert "instance_count = await self._instance_reference_count" in delete_method
    assert "instance_count is not None and instance_count > 0" in delete_method


def _assert_middleware_is_the_only_delete_authentication(
    route_source: str,
    service_source: str,
) -> None:
    route_tree = ast.parse(route_source)
    service_tree = ast.parse(service_source)
    route_methods = {
        node.name: node
        for node in ast.walk(route_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    service_methods = {
        node.name: node
        for node in ast.walk(service_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    delete_route = ast.unparse(route_methods["delete_model"])
    delete_service = service_methods["delete"]
    assert "current_authenticated_session(request)" in delete_route
    assert "actor_id=authenticated.user.id" in delete_route
    assert [argument.arg for argument in delete_service.args.kwonlyargs] == ["actor_id"]
    forbidden_service_identifiers = {
        node.id for node in ast.walk(service_tree) if isinstance(node, ast.Name)
    } & {"AuthenticationService", "session_token", "csrf_token"}
    secondary_authentication_calls = {
        node.func.attr
        for node in ast.walk(service_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "authenticate_session"
    }
    assert not forbidden_service_identifiers
    assert not secondary_authentication_calls


def test_M2_13B_ruling_1_TC_MDL_10_local_availability_is_imported_and_response_exact() -> None:
    _assert_availability_contract(ModelCatalogItemResponse)
    source_texts = tuple(path.read_text(encoding="utf-8") for path in CATALOG_REQUEST_PATH)
    _assert_no_second_catalog_enum(source_texts)
    assert all("LocalAvailability" in source for source in source_texts)


def test_TC_MDL_10_positive_control_omitting_absent_state_fails_loudly(
    tmp_path: Path,
) -> None:
    mutated = load_mutated_module(
        ROUTES_SOURCE,
        [("    local_availability: LocalAvailability\n", "")],
        module_name="_mutated_model_catalog_routes_missing_state",
        destination=tmp_path / "mutated_model_catalog_routes_missing_state.py",
    )
    with pytest.raises(AssertionError):
        _assert_availability_contract(mutated.ModelCatalogItemResponse)


def test_TC_MDL_10_positive_control_second_endpoint_enum_fails_loudly() -> None:
    mutated = mutate_source(
        ROUTES_SOURCE,
        [
            (
                "from __future__ import annotations",
                "from __future__ import annotations\n\nfrom enum import StrEnum",
            ),
            (
                'REVISION_UNAVAILABLE: Final[str] = "—"',
                'REVISION_UNAVAILABLE: Final[str] = "—"\n\n'
                "class ShadowAvailability(StrEnum):\n"
                '    ABSENT = "absent"',
            ),
        ],
    )
    with pytest.raises(AssertionError, match="another enum"):
        _assert_no_second_catalog_enum((mutated, SERVICE_SOURCE.read_text(encoding="utf-8")))


def test_TC_MDL_10_positive_control_literal_availability_annotation_fails_loudly(
    tmp_path: Path,
) -> None:
    mutated = load_mutated_module(
        ROUTES_SOURCE,
        [
            (
                "from typing import Annotated, Final",
                "from typing import Annotated, Final, Literal",
            ),
            (
                "    local_availability: LocalAvailability",
                '    local_availability: Literal["artifact_ready", "download_active", "absent"]',
            ),
        ],
        module_name="_mutated_model_catalog_literal_availability",
        destination=tmp_path / "mutated_model_catalog_literal_availability.py",
    )
    with pytest.raises(AssertionError):
        _assert_availability_contract(mutated.ModelCatalogItemResponse)


def test_TC_MDL_13_delete_uses_middleware_authentication_only() -> None:
    route_source = ROUTES_SOURCE.read_text(encoding="utf-8")
    service_source = SERVICE_SOURCE.read_text(encoding="utf-8")
    _assert_middleware_is_the_only_delete_authentication(route_source, service_source)


def test_TC_MDL_13_positive_control_second_delete_authentication_fails_loudly() -> None:
    mutated = mutate_source(
        SERVICE_SOURCE,
        [
            (
                "        actor_id: UUID,\n    ) -> None:\n"
                "        async with self._session_factory() as session:",
                "        actor_id: UUID,\n    ) -> None:\n"
                "        await self._authentication.authenticate_session(require_csrf=False)\n"
                "        async with self._session_factory() as session:",
            )
        ],
    )
    with pytest.raises(AssertionError):
        _assert_middleware_is_the_only_delete_authentication(
            ROUTES_SOURCE.read_text(encoding="utf-8"),
            mutated,
        )


def test_TC_MDL_10_existing_cursor_page_params_is_only_http_query_contract() -> None:
    router = create_model_catalog_router(cast(ModelCatalogService, object()))
    _assert_existing_pagination_is_the_only_query_contract(router)
    _assert_transport_adapter_delegates_the_complete_query(
        tuple(path.read_text(encoding="utf-8") for path in CATALOG_REQUEST_PATH)
    )


def test_TC_MDL_10_positive_control_second_pagination_parameter_fails_loudly(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "mutated_model_catalog_routes.py"
    mutated = load_mutated_module(
        ROUTES_SOURCE,
        [
            (
                "from fastapi import APIRouter, Depends",
                "from fastapi import APIRouter, Depends, Query",
            ),
            (
                "        params: Annotated[CursorPageParams, Depends(_catalog_page_params)],",
                "        params: Annotated[CursorPageParams, Depends(_catalog_page_params)],\n"
                "        limit: int = Query(default=20),",
            ),
        ],
        module_name="_mutated_model_catalog_routes_pagination",
        destination=destination,
    )
    router = mutated.create_model_catalog_router(cast(ModelCatalogService, object()))
    with pytest.raises(AssertionError):
        _assert_existing_pagination_is_the_only_query_contract(router)
    with pytest.raises(AssertionError):
        _assert_transport_adapter_delegates_the_complete_query(
            (
                destination.read_text(encoding="utf-8"),
                SERVICE_SOURCE.read_text(encoding="utf-8"),
            )
        )


def test_TC_MDL_10_positive_control_swallowed_query_parameter_fails_loudly() -> None:
    mutated = mutate_source(
        ROUTES_SOURCE,
        [
            (
                '    page_size = raw.get("page_size")',
                '    raw.pop("per_page", None)\n    page_size = raw.get("page_size")',
            )
        ],
    )
    with pytest.raises(AssertionError):
        _assert_transport_adapter_delegates_the_complete_query(
            (mutated, SERVICE_SOURCE.read_text(encoding="utf-8"))
        )


def _request_with_query(query: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/models",
            "headers": [],
            "query_string": query.encode("ascii"),
        }
    )


def _exception_chain_text(error: BaseException) -> str:
    rendered: list[str] = []
    current: BaseException | None = error
    while current is not None:
        rendered.append(str(current))
        current = current.__cause__ or current.__context__
    return "\n".join(rendered)


def test_TC_MDL_10_invalid_query_and_cursor_exception_chains_are_sanitized() -> None:
    sentinel = "/private/catalog/CREDENTIAL_QUERY_SENTINEL"
    request = _request_with_query(urlencode({"per_page": sentinel}))
    with pytest.raises(ModelCatalogCursorInvalid) as query_failure:
        _catalog_page_params(request)
    with pytest.raises(ModelCatalogCursorInvalid) as cursor_failure:
        ModelCatalogService._parse_cursor(sentinel)
    assert sentinel not in _exception_chain_text(query_failure.value)
    assert sentinel not in _exception_chain_text(cursor_failure.value)


def test_TC_MDL_12_instance_reference_branch_is_ready_for_M3_01() -> None:
    _assert_instance_reference_connection(SERVICE_SOURCE.read_text(encoding="utf-8"))


def test_TC_MDL_12_positive_control_removing_instance_branch_fails_loudly() -> None:
    mutated = mutate_source(
        SERVICE_SOURCE,
        [
            (
                "            instance_count = await self._instance_reference_count(\n"
                "                session=session,\n"
                "                model_id=model_id,\n"
                "            )",
                "            instance_count = 0",
            )
        ],
    )
    with pytest.raises(AssertionError):
        _assert_instance_reference_connection(mutated)


def test_TC_MDL_12_model_instance_runtime_half_is_BLOCKED_ENV() -> None:
    pytest.xfail(
        "BLOCKED_ENV: core.model_instances is created by M3-01; the injectable "
        "reference probe is present but cannot query a relation that does not yet exist"
    )

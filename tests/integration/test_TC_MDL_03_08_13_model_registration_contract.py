"""TC-MDL-03/08/13: registration schema, vocabulary, and audit boundary."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Final, cast

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from fastapi.routing import APIRoute
from fixtures.source_mutation import load_mutated_module
from holm_control_api.model_registration_routes import (
    ModelRegistrationRequest,
    RegisteredModelResponse,
    create_model_registration_router,
)
from holm_control_api.model_registration_service import ModelRegistrationService
from holm_domain import MetadataStatus
from migration_test_support import upgrade_head

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
DOMAIN_SOURCE: Final[Path] = (
    REPOSITORY_ROOT / "packages/domain/src/holm_domain/model_acquisition.py"
)
MIGRATION_SOURCE: Final[Path] = (
    REPOSITORY_ROOT / "migrations/versions/20260907_0011_model_registration_idempotency.py"
)

REGISTRATION_REQUEST_FIELDS: Final[frozenset[str]] = frozenset(
    {"source", "repository_id", "external_path", "revision"}
)


@pytest.fixture(autouse=True)
def migrated_schema(ephemeral_postgres: EphemeralPostgres) -> Iterator[None]:
    upgrade_head(ephemeral_postgres.engine)
    yield


def _check_values(engine: sa.Engine, table: str, constraint: str) -> frozenset[str]:
    checks = {
        item["name"]: str(item["sqltext"])
        for item in sa.inspect(engine).get_check_constraints(table, schema="core")
    }
    assert constraint in checks, f"CHECK discovery did not find {constraint}"
    return frozenset(re.findall(r"'([^']+)'", checks[constraint]))


def _assert_metadata_vocabularies(
    *,
    domain_values: frozenset[str],
    database_values: frozenset[str],
    response_values: frozenset[str],
) -> None:
    assert domain_values
    assert domain_values == database_values == response_values


def test_TC_MDL_08_metadata_status_is_two_state_and_derived_in_the_migration(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    domain_values = frozenset(item.value for item in MetadataStatus)
    database_values = _check_values(
        ephemeral_postgres.engine,
        "models",
        "ck_models_metadata_status",
    )
    response_values = frozenset(
        RegisteredModelResponse.model_json_schema()["$defs"]["MetadataStatus"]["enum"]
    )
    assert domain_values == {"CONFIRMED", "UNCONFIRMED"}
    _assert_metadata_vocabularies(
        domain_values=domain_values,
        database_values=database_values,
        response_values=response_values,
    )

    tree = ast.parse(MIGRATION_SOURCE.read_text(encoding="utf-8"))
    metadata_check_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create_check_constraint"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "ck_models_metadata_status"
    ]
    assert len(metadata_check_calls) == 1
    expression = metadata_check_calls[0].args[2]
    assert isinstance(expression, ast.Call)
    assert isinstance(expression.func, ast.Name)
    assert expression.func.id == "_values_clause"


def test_TC_MDL_08_positive_control_adding_an_enum_member_fails_loudly(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    mutated = load_mutated_module(
        DOMAIN_SOURCE,
        [
            (
                '    UNCONFIRMED = "UNCONFIRMED"',
                '    UNCONFIRMED = "UNCONFIRMED"\n    STALE = "STALE"',
            )
        ],
        module_name="_mutated_metadata_status",
        destination=tmp_path / "mutated_model_acquisition.py",
    )
    with pytest.raises(AssertionError):
        _assert_metadata_vocabularies(
            domain_values=frozenset(item.value for item in mutated.MetadataStatus),
            database_values=_check_values(
                ephemeral_postgres.engine,
                "models",
                "ck_models_metadata_status",
            ),
            response_values=frozenset(
                RegisteredModelResponse.model_json_schema()["$defs"]["MetadataStatus"]["enum"]
            ),
        )


def test_TC_MDL_03_request_fields_are_exact_and_only_post_is_shipped() -> None:
    assert frozenset(ModelRegistrationRequest.model_fields) == REGISTRATION_REQUEST_FIELDS
    assert ModelRegistrationRequest.model_config["extra"] == "forbid"
    router = create_model_registration_router(cast(ModelRegistrationService, object()))
    surface = {
        (route.path, frozenset(route.methods or ()))
        for route in router.routes
        if isinstance(route, APIRoute)
    }
    assert surface == {("/api/v1/models", frozenset({"POST"}))}


def test_TC_MDL_03_positive_control_a_metadata_request_field_fails_loudly(
    tmp_path: Path,
) -> None:
    route_source = REPOSITORY_ROOT / (
        "apps/control_api/src/holm_control_api/model_registration_routes.py"
    )
    mutated = load_mutated_module(
        route_source,
        [
            (
                '    """The Resolve identity shape, with no client-supplied metadata fields."""',
                '    """The Resolve identity shape, with no client-supplied metadata fields."""\n\n'
                "    architecture: str | None = None",
            )
        ],
        module_name="_mutated_model_registration_routes",
        destination=tmp_path / "mutated_model_registration_routes.py",
    )
    with pytest.raises(AssertionError):
        assert frozenset(mutated.ModelRegistrationRequest.model_fields) == (
            REGISTRATION_REQUEST_FIELDS
        )


def test_TC_MDL_06_idempotency_relation_is_exact_and_database_convergent(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    inspector = sa.inspect(ephemeral_postgres.engine)
    columns = {
        item["name"]: bool(item["nullable"])
        for item in inspector.get_columns("idempotency_records", schema="core")
    }
    assert columns == {
        "id": False,
        "actor_id": False,
        "operation": False,
        "key_hash": False,
        "request_hash": False,
        "response_object_id": False,
        "created_at": False,
    }
    unique_constraints = {
        item["name"]: tuple(item["column_names"])
        for item in inspector.get_unique_constraints("idempotency_records", schema="core")
    }
    assert unique_constraints == {"uq_idempotency_scope": ("actor_id", "operation", "key_hash")}

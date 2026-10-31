"""TC-MDL-04/09: registration calls the existing normalization and NULL rules."""

from __future__ import annotations

import ast
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest
from fixtures.source_mutation import mutate_source

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
REGISTRATION_SOURCE: Final[Path] = REPOSITORY_ROOT / (
    "apps/control_api/src/holm_control_api/model_registration_service.py"
)
REGISTRATION_ROUTE_SOURCE: Final[Path] = REPOSITORY_ROOT / (
    "apps/control_api/src/holm_control_api/model_registration_routes.py"
)
REGISTRATION_PATH_SOURCES: Final[tuple[Path, ...]] = (
    REGISTRATION_ROUTE_SOURCE,
    REGISTRATION_SOURCE,
)
SHARED_PATH_SOURCE: Final[Path] = REPOSITORY_ROOT / (
    "packages/contracts/src/holm_contracts/external_paths.py"
)
PERSISTED_METADATA_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "architecture",
        "modalities",
        "precision",
        "quantization",
        "context_length",
        "supported_runtimes",
        "estimated_size_bytes",
        "license",
    }
)
FORBIDDEN_NORMALIZERS: Final[frozenset[str]] = frozenset(
    {"strip", "lstrip", "rstrip", "lower", "upper", "casefold", "normalize"}
)


def _registration_function(tree: ast.AST) -> ast.FunctionDef:
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_registration_values"
    ]
    assert len(matches) == 1, "registration value builder is missing or duplicated"
    return matches[0]


def assert_registration_uses_only_shared_mappings(sources: Mapping[Path, str]) -> None:
    assert set(sources) == set(REGISTRATION_PATH_SOURCES), (
        "registration request-path source discovery is incomplete"
    )
    trees = {path: ast.parse(source, filename=str(path)) for path, source in sources.items()}
    service_tree = trees[REGISTRATION_SOURCE]
    function = _registration_function(service_tree)
    calls = [
        node for tree in trees.values() for node in ast.walk(tree) if isinstance(node, ast.Call)
    ]
    normalizer_calls = [
        node
        for node in calls
        if isinstance(node.func, ast.Name) and node.func.id == "normalize_source_key"
    ]
    assert len(normalizer_calls) == 2, "both identity branches must call normalize_source_key"
    forbidden = [
        node.func.attr
        for node in calls
        if isinstance(node.func, ast.Attribute) and node.func.attr in FORBIDDEN_NORMALIZERS
    ]
    assert forbidden == [], f"registration contains a second normalizer: {forbidden}"

    builders = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ModelUpsert"
    ]
    assert len(builders) == 2, "remote and External Path builders must both be discoverable"
    for builder in builders:
        metadata = {keyword.arg: keyword.value for keyword in builder.keywords if keyword.arg}
        assert metadata.keys() >= PERSISTED_METADATA_FIELDS
        for field_name in PERSISTED_METADATA_FIELDS:
            expression = metadata[field_name]
            assert isinstance(expression, ast.Call), f"{field_name} bypassed to_persisted_column"
            assert isinstance(expression.func, ast.Name)
            assert expression.func.id == "to_persisted_column"

    forbidden_null_fallbacks = [
        node
        for tree in trees.values()
        for node in ast.walk(tree)
        if isinstance(node, ast.BoolOp)
        and isinstance(node.op, ast.Or)
        and any(isinstance(value, ast.Constant) and value.value is None for value in node.values)
    ]
    assert forbidden_null_fallbacks == [], "registration contains a value-or-None mapping"


def test_TC_MDL_04_TC_MDL_09_registration_calls_the_two_existing_definition_sources() -> None:
    assert_registration_uses_only_shared_mappings(
        {path: path.read_text(encoding="utf-8") for path in REGISTRATION_PATH_SOURCES}
    )


def test_TC_MDL_04_positive_control_second_strip_normalizer_fails_loudly() -> None:
    mutated = mutate_source(
        REGISTRATION_SOURCE,
        [
            (
                "        source_key = normalize_source_key(external_path)",
                "        source_key = normalize_source_key(external_path)\n"
                "        source_key = external_path.strip()",
            )
        ],
    )
    with pytest.raises(AssertionError, match="second normalizer"):
        assert_registration_uses_only_shared_mappings(
            {
                REGISTRATION_ROUTE_SOURCE: REGISTRATION_ROUTE_SOURCE.read_text(encoding="utf-8"),
                REGISTRATION_SOURCE: mutated,
            }
        )


def test_TC_MDL_04_positive_control_register_normalizers_fail_loudly() -> None:
    mutated = mutate_source(
        REGISTRATION_SOURCE,
        [
            (
                '        """Re-resolve this request; a failed resolution writes no row at all."""',
                '        """Re-resolve this request; a failed resolution writes no row at all."""\n'
                "        repository_id = (\n"
                "            repository_id.strip() if repository_id else repository_id\n"
                "        )\n"
                "        external_path = (\n"
                "            external_path.strip() if external_path else external_path\n"
                "        )",
            )
        ],
    )
    with pytest.raises(AssertionError, match="second normalizer"):
        assert_registration_uses_only_shared_mappings(
            {
                REGISTRATION_ROUTE_SOURCE: REGISTRATION_ROUTE_SOURCE.read_text(encoding="utf-8"),
                REGISTRATION_SOURCE: mutated,
            }
        )


def test_TC_MDL_09_positive_control_value_or_none_mapping_fails_loudly() -> None:
    mutated = mutate_source(
        REGISTRATION_SOURCE,
        [
            (
                "                architecture=to_persisted_column(resolved.architecture),",
                "                architecture=resolved.architecture.value or None,",
            )
        ],
    )
    with pytest.raises(AssertionError):
        assert_registration_uses_only_shared_mappings(
            {
                REGISTRATION_ROUTE_SOURCE: REGISTRATION_ROUTE_SOURCE.read_text(encoding="utf-8"),
                REGISTRATION_SOURCE: mutated,
            }
        )


def assert_external_path_rule_has_one_definition(sources: Mapping[Path, str]) -> None:
    assert sources, "external-path source discovery returned zero files"
    definitions: set[Path] = set()
    for path, source in sources.items():
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {
                "judge_external_path",
                "path_is_within",
            }:
                definitions.add(path)
    assert definitions == {SHARED_PATH_SOURCE}


def test_TC_MDL_03_registration_does_not_define_a_second_external_path_rule() -> None:
    paths = tuple(sorted((REPOSITORY_ROOT / "apps").rglob("*.py"))) + tuple(
        sorted((REPOSITORY_ROOT / "packages").rglob("*.py"))
    )
    assert_external_path_rule_has_one_definition(
        {path: path.read_text(encoding="utf-8") for path in paths}
    )
    source = REGISTRATION_SOURCE.read_text(encoding="utf-8")
    assert "preview = await self._resolution.resolve(" in source


def test_TC_MDL_03_positive_control_second_path_judgement_fails_loudly(
    tmp_path: Path,
) -> None:
    duplicate = tmp_path / "duplicate_path_rule.py"
    duplicate_source = (
        "def judge_external_path(untrusted_path: str, policy: object) -> object:\n"
        "    return policy\n"
    )
    with pytest.raises(AssertionError):
        assert_external_path_rule_has_one_definition(
            {
                SHARED_PATH_SOURCE: SHARED_PATH_SOURCE.read_text(encoding="utf-8"),
                duplicate: duplicate_source,
            }
        )

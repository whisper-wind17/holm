"""TC-DL-01: single normalization source and application exposure guards."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import cast

import pytest
from fastapi.routing import APIRoute
from fixtures.source_mutation import mutate_source
from holm_control_api.download_creation_routes import create_download_creation_router
from holm_control_api.download_creation_service import DownloadCreationService

ROOT = Path(__file__).resolve().parents[2]
PATHS = (
    ROOT / "apps/control_api/src/holm_control_api/download_creation_service.py",
    ROOT / "apps/control_api/src/holm_control_api/download_creation_routes.py",
    ROOT / "packages/persistence/src/holm_persistence/download_task_repository.py",
)


def assert_shared_normalization(sources: dict[Path, str]) -> None:
    assert set(sources) == set(PATHS), "download source discovery is incomplete"
    forbidden = {
        "strip",
        "lstrip",
        "rstrip",
        "lower",
        "upper",
        "casefold",
        "normalize",
        "normalize_source_key",
        "normalize_requested_revision",
    }
    calls = [
        node
        for source in sources.values()
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
    ]
    assert not any(
        (isinstance(node.func, ast.Name) and node.func.id in forbidden)
        or (isinstance(node.func, ast.Attribute) and node.func.attr in forbidden)
        for node in calls
    ), "download creation defines another normalizer"
    assert any(
        isinstance(node.func, ast.Attribute) and node.func.attr == "register" for node in calls
    )


def test_TC_DL_01_creation_uses_registration_identity_without_second_normalizer() -> None:
    assert_shared_normalization({path: path.read_text() for path in PATHS})
    router = create_download_creation_router(cast(DownloadCreationService, object()))
    assert {
        (route.path, frozenset(route.methods))
        for route in router.routes
        if isinstance(route, APIRoute)
    } == {("/api/v1/downloads", frozenset({"POST"}))}


def test_TC_DL_01_positive_control_second_normalizer_is_detected() -> None:
    sources = {path: path.read_text() for path in PATHS}
    sources[PATHS[0]] = mutate_source(
        PATHS[0],
        [
            (
                "        requested_revision_norm = registered.requested_revision_norm",
                "        requested_revision_norm = registered.requested_revision_norm\n"
                "        requested_revision_norm = requested_revision_norm.strip()",
            ),
        ],
    )
    with pytest.raises(AssertionError, match="another normalizer"):
        assert_shared_normalization(sources)

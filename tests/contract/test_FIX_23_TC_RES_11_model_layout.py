"""FIX-23 / TC-RES-11: one path rule for preview, registration and both Worker gates."""

import ast
import inspect
from pathlib import Path

import pytest
from fixtures.model_layout import IMAGE_REPOSITORY, TEXT_REPOSITORY, resolved_layout
from holm_control_api import model_registration_service, model_resolution_routes
from holm_control_api.model_resolution import ResolutionSucceeded
from holm_control_api.model_resolution_service import ResolutionPreview
from holm_domain import LocalAvailability
from holm_domain.artifact_manifest import ArtifactManifestError
from holm_domain.model_layout import DeployableLayout, deployable_layout
from holm_download_worker import artifact_publishing, worker


@pytest.mark.parametrize(
    ("repository", "unknown", "expected"),
    [
        (IMAGE_REPOSITORY, False, "unsupported"),
        (TEXT_REPOSITORY, False, "supported"),
        (IMAGE_REPOSITORY, True, "unknown"),
    ],
)
def test_FIX_23_TC_RES_11_preview_three_states(
    repository: str, unknown: bool, expected: str
) -> None:
    preview = ResolutionPreview(
        outcome=ResolutionSucceeded(resolved_layout(repository, unknown=unknown)),
        local_availability=LocalAvailability.ABSENT,
    )
    response = model_resolution_routes.render_preview(preview).model_dump(mode="json")
    assert response["deployable_layout"] == expected


@pytest.mark.parametrize(
    "tokenizer", ["tokenizer.json", "tokenizer_config.json", "tokenizer.model"]
)
@pytest.mark.parametrize("suffix", [".safetensors", ".bin", ".pt", ".pth", ".gguf"])
def test_FIX_23_TC_RES_11_original_rule_keeps_nested_weights(tokenizer: str, suffix: str) -> None:
    assert (
        deployable_layout(["config.json", tokenizer, "nested/weights" + suffix])
        is DeployableLayout.SUPPORTED
    )
    assert (
        deployable_layout(["nested/config.json", tokenizer, "weights" + suffix])
        is DeployableLayout.UNSUPPORTED
    )
    assert (
        deployable_layout(["config.json", "nested/" + tokenizer, "weights" + suffix])
        is DeployableLayout.UNSUPPORTED
    )


@pytest.mark.parametrize("path", ["/private/credential", "../secret", "./config.json", "a//b"])
def test_FIX_23_TC_RES_11_noncanonical_paths_fail_without_echo(path: str) -> None:
    with pytest.raises(ArtifactManifestError) as caught:
        deployable_layout([path])
    assert path not in str(caught.value)


def _assert_shared_call(source: str, function: str) -> None:
    tree = ast.parse(source)
    assert any(
        isinstance(node, ast.ImportFrom)
        and node.module == "holm_domain.model_layout"
        and any(alias.name == "deployable_layout" for alias in node.names)
        for node in tree.body
    )
    target = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function
    )
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "deployable_layout"
        for node in ast.walk(target)
    )
    assert not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "deployable_layout"
        for node in ast.walk(tree)
    )


def test_FIX_23_TC_RES_11_four_consumers_import_and_call_one_rule() -> None:
    for module, function in (
        (artifact_publishing, "_required_configuration"),
        (model_resolution_routes, "render_preview"),
        (model_registration_service, "register"),
        (worker, "process"),
    ):
        assert module.deployable_layout is deployable_layout
        source = inspect.getsource(module)
        _assert_shared_call(source, function)
        # A second rule may initially return identical values: still reject the copied definition.
        mutant = source.replace("deployable_layout(", "copied_layout(")
        mutant += "\n" + inspect.getsource(deployable_layout).replace(
            "deployable_layout(", "copied_layout("
        )
        with pytest.raises(AssertionError):
            _assert_shared_call(mutant, function)
    frontend = (
        Path(__file__).resolve().parents[2] / "apps/web/src/model-selection/selection-step.tsx"
    ).read_text()
    assert 'preview.deployable_layout === "unsupported"' in frontend
    assert "WEIGHT_SUFFIXES" not in frontend


def test_FIX_23_TC_RES_11_positive_control_always_supported_is_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        model_resolution_routes, "deployable_layout", lambda paths: DeployableLayout.SUPPORTED
    )
    with pytest.raises(AssertionError):
        test_FIX_23_TC_RES_11_preview_three_states(IMAGE_REPOSITORY, False, "unsupported")

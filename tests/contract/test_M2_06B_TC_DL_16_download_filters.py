"""TC-DL-16: the download child has one closed, Runtime-owned filter set."""

from __future__ import annotations

import ast
from dataclasses import fields
from pathlib import Path
from typing import Final

import pytest
from holm_download_worker import (
    DOWNLOAD_FILTER_RULES,
    DownloadFilterRules,
    DownloadSubprocessRequest,
)
from holm_runtimes import load_runtime_compatibility

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
DOWNLOAD_SOURCE: Final[Path] = (
    REPOSITORY_ROOT / "apps/workers/download/src/holm_download_worker/download_subprocess.py"
)
RUNTIME_COMPAT: Final[Path] = REPOSITORY_ROOT / "runtime-compat.yaml"

EXPECTED_ARGUMENTS: Final[dict[str, dict[str, str]]] = {
    "_download_hugging_face": {
        "allow_patterns": "hugging_face_allow_patterns",
        "ignore_patterns": "hugging_face_ignore_patterns",
    },
    "_download_modelscope": {
        "allow_patterns": "modelscope_allow_patterns",
        "ignore_patterns": "modelscope_ignore_patterns",
        "allow_file_pattern": "modelscope_allow_file_pattern",
        "ignore_file_pattern": "modelscope_ignore_file_pattern",
    },
}


def assert_download_filters_are_closed(source: str) -> None:
    tree = ast.parse(source)
    methods = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in EXPECTED_ARGUMENTS
    }
    assert set(methods) == set(EXPECTED_ARGUMENTS)
    for method_name, expected in EXPECTED_ARGUMENTS.items():
        calls = [
            node
            for node in ast.walk(methods[method_name])
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "snapshot_download"
        ]
        assert len(calls) == 1
        keywords = {item.arg: item.value for item in calls[0].keywords if item.arg}
        assert expected.keys() <= keywords.keys()
        for argument, field_name in expected.items():
            value = keywords[argument]
            assert isinstance(value, ast.Call)
            assert isinstance(value.func, ast.Name) and value.func.id == "_sdk_patterns"
            assert len(value.args) == 1
            rule = value.args[0]
            assert isinstance(rule, ast.Attribute) and rule.attr == field_name
            owner = rule.value
            assert isinstance(owner, ast.Attribute) and owner.attr == "filter_rules"
            assert isinstance(owner.value, ast.Name) and owner.value.id == "self"

    rule_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "DownloadFilterRules"
    )
    defaults = {
        node.target.id: node.value
        for node in rule_class.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.value is not None
    }
    assert set(defaults) == {
        field_name for expected in EXPECTED_ARGUMENTS.values() for field_name in expected.values()
    }
    assert all(isinstance(value, ast.Tuple) and not value.elts for value in defaults.values())


def test_TC_DL_16_download_filters_remain_empty_and_not_request_fields() -> None:
    assert load_runtime_compatibility(RUNTIME_COMPAT).runtime("vllm") is not None
    assert all(
        getattr(DOWNLOAD_FILTER_RULES, item.name) == () for item in fields(DownloadFilterRules)
    )
    assert not (
        {item.name for item in fields(DownloadFilterRules)}
        & {item.name for item in fields(DownloadSubprocessRequest)}
    )
    assert_download_filters_are_closed(DOWNLOAD_SOURCE.read_text(encoding="utf-8"))


def test_TC_DL_16_positive_control_user_input_on_any_modelscope_filter_fails() -> None:
    source = DOWNLOAD_SOURCE.read_text(encoding="utf-8")
    for field_name in EXPECTED_ARGUMENTS["_download_modelscope"].values():
        mutated = source.replace(
            f"self.filter_rules.{field_name}",
            "request.repository_id",
            1,
        )
        assert mutated != source
        with pytest.raises(AssertionError):
            assert_download_filters_are_closed(mutated)


def test_TC_DL_16_positive_control_nonempty_default_rule_fails() -> None:
    source = DOWNLOAD_SOURCE.read_text(encoding="utf-8")
    mutated = source.replace(
        "modelscope_ignore_file_pattern: tuple[str, ...] = ()",
        'modelscope_ignore_file_pattern: tuple[str, ...] = ("*.ckpt",)',
    )
    assert mutated != source
    with pytest.raises(AssertionError):
        assert_download_filters_are_closed(mutated)

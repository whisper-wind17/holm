"""TC-SEC-21 layered non-sensitive configuration contract tests."""

from __future__ import annotations

import copy
import os
from collections.abc import Callable
from pathlib import Path
from typing import Final

import pytest
from fixtures.source_mutation import load_mutated_module
from holm_common.config import MIN_RETENTION_DAYS, ConfigLoadError, load_config

BASE_CONFIG = {"public_base_url": "https://ai.example.test"}
REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
CONFIG_SOURCE: Final[Path] = REPOSITORY_ROOT / "packages/common/src/holm_common/config.py"
REALPATH_RULE_SOURCE: Final[str] = (
    "        if any(os.path.realpath(path) != str(path) for path in value):\n"
    '            raise ValueError("all paths must already be their own realpath")\n'
)


def test_TC_SEC_21_defaults_mapping_environment_priority_and_immutability() -> None:
    source = {
        **BASE_CONFIG,
        "gateway": {"max_request_body_mib": 12},
        "storage": {"external_model_roots": ["/srv/from-config"]},
    }
    original = copy.deepcopy(source)

    config = load_config(
        source,
        environ={
            "HOLM__GATEWAY__MAX_REQUEST_BODY_MIB": "8",
            "HOLM__STORAGE__EXTERNAL_MODEL_ROOTS": '["/srv/from-env"]',
        },
    )

    assert config.gateway.max_request_body_mib == 8
    assert config.gateway.max_inflight_requests == 512
    assert config.storage.external_model_roots == (Path("/srv/from-env"),)
    assert config.timezone_default == "Asia/Shanghai"
    assert source == original


def test_TC_SEC_21_auto_or_positive_integer_environment_values() -> None:
    automatic = load_config(
        BASE_CONFIG,
        environ={"HOLM__RUNTIME__MAX_GPUS_PER_INSTANCE": "auto"},
    )
    bounded = load_config(
        BASE_CONFIG,
        environ={"HOLM__RUNTIME__MAX_GPUS_PER_INSTANCE": "8"},
    )

    assert automatic.runtime.max_gpus_per_instance == "auto"
    assert bounded.runtime.max_gpus_per_instance == 8


@pytest.mark.parametrize(
    "section,field",
    [("retention", "request_records_days"), ("telemetry", "aggregate_retention_days")],
)
def test_FIX_11_retention_minimum_configuration(section: str, field: str) -> None:
    assert MIN_RETENTION_DAYS == 30
    assert getattr(getattr(load_config(BASE_CONFIG, environ={}), section), field) == 30
    assert (
        getattr(
            getattr(load_config({**BASE_CONFIG, section: {field: 30}}, environ={}), section), field
        )
        == 30
    )

    with pytest.raises(ConfigLoadError) as captured:
        load_config({**BASE_CONFIG, section: {field: 29}}, environ={})
    assert captured.value.code == "validation_failed"
    assert f"{section}.{field}" in captured.value.fields


@pytest.mark.parametrize(
    "invalid_config,field",
    [
        ({**BASE_CONFIG, "unknown": 1}, "unknown"),
        ({**BASE_CONFIG, "gateway": {"unknown": 1}}, "gateway.unknown"),
        ({"public_base_url": "http://ai.example.test"}, "public_base_url"),
        ({**BASE_CONFIG, "timezone_default": "Not/A-Timezone"}, "timezone_default"),
        ({**BASE_CONFIG, "storage": {"model_root": "relative"}}, "storage.model_root"),
        (
            {
                **BASE_CONFIG,
                "storage": {"disk_warning_percent": 5, "disk_critical_percent": 5},
            },
            "storage",
        ),
        ({**BASE_CONFIG, "gateway": {"max_inflight_requests": 0}}, "gateway.max_inflight_requests"),
    ],
)
def test_TC_SEC_21_closed_schema_and_value_boundaries(
    invalid_config: dict[str, object], field: str
) -> None:
    with pytest.raises(ConfigLoadError) as captured:
        load_config(invalid_config, environ={})

    assert captured.value.code == "validation_failed"
    assert field in captured.value.fields


@pytest.mark.parametrize(
    "environment,code",
    [
        ({"HOLM__GATEWAY__UNKNOWN": "value"}, "unknown_environment_field"),
        ({"HOLM__MASTER_KEY": "value"}, "sensitive_environment_field"),
        ({"HOLM__": "value"}, "unknown_environment_field"),
    ],
)
def test_TC_SEC_21_unknown_and_sensitive_environment_fields_fail_closed(
    environment: dict[str, str], code: str
) -> None:
    with pytest.raises(ConfigLoadError) as captured:
        load_config(BASE_CONFIG, environ=environment)

    assert captured.value.code == code


def test_TC_SEC_21_environment_value_is_absent_from_error_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "TC_SEC_21_ENV_VALUE_MUST_NOT_BE_REPORTED"

    with pytest.raises(ConfigLoadError) as captured:
        load_config(
            BASE_CONFIG,
            environ={"HOLM__GATEWAY__MAX_INFLIGHT_REQUESTS": sentinel},
        )

    assert sentinel not in str(captured.value)
    assert sentinel not in repr(captured.value)
    assert sentinel not in caplog.text


def _config_with_roots(roots: list[str]) -> dict[str, object]:
    return {**BASE_CONFIG, "storage": {"external_model_roots": roots}}


def assert_external_roots_are_their_own_realpath(
    loader: Callable[..., object],
    error_type: type[Exception],
    root: str,
) -> None:
    """The one judgement shared by the real case and its positive control.

    TDD 10.2 compares a candidate path to the allowed roots *after*
    ``realpath``.  A root reached through a symbolic link would never match its
    own resolved form, and re-pointing that link silently moves the whole
    Allowlist, so a configured root must already name the real path
    (M2-07A).

    The refusal is raised as an ``AssertionError`` rather than through
    ``pytest.raises`` so that the positive control below can catch it: a
    ``pytest.fail`` outcome is not an ``AssertionError`` and would escape.
    """

    try:
        loader(_config_with_roots([root]), environ={})
    except error_type as failure:
        assert getattr(failure, "code", None) == "validation_failed"
        assert "storage.external_model_roots" in getattr(failure, "fields", ())
        return
    raise AssertionError("an External Root that is not its own realpath was accepted")


def _symlinked_roots(tmp_path: Path) -> tuple[Path, Path]:
    # ``tmp_path`` itself may sit under a symbolic link; resolve the base so
    # the only link in play is the one this case creates.
    base = Path(os.path.realpath(tmp_path))
    real_root = base / "real-models"
    real_root.mkdir()
    linked_root = base / "linked-models"
    linked_root.symlink_to(real_root, target_is_directory=True)
    return real_root, linked_root


def test_TC_SEC_21_TC_DL_26_a_symlinked_external_root_is_refused(tmp_path: Path) -> None:
    real_root, linked_root = _symlinked_roots(tmp_path)

    # The same directory named by its real path is accepted, so the rule
    # discriminates rather than refusing every path it is given.
    accepted = load_config(_config_with_roots([str(real_root)]), environ={})
    assert accepted.storage.external_model_roots == (real_root,)

    assert_external_roots_are_their_own_realpath(load_config, ConfigLoadError, str(linked_root))
    # An unnormalized spelling of a real directory is refused for the same
    # reason: it is not its own realpath.
    assert_external_roots_are_their_own_realpath(
        load_config,
        ConfigLoadError,
        str(real_root / ".." / "real-models"),
    )


def test_TC_SEC_21_TC_DL_26_positive_control_dropping_the_realpath_rule_fails_the_guard(
    tmp_path: Path,
) -> None:
    """Remove the check from production; the guard must fail loudly."""

    _, linked_root = _symlinked_roots(tmp_path)
    module = load_mutated_module(
        CONFIG_SOURCE,
        [(REALPATH_RULE_SOURCE, "")],
        module_name="_unresolved_root_config",
        destination=tmp_path / "unresolved_root_config.py",
    )

    with pytest.raises(AssertionError):
        assert_external_roots_are_their_own_realpath(
            module.load_config,
            module.ConfigLoadError,
            str(linked_root),
        )


@pytest.mark.parametrize("raw_value", ["true", "8.0", '" 8 "'])
def test_TC_SEC_21_integer_environment_values_are_strict(raw_value: str) -> None:
    with pytest.raises(ConfigLoadError) as captured:
        load_config(
            BASE_CONFIG,
            environ={"HOLM__GATEWAY__MAX_INFLIGHT_REQUESTS": raw_value},
        )

    assert captured.value.code == "validation_failed"
    assert "gateway.max_inflight_requests" in captured.value.fields
    assert raw_value not in str(captured.value)

"""M7-01B Docker Secret directory and sanitized process startup diagnostics."""

from __future__ import annotations

import argparse
import importlib
import os
import stat
from pathlib import Path

import pytest
from holm_common import SecretFileError
from holm_common.secrets import _validate_directory
from holm_contracts.release import PRODUCTION_SERVICE_IDS


def _metadata(*, uid: int, mode: int) -> os.stat_result:
    fields = list(os.stat("/usr"))
    fields[0] = stat.S_IFDIR | mode
    fields[4] = uid
    fields[5] = 0
    return os.stat_result(fields)


def test_M7_01B_root_owned_nonwritable_secret_parent_is_allowed() -> None:
    metadata = os.stat("/usr")
    assert metadata.st_uid == 0
    assert stat.S_IMODE(metadata.st_mode) == 0o755
    expected_uid = os.geteuid() or PRODUCTION_SERVICE_IDS["control-api"][0]
    assert expected_uid != 0

    _validate_directory(metadata, expected_uid, os.getegid())


@pytest.mark.parametrize("uid,mode", [(0, 0o775), (0, 0o757), (12345, 0o755)])
def test_M7_01B_rejects_writable_or_untrusted_secret_parent(uid: int, mode: int) -> None:
    with pytest.raises(SecretFileError) as captured:
        _validate_directory(_metadata(uid=uid, mode=mode), os.geteuid(), os.getegid())

    assert captured.value.code in {"owner_mismatch", "directory_permissions"}


@pytest.mark.parametrize(
    "module_name,configuration_module,component",
    [
        ("holm_control_api", "holm_control_api.__main__", "control API"),
        ("holm_gateway", "holm_gateway.configuration", "gateway"),
        ("holm_download_worker", "holm_download_worker.configuration", "download worker"),
        (
            "holm_instance_controller",
            "holm_instance_controller.configuration",
            "instance controller",
        ),
        ("holm_ledger_writer", "holm_ledger_writer.configuration", "ledger writer"),
        (
            "holm_maintenance_worker",
            "holm_maintenance_worker.configuration",
            "maintenance worker",
        ),
    ],
)
def test_M7_01B_missing_secret_startup_reports_only_code(
    module_name: str,
    configuration_module: str,
    component: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entrypoint = importlib.import_module(f"{module_name}.__main__")
    configuration = importlib.import_module(configuration_module)
    missing_path = tmp_path / "missing-secret-must-not-appear"

    async def fail_on_secret(_: object) -> None:
        loader = (
            configuration._load_database_url
            if module_name == "holm_control_api"
            else configuration.load_database_url
        )
        loader(missing_path)

    monkeypatch.setattr(entrypoint, "_run", fail_on_secret)
    monkeypatch.setattr(entrypoint, "_parse_args", lambda *_: argparse.Namespace())
    if module_name == "holm_download_worker":
        monkeypatch.setattr(entrypoint, "_configuration", lambda _: None)

    with pytest.raises(SystemExit) as captured:
        entrypoint.main([])

    assert captured.value.code == 1
    message = capsys.readouterr().err
    assert f"{component} startup failed: database_secret_unavailable:unreadable" in message
    assert str(missing_path) not in message


def test_M7_01B_unknown_startup_error_exposes_only_class(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entrypoint = importlib.import_module("holm_control_api.__main__")
    missing_path = tmp_path / "third-party-error-path"

    async def fail_other(_: object) -> None:
        raise ValueError(str(missing_path))

    monkeypatch.setattr(entrypoint, "_run", fail_other)
    monkeypatch.setattr(entrypoint, "_parse_args", lambda *_: argparse.Namespace())
    with pytest.raises(SystemExit):
        entrypoint.main([])

    message = capsys.readouterr().err
    assert "control API startup failed: ValueError" in message
    assert str(missing_path) not in message

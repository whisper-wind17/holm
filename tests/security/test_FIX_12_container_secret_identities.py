"""FIX-12: production service identities must isolate file-level secrets."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from holm_contracts.release import PRODUCTION_SERVICE_IDS

DATABASE_LOADERS = {
    "control-api": ("holm_control_api.__main__", "_load_database_url"),
    "gateway": ("holm_gateway.configuration", "load_database_url"),
    "download-worker": ("holm_download_worker.configuration", "load_database_url"),
    "instance-controller": ("holm_instance_controller.configuration", "load_database_url"),
    "ledger-writer": ("holm_ledger_writer.configuration", "load_database_url"),
    "maintenance-worker": ("holm_maintenance_worker.configuration", "load_database_url"),
}


@pytest.mark.parametrize("service_name", tuple(DATABASE_LOADERS))
def test_FIX_12_secret_read_succeeds_for_owner_and_denies_other_service(
    service_name: str,
) -> None:
    uid, gid = PRODUCTION_SERVICE_IDS[service_name]
    other_name = "gateway" if service_name == "maintenance-worker" else "maintenance-worker"
    other_uid, other_gid = PRODUCTION_SERVICE_IDS[other_name]
    module_name, loader_name = DATABASE_LOADERS[service_name]

    with tempfile.TemporaryDirectory(prefix="fix-12-secret-") as directory_name:
        directory = Path(directory_name)
        directory.chmod(0o755)
        secret = directory / "database_url"
        secret.write_text("postgresql+psycopg://user:password@postgres/holm", encoding="utf-8")
        os.chown(secret, uid, gid)
        secret.chmod(0o400)
        command = [
            sys.executable,
            "-c",
            (
                "import importlib, sys; "
                f"loader = getattr(importlib.import_module({module_name!r}), {loader_name!r}); "
                "loader(__import__('pathlib').Path(sys.argv[1]))"
            ),
            str(secret),
        ]

        def run_as(target_uid: int, target_gid: int) -> subprocess.CompletedProcess[str]:
            def change_identity() -> None:
                os.setgroups([])
                os.setgid(target_gid)
                os.setuid(target_uid)

            return subprocess.run(
                command, check=False, capture_output=True, text=True, preexec_fn=change_identity
            )

        owner = run_as(uid, gid)
        other = run_as(other_uid, other_gid)

        assert owner.returncode == 0, owner.stderr
        assert other.returncode != 0
        assert "password" not in other.stderr


def test_FIX_12_master_keyring_is_root_owned_and_shared_only_by_its_gid() -> None:
    with tempfile.TemporaryDirectory(prefix="fix-12-master-") as directory_name:
        directory = Path(directory_name)
        directory.chmod(0o755)
        secret = directory / "master_keyring"
        secret.write_bytes(b"keyring fixture")
        shared_gid = PRODUCTION_SERVICE_IDS["control-api"][1]
        assert shared_gid == PRODUCTION_SERVICE_IDS["download-worker"][1]
        os.chown(secret, 0, shared_gid)
        secret.chmod(0o440)
        command = [
            sys.executable,
            "-c",
            (
                "import os, sys; from holm_common import read_secret_file; "
                "read_secret_file(sys.argv[1], expected_uid=0, "
                "expected_gid=os.getegid(), allowed_modes={0o440})"
            ),
            str(secret),
        ]

        def run_as(service_name: str) -> subprocess.CompletedProcess[str]:
            uid, gid = PRODUCTION_SERVICE_IDS[service_name]

            def change_identity() -> None:
                os.setgroups([])
                os.setgid(gid)
                os.setuid(uid)

            return subprocess.run(
                command, check=False, capture_output=True, text=True, preexec_fn=change_identity
            )

        for service_name in ("control-api", "download-worker"):
            result = run_as(service_name)
            assert result.returncode == 0, result.stderr
        assert run_as("gateway").returncode != 0


@pytest.mark.parametrize("service_name", ("control-api", "gateway"))
def test_FIX_12_pepper_keyring_is_owner_only_for_each_service(service_name: str) -> None:
    uid, gid = PRODUCTION_SERVICE_IDS[service_name]
    other_name = "gateway" if service_name == "control-api" else "control-api"
    other_uid, other_gid = PRODUCTION_SERVICE_IDS[other_name]

    with tempfile.TemporaryDirectory(prefix="fix-12-pepper-") as directory_name:
        directory = Path(directory_name)
        directory.chmod(0o755)
        secret = directory / "api_key_pepper_keyring"
        secret.write_bytes(b"pepper fixture")
        os.chown(secret, uid, gid)
        secret.chmod(0o600)
        command = [
            sys.executable,
            "-c",
            (
                "import sys; from holm_common import read_secret_file; "
                "read_secret_file(sys.argv[1], allowed_modes={0o600})"
            ),
            str(secret),
        ]

        def run_as(target_uid: int, target_gid: int) -> subprocess.CompletedProcess[str]:
            def change_identity() -> None:
                os.setgroups([])
                os.setgid(target_gid)
                os.setuid(target_uid)

            return subprocess.run(
                command, check=False, capture_output=True, text=True, preexec_fn=change_identity
            )

        owner = run_as(uid, gid)
        assert owner.returncode == 0, owner.stderr
        assert run_as(other_uid, other_gid).returncode != 0

"""M7-01B Control API production composition-root tests."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
from holm_common import PlatformConfig, load_config
from holm_contracts import ReleaseManifest
from holm_control_api import __main__ as entrypoint
from holm_control_api.state import STATE_ATTRIBUTE
from holm_security import (
    ApiKeyPepperKeyring,
    MasterKey,
    MasterKeyring,
    MasterKeyState,
)
from sqlalchemy.engine import make_url

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _release_manifest() -> ReleaseManifest:
    digest = "1" * 64
    return ReleaseManifest.model_validate(
        {
            "schema_version": 1,
            "build_version": "0.1.0+m701b",
            "commit": "2" * 40,
            "image_digests": [{"name": "holm/application", "digest": f"sha256:{digest}"}],
            "artifact_hashes": {
                "bundle": digest,
                "compose": digest,
                "caddyfile": digest,
                "systemd_units": [{"name": "holm-platform.service", "sha256": digest}],
            },
            "service_identities": [{"service": "control-api", "uid": 10003, "gid": 10003}],
            "default_resource_limits": [
                {
                    "service": "control-api",
                    "cpu_millicores": 1000,
                    "memory_mib": 512,
                    "pids": 256,
                    "log_rotation_mib": 10,
                    "tmpfs_mib": 64,
                }
            ],
            "runtime_compat_hash": digest,
            "python_lockfile_hash": digest,
            "node_lockfile_hash": digest,
            "gateway": {
                "max_inflight_requests": 64,
                "runtime_pool_max_connections_per_instance": 64,
                "boundary_semantics": ("reject_when_current_is_greater_than_or_equal_to_limit"),
            },
            "retention": {
                "request_records_days": 30,
                "download_tasks_days": 30,
                "telemetry_raw_seconds": 3600,
                "telemetry_aggregate_days": 30,
            },
            "default_timezone": "Asia/Shanghai",
        }
    )


def _platform_config() -> PlatformConfig:
    return load_config(
        {
            "public_base_url": "https://holm.invalid",
            "storage": {
                "model_root": "/var/lib/holm/models",
                "tokenizer_root": "/var/lib/holm/tokenizers",
                "external_model_roots": ["/var/lib/holm/external-models"],
                "disk_warning_percent": 10,
                "disk_critical_percent": 5,
                "disk_critical_free_gib": 5,
            },
        },
        environ={},
    )


def test_M7_01B_secret_is_read_only_from_required_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = tmp_path / "control_database_url"
    secret.write_text(
        "postgresql+psycopg://control:file-value@postgres/holm\n",
        encoding="utf-8",
    )
    secret.chmod(0o400)
    monkeypatch.setenv(
        "HOLM_DATABASE_URL",
        "postgresql+psycopg://control:environment-value@elsewhere/holm",
    )

    loaded = entrypoint._load_database_url(secret)

    assert loaded == make_url("postgresql+psycopg://control:file-value@postgres/holm")


def test_M7_01B_missing_secret_fails_loudly_even_with_environment_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "HOLM_DATABASE_URL",
        "postgresql+psycopg://control:environment-value@postgres/holm",
    )

    with pytest.raises(
        entrypoint.ControlApiConfigurationError,
        match="database_secret_unavailable",
    ):
        entrypoint._load_database_url(tmp_path / "missing-control-database-url")


def test_M7_01B_combined_nonsecret_config_keeps_platform_and_manifest_closed(
    tmp_path: Path,
) -> None:
    document = {
        "platform": _platform_config().model_dump(mode="json"),
        "release_manifest": _release_manifest().model_dump(mode="json"),
    }
    path = tmp_path / "platform.yaml"
    path.write_text(json.dumps(document), encoding="utf-8")

    assert entrypoint._load_platform_config(path) == _platform_config()
    assert entrypoint._load_release_manifest(path) == _release_manifest()


def test_M7_01B_production_readiness_excludes_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    master_keyring = MasterKeyring((MasterKey("m701b-master", MasterKeyState.WRITABLE, b"M" * 32),))
    pepper_keyring = ApiKeyPepperKeyring(
        current_key_id="m701b-pepper",
        keys={"m701b-pepper": b"P" * 32},
    )
    monkeypatch.setattr(entrypoint, "load_master_keyring", lambda *_args, **_kwargs: master_keyring)
    monkeypatch.setattr(
        entrypoint,
        "load_api_key_pepper_keyring",
        lambda *_args, **_kwargs: pepper_keyring,
    )
    args = argparse.Namespace(
        runtime_compatibility=PROJECT_ROOT / "runtime-compat.yaml",
        alembic_config=PROJECT_ROOT / "migrations" / "alembic.ini",
        agent_socket=Path("/run/holm/agent.sock"),
    )

    app, engine = entrypoint._build_application(
        args=args,
        configuration=_platform_config(),
        manifest=_release_manifest(),
        database_url=make_url("postgresql+psycopg://control:file-value@postgres/holm"),
        master_keyring_file=Path("/run/secrets/master_keyring"),
        api_key_pepper_keyring_file=Path("/run/secrets/api_key_pepper_keyring"),
        idempotency_secret=b"I" * 32,
    )

    state = getattr(app.state, STATE_ATTRIBUTE)
    assert state.readiness.dependency_names == ("database",)
    assert "agent" not in state.readiness.dependency_names
    assert engine is not None
    # M7-11B: production composition actually exposes the safe adapters.
    assert set(app.openapi()["paths"]["/api/v1/diagnostics"]) == {"get", "post"}
    assert "/api/v1/diagnostics/preview" in app.openapi()["paths"]

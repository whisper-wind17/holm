"""Deterministic Release Manifest and version payload checks for M0-08."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from holm_contracts import (
    NamedHash,
    ReleaseArtifactHashes,
    ReleaseManifest,
    version_payload_from_manifest,
)
from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HASH_A = "a" * 64
HASH_B = "b" * 64


def _load_build_script() -> ModuleType:
    script_path = PROJECT_ROOT / "scripts" / "build_artifacts.py"
    sys.path.insert(0, str(script_path.parent))
    try:
        spec = importlib.util.spec_from_file_location("m0_08_build_artifacts", script_path)
        if spec is None or spec.loader is None:
            raise AssertionError("could not load scripts/build_artifacts.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


def _valid_manifest_data() -> dict[str, object]:
    runtime_compat = PROJECT_ROOT / "runtime-compat.yaml"
    return {
        "schema_version": 1,
        "build_version": "0.0.0+gm00800000000",
        "commit": "8" * 40,
        "image_digests": [
            {"name": "platform", "digest": f"sha256:{HASH_A}"},
        ],
        "artifact_hashes": {
            "bundle": HASH_A,
            "compose": HASH_B,
            "caddyfile": HASH_A,
            "systemd_units": [{"name": "holm.service", "sha256": HASH_B}],
        },
        "service_identities": [{"service": "gateway", "uid": 21001, "gid": 21001}],
        "default_resource_limits": [
            {
                "service": "gateway",
                "cpu_millicores": 1000,
                "memory_mib": 512,
                "pids": 256,
                "log_rotation_mib": 10,
                "tmpfs_mib": 64,
            }
        ],
        "runtime_compat_hash": hashlib.sha256(runtime_compat.read_bytes()).hexdigest(),
        "python_lockfile_hash": HASH_A,
        "node_lockfile_hash": HASH_B,
        "gateway": {
            "max_inflight_requests": 512,
            "runtime_pool_max_connections_per_instance": 512,
            "boundary_semantics": "reject_when_current_is_greater_than_or_equal_to_limit",
        },
        "retention": {
            "request_records_days": 30,
            "download_tasks_days": 30,
            "telemetry_raw_seconds": 3600,
            "telemetry_aggregate_days": 30,
        },
        "default_timezone": "Asia/Shanghai",
    }


def test_M0_08_release_manifest_accepts_runtime_matrix_by_raw_hash() -> None:
    runtime_compat = PROJECT_ROOT / "runtime-compat.yaml"
    assert runtime_compat.is_file()

    manifest = ReleaseManifest.model_validate(_valid_manifest_data())

    assert manifest.runtime_compat_hash == hashlib.sha256(runtime_compat.read_bytes()).hexdigest()
    assert manifest.image_digests[0].name == "platform"


def test_M0_08_version_payload_is_projected_from_release_manifest() -> None:
    manifest = ReleaseManifest.model_validate(_valid_manifest_data())

    payload = version_payload_from_manifest(manifest)

    assert payload.model_dump(mode="json") == {
        "build_version": manifest.build_version,
        "commit": manifest.commit,
        "schema_version": manifest.schema_version,
        "runtime_compat_hash": manifest.runtime_compat_hash,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("build_version", "TBD"),
        ("image_digests", []),
        ("service_identities", []),
    ),
)
def test_M0_08_release_manifest_rejects_placeholders_and_empty_required_values(
    field: str, value: object
) -> None:
    data = _valid_manifest_data()
    data[field] = value

    with pytest.raises(ValidationError) as captured:
        ReleaseManifest.model_validate(data)

    assert field in str(captured.value)


def test_M0_08_release_manifest_rejects_unknown_fields() -> None:
    data = _valid_manifest_data()
    data["future_release_value"] = "must-not-be-silently-accepted"

    with pytest.raises(ValidationError) as captured:
        ReleaseManifest.model_validate(data)

    assert "future_release_value" in str(captured.value)


def test_M0_08_build_validation_and_release_source_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    data = copy.deepcopy(_valid_manifest_data())
    del data["runtime_compat_hash"]
    manifest_path = tmp_path / "missing-runtime-compat-hash.json"
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "build_artifacts.py"),
            "--validate-release-manifest",
            str(manifest_path),
        ],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "runtime_compat_hash" in result.stderr

    build_script = _load_build_script()
    release_output = tmp_path / "release-manifest.json"
    release_output.write_text("stale", encoding="utf-8")
    monkeypatch.setattr(build_script, "RELEASE_MANIFEST_OUTPUT", release_output)

    assert build_script._load_release_manifest_source(tmp_path / "missing.json") is None
    assert build_script._generate_release_manifest(None) is None
    assert not release_output.exists()
    captured_output = capsys.readouterr().out
    assert "generation skipped" in captured_output
    assert "deploy/release-manifest.source.json" in captured_output

    source_data = copy.deepcopy(_valid_manifest_data())
    for field in build_script.BUILD_INJECTED_MANIFEST_FIELDS:
        del source_data[field]
    del source_data["gateway"]
    source_path = tmp_path / "release-manifest.source.json"
    source_path.write_text(json.dumps(source_data), encoding="utf-8")
    artifact_hashes = ReleaseArtifactHashes(
        bundle=HASH_A,
        compose=HASH_B,
        caddyfile=HASH_A,
        systemd_units=(NamedHash(name="holm.service", sha256=HASH_B),),
    )
    loaded_source = build_script._load_release_manifest_source(source_path)
    assert loaded_source is not None

    with pytest.raises(RuntimeError, match="gateway"):
        build_script._release_manifest(loaded_source, artifact_hashes)

    source_data = copy.deepcopy(_valid_manifest_data())
    for field in build_script.BUILD_INJECTED_MANIFEST_FIELDS:
        del source_data[field]
    source_data["default_timezone"] = "TBD"

    with pytest.raises(RuntimeError, match="default_timezone"):
        build_script._release_manifest(source_data, artifact_hashes)


def test_RELEASE_07_build_manifest_uses_current_project_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_script = _load_build_script()
    source = _valid_manifest_data()
    hashes = ReleaseArtifactHashes.model_validate(source["artifact_hashes"])
    for field in build_script.BUILD_INJECTED_MANIFEST_FIELDS:
        del source[field]
    monkeypatch.setattr(build_script, "_build_commit", lambda: "a" * 40)
    manifest = build_script._release_manifest(source, hashes)
    assert manifest.build_version == "0.1.0rc1+gaaaaaaaaaaaa"

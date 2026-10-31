"""FX-SYS-03: M0-08 legacy manifest plus repository runtime compatibility data."""

import json
from pathlib import Path

import pytest
from holm_contracts import ReleaseManifest, version_payload_from_manifest
from holm_contracts.release import HostRequirements
from holm_runtimes import RuntimeCompatibilityError, load_runtime_compatibility
from pydantic import ValidationError
from test_m0_08_release_manifest import _valid_manifest_data


def test_SYS_03_TC_HEALTH_26_old_manifest_and_optional_minimums() -> None:
    legacy = ReleaseManifest.model_validate(_valid_manifest_data())
    assert legacy.host_requirements is None
    current = ReleaseManifest.model_validate(
        {
            **_valid_manifest_data(),
            "host_requirements": {"nvidia_driver": "580.65.06", "docker": "24.0"},
        }
    )
    assert current.host_requirements == HostRequirements(nvidia_driver="580.65.06", docker="24.0")
    assert version_payload_from_manifest(current) == version_payload_from_manifest(legacy)


@pytest.mark.parametrize(
    "requirements",
    [
        {"nvidia_driver": "580.65-rc1"},
        {"docker": "v24"},
        {"compose": "2..20"},
        {"nvidia_container_toolkit": 1.16},
        {"extra_internal_field": "1.0"},
    ],
)
def test_SYS_03_TC_HEALTH_26_manifest_rejects_bad_minimums(requirements) -> None:
    with pytest.raises(ValidationError):
        ReleaseManifest.model_validate(
            {**_valid_manifest_data(), "host_requirements": requirements}
        )


@pytest.mark.parametrize("minimum", ["580.65+cuda", "v580", 580, None])
def test_SYS_03_TC_HEALTH_27_runtime_rejects_bad_minimums(tmp_path: Path, minimum) -> None:
    data = json.loads(Path("runtime-compat.yaml").read_text())
    data["runtimes"][0]["minimum_driver_version"] = minimum
    path = tmp_path / "FX-SYS-03-runtime.json"
    path.write_text(json.dumps(data))
    with pytest.raises(RuntimeCompatibilityError, match="minimum_driver_version"):
        load_runtime_compatibility(path)


def test_SYS_03_TC_HEALTH_27_runtime_minimums_and_legacy(tmp_path: Path) -> None:
    catalog = load_runtime_compatibility(Path("runtime-compat.yaml"))
    assert [runtime.minimum_driver_version for runtime in catalog.runtimes.values()] == [
        "580.65.06",
        "580.65.06",
    ]
    data = json.loads(Path("runtime-compat.yaml").read_text())
    for runtime in data["runtimes"]:
        del runtime["minimum_driver_version"]
    path = tmp_path / "FX-SYS-03-legacy-runtime.json"
    path.write_text(json.dumps(data))
    legacy = load_runtime_compatibility(path)
    assert all(runtime.minimum_driver_version is None for runtime in legacy.runtimes.values())

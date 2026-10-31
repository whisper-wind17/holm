"""Fake integration and GPU-gated real NVML checks for M1-08."""

from __future__ import annotations

import ctypes.util
import re
import shutil
import subprocess
from dataclasses import dataclass

import pytest
from fixtures.fake_nvml import FakeNvml, FixtureId, scenario_for
from holm_contracts.agent.v1 import agent_pb2
from holm_host_agent.gpu_discovery import (
    DiscoveryState,
    GpuInventory,
    HealthState,
    NvmlErrorKind,
    PynvmlLibrary,
    TopologyState,
    discover_gpus,
    read_driver_version,
)
from holm_host_agent.host_info import get_host_info


@dataclass(frozen=True, slots=True)
class _SmiGpu:
    uuid: str
    model: str
    memory_mib: int
    driver_version: str


def _environment_block_reason(
    *,
    library_path: str | None,
    executable_path: str | None,
    command_result: subprocess.CompletedProcess[str] | None,
) -> str | None:
    if library_path is None:
        return "BLOCKED_ENV: libnvidia-ml is not discoverable"
    if executable_path is None:
        return "BLOCKED_ENV: nvidia-smi is not installed"
    if command_result is None or command_result.returncode != 0:
        return "BLOCKED_ENV: nvidia-smi cannot query the GPU environment"
    if not command_result.stdout.strip():
        return "BLOCKED_ENV: nvidia-smi returned no GPU rows"
    return None


def _query_nvidia_smi() -> tuple[_SmiGpu, ...]:
    executable = shutil.which("nvidia-smi")
    result = None
    if executable is not None:
        result = subprocess.run(
            (
                executable,
                "--query-gpu=uuid,name,memory.total,driver_version",
                "--format=csv,noheader,nounits",
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    reason = _environment_block_reason(
        library_path=ctypes.util.find_library("nvidia-ml"),
        executable_path=executable,
        command_result=result,
    )
    if reason is not None:
        pytest.xfail(reason)
    assert result is not None
    rows: list[_SmiGpu] = []
    for line in result.stdout.splitlines():
        uuid, model, memory_mib, driver_version = (
            field.strip() for field in line.split(",", maxsplit=3)
        )
        rows.append(
            _SmiGpu(
                uuid=uuid,
                model=model,
                memory_mib=int(memory_mib),
                driver_version=driver_version,
            )
        )
    return tuple(rows)


def _assert_real_inventory(inventory: GpuInventory, expected_uuids: set[str]) -> None:
    assert inventory.state is DiscoveryState.READY
    assert {device.uuid for device in inventory.devices} == expected_uuids


def test_M1_08_TC_GPU_17_FX_NV_08_production_translation_keeps_UUIDs_across_drift() -> None:
    fake = scenario_for(FixtureId.FX_NV_08).open()
    before = discover_gpus(fake)
    fake.advance()
    after = discover_gpus(fake)

    before_by_uuid = {device.uuid: device for device in before.devices}
    after_by_uuid = {device.uuid: device for device in after.devices}
    assert before_by_uuid.keys() == after_by_uuid.keys()
    assert all(
        before_by_uuid[gpu_uuid].display_index != after_by_uuid[gpu_uuid].display_index
        for gpu_uuid in before_by_uuid
    )


def test_M1_08_TC_GPU_18_real_NVML_discovery_and_topology_matches_nvidia_smi() -> None:
    smi_devices = _query_nvidia_smi()
    expected_by_uuid = {device.uuid: device for device in smi_devices}

    inventory = discover_gpus(PynvmlLibrary())
    _assert_real_inventory(inventory, set(expected_by_uuid))

    assert inventory.snapshot_id
    assert (
        len(inventory.topology_links) == len(inventory.devices) * (len(inventory.devices) - 1) // 2
    )
    assert all(device.health is HealthState.HEALTHY for device in inventory.devices)
    assert all(
        device.model == expected_by_uuid[device.uuid].model
        and device.memory_bytes // 1024**2 == expected_by_uuid[device.uuid].memory_mib
        for device in inventory.devices
    )
    assert all(link.gpu_a_uuid < link.gpu_b_uuid for link in inventory.topology_links)
    assert all(link.pcie_state is TopologyState.PRESENT for link in inventory.topology_links)
    assert all(link.pcie_relation is not None for link in inventory.topology_links)
    assert all(
        link.nvlink_state is not TopologyState.QUERY_FAILED for link in inventory.topology_links
    )
    assert all(link.p2p_read_state is TopologyState.PRESENT for link in inventory.topology_links)


def test_M1_08_TC_GPU_18_real_driver_version_matches_nvidia_smi() -> None:
    smi_devices = _query_nvidia_smi()
    expected_versions = {device.driver_version for device in smi_devices}
    observation = read_driver_version(PynvmlLibrary())

    assert len(expected_versions) == 1
    assert observation.value == expected_versions.pop()


def test_M1_09A_TC_GPU_17_real_cuda_driver_version_matches_nvidia_smi() -> None:
    executable = shutil.which("nvidia-smi")
    if executable is None:
        pytest.xfail("BLOCKED_ENV: nvidia-smi is not installed")
    result = subprocess.run(
        (executable,),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        pytest.xfail("BLOCKED_ENV: nvidia-smi cannot query the CUDA environment")
    match = re.search(r"CUDA Version:\s*([0-9]+\.[0-9]+)", result.stdout)
    if match is None:
        pytest.xfail("BLOCKED_ENV: nvidia-smi did not report CUDA Driver version")

    response = get_host_info(PynvmlLibrary())

    assert response.cuda_driver_version.state == agent_pb2.KNOWLEDGE_STATE_KNOWN
    assert response.cuda_driver_version.value == match.group(1)


def test_M1_08_TC_GPU_18_missing_library_is_explicit_BLOCKED_ENV_positive_control() -> None:
    reason = _environment_block_reason(
        library_path=None,
        executable_path="/usr/bin/nvidia-smi",
        command_result=subprocess.CompletedProcess(("nvidia-smi",), 0, "GPU-fixture\n", ""),
    )

    assert reason is not None
    assert reason.startswith("BLOCKED_ENV:")


def test_M1_08_TC_GPU_18_code_defect_is_failure_not_BLOCKED_ENV_positive_control() -> None:
    expected_uuids = {"GPU-expected"}
    fake = FakeNvml(
        scenario_for(FixtureId.FX_NV_02).snapshots,
        initialization_error=NvmlErrorKind.OTHER,
    )
    defective_result = discover_gpus(fake)

    assert (
        _environment_block_reason(
            library_path="libnvidia-ml.so.1",
            executable_path="/usr/bin/nvidia-smi",
            command_result=subprocess.CompletedProcess(("nvidia-smi",), 0, "GPU-expected\n", ""),
        )
        is None
    )
    with pytest.raises(AssertionError):
        _assert_real_inventory(defective_result, expected_uuids)

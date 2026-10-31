"""Deterministic N=1..8 GPU discovery properties required by M1-08."""

from __future__ import annotations

from dataclasses import replace

import pytest
from fixtures.fake_nvml import FakeNvml, FixtureId, scenario_for
from holm_host_agent.gpu_discovery import DiscoveryState, discover_gpus


def _fake_with_count(gpu_count: int) -> FakeNvml:
    source = scenario_for(FixtureId.FX_NV_04).snapshots[0]
    devices = source.devices[:gpu_count]
    uuids = {device.uuid for device in devices}
    links = tuple(
        link
        for link in source.topology_links
        if link.gpu_a_uuid in uuids and link.gpu_b_uuid in uuids
    )
    return FakeNvml((replace(source, devices=devices, topology_links=links),))


@pytest.mark.parametrize("gpu_count", tuple(range(1, 9)), ids=lambda count: f"N-{count}")
def test_M1_08_TC_GPU_17_FX_NV_04_every_N_from_1_through_8_is_complete(
    gpu_count: int,
) -> None:
    inventory = discover_gpus(_fake_with_count(gpu_count))

    assert inventory.state is DiscoveryState.READY
    assert len(inventory.devices) == gpu_count
    assert len({device.uuid for device in inventory.devices}) == gpu_count
    assert len(inventory.topology_links) == gpu_count * (gpu_count - 1) // 2
    assert all(device.memory_bytes > 0 for device in inventory.devices)


@pytest.mark.parametrize("gpu_count", (1, 8), ids=("fixed-N-1", "fixed-N-8"))
def test_M1_08_TC_GPU_17_FX_NV_04_fixed_endpoint_cases(gpu_count: int) -> None:
    inventory = discover_gpus(_fake_with_count(gpu_count))

    assert len(inventory.devices) == gpu_count
    assert len(inventory.topology_links) == gpu_count * (gpu_count - 1) // 2


def test_M1_08_TC_GPU_17_FX_NV_04_four_device_truncation_positive_control() -> None:
    expected = discover_gpus(_fake_with_count(8))
    truncated_devices = expected.devices[:4]

    with pytest.raises(AssertionError):
        assert {device.uuid for device in truncated_devices} == {
            device.uuid for device in expected.devices
        }

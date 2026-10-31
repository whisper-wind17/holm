"""Contract checks for all Fake NVML scenarios in TST section 3.2."""

from __future__ import annotations

import pytest
from fixtures.fake_nvml import (
    FIXTURE_REVIEW_STATUS,
    ExpectedInstanceState,
    FakeNvmlScenario,
    FixtureId,
    GpuHealth,
    NvmlSnapshot,
    scenario_for,
)


def _scenario(fixture_id: FixtureId) -> FakeNvmlScenario:
    scenario = scenario_for(fixture_id)
    assert scenario.fixture_id is fixture_id
    assert scenario.review_status == FIXTURE_REVIEW_STATUS == "待真机复核"
    return scenario


def _deployment_by_count(scenario: FakeNvmlScenario, gpu_count: int) -> tuple[str, ...]:
    matches = tuple(
        expectation.gpu_uuids
        for expectation in scenario.deployment_expectations
        if expectation.accepted and expectation.tensor_parallel_size == gpu_count
    )
    assert len(matches) == 1
    return matches[0]


def test_FX_NV_01_zero_devices_is_a_blocking_failure_not_a_normal_empty_state() -> None:
    scenario = _scenario(FixtureId.FX_NV_01)
    snapshot = scenario.open().current_snapshot

    assert snapshot.devices == ()
    assert not snapshot.can_complete_initialization
    assert len(snapshot.failure_items) == 1
    failure = snapshot.failure_items[0]
    assert failure.code == "gpu_detection_failed"
    assert failure.blocks_completion
    assert "初始化无法完成" in failure.message
    assert "已发现 0 张 GPU" not in failure.message
    with pytest.raises(ValueError, match="blocking failure item"):
        NvmlSnapshot(devices=())


def test_FX_NV_02_one_healthy_device_supports_the_single_card_probe() -> None:
    scenario = _scenario(FixtureId.FX_NV_02)
    fake_nvml = scenario.open()
    devices = fake_nvml.list_devices()

    assert len(devices) == 1
    assert devices[0].health is GpuHealth.HEALTHY
    assert fake_nvml.device_by_uuid(devices[0].uuid) is devices[0]
    assert _deployment_by_count(scenario, 1) == (devices[0].uuid,)


@pytest.mark.parametrize("gpu_count", (1, 2, 4), ids=("one-card", "two-card", "four-card"))
def test_FX_NV_03_four_homogeneous_devices_supply_1_2_4_card_TP_probes(
    gpu_count: int,
) -> None:
    scenario = _scenario(FixtureId.FX_NV_03)
    snapshot = scenario.open().current_snapshot
    selected_uuids = _deployment_by_count(scenario, gpu_count)
    selected_devices = tuple(snapshot.device_by_uuid(gpu_uuid) for gpu_uuid in selected_uuids)

    assert len(snapshot.devices) == 4
    assert len(selected_devices) == gpu_count
    assert len({device.model for device in selected_devices}) == 1
    assert len({device.memory_bytes for device in selected_devices}) == 1


_FX_NV_04_SCENARIO = scenario_for(FixtureId.FX_NV_04)
_FX_NV_04_GPU_COUNTS = tuple(
    expectation.tensor_parallel_size for expectation in _FX_NV_04_SCENARIO.deployment_expectations
)


@pytest.mark.parametrize(
    "gpu_count",
    _FX_NV_04_GPU_COUNTS,
    ids=tuple(f"gpu-count-{count}" for count in _FX_NV_04_GPU_COUNTS),
)
def test_FX_NV_04_eight_homogeneous_devices_cover_every_N_through_8(
    gpu_count: int,
) -> None:
    scenario = _scenario(FixtureId.FX_NV_04)
    snapshot = scenario.open().current_snapshot
    selected_uuids = _deployment_by_count(scenario, gpu_count)

    assert len(snapshot.devices) == 8
    assert tuple(range(1, len(snapshot.devices) + 1)) == _FX_NV_04_GPU_COUNTS
    assert _FX_NV_04_GPU_COUNTS[-1] == 8
    assert len(selected_uuids) == gpu_count
    assert all(
        snapshot.device_by_uuid(gpu_uuid).health is GpuHealth.HEALTHY for gpu_uuid in selected_uuids
    )


@pytest.mark.parametrize(
    ("case_id", "heterogeneous_field"),
    (
        ("heterogeneous-model", "model"),
        ("heterogeneous-memory", "memory"),
    ),
    ids=("model-mismatch", "memory-mismatch"),
)
def test_FX_NV_05_heterogeneous_multi_card_TP_probes_are_rejected(
    case_id: str,
    heterogeneous_field: str,
) -> None:
    scenario = _scenario(FixtureId.FX_NV_05)
    snapshot = scenario.open().current_snapshot
    expectation = next(item for item in scenario.deployment_expectations if item.case_id == case_id)
    devices = tuple(snapshot.device_by_uuid(gpu_uuid) for gpu_uuid in expectation.gpu_uuids)

    assert not expectation.accepted
    assert expectation.rejection_reason == "multi-GPU TP requires the same model and memory"
    if heterogeneous_field == "model":
        assert len({device.model for device in devices}) > 1
        assert len({device.memory_bytes for device in devices}) == 1
    else:
        assert len({device.model for device in devices}) == 1
        assert len({device.memory_bytes for device in devices}) > 1


def test_FX_NV_06_device_disappearance_marks_binding_unhealthy_without_replacement() -> None:
    scenario = _scenario(FixtureId.FX_NV_06)
    transition = scenario.instance_transition
    assert transition is not None
    fake_nvml = scenario.open()
    isolated_fake_nvml = scenario.open()
    before = fake_nvml.current_snapshot
    allocated_uuid = transition.allocation_gpu_uuids_before[0]

    assert before.device_by_uuid(allocated_uuid).health is GpuHealth.HEALTHY
    after = fake_nvml.advance()
    assert allocated_uuid not in after.device_uuids
    assert after.devices
    assert transition.state_before is ExpectedInstanceState.RUNNING
    assert transition.state_after is ExpectedInstanceState.UNHEALTHY
    assert transition.allocation_gpu_uuids_after == transition.allocation_gpu_uuids_before
    assert transition.automatic_replacement_gpu_uuids == ()
    assert set(transition.allocation_gpu_uuids_after).isdisjoint(after.device_uuids)
    assert allocated_uuid in isolated_fake_nvml.current_snapshot.device_uuids


def test_FX_NV_07_unhealthy_device_is_rejected_with_a_visible_reason() -> None:
    scenario = _scenario(FixtureId.FX_NV_07)
    snapshot = scenario.open().current_snapshot
    expectation = next(
        item for item in scenario.deployment_expectations if item.case_id == "unhealthy-device"
    )
    unhealthy_device = snapshot.device_by_uuid(expectation.gpu_uuids[0])

    assert unhealthy_device.health is GpuHealth.UNHEALTHY
    assert unhealthy_device.health_reason
    assert not expectation.accepted
    assert expectation.rejection_reason == unhealthy_device.health_reason


def test_FX_NV_08_index_drift_preserves_UUID_identity_allocation_and_instance_binding() -> None:
    scenario = _scenario(FixtureId.FX_NV_08)
    transition = scenario.instance_transition
    assert transition is not None
    fake_nvml = scenario.open()
    before = fake_nvml.current_snapshot
    devices_before = {gpu_uuid: before.device_by_uuid(gpu_uuid) for gpu_uuid in before.device_uuids}

    after = fake_nvml.advance()
    devices_after = {gpu_uuid: after.device_by_uuid(gpu_uuid) for gpu_uuid in after.device_uuids}

    assert devices_before.keys() == devices_after.keys()
    for gpu_uuid, device_before in devices_before.items():
        device_after = devices_after[gpu_uuid]
        assert device_before.display_index != device_after.display_index
        assert device_before == device_after
        assert hash(device_before) == hash(device_after)
        assert device_before.model == device_after.model
        assert device_before.memory_bytes == device_after.memory_bytes
    assert transition.instance_id == "fixture-instance-index-drift"
    assert transition.state_before is transition.state_after is ExpectedInstanceState.RUNNING
    assert transition.allocation_gpu_uuids_after == transition.allocation_gpu_uuids_before
    assert transition.automatic_replacement_gpu_uuids == ()

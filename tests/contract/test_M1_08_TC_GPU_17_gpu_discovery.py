"""M1-08 contract tests driven through the low-level FX-NV call boundary."""

from __future__ import annotations

from dataclasses import replace

import pytest
from fixtures.fake_nvml import FakeNvml, FixtureId, GpuHealth, QueryFailure, scenario_for
from holm_contracts.agent.v1 import agent_pb2
from holm_host_agent.gpu_discovery import (
    DiscoveryFailureReason,
    DiscoveryState,
    GpuInventory,
    HealthReason,
    HealthState,
    NvmlErrorKind,
    TopologyState,
    discover_gpus,
    inventory_to_proto,
)
from holm_host_agent.host_info import get_host_info


def _discover(fixture_id: FixtureId) -> GpuInventory:
    return discover_gpus(
        scenario_for(fixture_id).open(),
        snapshot_id_factory=lambda: f"snapshot-{fixture_id.value}",
    )


def _assert_identity_and_topology_without_display_index(
    response: agent_pb2.ListGPUsResponse,
    expected_uuids: set[str],
) -> None:
    assert response.discovery_state == agent_pb2.GPU_DISCOVERY_STATE_READY
    assert {device.uuid for device in response.devices} == expected_uuids
    assert {
        endpoint
        for link in response.topology_links
        for endpoint in (link.gpu_a_uuid, link.gpu_b_uuid)
    }.issubset(expected_uuids)
    assert len(response.topology_links) == len(expected_uuids) * (len(expected_uuids) - 1) // 2


def test_M1_08_TC_GPU_17_FX_NV_01_no_devices_and_unavailable_are_distinct() -> None:
    no_devices = _discover(FixtureId.FX_NV_01)
    unavailable_fake = FakeNvml(
        scenario_for(FixtureId.FX_NV_01).snapshots,
        initialization_error=NvmlErrorKind.LIBRARY_UNAVAILABLE,
    )
    unavailable = discover_gpus(unavailable_fake)

    assert no_devices.state is DiscoveryState.NO_DEVICES
    assert no_devices.failure_reason is DiscoveryFailureReason.NO_DEVICES
    assert no_devices.snapshot_id is not None
    assert unavailable.state is DiscoveryState.UNAVAILABLE
    assert unavailable.failure_reason is DiscoveryFailureReason.LIBRARY_UNAVAILABLE
    assert unavailable.snapshot_id is None
    assert inventory_to_proto(no_devices) != inventory_to_proto(unavailable)


def test_M1_08_TC_GPU_17_FX_NV_01_collapsed_empty_state_positive_control() -> None:
    no_devices = inventory_to_proto(_discover(FixtureId.FX_NV_01))
    unavailable_fake = FakeNvml(
        scenario_for(FixtureId.FX_NV_01).snapshots,
        initialization_error=NvmlErrorKind.LIBRARY_UNAVAILABLE,
    )
    unavailable = inventory_to_proto(discover_gpus(unavailable_fake))
    unavailable.discovery_state = no_devices.discovery_state
    unavailable.failure_reason = no_devices.failure_reason
    unavailable.snapshot_id = no_devices.snapshot_id

    with pytest.raises(AssertionError):
        assert unavailable != no_devices


@pytest.mark.parametrize("fixture_id", tuple(FixtureId), ids=lambda item: item.value)
def test_M1_08_TC_GPU_17_FX_NV_01_through_08_run_production_translation(
    fixture_id: FixtureId,
) -> None:
    inventory = _discover(fixture_id)
    if fixture_id is FixtureId.FX_NV_01:
        assert inventory.state is DiscoveryState.NO_DEVICES
        return

    scenario = scenario_for(fixture_id)
    expected = scenario.snapshots[0]
    assert inventory.state is DiscoveryState.READY
    assert {device.uuid for device in inventory.devices} == set(expected.device_uuids)
    assert len(inventory.topology_links) == len(expected.devices) * (len(expected.devices) - 1) // 2
    assert all(link.gpu_a_uuid < link.gpu_b_uuid for link in inventory.topology_links)


def test_M1_08_TC_GPU_17_FX_NV_04_eight_devices_have_exactly_28_links() -> None:
    inventory = _discover(FixtureId.FX_NV_04)

    assert len(inventory.devices) == 8
    assert len(inventory.topology_links) == 28


def test_M1_08_TC_GPU_17_FX_NV_08_index_drift_preserves_only_UUID_identity() -> None:
    fake = scenario_for(FixtureId.FX_NV_08).open()
    before = discover_gpus(fake, snapshot_id_factory=lambda: "before")
    fake.advance()
    after = discover_gpus(fake, snapshot_id_factory=lambda: "after")

    before_by_uuid = {device.uuid: device for device in before.devices}
    after_by_uuid = {device.uuid: device for device in after.devices}
    assert before_by_uuid.keys() == after_by_uuid.keys()
    assert all(
        before_by_uuid[gpu_uuid].display_index != after_by_uuid[gpu_uuid].display_index
        for gpu_uuid in before_by_uuid
    )
    assert {(link.gpu_a_uuid, link.gpu_b_uuid) for link in before.topology_links} == {
        (link.gpu_a_uuid, link.gpu_b_uuid) for link in after.topology_links
    }


def test_M1_08_TC_GPU_17_display_index_field_is_not_required_for_identity_logic() -> None:
    response = inventory_to_proto(_discover(FixtureId.FX_NV_04))
    expected_uuids = {device.uuid for device in response.devices}
    for device in response.devices:
        device.ClearField("display_index")

    _assert_identity_and_topology_without_display_index(response, expected_uuids)
    assert all(not device.HasField("display_index") for device in response.devices)


def test_M1_08_TC_GPU_17_FX_NV_07_health_is_three_state_and_structured() -> None:
    unhealthy = _discover(FixtureId.FX_NV_07)
    assert {device.health for device in unhealthy.devices} == {
        HealthState.HEALTHY,
        HealthState.UNHEALTHY,
    }
    unhealthy_device = next(
        device for device in unhealthy.devices if device.health is HealthState.UNHEALTHY
    )
    assert unhealthy_device.health_reasons == (HealthReason.ECC_ERRORS_DETECTED,)

    scenario = scenario_for(FixtureId.FX_NV_07)
    snapshot = scenario.snapshots[0]
    unknown_device = replace(
        snapshot.devices[0],
        health=GpuHealth.UNKNOWN,
        health_reason="query unsupported",
        health_query_failure=QueryFailure.UNSUPPORTED,
    )
    unknown_snapshot = replace(snapshot, devices=(unknown_device, *snapshot.devices[1:]))
    unknown = discover_gpus(scenario.open().__class__((unknown_snapshot,)))
    translated = next(device for device in unknown.devices if device.uuid == unknown_device.uuid)

    assert translated.health is HealthState.UNKNOWN
    assert translated.health_reasons == (HealthReason.QUERY_UNSUPPORTED,)
    assert translated.health is not HealthState.HEALTHY
    assert translated.health is not HealthState.UNHEALTHY


def test_M1_08_TC_GPU_17_unsupported_health_as_healthy_positive_control() -> None:
    scenario = scenario_for(FixtureId.FX_NV_07)
    snapshot = scenario.snapshots[0]
    unknown_device = replace(
        snapshot.devices[0],
        health=GpuHealth.UNKNOWN,
        health_reason="query unsupported",
        health_query_failure=QueryFailure.UNSUPPORTED,
    )
    fake = scenario.open().__class__(
        (replace(snapshot, devices=(unknown_device, *snapshot.devices[1:])),)
    )
    actual = next(
        device for device in discover_gpus(fake).devices if device.uuid == unknown_device.uuid
    )
    degraded_health = HealthState.HEALTHY

    with pytest.raises(AssertionError):
        assert degraded_health is actual.health


def test_M1_08_TC_GPU_17_topology_states_distinguish_absent_unsupported_and_failed() -> None:
    scenario = scenario_for(FixtureId.FX_NV_03)
    absent = discover_gpus(scenario.open())
    assert {link.nvlink_state for link in absent.topology_links} == {TopologyState.ABSENT}

    unsupported_snapshot = replace(
        scenario.snapshots[0], nvlink_query_failure=QueryFailure.UNSUPPORTED
    )
    unsupported = discover_gpus(scenario.open().__class__((unsupported_snapshot,)))
    assert {link.nvlink_state for link in unsupported.topology_links} == {TopologyState.UNSUPPORTED}

    failed_snapshot = replace(scenario.snapshots[0], nvlink_query_failure=QueryFailure.FAILED)
    failed = discover_gpus(scenario.open().__class__((failed_snapshot,)))
    assert {link.nvlink_state for link in failed.topology_links} == {TopologyState.QUERY_FAILED}
    assert TopologyState.ABSENT is not TopologyState.UNSUPPORTED
    assert TopologyState.UNSUPPORTED is not TopologyState.QUERY_FAILED


def test_M1_08_TC_GPU_17_topology_completeness_and_snapshot_guards_are_loud() -> None:
    inventory = _discover(FixtureId.FX_NV_04)
    with pytest.raises(ValueError, match="every normalized pair"):
        replace(inventory, topology_links=inventory.topology_links[:-1])

    hanging = replace(
        inventory.topology_links[0],
        gpu_b_uuid="GPU-not-in-this-snapshot",
    )
    with pytest.raises(ValueError, match="every normalized pair"):
        replace(inventory, topology_links=(hanging, *inventory.topology_links[1:]))

    collapsed = replace(inventory.topology_links[0], nvlink_state=TopologyState.ABSENT)
    with pytest.raises(AssertionError):
        assert collapsed.nvlink_state is TopologyState.UNSUPPORTED


def test_M1_08_TC_GPU_17_ListGPUsRequest_has_no_caller_fields() -> None:
    assert len(agent_pb2.ListGPUsRequest.DESCRIPTOR.fields) == 0


def test_M1_08_TC_GPU_17_driver_version_known_unavailable_and_empty_are_explicit() -> None:
    snapshots = scenario_for(FixtureId.FX_NV_02).snapshots
    known = get_host_info(FakeNvml(snapshots, driver_version="fixture-driver"))
    unavailable = get_host_info(
        FakeNvml(snapshots, initialization_error=NvmlErrorKind.LIBRARY_UNAVAILABLE)
    )
    empty = get_host_info(FakeNvml(snapshots, driver_version="  "))

    assert known.driver_version.state == agent_pb2.KNOWLEDGE_STATE_KNOWN
    assert known.driver_version.value == "fixture-driver"
    assert unavailable.driver_version.state == agent_pb2.KNOWLEDGE_STATE_UNKNOWN
    assert unavailable.driver_version.unknown_reason == (
        agent_pb2.OBSERVATION_UNKNOWN_REASON_SOURCE_UNAVAILABLE
    )
    assert empty.driver_version.state == agent_pb2.KNOWLEDGE_STATE_UNKNOWN
    assert empty.driver_version.unknown_reason == agent_pb2.OBSERVATION_UNKNOWN_REASON_EMPTY_VALUE
    assert unavailable.driver_version != empty.driver_version
    assert unavailable.docker_version.unknown_reason == (
        agent_pb2.OBSERVATION_UNKNOWN_REASON_SOURCE_UNAVAILABLE
    )
    assert unavailable.toolkit_version.unknown_reason == (
        agent_pb2.OBSERVATION_UNKNOWN_REASON_SOURCE_UNAVAILABLE
    )

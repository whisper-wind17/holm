"""Constructed FX-NV-01 through FX-NV-08 scenarios from TST section 3.2."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from itertools import combinations
from types import MappingProxyType
from typing import Final
from uuid import UUID

from .models import (
    DeploymentExpectation,
    ExpectedInstanceState,
    FakeNvmlScenario,
    FixtureId,
    GpuDevice,
    GpuHealth,
    InitializationFailure,
    InstanceTransitionExpectation,
    NvmlSnapshot,
    NvmlTopologyLink,
)

_FULL_MEMORY_BYTES: Final = 48 * 1024**3
_REDUCED_MEMORY_BYTES: Final = 24 * 1024**3
_DEFAULT_MODEL: Final = "fixture-accelerator-a"
_TOPOLOGY_REJECTION: Final = "multi-GPU TP requires the same model and memory"


def _gpu_uuid(fixture_id: FixtureId, ordinal: int) -> str:
    fixture_number = int(fixture_id.value.rsplit("-", maxsplit=1)[1])
    return f"GPU-{UUID(int=(fixture_number << 64) | (ordinal + 1))}"


def _device(
    fixture_id: FixtureId,
    ordinal: int,
    *,
    model: str = _DEFAULT_MODEL,
    memory_bytes: int = _FULL_MEMORY_BYTES,
    health: GpuHealth = GpuHealth.HEALTHY,
    health_reason: str | None = None,
) -> GpuDevice:
    return GpuDevice(
        uuid=_gpu_uuid(fixture_id, ordinal),
        display_index=ordinal,
        model=model,
        memory_bytes=memory_bytes,
        health=health,
        health_reason=health_reason,
    )


def _homogeneous_devices(fixture_id: FixtureId, count: int) -> tuple[GpuDevice, ...]:
    if count <= 0:
        raise ValueError("homogeneous Fake NVML device count must be positive")
    return tuple(_device(fixture_id, ordinal) for ordinal in range(count))


def _snapshot(
    devices: tuple[GpuDevice, ...],
    *,
    failure_items: tuple[InitializationFailure, ...] = (),
) -> NvmlSnapshot:
    links: list[NvmlTopologyLink] = []
    for first, second in combinations(devices, 2):
        gpu_a_uuid, gpu_b_uuid = sorted((first.uuid, second.uuid))
        links.append(NvmlTopologyLink(gpu_a_uuid=gpu_a_uuid, gpu_b_uuid=gpu_b_uuid))
    return NvmlSnapshot(
        devices=devices,
        failure_items=failure_items,
        topology_links=tuple(links),
    )


def _accepted_deployments(
    devices: tuple[GpuDevice, ...], counts: tuple[int, ...]
) -> tuple[DeploymentExpectation, ...]:
    return tuple(
        DeploymentExpectation(
            case_id=f"gpu-count-{count}",
            gpu_uuids=tuple(device.uuid for device in devices[:count]),
            tensor_parallel_size=count,
        )
        for count in counts
    )


def _FX_NV_01() -> FakeNvmlScenario:
    failure = InitializationFailure(
        code="gpu_detection_failed",
        message="未检测到可用加速设备，初始化无法完成，请检查设备或驱动后重试。",
    )
    return FakeNvmlScenario(
        fixture_id=FixtureId.FX_NV_01,
        snapshots=(_snapshot((), failure_items=(failure,)),),
    )


def _FX_NV_02() -> FakeNvmlScenario:
    devices = _homogeneous_devices(FixtureId.FX_NV_02, 1)
    return FakeNvmlScenario(
        fixture_id=FixtureId.FX_NV_02,
        snapshots=(_snapshot(devices),),
        deployment_expectations=_accepted_deployments(devices, (len(devices),)),
    )


def _FX_NV_03() -> FakeNvmlScenario:
    devices = _homogeneous_devices(FixtureId.FX_NV_03, 4)
    return FakeNvmlScenario(
        fixture_id=FixtureId.FX_NV_03,
        snapshots=(_snapshot(devices),),
        deployment_expectations=_accepted_deployments(devices, (1, 2, len(devices))),
    )


def _FX_NV_04() -> FakeNvmlScenario:
    devices = _homogeneous_devices(FixtureId.FX_NV_04, 8)
    supported_counts = tuple(range(1, len(devices) + 1))
    return FakeNvmlScenario(
        fixture_id=FixtureId.FX_NV_04,
        snapshots=(_snapshot(devices),),
        deployment_expectations=_accepted_deployments(devices, supported_counts),
    )


def _FX_NV_05() -> FakeNvmlScenario:
    devices = (
        _device(FixtureId.FX_NV_05, 0),
        _device(FixtureId.FX_NV_05, 1),
        _device(FixtureId.FX_NV_05, 2, model="fixture-accelerator-b"),
        _device(FixtureId.FX_NV_05, 3, memory_bytes=_REDUCED_MEMORY_BYTES),
    )
    return FakeNvmlScenario(
        fixture_id=FixtureId.FX_NV_05,
        snapshots=(_snapshot(devices),),
        deployment_expectations=(
            DeploymentExpectation(
                case_id="homogeneous",
                gpu_uuids=(devices[0].uuid, devices[1].uuid),
                tensor_parallel_size=2,
            ),
            DeploymentExpectation(
                case_id="heterogeneous-model",
                gpu_uuids=(devices[0].uuid, devices[2].uuid),
                tensor_parallel_size=2,
                accepted=False,
                rejection_reason=_TOPOLOGY_REJECTION,
            ),
            DeploymentExpectation(
                case_id="heterogeneous-memory",
                gpu_uuids=(devices[0].uuid, devices[3].uuid),
                tensor_parallel_size=2,
                accepted=False,
                rejection_reason=_TOPOLOGY_REJECTION,
            ),
        ),
    )


def _FX_NV_06() -> FakeNvmlScenario:
    devices = _homogeneous_devices(FixtureId.FX_NV_06, 3)
    allocated_uuid = devices[0].uuid
    return FakeNvmlScenario(
        fixture_id=FixtureId.FX_NV_06,
        snapshots=(_snapshot(devices), _snapshot(devices[1:])),
        instance_transition=InstanceTransitionExpectation(
            instance_id="fixture-instance-device-disappeared",
            state_before=ExpectedInstanceState.RUNNING,
            state_after=ExpectedInstanceState.UNHEALTHY,
            allocation_gpu_uuids_before=(allocated_uuid,),
            allocation_gpu_uuids_after=(allocated_uuid,),
        ),
    )


def _FX_NV_07() -> FakeNvmlScenario:
    unhealthy_reason = "simulated NVML health check failure"
    devices = (
        _device(FixtureId.FX_NV_07, 0),
        _device(
            FixtureId.FX_NV_07,
            1,
            health=GpuHealth.UNHEALTHY,
            health_reason=unhealthy_reason,
        ),
    )
    return FakeNvmlScenario(
        fixture_id=FixtureId.FX_NV_07,
        snapshots=(_snapshot(devices),),
        deployment_expectations=(
            DeploymentExpectation(
                case_id="healthy-device",
                gpu_uuids=(devices[0].uuid,),
                tensor_parallel_size=1,
            ),
            DeploymentExpectation(
                case_id="unhealthy-device",
                gpu_uuids=(devices[1].uuid,),
                tensor_parallel_size=1,
                accepted=False,
                rejection_reason=unhealthy_reason,
            ),
        ),
    )


def _FX_NV_08() -> FakeNvmlScenario:
    devices_before = _homogeneous_devices(FixtureId.FX_NV_08, 3)
    drifted_indices = (2, 0, 1)
    devices_after = tuple(
        replace(device, display_index=display_index)
        for device, display_index in zip(devices_before, drifted_indices, strict=True)
    )
    allocated_uuids = tuple(device.uuid for device in devices_before[:2])
    return FakeNvmlScenario(
        fixture_id=FixtureId.FX_NV_08,
        snapshots=(_snapshot(devices_before), _snapshot(devices_after)),
        instance_transition=InstanceTransitionExpectation(
            instance_id="fixture-instance-index-drift",
            state_before=ExpectedInstanceState.RUNNING,
            state_after=ExpectedInstanceState.RUNNING,
            allocation_gpu_uuids_before=allocated_uuids,
            allocation_gpu_uuids_after=allocated_uuids,
        ),
    )


_SCENARIO_BUILDERS: Final[Mapping[FixtureId, Callable[[], FakeNvmlScenario]]] = MappingProxyType(
    {
        FixtureId.FX_NV_01: _FX_NV_01,
        FixtureId.FX_NV_02: _FX_NV_02,
        FixtureId.FX_NV_03: _FX_NV_03,
        FixtureId.FX_NV_04: _FX_NV_04,
        FixtureId.FX_NV_05: _FX_NV_05,
        FixtureId.FX_NV_06: _FX_NV_06,
        FixtureId.FX_NV_07: _FX_NV_07,
        FixtureId.FX_NV_08: _FX_NV_08,
    }
)


def scenario_for(fixture_id: FixtureId) -> FakeNvmlScenario:
    """Return a new isolated scenario for one TST fixture ID."""

    return _SCENARIO_BUILDERS[fixture_id]()

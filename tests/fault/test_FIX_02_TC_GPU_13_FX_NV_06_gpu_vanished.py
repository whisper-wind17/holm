"""FIX-02 / TC-GPU-13: FX-NV-06 device disappearance convergence."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from fixtures.fake_nvml import FixtureId, scenario_for
from holm_domain import ModelInstanceStatus, RuntimeType
from holm_instance_controller import (
    InstanceController,
    RuntimeObservation,
    RuntimeProcessState,
    RuntimeSummary,
)
from holm_persistence import ControllerModelInstanceRecord, ModelInstanceRecord
from holm_runtimes import load_runtime_compatibility

ROOT = Path(__file__).resolve().parents[2]
INSTANCE_ID = UUID("43000000-0000-4000-8000-00000000f002")


class _Store:
    async def pending_cleanup(self, after: UUID | None) -> tuple[UUID, ...]:
        return ()

    def __init__(self, item: ControllerModelInstanceRecord) -> None:
        self.item = item
        self.status = item.instance.status
        self.error_code: str | None = None
        self.active_gpu_uuids = item.active_gpu_uuids

    async def list_for_reconciliation(self) -> tuple[ControllerModelInstanceRecord, ...]:
        return (self.item,)

    async def release_terminal_allocations(self) -> tuple[UUID, ...]:
        return ()

    @asynccontextmanager
    async def lifecycle_lock(self, instance_id: UUID) -> AsyncIterator[None]:
        assert instance_id == INSTANCE_ID
        yield

    async def mark_unhealthy(
        self,
        instance_id: UUID,
        *,
        expected_status: ModelInstanceStatus,
        error_code: str,
    ) -> bool:
        assert instance_id == INSTANCE_ID
        assert self.status is expected_status
        self.status = ModelInstanceStatus.UNHEALTHY
        self.error_code = error_code
        return True

    def __getattr__(self, name: str) -> object:
        async def unexpected(*_args: object, **_kwargs: object) -> object:
            raise AssertionError(f"unexpected store call: {name}")

        return unexpected


class _Agent:
    def __init__(self, inventory_gpu_uuids: frozenset[str]) -> None:
        self.inventory_gpu_uuids = inventory_gpu_uuids
        self.stop_calls = 0

    async def list(self) -> tuple[RuntimeSummary, ...]:
        return ()

    async def list_gpus(self) -> frozenset[str]:
        return self.inventory_gpu_uuids

    async def inspect(self, instance_id: str) -> RuntimeObservation:
        raise AssertionError(f"vanished device should be handled before inspection: {instance_id}")

    async def stop(self, instance_id: str, *, grace_seconds: int) -> RuntimeObservation:
        del instance_id, grace_seconds
        self.stop_calls += 1
        return RuntimeObservation(RuntimeProcessState.EXITED)

    async def ensure(self, request: object) -> RuntimeObservation:
        del request
        raise AssertionError("unexpected ensure")


class _Health:
    async def ready(self, endpoint: str, served_model_name: str) -> bool:
        del endpoint, served_model_name
        return True

    async def warmup(self, endpoint: str, served_model_name: str) -> bool:
        del endpoint, served_model_name
        return True


def test_FIX_02_TC_GPU_13_FX_NV_06_vanished_device_retains_original_allocation() -> None:
    scenario = scenario_for(FixtureId.FX_NV_06)
    transition = scenario.instance_transition
    assert transition is not None
    allocated_gpu = transition.allocation_gpu_uuids_before[0]
    assert allocated_gpu in scenario.snapshots[0].device_uuids
    remaining_gpu_uuids = frozenset(scenario.snapshots[1].device_uuids)
    assert allocated_gpu not in remaining_gpu_uuids

    now = datetime.now(UTC)
    item = ControllerModelInstanceRecord(
        instance=ModelInstanceRecord(
            name="FX-M3-10B instance",
            id=INSTANCE_ID,
            artifact_id=UUID("42000000-0000-4000-8000-00000000f002"),
            runtime_type=RuntimeType.VLLM,
            expected_gpu_uuids=(allocated_gpu,),
            tensor_parallel_size=1,
            context_length=4096,
            gpu_memory_utilization=0.8,
            internal_model_name="mi_430000000000",
            status=ModelInstanceStatus.RUNNING,
            error_code=None,
            internal_endpoint="127.0.0.1:8123",
            config_version=1,
            created_at=now,
            updated_at=now,
        ),
        artifact_path="/fixture/model",
        active_gpu_uuids=(allocated_gpu,),
    )
    store = _Store(item)
    agent = _Agent(remaining_gpu_uuids)
    runtime = load_runtime_compatibility(ROOT / "runtime-compat.yaml").runtime("vllm")
    assert runtime is not None

    asyncio.run(
        InstanceController(
            store=store, agent=agent, health=_Health(), runtime=runtime
        ).recover_once()
    )

    assert store.status.value == transition.state_after.value
    assert store.error_code == "gpu_device_missing"
    assert store.active_gpu_uuids == transition.allocation_gpu_uuids_after
    assert item.instance.expected_gpu_uuids == transition.allocation_gpu_uuids_after
    assert agent.stop_calls == 0

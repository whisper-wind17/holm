"""M3-09C orphan isolation and GPU ownership reconciliation guards."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from holm_contracts.agent.v1 import agent_pb2
from holm_domain import ModelInstanceStatus, RuntimeType
from holm_instance_controller import (
    GpuObservationUnknownReason,
    InstanceController,
    ObservationKnowledgeState,
    ObservedGpuUuids,
    RuntimeAgentError,
    RuntimeObservation,
    RuntimeProcessState,
    RuntimeSummary,
)
from holm_instance_controller.agent_client import _inventory_gpu_uuids
from holm_persistence import ControllerModelInstanceRecord, ModelInstanceRecord
from holm_runtimes import load_runtime_compatibility

ROOT = Path(__file__).resolve().parents[2]
INSTANCE_ID = UUID("43000000-0000-4000-8000-00000000090c")
GPU_DB = "GPU-db"
GPU_NVML = "GPU-nvml"


def _item(
    status: ModelInstanceStatus = ModelInstanceStatus.RUNNING,
) -> ControllerModelInstanceRecord:
    now = datetime.now(UTC)
    return ControllerModelInstanceRecord(
        instance=ModelInstanceRecord(
            name="FX-M3-10B instance",
            id=INSTANCE_ID,
            artifact_id=UUID("42000000-0000-4000-8000-00000000090c"),
            runtime_type=RuntimeType.VLLM,
            expected_gpu_uuids=(GPU_DB,),
            tensor_parallel_size=1,
            context_length=4096,
            gpu_memory_utilization=0.8,
            internal_model_name="mi_m309c000000",
            status=status,
            error_code=None,
            internal_endpoint="127.0.0.1:8123",
            config_version=1,
            created_at=now,
            updated_at=now,
        ),
        artifact_path="/models/m3-09c",
        active_gpu_uuids=(GPU_DB,),
    )


class _Store:
    async def pending_cleanup(self, after: UUID | None) -> tuple[UUID, ...]:
        return ()

    def __init__(
        self,
        *,
        items: tuple[ControllerModelInstanceRecord, ...] = (),
        status: ModelInstanceStatus | None = None,
        actions: list[str] | None = None,
    ) -> None:
        self.items = items
        self.current_status = status
        self.actions = actions if actions is not None else []
        self.error_code: str | None = None
        self.events = 0

    async def launch_verification(self, instance_id: UUID) -> None:
        # Existing reconciliation fixtures represent already launched runtimes.
        return None

    async def list_for_reconciliation(self) -> tuple[ControllerModelInstanceRecord, ...]:
        return self.items

    async def release_terminal_allocations(self) -> tuple[UUID, ...]:
        return ()

    @asynccontextmanager
    async def lifecycle_lock(self, instance_id: UUID) -> AsyncIterator[None]:
        assert instance_id == INSTANCE_ID
        self.actions.append("lock")
        yield

    async def status(self, instance_id: UUID) -> ModelInstanceStatus | None:
        assert instance_id == INSTANCE_ID
        self.actions.append("status")
        return self.current_status

    async def record_orphan_isolated(self, instance_id: UUID) -> bool:
        assert instance_id == INSTANCE_ID
        self.actions.append("event")
        if self.events:
            return False
        self.events += 1
        return True

    async def mark_failed(
        self,
        instance_id: UUID,
        *,
        expected_status: ModelInstanceStatus,
        error_code: str,
    ) -> bool:
        assert instance_id == INSTANCE_ID
        assert self.current_status is expected_status
        self.actions.append("failed")
        self.current_status = ModelInstanceStatus.FAILED
        self.error_code = error_code
        return True

    def __getattr__(self, name: str) -> object:
        async def unused(*_args: object, **_kwargs: object) -> object:
            raise AssertionError(f"unexpected store call: {name}")

        return unused


class _Agent:
    def __init__(
        self,
        *,
        listed: tuple[RuntimeSummary, ...] = (),
        inspected: RuntimeObservation | None = None,
        inventory_gpu_uuids: frozenset[str] = frozenset({GPU_DB, GPU_NVML}),
        inventory_error: bool = False,
        actions: list[str] | None = None,
    ) -> None:
        self.listed = listed
        self.inspected = inspected
        self.inventory_gpu_uuids = inventory_gpu_uuids
        self.inventory_error = inventory_error
        self.actions = actions if actions is not None else []
        self.stop_count = 0

    async def list(self) -> tuple[RuntimeSummary, ...]:
        return self.listed

    async def list_gpus(self) -> frozenset[str]:
        if self.inventory_error:
            raise RuntimeAgentError("runtime_agent_gpu_inventory_unavailable")
        return self.inventory_gpu_uuids

    async def inspect(self, instance_id: str) -> RuntimeObservation:
        assert instance_id == str(INSTANCE_ID)
        assert self.inspected is not None
        return self.inspected

    async def stop(self, instance_id: str, *, grace_seconds: int) -> RuntimeObservation:
        assert instance_id == str(INSTANCE_ID)
        assert grace_seconds == 60
        self.actions.append("stop")
        self.stop_count += 1
        self.listed = tuple(
            RuntimeSummary(summary.instance_id, RuntimeProcessState.EXITED, summary.gpu_uuids)
            for summary in self.listed
        )
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


def _controller(store: _Store, agent: _Agent) -> InstanceController:
    runtime = load_runtime_compatibility(ROOT / "runtime-compat.yaml").runtime("vllm")
    assert runtime is not None
    return InstanceController(
        store=store,
        agent=agent,
        health=_Health(),
        runtime=runtime,
    )


@pytest.mark.parametrize("status", [None, ModelInstanceStatus.STOPPED, ModelInstanceStatus.FAILED])
def test_M3_09C_running_orphan_is_stopped_after_locked_reread_and_event_is_idempotent(
    status: ModelInstanceStatus | None,
) -> None:
    actions: list[str] = []
    unknown = ObservedGpuUuids(
        ObservationKnowledgeState.UNKNOWN,
        unknown_reason=GpuObservationUnknownReason.NOT_COLLECTED,
    )
    summary = RuntimeSummary(INSTANCE_ID, RuntimeProcessState.RUNNING, unknown)
    store = _Store(status=status, actions=actions)
    agent = _Agent(listed=(summary,), actions=actions)
    controller = _controller(store, agent)

    asyncio.run(controller.recover_once())
    asyncio.run(controller.recover_once())

    assert actions == ["lock", "status", "stop", "event"]
    assert agent.stop_count == 1
    assert store.events == 1


def test_M3_09C_orphan_snapshot_is_not_acted_on_when_locked_reread_is_active() -> None:
    unknown = ObservedGpuUuids(
        ObservationKnowledgeState.UNKNOWN,
        unknown_reason=GpuObservationUnknownReason.NOT_COLLECTED,
    )
    store = _Store(status=ModelInstanceStatus.STARTING)
    agent = _Agent(listed=(RuntimeSummary(INSTANCE_ID, RuntimeProcessState.RUNNING, unknown),))

    asyncio.run(_controller(store, agent).recover_once())

    assert store.actions == ["lock", "status"]
    assert agent.stop_count == 0
    assert store.events == 0


@pytest.mark.parametrize(
    "status",
    [
        ModelInstanceStatus.STARTING,
        ModelInstanceStatus.RUNNING,
        ModelInstanceStatus.RESTARTING,
    ],
)
def test_M3_09C_known_NVML_GPU_mismatch_stops_before_marking_failed(
    status: ModelInstanceStatus,
) -> None:
    actions: list[str] = []
    item = _item(status)
    store = _Store(items=(item,), status=status, actions=actions)
    observed = ObservedGpuUuids(
        ObservationKnowledgeState.KNOWN,
        values=(GPU_NVML,),
    )
    agent = _Agent(
        listed=(RuntimeSummary(INSTANCE_ID, RuntimeProcessState.RUNNING, observed),),
        inspected=RuntimeObservation(
            RuntimeProcessState.RUNNING,
            "127.0.0.1:8123",
            observed,
        ),
        actions=actions,
    )

    asyncio.run(_controller(store, agent).recover_once())

    assert actions == ["lock", "stop", "failed"]
    assert store.current_status is ModelInstanceStatus.FAILED
    assert store.error_code == "runtime_gpu_mismatch"


def test_M3_09C_unknown_GPU_observation_never_triggers_conflict_action() -> None:
    item = _item()
    store = _Store(items=(item,), status=ModelInstanceStatus.RUNNING)
    agent = _Agent()
    unknown = ObservedGpuUuids(
        ObservationKnowledgeState.UNKNOWN,
        unknown_reason=GpuObservationUnknownReason.QUERY_FAILED,
    )
    controller = _controller(store, agent)

    acted = asyncio.run(
        controller._isolate_gpu_mismatch(  # noqa: SLF001
            item,
            RuntimeObservation(RuntimeProcessState.RUNNING, gpu_uuids=unknown),
        )
    )

    assert not acted
    assert agent.stop_count == 0
    assert store.current_status is ModelInstanceStatus.RUNNING


def test_M3_09C_Starting_empty_NVML_set_waits_for_CUDA_registration() -> None:
    item = _item(ModelInstanceStatus.STARTING)
    store = _Store(items=(item,), status=ModelInstanceStatus.STARTING)
    agent = _Agent()
    empty = ObservedGpuUuids(ObservationKnowledgeState.KNOWN)
    controller = _controller(store, agent)

    acted = asyncio.run(
        controller._isolate_gpu_mismatch(  # noqa: SLF001
            item,
            RuntimeObservation(RuntimeProcessState.RUNNING, gpu_uuids=empty),
        )
    )

    assert not acted
    assert agent.stop_count == 0
    assert store.current_status is ModelInstanceStatus.STARTING


def test_M3_09C_Restarting_empty_NVML_set_waits_for_CUDA_registration() -> None:
    item = _item(ModelInstanceStatus.RESTARTING)
    store = _Store(items=(item,), status=ModelInstanceStatus.RESTARTING)
    agent = _Agent()
    empty = ObservedGpuUuids(ObservationKnowledgeState.KNOWN)
    controller = _controller(store, agent)

    acted = asyncio.run(
        controller._isolate_gpu_mismatch(  # noqa: SLF001
            item,
            RuntimeObservation(RuntimeProcessState.RUNNING, gpu_uuids=empty),
        )
    )

    assert not acted
    assert agent.stop_count == 0
    assert store.current_status is ModelInstanceStatus.RESTARTING
    assert store.error_code is None


def test_FIX_02_ListGPUs_mapping_accepts_complete_and_zero_device_snapshots() -> None:
    ready = agent_pb2.ListGPUsResponse(
        discovery_state=agent_pb2.GPU_DISCOVERY_STATE_READY,
        devices=(agent_pb2.GpuDevice(uuid=GPU_DB), agent_pb2.GpuDevice(uuid=GPU_NVML)),
    )
    empty = agent_pb2.ListGPUsResponse(
        discovery_state=agent_pb2.GPU_DISCOVERY_STATE_NO_DEVICES,
        failure_reason=agent_pb2.GPU_DISCOVERY_FAILURE_REASON_NO_DEVICES,
    )

    assert _inventory_gpu_uuids(ready) == frozenset({GPU_DB, GPU_NVML})
    assert _inventory_gpu_uuids(empty) == frozenset()


def test_FIX_02_unavailable_gpu_inventory_does_not_turn_mismatch_into_conflict() -> None:
    item = _item(ModelInstanceStatus.RESTARTING)
    store = _Store(items=(item,), status=ModelInstanceStatus.RESTARTING)
    observed = ObservedGpuUuids(ObservationKnowledgeState.KNOWN, values=(GPU_NVML,))
    agent = _Agent(
        inspected=RuntimeObservation(RuntimeProcessState.RUNNING, gpu_uuids=observed),
        inventory_error=True,
    )

    asyncio.run(_controller(store, agent).recover_once())

    assert store.current_status is ModelInstanceStatus.RESTARTING
    assert store.error_code is None
    assert agent.stop_count == 0

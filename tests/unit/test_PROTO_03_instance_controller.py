"""PROTO-03: Model Instance controller state transitions with fake dependencies."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fixtures.fake_host_agent import FakeHostAgent, LaunchSpec
from holm_domain import ModelInstanceStatus, RuntimeType
from holm_domain.launch_verification import LaunchVerification, LaunchVerificationResult
from holm_instance_controller import (
    InstanceController,
    RuntimeAgentError,
    RuntimeLaunchRequest,
    RuntimeObservation,
    RuntimeProcessState,
    RuntimeSummary,
)
from holm_persistence import ControllerModelInstanceRecord, ModelInstanceRecord
from holm_runtimes import RuntimeCompatibilityCatalog, load_runtime_compatibility

ROOT = Path(__file__).resolve().parents[2]
INSTANCE_ID = UUID("43000000-0000-4000-8000-000000000001")
GPU = "GPU-proto-03-0000"
_FIXTURE_IMAGE_DIGEST = f"sha256:{'a' * 64}"


def _item(
    status: ModelInstanceStatus,
    *,
    runtime_type: RuntimeType = RuntimeType.VLLM,
) -> ControllerModelInstanceRecord:
    now = datetime.now(UTC)
    return ControllerModelInstanceRecord(
        instance=ModelInstanceRecord(
            name="FX-M3-10B instance",
            id=INSTANCE_ID,
            artifact_id=UUID("42000000-0000-4000-8000-000000000001"),
            runtime_type=runtime_type,
            expected_gpu_uuids=(GPU,),
            tensor_parallel_size=1,
            context_length=4096,
            gpu_memory_utilization=0.8,
            internal_model_name="mi_430000000000",
            status=status,
            error_code=None,
            internal_endpoint="127.0.0.1:8123" if status is ModelInstanceStatus.RUNNING else None,
            config_version=1,
            created_at=now,
            updated_at=now,
        ),
        artifact_path="/models/proto-03",
        active_gpu_uuids=(GPU,),
    )


class _Store:
    async def pending_cleanup(self, after: UUID | None) -> tuple[UUID, ...]:
        return ()

    def __init__(self, items: tuple[ControllerModelInstanceRecord, ...]) -> None:
        self.items = {item.instance.id: item for item in items}
        self.statuses = {item.instance.id: item.instance.status for item in items}
        self.endpoints: dict[UUID, str] = {}
        self.error_codes: dict[UUID, str] = {}
        self.running = False
        self.released: set[UUID] = set()
        self.restart_counts = {item.instance.id: item.instance.restart_count for item in items}
        # FX-M3-07B unit boundary: explicit verified/previously launched attempts.
        # Real files, Worker and role enforcement live in the integration suite.
        self.verifications = {
            item.instance.id: LaunchVerification(
                uuid4(),
                item.instance.id,
                item.instance.artifact_id,
                item.instance.config_version,
                item.instance.status,
                item.instance.restart_count,
                item.instance.updated_at,
                LaunchVerificationResult.READY,
                authorized=item.instance.status is not ModelInstanceStatus.STARTING,
            )
            for item in items
        }

    async def launch_verification(self, instance_id: UUID) -> LaunchVerification | None:
        return self.verifications.get(instance_id)

    async def authorize_launch(self, expected: LaunchVerification) -> bool:
        return (
            self.statuses[expected.instance_id] is expected.status
            and self.verifications[expected.instance_id].request_id == expected.request_id
            and expected.result is LaunchVerificationResult.READY
        )

    async def list_for_reconciliation(self) -> tuple[ControllerModelInstanceRecord, ...]:
        return tuple(
            ControllerModelInstanceRecord(
                instance=replace(item.instance, status=self.statuses[instance_id]),
                artifact_path=item.artifact_path,
                active_gpu_uuids=item.active_gpu_uuids,
            )
            for instance_id, item in self.items.items()
            if self.statuses[instance_id]
            in {
                ModelInstanceStatus.STARTING,
                ModelInstanceStatus.RUNNING,
                ModelInstanceStatus.STOPPING,
                ModelInstanceStatus.UNHEALTHY,
                ModelInstanceStatus.RESTARTING,
            }
        )

    @asynccontextmanager
    async def lifecycle_lock(self, instance_id: UUID) -> AsyncIterator[None]:
        assert instance_id in self.items
        yield

    async def release_terminal_allocations(self) -> tuple[UUID, ...]:
        released = tuple(
            instance_id
            for instance_id, status in self.statuses.items()
            if status in {ModelInstanceStatus.STOPPED, ModelInstanceStatus.FAILED}
        )
        self.released.update(released)
        return released

    async def record_orphan_isolated(self, instance_id: UUID) -> bool:
        del instance_id
        return True

    async def claim(
        self,
        status: ModelInstanceStatus,
        *,
        excluded_ids: tuple[UUID, ...] = (),
    ) -> ControllerModelInstanceRecord | None:
        excluded = set(excluded_ids)
        for instance_id, item in self.items.items():
            if self.statuses[instance_id] is status and instance_id not in excluded:
                return item
        return None

    async def status(self, instance_id: UUID) -> ModelInstanceStatus | None:
        return self.statuses.get(instance_id)

    async def set_endpoint(
        self,
        instance_id: UUID,
        *,
        expected_status: ModelInstanceStatus,
        endpoint: str,
    ) -> bool:
        if self.statuses.get(instance_id) is not expected_status:
            return False
        self.endpoints[instance_id] = endpoint
        return True

    async def mark_running(self, instance_id: UUID) -> bool:
        if self.statuses.get(instance_id) is not ModelInstanceStatus.STARTING:
            return False
        self.statuses[instance_id] = ModelInstanceStatus.RUNNING
        self.running = True
        return True

    async def mark_failed(
        self,
        instance_id: UUID,
        *,
        expected_status: ModelInstanceStatus,
        error_code: str,
    ) -> bool:
        if self.statuses.get(instance_id) is not expected_status:
            return False
        self.statuses[instance_id] = ModelInstanceStatus.FAILED
        self.error_codes[instance_id] = error_code
        self.released.add(instance_id)
        self.endpoints.pop(instance_id, None)
        return True

    async def mark_stopped(self, instance_id: UUID) -> bool:
        if self.statuses.get(instance_id) is not ModelInstanceStatus.STOPPING:
            return False
        self.statuses[instance_id] = ModelInstanceStatus.STOPPED
        self.released.add(instance_id)
        self.endpoints.pop(instance_id, None)
        return True

    async def mark_unhealthy(
        self,
        instance_id: UUID,
        *,
        expected_status: ModelInstanceStatus,
        error_code: str,
    ) -> bool:
        if self.statuses.get(instance_id) is not expected_status:
            return False
        self.statuses[instance_id] = ModelInstanceStatus.UNHEALTHY
        self.error_codes[instance_id] = error_code
        self.endpoints.pop(instance_id, None)
        return True

    async def begin_restart(self, instance_id: UUID, *, restart_window_seconds: int) -> bool:
        assert restart_window_seconds > 0
        if self.statuses.get(instance_id) is not ModelInstanceStatus.UNHEALTHY:
            return False
        self.statuses[instance_id] = ModelInstanceStatus.RESTARTING
        return True

    async def record_restart_attempt(self, instance_id: UUID) -> int | None:
        if self.statuses.get(instance_id) is not ModelInstanceStatus.RESTARTING:
            return None
        self.restart_counts[instance_id] += 1
        self.verifications[instance_id] = replace(
            self.verifications[instance_id],
            request_id=uuid4(),
            status=ModelInstanceStatus.RESTARTING,
            attempt=self.restart_counts[instance_id],
            requested_at=datetime.now(UTC),
            authorized=False,
        )
        return self.restart_counts[instance_id]

    async def mark_restarted_running(self, instance_id: UUID, *, endpoint: str) -> bool:
        if self.statuses.get(instance_id) is not ModelInstanceStatus.RESTARTING:
            return False
        self.statuses[instance_id] = ModelInstanceStatus.RUNNING
        self.endpoints[instance_id] = endpoint
        return True


class _Agent:
    def __init__(
        self,
        *,
        ensure: RuntimeObservation | None = None,
        inspect: tuple[RuntimeObservation, ...] = (),
        stop: RuntimeObservation | None = None,
    ) -> None:
        self.ensure_result = ensure or RuntimeObservation(
            RuntimeProcessState.RUNNING, "127.0.0.1:8123"
        )
        self.inspect_results = deque(inspect)
        self.stop_result = stop or RuntimeObservation(RuntimeProcessState.EXITED)
        self.launches: list[RuntimeLaunchRequest] = []
        self.stop_calls: list[tuple[str, int]] = []

    async def ensure(self, request: RuntimeLaunchRequest) -> RuntimeObservation:
        self.launches.append(request)
        return self.ensure_result

    async def inspect(self, instance_id: str) -> RuntimeObservation:
        del instance_id
        return self.inspect_results.popleft()

    async def stop(self, instance_id: str, *, grace_seconds: int) -> RuntimeObservation:
        self.stop_calls.append((instance_id, grace_seconds))
        return self.stop_result

    async def list(self) -> tuple[RuntimeSummary, ...]:
        return ()

    async def list_gpus(self) -> frozenset[str]:
        return frozenset({GPU})


def _fx_ha_04_exit_observation(
    *,
    exit_code: int,
    oom_killed: bool | None,
) -> RuntimeObservation:
    fixture = FakeHostAgent(allowed_image_digests=(_FIXTURE_IMAGE_DIGEST,))
    spec = LaunchSpec(
        runtime_id=str(INSTANCE_ID),
        image_digest=_FIXTURE_IMAGE_DIGEST,
        gpu_uuids=(GPU,),
    )
    fixture.ensure_runtime(spec)
    fixture.inject_exit(
        spec.runtime_id,
        exit_code=exit_code,
        oom_killed=oom_killed,
    )
    inspection = fixture.inspect_runtime(spec.runtime_id)
    return RuntimeObservation(
        RuntimeProcessState.EXITED,
        exit_code=inspection.exit_code,
        oom_killed=inspection.oom_killed,
    )


class _Health:
    def __init__(
        self,
        outcomes: tuple[bool, ...],
        *,
        warmup_outcomes: tuple[bool, ...] | None = None,
    ) -> None:
        self.outcomes = deque(outcomes)
        self.warmup_outcomes = None if warmup_outcomes is None else deque(warmup_outcomes)
        self.warmup_calls: list[tuple[str, str]] = []

    async def ready(self, endpoint: str, served_model_name: str) -> bool:
        assert endpoint == "127.0.0.1:8123"
        assert served_model_name == "mi_430000000000"
        return self.outcomes.popleft()

    async def warmup(self, endpoint: str, served_model_name: str) -> bool:
        assert endpoint == "127.0.0.1:8123"
        assert served_model_name == "mi_430000000000"
        self.warmup_calls.append((endpoint, served_model_name))
        if self.warmup_outcomes is None:
            return True
        return self.warmup_outcomes.popleft()


def _controller(
    store: _Store,
    agent: _Agent,
    health: _Health,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] | None = None,
    dependency_timeout_seconds: float = 5.0,
    stop_drain_seconds: float = 0.001,
    stop_grace_seconds: int = 1,
    runtime_catalog: RuntimeCompatibilityCatalog | None = None,
) -> InstanceController:
    catalog = runtime_catalog or load_runtime_compatibility(ROOT / "runtime-compat.yaml")
    return InstanceController(
        store=store,
        agent=agent,
        health=health,
        runtime_catalog=catalog,
        start_timeout_seconds=5,
        start_poll_seconds=0.001,
        health_seconds=0.001,
        dispatch_seconds=0.001,
        stop_drain_seconds=stop_drain_seconds,
        stop_grace_seconds=stop_grace_seconds,
        sleep=sleep,
        monotonic=time.monotonic if monotonic is None else monotonic,
        dependency_timeout_seconds=dependency_timeout_seconds,
    )


def test_PROTO_03_starting_requires_two_health_successes_and_marks_allocated() -> None:
    item = _item(ModelInstanceStatus.STARTING)
    store = _Store((item,))
    agent = _Agent(
        inspect=(
            RuntimeObservation(RuntimeProcessState.RUNNING, "127.0.0.1:8123"),
            RuntimeObservation(RuntimeProcessState.RUNNING, "127.0.0.1:8123"),
        )
    )
    asyncio.run(_controller(store, agent, _Health((True, True))).process_starting(item))

    assert store.statuses[INSTANCE_ID] is ModelInstanceStatus.RUNNING
    assert store.running
    assert store.endpoints[INSTANCE_ID] == "127.0.0.1:8123"
    assert agent.launches == [
        RuntimeLaunchRequest(
            instance_id=str(INSTANCE_ID),
            runtime_name="vllm",
            model_path="/models/proto-03",
            gpu_uuids=(GPU,),
            tensor_parallel_size=1,
            context_length=4096,
            gpu_memory_utilization=0.8,
            served_model_name="mi_430000000000",
            config_version="instance-1",
        )
    ]


def test_FIX_05_sglang_instance_launches_the_declared_runtime() -> None:
    item = _item(ModelInstanceStatus.STARTING, runtime_type=RuntimeType.SGLANG)
    store = _Store((item,))
    agent = _Agent(
        inspect=(
            RuntimeObservation(RuntimeProcessState.RUNNING, "127.0.0.1:8123"),
            RuntimeObservation(RuntimeProcessState.RUNNING, "127.0.0.1:8123"),
        )
    )

    asyncio.run(_controller(store, agent, _Health((True, True))).process_starting(item))

    assert store.statuses[INSTANCE_ID] is ModelInstanceStatus.RUNNING
    assert [launch.runtime_name for launch in agent.launches] == ["sglang"]


def test_FIX_05_uncertified_runtime_fails_loudly_without_launching_vllm() -> None:
    full_catalog = load_runtime_compatibility(ROOT / "runtime-compat.yaml")
    vllm = full_catalog.runtime("vllm")
    assert vllm is not None
    vllm_only_catalog = RuntimeCompatibilityCatalog(
        schema_version=full_catalog.schema_version,
        runtimes={"vllm": vllm},
    )
    item = _item(ModelInstanceStatus.STARTING, runtime_type=RuntimeType.SGLANG)
    store = _Store((item,))
    agent = _Agent()

    asyncio.run(
        _controller(
            store,
            agent,
            _Health(()),
            runtime_catalog=vllm_only_catalog,
        ).process_starting(item)
    )

    assert store.statuses[INSTANCE_ID] is ModelInstanceStatus.FAILED
    assert store.error_codes[INSTANCE_ID] == "runtime_not_certified"
    assert agent.launches == []
    assert INSTANCE_ID in store.released


def test_FIX_06_TC_RT_01_starting_waits_for_successful_inference_warmup() -> None:
    item = _item(ModelInstanceStatus.STARTING, runtime_type=RuntimeType.SGLANG)
    store = _Store((item,))
    agent = _Agent(
        inspect=tuple(
            RuntimeObservation(RuntimeProcessState.RUNNING, "127.0.0.1:8123") for _ in range(4)
        )
    )
    health = _Health(
        (True, True, True, True),
        warmup_outcomes=(False, True),
    )
    observed_statuses: list[ModelInstanceStatus] = []

    async def observe_sleep(_seconds: float) -> None:
        observed_statuses.append(store.statuses[INSTANCE_ID])

    asyncio.run(_controller(store, agent, health, sleep=observe_sleep).process_starting(item))

    assert observed_statuses == [
        ModelInstanceStatus.STARTING,
        ModelInstanceStatus.STARTING,
        ModelInstanceStatus.STARTING,
    ]
    assert store.statuses[INSTANCE_ID] is ModelInstanceStatus.RUNNING
    assert health.warmup_calls == [
        ("127.0.0.1:8123", "mi_430000000000"),
        ("127.0.0.1:8123", "mi_430000000000"),
    ]


def test_FIX_06_TC_GPU_07_warmup_failure_keeps_existing_start_timeout_path() -> None:
    item = _item(ModelInstanceStatus.STARTING, runtime_type=RuntimeType.SGLANG)
    store = _Store((item,))
    agent = _Agent(
        inspect=tuple(
            RuntimeObservation(RuntimeProcessState.RUNNING, "127.0.0.1:8123") for _ in range(5)
        )
    )
    health = _Health(
        (True, True, True, True, True),
        warmup_outcomes=(False, False),
    )
    moments = iter((0.0, 0.0, 1.0, 2.0, 3.0, 4.0, 5.0))

    asyncio.run(
        _controller(
            store,
            agent,
            health,
            sleep=lambda _seconds: asyncio.sleep(0),
            monotonic=lambda: next(moments),
        ).process_starting(item)
    )

    assert store.statuses[INSTANCE_ID] is ModelInstanceStatus.FAILED
    assert store.error_codes[INSTANCE_ID] == "runtime_start_timeout"
    assert INSTANCE_ID in store.released
    assert agent.stop_calls == [(str(INSTANCE_ID), 1)]


def test_PROTO_03_start_failure_stops_runtime_releases_and_sanitizes_error() -> None:
    item = _item(ModelInstanceStatus.STARTING)
    store = _Store((item,))
    agent = _Agent(ensure=RuntimeObservation(RuntimeProcessState.EXITED))
    asyncio.run(_controller(store, agent, _Health(())).process_starting(item))

    assert store.statuses[INSTANCE_ID] is ModelInstanceStatus.FAILED
    assert store.error_codes[INSTANCE_ID] == "runtime_start_failed"
    assert INSTANCE_ID in store.released
    assert agent.stop_calls == [(str(INSTANCE_ID), 1)]


def test_M3_13A_TC_RT_05_FX_HA_04_startup_OOM_fails_with_stable_code_and_releases_GPU() -> None:
    class ImmediateExitAgent(_Agent):
        async def ensure(self, request: RuntimeLaunchRequest) -> RuntimeObservation:
            self.launches.append(request)
            raise RuntimeAgentError()

    item = _item(ModelInstanceStatus.STARTING)
    store = _Store((item,))
    observation = _fx_ha_04_exit_observation(exit_code=23, oom_killed=True)
    agent = ImmediateExitAgent(inspect=(observation,))

    asyncio.run(_controller(store, agent, _Health(())).process_starting(item))

    assert store.statuses[INSTANCE_ID] is ModelInstanceStatus.FAILED
    assert store.error_codes[INSTANCE_ID] == "runtime_out_of_memory"
    assert INSTANCE_ID in store.released
    assert agent.stop_calls == [(str(INSTANCE_ID), 1)]


@pytest.mark.parametrize(
    ("exit_code", "oom_killed"),
    ((17, False), (-9, False), (17, None)),
    ids=("non-oom-nonzero", "signal-kill", "not-collected"),
)
def test_M3_13A_positive_control_non_OOM_exit_keeps_start_failure_code(
    exit_code: int,
    oom_killed: bool | None,
) -> None:
    item = _item(ModelInstanceStatus.STARTING)
    store = _Store((item,))
    observation = _fx_ha_04_exit_observation(
        exit_code=exit_code,
        oom_killed=oom_killed,
    )

    asyncio.run(_controller(store, _Agent(ensure=observation), _Health(())).process_starting(item))

    assert store.statuses[INSTANCE_ID] is ModelInstanceStatus.FAILED
    assert store.error_codes[INSTANCE_ID] == "runtime_start_failed"
    assert INSTANCE_ID in store.released


def test_PROTO_03_start_timeout_stops_runtime_and_uses_stable_error_code() -> None:
    original = _item(ModelInstanceStatus.STARTING)
    item = ControllerModelInstanceRecord(
        instance=replace(
            original.instance,
            updated_at=datetime.now(UTC) - timedelta(seconds=10),
        ),
        artifact_path=original.artifact_path,
        active_gpu_uuids=original.active_gpu_uuids,
    )
    store = _Store((item,))
    store.verifications[INSTANCE_ID] = replace(
        store.verifications[INSTANCE_ID],
        authorized=True,
    )
    agent = _Agent()
    asyncio.run(_controller(store, agent, _Health(())).process_starting(item))

    assert store.statuses[INSTANCE_ID] is ModelInstanceStatus.FAILED
    assert store.error_codes[INSTANCE_ID] == "runtime_start_timeout"
    assert INSTANCE_ID in store.released


def test_PROTO_03_stopping_confirms_exit_before_releasing() -> None:
    item = _item(ModelInstanceStatus.STOPPING)
    store = _Store((item,))
    agent = _Agent()
    asyncio.run(_controller(store, agent, _Health(())).process_stopping(item))

    assert store.statuses[INSTANCE_ID] is ModelInstanceStatus.STOPPED
    assert INSTANCE_ID in store.released


def test_M3_07_TC_GPU_20_stopping_waits_for_drain_before_termination() -> None:
    item = _item(ModelInstanceStatus.STOPPING)
    store = _Store((item,))
    agent = _Agent()
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        assert agent.stop_calls == []
        assert store.statuses[INSTANCE_ID] is ModelInstanceStatus.STOPPING
        sleeps.append(seconds)

    asyncio.run(
        _controller(
            store,
            agent,
            _Health(()),
            sleep=fake_sleep,
            stop_drain_seconds=0.05,
            stop_grace_seconds=60,
        ).process_stopping(item)
    )

    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(0.05, abs=0.01)
    assert agent.stop_calls == [(str(INSTANCE_ID), 60)]
    assert store.statuses[INSTANCE_ID] is ModelInstanceStatus.STOPPED
    assert INSTANCE_ID in store.released


def test_M3_07_TC_GPU_20_resumed_stop_waits_only_remaining_drain_window() -> None:
    original = _item(ModelInstanceStatus.STOPPING)
    item = ControllerModelInstanceRecord(
        instance=replace(
            original.instance,
            updated_at=datetime.now(UTC) - timedelta(seconds=20),
        ),
        artifact_path=original.artifact_path,
        active_gpu_uuids=original.active_gpu_uuids,
    )
    store = _Store((item,))
    agent = _Agent()
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    asyncio.run(
        _controller(
            store,
            agent,
            _Health(()),
            sleep=fake_sleep,
            stop_drain_seconds=30.0,
        ).process_stopping(item)
    )

    assert sleeps == pytest.approx([10.0], abs=0.1)
    assert agent.stop_calls == [(str(INSTANCE_ID), 1)]
    assert store.statuses[INSTANCE_ID] is ModelInstanceStatus.STOPPED


def test_M3_07_TC_GPU_20_elapsed_drain_window_does_not_wait_again() -> None:
    original = _item(ModelInstanceStatus.STOPPING)
    item = ControllerModelInstanceRecord(
        instance=replace(
            original.instance,
            updated_at=datetime.now(UTC) - timedelta(seconds=31),
        ),
        artifact_path=original.artifact_path,
        active_gpu_uuids=original.active_gpu_uuids,
    )
    store = _Store((item,))
    agent = _Agent()

    async def unexpected_sleep(_seconds: float) -> None:
        pytest.fail("an elapsed drain window must not be restarted")

    asyncio.run(
        _controller(
            store,
            agent,
            _Health(()),
            sleep=unexpected_sleep,
            stop_drain_seconds=30.0,
        ).process_stopping(item)
    )

    assert agent.stop_calls == [(str(INSTANCE_ID), 1)]
    assert store.statuses[INSTANCE_ID] is ModelInstanceStatus.STOPPED


def test_PROTO_03_M3_09_running_three_health_failures_become_unhealthy() -> None:
    item = _item(ModelInstanceStatus.RUNNING)
    store = _Store((item,))
    agent = _Agent(
        inspect=tuple(
            RuntimeObservation(RuntimeProcessState.RUNNING, "127.0.0.1:8123") for _ in range(3)
        )
    )
    health = _Health((False, False, False))
    asyncio.run(_controller(store, agent, health).process_running(item))

    assert store.statuses[INSTANCE_ID] is ModelInstanceStatus.UNHEALTHY
    assert store.error_codes[INSTANCE_ID] == "runtime_health_failed"
    assert INSTANCE_ID not in store.released
    assert health.warmup_calls == []


def test_PROTO_03_startup_convergence_inspects_missing_active_processes() -> None:
    starting = _item(ModelInstanceStatus.STARTING)
    stopping = ControllerModelInstanceRecord(
        instance=replace(
            starting.instance,
            id=UUID("43000000-0000-4000-8000-000000000002"),
            status=ModelInstanceStatus.STOPPING,
        ),
        artifact_path=starting.artifact_path,
        active_gpu_uuids=starting.active_gpu_uuids,
    )
    running = ControllerModelInstanceRecord(
        instance=replace(
            starting.instance,
            id=UUID("43000000-0000-4000-8000-000000000003"),
            status=ModelInstanceStatus.RUNNING,
        ),
        artifact_path=starting.artifact_path,
        active_gpu_uuids=starting.active_gpu_uuids,
    )
    store = _Store((starting, stopping, running))
    store.verifications[INSTANCE_ID] = replace(
        store.verifications[INSTANCE_ID],
        authorized=True,
    )
    agent = _Agent(
        inspect=(
            RuntimeObservation(RuntimeProcessState.NOT_FOUND),
            RuntimeObservation(RuntimeProcessState.NOT_FOUND),
            RuntimeObservation(RuntimeProcessState.EXITED),
        )
    )
    asyncio.run(_controller(store, agent, _Health(())).recover_once())

    assert store.statuses[starting.instance.id] is ModelInstanceStatus.UNHEALTHY
    assert store.statuses[stopping.instance.id] is ModelInstanceStatus.STOPPED
    assert store.statuses[running.instance.id] is ModelInstanceStatus.UNHEALTHY


def test_M3_09_TC_GPU_16_restart_backoff_is_exact_and_exhaustion_releases_gpu() -> None:
    item = _item(ModelInstanceStatus.RESTARTING)
    store = _Store((item,))
    agent = _Agent(
        ensure=RuntimeObservation(RuntimeProcessState.EXITED),
        inspect=(RuntimeObservation(RuntimeProcessState.EXITED),) * 3,
    )
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    asyncio.run(_controller(store, agent, _Health(()), sleep=fake_sleep).process_restarting(item))

    assert sleeps == [5.0, 15.0, 45.0]
    assert store.restart_counts[INSTANCE_ID] == 3
    assert store.statuses[INSTANCE_ID] is ModelInstanceStatus.FAILED
    assert store.error_codes[INSTANCE_ID] == "runtime_restart_failed"
    assert INSTANCE_ID in store.released


def test_M3_13A_TC_FI_10_FX_HA_04_runtime_OOM_restarts_then_fails_and_releases_GPU() -> None:
    running = _item(ModelInstanceStatus.RUNNING)
    store = _Store((running,))
    observation = _fx_ha_04_exit_observation(exit_code=23, oom_killed=True)
    agent = _Agent(ensure=observation, inspect=(observation,) * 4)
    controller = _controller(store, agent, _Health(()), sleep=lambda _seconds: asyncio.sleep(0))

    asyncio.run(controller.process_running(running))
    assert store.statuses[INSTANCE_ID] is ModelInstanceStatus.UNHEALTHY
    assert store.error_codes[INSTANCE_ID] == "runtime_out_of_memory"
    assert INSTANCE_ID not in store.released

    unhealthy = ControllerModelInstanceRecord(
        instance=replace(
            running.instance,
            status=ModelInstanceStatus.UNHEALTHY,
            error_code="runtime_out_of_memory",
        ),
        artifact_path=running.artifact_path,
        active_gpu_uuids=running.active_gpu_uuids,
    )
    asyncio.run(controller.process_unhealthy(unhealthy))
    assert store.statuses[INSTANCE_ID] is ModelInstanceStatus.RESTARTING
    assert INSTANCE_ID not in store.released

    restarting = ControllerModelInstanceRecord(
        instance=replace(
            unhealthy.instance,
            status=ModelInstanceStatus.RESTARTING,
            error_code="runtime_out_of_memory",
        ),
        artifact_path=running.artifact_path,
        active_gpu_uuids=running.active_gpu_uuids,
    )
    asyncio.run(controller.process_restarting(restarting))

    assert store.restart_counts[INSTANCE_ID] == 3
    assert store.statuses[INSTANCE_ID] is ModelInstanceStatus.FAILED
    assert store.error_codes[INSTANCE_ID] == "runtime_out_of_memory"
    assert INSTANCE_ID in store.released


def test_M3_09_TC_GPU_09_restart_success_retains_original_gpu_set() -> None:
    item = _item(ModelInstanceStatus.RESTARTING)
    store = _Store((item,))
    agent = _Agent(
        inspect=(
            RuntimeObservation(RuntimeProcessState.NOT_FOUND),
            RuntimeObservation(RuntimeProcessState.RUNNING, "127.0.0.1:8123"),
            RuntimeObservation(RuntimeProcessState.RUNNING, "127.0.0.1:8123"),
        )
    )
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    asyncio.run(
        _controller(store, agent, _Health((True, True)), sleep=fake_sleep).process_restarting(item)
    )

    assert sleeps[0] == 5.0
    assert store.statuses[INSTANCE_ID] is ModelInstanceStatus.RUNNING
    assert INSTANCE_ID not in store.released
    assert agent.launches[0].gpu_uuids == (GPU,)


def test_M3_09_reconciliation_inspects_affected_objects_in_parallel() -> None:
    first = _item(ModelInstanceStatus.STOPPING)
    second = ControllerModelInstanceRecord(
        instance=replace(
            first.instance,
            id=UUID("43000000-0000-4000-8000-000000000002"),
        ),
        artifact_path=first.artifact_path,
        active_gpu_uuids=first.active_gpu_uuids,
    )
    store = _Store((first, second))

    class ParallelAgent(_Agent):
        def __init__(self) -> None:
            super().__init__()
            self.active = 0
            self.maximum = 0
            self.release = asyncio.Event()

        async def inspect(self, instance_id: str) -> RuntimeObservation:
            del instance_id
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            if self.active == 2:
                self.release.set()
            await self.release.wait()
            self.active -= 1
            return RuntimeObservation(RuntimeProcessState.EXITED)

    agent = ParallelAgent()
    asyncio.run(_controller(store, agent, _Health(())).recover_once())

    assert agent.maximum == 2
    assert store.statuses[first.instance.id] is ModelInstanceStatus.STOPPED
    assert store.statuses[second.instance.id] is ModelInstanceStatus.STOPPED


def test_M3_09_reconciliation_enforces_single_dependency_timeout() -> None:
    class BlockedStore(_Store):
        async def list_for_reconciliation(self) -> tuple[ControllerModelInstanceRecord, ...]:
            await asyncio.Event().wait()
            return ()

    async def scenario() -> None:
        controller = _controller(
            BlockedStore(()),
            _Agent(),
            _Health(()),
            dependency_timeout_seconds=0.01,
        )
        with pytest.raises(TimeoutError):
            await controller.recover_once()

    asyncio.run(scenario())


def test_M3_09_initial_reconciliation_retries_within_sixty_second_window() -> None:
    class FlakyStore(_Store):
        def __init__(self) -> None:
            super().__init__(())
            self.calls = 0

        async def list_for_reconciliation(self) -> tuple[ControllerModelInstanceRecord, ...]:
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError("dependency unavailable")
            return ()

    store = FlakyStore()
    now = [0.0]
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    controller = _controller(
        store,
        _Agent(),
        _Health(()),
        sleep=fake_sleep,
        monotonic=lambda: now[0],
    )
    asyncio.run(controller._initial_reconciliation())

    assert store.calls == 3
    assert sleeps == [1.0, 2.0]
    assert now[0] < 60.0


def test_M3_09_periodic_reconciliation_waits_thirty_seconds_after_completion() -> None:
    class TwoPassStore(_Store):
        def __init__(self) -> None:
            super().__init__(())
            self.calls = 0

        async def list_for_reconciliation(self) -> tuple[ControllerModelInstanceRecord, ...]:
            self.calls += 1
            if self.calls == 2:
                raise asyncio.CancelledError()
            return ()

    store = TwoPassStore()
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def scenario() -> None:
        controller = _controller(store, _Agent(), _Health(()), sleep=fake_sleep)
        with pytest.raises(asyncio.CancelledError):
            await controller._periodic_reconciliation()

    asyncio.run(scenario())

    assert store.calls == 2
    assert sleeps == [30.0, 30.0]


@pytest.mark.parametrize(
    "state",
    [RuntimeProcessState.RUNNING, RuntimeProcessState.NOT_FOUND, RuntimeProcessState.EXITED],
)
def test_M3_07B_TC_RT_07_authorized_reentry_never_reuses_hash_to_recreate_runtime(
    state: RuntimeProcessState,
) -> None:
    item = _item(ModelInstanceStatus.STARTING)
    store = _Store((item,))
    store.verifications[INSTANCE_ID] = replace(store.verifications[INSTANCE_ID], authorized=True)
    running = RuntimeObservation(RuntimeProcessState.RUNNING, "127.0.0.1:8123")
    agent = _Agent(inspect=(RuntimeObservation(state, "127.0.0.1:8123"), running, running))
    asyncio.run(_controller(store, agent, _Health((True, True))).process_starting(item))
    assert not agent.launches
    assert store.statuses[INSTANCE_ID] is (
        ModelInstanceStatus.RUNNING
        if state is RuntimeProcessState.RUNNING
        else ModelInstanceStatus.UNHEALTHY
    )
    assert INSTANCE_ID not in store.released


# FX-FIX-32: Host Agent container authority shape from TDD 9.7; fake dependencies only.
class _Fix32Health(_Health):
    async def ready(self, endpoint: str, served_model_name: str) -> bool:
        assert endpoint == f"runtime-{INSTANCE_ID}:8000"
        assert served_model_name == "mi_430000000000"
        return self.outcomes.popleft()

    async def warmup(self, endpoint: str, served_model_name: str) -> bool:
        assert endpoint == f"runtime-{INSTANCE_ID}:8000"
        assert served_model_name == "mi_430000000000"
        self.warmup_calls.append((endpoint, served_model_name))
        return True


@pytest.mark.parametrize("status", [ModelInstanceStatus.STARTING, ModelInstanceStatus.RESTARTING])
def test_FIX_32_container_alias_becomes_running_and_is_published(
    status: ModelInstanceStatus,
) -> None:
    item = _item(status)
    store = _Store((item,))
    endpoint = f"runtime-{INSTANCE_ID}:8000"
    observation = RuntimeObservation(RuntimeProcessState.RUNNING, endpoint)
    agent = _Agent(
        ensure=observation,
        inspect=(RuntimeObservation(RuntimeProcessState.NOT_FOUND), observation, observation)
        if status is ModelInstanceStatus.RESTARTING
        else (observation, observation),
    )
    health = _Fix32Health((True, True))
    controller = _controller(store, agent, health, sleep=lambda _: asyncio.sleep(0))
    asyncio.run(
        controller.process_starting(item)
        if status is ModelInstanceStatus.STARTING
        else controller.process_restarting(item)
    )
    assert store.statuses[INSTANCE_ID] is ModelInstanceStatus.RUNNING
    assert store.endpoints[INSTANCE_ID] == endpoint
    assert INSTANCE_ID not in store.released
    assert health.warmup_calls == [(endpoint, item.instance.internal_model_name)]


@pytest.mark.parametrize(
    "endpoint",
    [
        "runtime-43000000-0000-4000-8000-000000000002:8000",
        "runtime-43000000000040008000000000000001:8000",
        "runtime-AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA:8000",
        "example.com:8000",
        "localhost:8000",
        "127.0.0.2:8000",
        f"http://runtime-{INSTANCE_ID}:8000",
        f"user@runtime-{INSTANCE_ID}:8000",
        f"runtime-{INSTANCE_ID}:8000?query",
        f"runtime-{INSTANCE_ID}:8000#fragment",
        f"runtime-{INSTANCE_ID}:8000/path",
        f"runtime-{INSTANCE_ID}:0",
        f"runtime-{INSTANCE_ID}:65536",
        f"runtime-{INSTANCE_ID}:8000\n",
        "127.0.0.1:0",
        "127.0.0.1:65536",
    ],
)
def test_FIX_32_untrusted_endpoint_fails_before_health_or_publication(endpoint: str) -> None:
    item = _item(ModelInstanceStatus.STARTING)
    store = _Store((item,))
    agent = _Agent(ensure=RuntimeObservation(RuntimeProcessState.RUNNING, endpoint))
    health = _Health(())
    asyncio.run(_controller(store, agent, health).process_starting(item))
    assert store.statuses[INSTANCE_ID] is ModelInstanceStatus.FAILED
    assert store.error_codes[INSTANCE_ID] == "runtime_start_failed"
    assert INSTANCE_ID not in store.endpoints
    assert INSTANCE_ID in store.released
    assert agent.stop_calls == [(str(INSTANCE_ID), 1)]
    assert health.warmup_calls == []


@pytest.mark.parametrize("host", ["127.0.0.1", f"runtime-{INSTANCE_ID}"])
@pytest.mark.parametrize("port", [1, 65535])
def test_FIX_32_controller_accepts_valid_port_limits(host: str, port: int) -> None:
    from holm_instance_controller.controller import _trusted_endpoint

    endpoint = f"{host}:{port}"
    assert _trusted_endpoint(endpoint, INSTANCE_ID) == endpoint

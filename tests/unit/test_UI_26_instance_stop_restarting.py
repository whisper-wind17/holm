"""FX-UI-26: deterministic stop interleavings using PROTO-03 store/Agent fixtures."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest
from holm_domain import ModelInstanceStatus
from holm_instance_controller import (
    RuntimeLaunchRequest,
    RuntimeObservation,
    RuntimeProcessState,
)
from test_PROTO_03_instance_controller import (
    INSTANCE_ID,
    _Agent,
    _controller,
    _Health,
    _item,
    _Store,
)


class _ActiveAgent(_Agent):
    """Track active fake processes, not just whether Stop was called."""

    def __init__(self) -> None:
        super().__init__()
        self.active: set[str] = set()
        self.inspections = 0

    async def ensure(self, request: RuntimeLaunchRequest) -> RuntimeObservation:
        self.active.add(str(INSTANCE_ID))
        return await super().ensure(request)

    async def inspect(self, instance_id: str) -> RuntimeObservation:
        if instance_id not in self.active:
            return RuntimeObservation(RuntimeProcessState.NOT_FOUND)
        self.inspections += 1
        return self.ensure_result

    async def stop(self, instance_id: str, *, grace_seconds: int) -> RuntimeObservation:
        self.active.discard(instance_id)
        return await super().stop(instance_id, grace_seconds=grace_seconds)


@pytest.mark.parametrize("completed", [0, 1, 2])
def test_UI_26_TC_GPU_16_stop_during_backoff_abandons_next_attempt(completed: int) -> None:
    item = _item(ModelInstanceStatus.RESTARTING)
    item = replace(
        item,
        instance=replace(
            item.instance, updated_at=item.instance.updated_at - timedelta(seconds=60)
        ),
    )
    item = replace(item, instance=replace(item.instance, restart_count=completed))
    store = _Store((item,))
    agent = _ActiveAgent()
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        assert seconds == (5, 15, 45)[completed]
        assert len(sleeps) == 1
        store.statuses[INSTANCE_ID] = ModelInstanceStatus.STOPPING

    async def scenario() -> None:
        controller = _controller(store, agent, _Health(()), sleep=sleep)
        await controller.process_restarting(item)
        assert store.statuses[INSTANCE_ID] is ModelInstanceStatus.STOPPING
        assert store.restart_counts[INSTANCE_ID] == completed
        assert agent.launches == []
        assert agent.stop_calls == []
        assert store.released == set()
        await controller.process_stopping(
            replace(item, instance=replace(item.instance, status=ModelInstanceStatus.STOPPING))
        )

    asyncio.run(scenario())
    assert sleeps == [(5, 15, 45)[completed]]
    assert store.statuses[INSTANCE_ID] is ModelInstanceStatus.STOPPED
    assert store.released == {INSTANCE_ID}
    assert store.restart_counts[INSTANCE_ID] == completed
    assert agent.stop_calls == [(str(INSTANCE_ID), 1)]
    assert agent.active == set()
    assert store.error_codes == {}


def test_UI_26_TC_GPU_09_stop_during_start_yields_on_next_poll_before_timeout() -> None:
    item = _item(ModelInstanceStatus.RESTARTING)
    item = replace(
        item,
        instance=replace(
            item.instance, updated_at=item.instance.updated_at - timedelta(seconds=60)
        ),
    )
    store = _Store((item,))
    agent = _ActiveAgent()
    health = _Health((False,))
    sleeps: list[float] = []
    now = 0.0

    async def sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds
        if len(sleeps) == 1:
            assert seconds == 5
            assert agent.active == set()
        else:
            assert sleeps == [5, 0.001]
            assert agent.inspections == 1
            assert agent.active == {str(INSTANCE_ID)}
            # Stop arrives while the first health poll is sleeping. The fake clock
            # stays far below the five-second startup deadline, with no scheduling race.
            store.statuses[INSTANCE_ID] = ModelInstanceStatus.STOPPING

    async def scenario() -> None:
        controller = _controller(store, agent, health, sleep=sleep, monotonic=lambda: now)
        await controller.process_restarting(item)
        assert now == 5.001
        assert store.statuses[INSTANCE_ID] is ModelInstanceStatus.STOPPING
        assert agent.stop_calls == []
        assert agent.active == {str(INSTANCE_ID)}
        assert store.released == set()
        await controller.process_stopping(
            replace(item, instance=replace(item.instance, status=ModelInstanceStatus.STOPPING))
        )

    asyncio.run(scenario())
    assert sleeps == [5, 0.001]
    assert len(agent.launches) == 1
    assert agent.inspections == 1
    assert store.restart_counts[INSTANCE_ID] == 1
    assert store.statuses[INSTANCE_ID] is ModelInstanceStatus.STOPPED
    assert store.released == {INSTANCE_ID}
    assert agent.stop_calls == [(str(INSTANCE_ID), 1)]
    assert agent.active == set()
    assert store.error_codes == {}
    assert health.warmup_calls == []

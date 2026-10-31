"""FX-FIX-21: stateful processes and PROTO-03's deterministic lifecycle store."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from holm_contracts.agent.v1 import agent_pb2
from holm_domain import ModelInstanceStatus
from holm_instance_controller.agent_client import (
    RuntimeAgentError,
    RuntimeLaunchRequest,
    RuntimeObservation,
    RuntimeProcessState,
    _runtime_observation,
)
from test_PROTO_03_instance_controller import (
    GPU,
    INSTANCE_ID,
    _Agent,
    _controller,
    _Health,
    _item,
    _Store,
)


class _ProcessAgent(_Agent):
    def __init__(self, started_at: datetime | None, *, active: bool = True) -> None:
        super().__init__()
        self.active = active
        self.started_at = started_at
        self.events: list[str] = []
        self.stop_failure: str | None = None

    async def inspect(self, instance_id: str) -> RuntimeObservation:
        return RuntimeObservation(
            RuntimeProcessState.RUNNING if self.active else RuntimeProcessState.NOT_FOUND,
            "127.0.0.1:8123" if self.active else "",
            started_at=self.started_at,
        )

    async def stop(self, instance_id: str, *, grace_seconds: int) -> RuntimeObservation:
        self.events.append("stop")
        if self.stop_failure == "error":
            raise RuntimeAgentError()
        if self.stop_failure == "timeout":
            raise TimeoutError()
        if self.stop_failure == "still_running":
            return await self.inspect(instance_id)
        self.active = False
        return await super().stop(instance_id, grace_seconds=grace_seconds)

    async def ensure(self, request: RuntimeLaunchRequest) -> RuntimeObservation:
        assert not self.active, "Ensure must never adopt the old process"
        self.events.append("ensure")
        self.active = True
        self.started_at = datetime.now(UTC)
        return await super().ensure(request)


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.mark.parametrize("active", [True, False])
def test_FIX_21_manual_restart_stops_old_process_and_crash_restart_keeps_gpu(active: bool) -> None:
    item = _item(ModelInstanceStatus.RESTARTING)
    store = _Store((item,))
    agent = _ProcessAgent(item.instance.updated_at - timedelta(seconds=30), active=active)
    asyncio.run(
        _controller(store, agent, _Health((True, True)), sleep=_no_sleep).process_restarting(item)
    )
    assert agent.events == (["stop", "ensure"] if active else ["ensure"])
    assert store.statuses[INSTANCE_ID] is ModelInstanceStatus.RUNNING
    assert store.released == set()
    assert agent.launches[0].gpu_uuids == (GPU,)


@pytest.mark.parametrize("failure", ["error", "timeout", "still_running"])
def test_FIX_21_stop_failure_never_ensures_or_releases_live_gpu(failure: str) -> None:
    item = _item(ModelInstanceStatus.RESTARTING)
    store = _Store((item,))
    agent = _ProcessAgent(item.instance.updated_at - timedelta(seconds=30))
    agent.stop_failure = failure
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    asyncio.run(_controller(store, agent, _Health(()), sleep=sleep).process_restarting(item))
    assert agent.launches == []
    assert agent.active
    assert sleeps == [5, 15, 45]
    assert store.restart_counts[INSTANCE_ID] == 3
    assert store.statuses[INSTANCE_ID] is ModelInstanceStatus.RESTARTING
    assert store.released == set()


@pytest.mark.parametrize("offset", [-1.001, -1.0, 0.0, 0.5])
@pytest.mark.parametrize("attempt", [1, 3])
def test_FIX_21_authorized_recovery_preserves_request_and_new_process(
    offset: float,
    attempt: int,
) -> None:
    item = _item(ModelInstanceStatus.RESTARTING)
    item = replace(item, instance=replace(item.instance, restart_count=attempt))
    store = _Store((item,))
    verification = store.verifications[INSTANCE_ID]
    agent = _ProcessAgent(verification.requested_at + timedelta(seconds=offset))
    asyncio.run(
        _controller(store, agent, _Health((True, True)), sleep=_no_sleep).process_restarting(item)
    )
    assert agent.events == (["stop", "ensure"] if offset < -1 else [])
    assert store.verifications[INSTANCE_ID].request_id == verification.request_id
    assert store.restart_counts[INSTANCE_ID] == attempt
    assert store.statuses[INSTANCE_ID] is ModelInstanceStatus.RUNNING
    assert store.released == set()


@pytest.mark.parametrize("wire", ["", "not-a-time SECRET-FIX-21", "2026-10-16T12:00:00"])
def test_FIX_21_missing_invalid_time_warns_without_stop_ensure_or_new_request(
    wire: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    item = _item(ModelInstanceStatus.RESTARTING)
    item = replace(item, instance=replace(item.instance, restart_count=1))
    store = _Store((item,))
    verification = store.verifications[INSTANCE_ID]

    class WireAgent(_ProcessAgent):
        async def inspect(self, instance_id: str) -> RuntimeObservation:
            return _runtime_observation(_wire(wire))

    agent = WireAgent(None)
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    asyncio.run(_controller(store, agent, _Health(()), sleep=sleep).process_restarting(item))
    assert agent.events == []
    assert store.verifications[INSTANCE_ID] == verification
    assert store.statuses[INSTANCE_ID] is ModelInstanceStatus.RESTARTING
    assert store.released == set()
    assert sleeps == [15]
    assert "runtime_restart_inspection_unavailable" in caplog.text
    assert "SECRET-FIX-21" not in caplog.text


def _wire(started_at: str) -> agent_pb2.InspectRuntimeResponse:
    return agent_pb2.InspectRuntimeResponse(
        status=agent_pb2.RUNTIME_PROCESS_STATE_RUNNING,
        endpoint="127.0.0.1:8123",
        started_at=started_at,
        gpu_uuids=agent_pb2.ObservedGpuUuids(
            state=agent_pb2.KNOWLEDGE_STATE_KNOWN,
            values=[GPU],
        ),
    )


@pytest.mark.parametrize("wire", ["2026-10-16T04:00:00Z", "2026-10-16T12:00:00+08:00"])
def test_FIX_21_inspect_preserves_started_at_as_utc(wire: str) -> None:
    assert _runtime_observation(_wire(wire)).started_at == datetime(2026, 10, 16, 4, tzinfo=UTC)

"""M4-17 / FX-CALL-LIMITS: deterministic monotonic clock, no real 60s waits."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from uuid import UUID, uuid4

import pytest
import test_M5_03_gateway_ledger_lifecycle as lifecycle_fixture
from holm_domain import ErrorCode
from holm_gateway.openai_adapter import OpenAIProtocolAdapter
from holm_ledger import EventType, SequenceAction, decide_event_sequence
from holm_protocols import (
    AdmissionController,
    AuthenticatedPrincipal,
    CircuitBreaker,
    InferenceFailure,
)
from holm_protocols.admission import AdmissionPermit
from holm_runtimes import RuntimeRequestCapabilities

USER, SERVICE, INSTANCE = UUID(int=4171), UUID(int=4172), UUID(int=4173)
KEY = (USER, SERVICE)


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


async def acquire(
    controller: AdmissionController,
    limits: tuple[int | None, int | None] = (2, None),
    *,
    user: UUID = USER,
    service: UUID = SERVICE,
    instance: UUID = INSTANCE,
    policy: Callable[[], tuple[int | None, int | None]] | None = None,
) -> AdmissionPermit | None:
    return await controller.acquire(
        instance, user_id=user, service_id=service, limits=policy or (lambda: limits)
    )


async def queued(controller: AdmissionController, size: int) -> None:
    async with asyncio.timeout(2):
        while (await controller.grant_counts())[1].get(KEY, 0) != size:
            await asyncio.sleep(0)


async def zero(controller: AdmissionController, clock: Clock) -> None:
    clock.now += 61
    assert await controller.active_counts() == (0, {})
    assert await controller.grant_counts() == ({}, {}, {})


def test_M4_17_TC_GW_35_fifo_isolation_queue_full_timeout_and_cancel() -> None:
    async def scenario() -> None:
        clock = Clock()
        controller = AdmissionController(64, clock=clock)
        first, second = await acquire(controller), await acquire(controller)
        assert first is not None and second is not None
        await first.mark_started()
        await second.mark_started()
        independent = [
            await acquire(controller, user=uuid4()),
            await acquire(controller, service=uuid4()),
        ]
        assert all(independent)
        tasks = []
        for number in range(32):
            tasks.append(asyncio.create_task(acquire(controller)))
            await queued(controller, number + 1)
        with pytest.raises(InferenceFailure) as full:
            await acquire(controller)
        assert full.value.error.limit_type == "concurrent"
        assert full.value.error.limit == 2
        assert (await controller.grant_counts())[2][KEY] == 34
        await first.release()
        third = await asyncio.wait_for(tasks[0], 1)
        assert third is not None
        await third.mark_started()
        assert not any(task.done() for task in tasks[1:])
        tasks[1].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[1]
        await queued(controller, 30)
        clock.now = 60
        for task in tasks[2:]:
            with pytest.raises(InferenceFailure) as timeout:
                await asyncio.wait_for(task, 1)
            assert timeout.value.error.queue_timed_out
            assert timeout.value.error.retry_after == 1
        for permit in [second, third, *independent]:
            assert permit is not None
            await permit.release()
        await zero(controller, clock)

    asyncio.run(scenario())


def test_M4_17_TC_GW_36_sliding_window_rounding_rollback_and_unlimited() -> None:
    async def scenario() -> None:
        clock = Clock()
        controller = AdmissionController(8, clock=clock)
        for moment in [0.0, 0.2, 1.0]:
            clock.now = moment
            permit = await acquire(controller, (None, 3))
            assert permit is not None
            await permit.mark_started()
            await permit.release()
        clock.now = 1.1
        with pytest.raises(InferenceFailure) as rejected:
            await acquire(controller, (None, 3))
        assert rejected.value.error.limit_type == "per_minute"
        assert rejected.value.error.retry_after == 59
        assert (await controller.grant_counts())[2][KEY] == 3
        clock.now = 60
        permit = await acquire(controller, (None, 3))
        assert permit is not None
        await permit.release()  # before InProgress: rollback
        assert (await controller.grant_counts())[2][KEY] == 2
        for _ in range(5):
            permit = await acquire(controller, (None, None))
            assert permit is not None
            await permit.mark_started()
            await permit.release()
        await zero(controller, clock)

    asyncio.run(scenario())


@pytest.mark.parametrize("instance_limit", [1, 2])
def test_M4_17_TC_GW_37_platform_fast_fail_returns_all_reservations(instance_limit: int) -> None:
    async def scenario() -> None:
        clock = Clock()
        controller = AdmissionController(
            2 if instance_limit == 1 else 1, instance_limit, clock=clock
        )
        occupied = await controller.try_acquire(INSTANCE)
        assert occupied is not None
        assert await acquire(controller, (2, 3)) is None
        assert await controller.grant_counts() == ({}, {}, {})
        await occupied.release()
        await zero(controller, clock)

    asyncio.run(scenario())


def test_M4_17_TC_GW_39_lowering_policy_keeps_inflight_and_rechecks_waiters() -> None:
    async def scenario() -> None:
        clock = Clock()
        controller = AdmissionController(8, clock=clock)
        policy = [2]
        first = await acquire(controller, policy=lambda: (policy[0], None))
        second = await acquire(controller, policy=lambda: (policy[0], None))
        assert first is not None and second is not None
        waiting = asyncio.create_task(acquire(controller, policy=lambda: (policy[0], None)))
        await queued(controller, 1)
        policy[0] = 1
        await first.release()
        await asyncio.sleep(0.12)
        assert not waiting.done()
        assert (await controller.active_counts())[0] == 1
        policy[0] = 3
        third = await asyncio.wait_for(waiting, 1)
        assert third is not None
        await second.release()
        await third.release()
        await zero(controller, clock)

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["per_minute", "full", "timeout", "disconnect", "platform"])
def test_M4_17_TC_GW_35_38_lifecycle_rejection_abort_and_503(kind: str) -> None:
    async def scenario() -> None:
        f = lifecycle_fixture
        clock = Clock()
        controller = AdmissionController(1 if kind == "platform" else 64, clock=clock)
        occupied = await controller.acquire(
            f.INSTANCE_ID,
            user_id=f.USER_ID,
            service_id=f.SERVICE_ID,
            limits=lambda: (1, 1 if kind == "per_minute" else None),
        )
        assert occupied is not None
        await occupied.mark_started()
        tasks = []
        key = (f.USER_ID, f.SERVICE_ID)
        if kind == "full":
            for _ in range(32):
                tasks.append(
                    asyncio.create_task(
                        controller.acquire(
                            f.INSTANCE_ID,
                            user_id=f.USER_ID,
                            service_id=f.SERVICE_ID,
                            limits=lambda: (1, None),
                        )
                    )
                )
            async with asyncio.timeout(1):
                while (await controller.grant_counts())[1].get(key) != 32:
                    await asyncio.sleep(0)
        ledger = f._Ledger()
        context = f._context(ledger)
        adapter = OpenAIProtocolAdapter(
            {
                "vllm": RuntimeRequestCapabilities(
                    openai_chat_completions=frozenset({"model", "messages"})
                )
            },
            maximum_json_depth=32,
        )
        context.failure_status = adapter.failure_status
        runtime = f._Runtime()
        principal = AuthenticatedPrincipal(
            f.USER_ID,
            f.KEY_ID,
            frozenset(),
            True,
            frozenset({f.SERVICE_ID}),
            {f.SERVICE_ID: (2 if kind == "platform" else 1, 1 if kind == "per_minute" else None)},
        )
        core = f._core(runtime, controller)
        breaker = CircuitBreaker(
            failure_threshold=1, failure_window_seconds=60, cooldown_seconds=30
        )
        core._breaker = breaker
        task = asyncio.create_task(
            core.infer_routed(
                route=f._route(),
                principal=principal,
                request_id=f.REQUEST_ID,
                request=f._request(),
                lifecycle=context,
            )
        )
        if kind in {"timeout", "disconnect"}:
            async with asyncio.timeout(1):
                while (await controller.grant_counts())[1].get(key) != 1:
                    await asyncio.sleep(0)
            assert [event.event_type for event in ledger.events] == [EventType.RECEIVED]
            if kind == "timeout":
                clock.now = 60
            else:
                task.cancel()
        if kind == "disconnect":
            with pytest.raises(asyncio.CancelledError):
                await task
            assert [event.event_type for event in ledger.events] == [
                EventType.RECEIVED,
                EventType.ABORTED,
            ]
            assert (
                decide_event_sequence(ledger.events[:1], ledger.events[1]).action
                is SequenceAction.WRITE
            )
        else:
            with pytest.raises(InferenceFailure) as failure:
                await task
            if kind == "platform":
                assert failure.value.error.code is ErrorCode.GATEWAY_OVERLOADED
                assert [event.event_type for event in ledger.events] == [
                    EventType.RECEIVED,
                    EventType.IN_PROGRESS,
                    EventType.FAILED,
                ]
            else:
                mapped = adapter.map_error(failure.value.error, f.REQUEST_ID)
                body = json.loads(mapped.body)["error"]
                assert mapped.status_code == 429
                assert body["details"] == {
                    "limit_type": "per_minute" if kind == "per_minute" else "concurrent",
                    "limit": 1,
                }
                assert "（1 " in body["message"]
                assert dict(mapped.headers)["Retry-After"] == (
                    "60" if kind == "per_minute" else "1"
                )
                assert [event.event_type for event in ledger.events] == [
                    EventType.RECEIVED,
                    EventType.REJECTED,
                ]
        assert runtime.calls == 0
        assert breaker._states == {}
        assert context.input_tokens is None and context.output_tokens is None
        for waiting in tasks:
            waiting.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await occupied.release()
        await zero(controller, clock)

    asyncio.run(scenario())


def test_M4_17_TC_GW_38_error_mapping_requires_limit_details() -> None:
    from holm_protocols import PlatformError

    adapter = OpenAIProtocolAdapter(
        {
            "vllm": RuntimeRequestCapabilities(
                openai_chat_completions=frozenset({"model", "messages"})
            )
        },
        maximum_json_depth=32,
    )
    with pytest.raises(ValueError, match="requires policy details"):
        adapter.map_error(PlatformError(ErrorCode.RATE_LIMIT_EXCEEDED), uuid4())


@pytest.mark.parametrize("terminal", ["completed", "runtime_failure", "storage_failure", "aborted"])
def test_M4_17_TC_GW_35_37_terminal_paths_release_all_permits(terminal: str) -> None:
    from holm_protocols import CircuitBreaker, ContentDelta, PlatformError

    async def scenario() -> None:
        f = lifecycle_fixture
        clock = Clock()
        controller = AdmissionController(2, clock=clock)
        ledger = f._Ledger(fail_on=EventType.IN_PROGRESS if terminal == "storage_failure" else None)
        context = f._context(ledger)
        principal = AuthenticatedPrincipal(
            f.USER_ID,
            f.KEY_ID,
            frozenset(),
            False,
            frozenset({f.SERVICE_ID}),
            {f.SERVICE_ID: (1, 3)},
        )

        class Runtime(f._Runtime):
            async def stream(self, endpoint, request_id, canonical_request):
                if terminal == "runtime_failure":
                    raise InferenceFailure(
                        PlatformError(ErrorCode.BACKEND_UNAVAILABLE), counts_toward_breaker=True
                    )
                yield ContentDelta("memory-only")
                if terminal == "aborted":
                    raise asyncio.CancelledError

        core = f._core(Runtime(), controller)
        core._breaker = CircuitBreaker(
            failure_threshold=1, failure_window_seconds=60, cooldown_seconds=30
        )

        async def call() -> None:
            response = await core.infer_routed(
                route=f._route(),
                principal=principal,
                request_id=f.REQUEST_ID,
                request=f._request(),
                lifecycle=context,
            )
            async for _ in response.events:
                pass

        if terminal in {"runtime_failure", "storage_failure"}:
            with pytest.raises(InferenceFailure):
                await call()
        elif terminal == "aborted":
            with pytest.raises(asyncio.CancelledError):
                await call()
        else:
            await call()
        if terminal == "storage_failure":
            assert (await controller.grant_counts())[2] == {}
        await zero(controller, clock)

    asyncio.run(scenario())


def test_M4_17_TC_GW_36_39_lowered_minute_limit_rechecks_queued_reservations() -> None:
    async def scenario() -> None:
        clock = Clock()
        controller = AdmissionController(8, clock=clock)
        policy: list[tuple[int | None, int | None]] = [(1, 3)]
        first = await acquire(controller, policy=lambda: policy[0])
        assert first is not None
        await first.mark_started()
        waiting = []
        for size in (1, 2):
            waiting.append(asyncio.create_task(acquire(controller, policy=lambda: policy[0])))
            await queued(controller, size)
        policy[0] = (1, 1)
        for task in waiting:
            with pytest.raises(InferenceFailure) as rejected:
                await asyncio.wait_for(task, 2)
            assert rejected.value.error.limit_type == "per_minute"
            assert rejected.value.error.limit == 1
            assert rejected.value.error.retry_after == 60
        assert await controller.grant_counts() == ({KEY: 1}, {}, {KEY: 1})
        policy[0] = (None, None)
        restored = await acquire(controller, policy=lambda: policy[0])
        assert restored is not None
        await restored.release()
        await first.release()
        await zero(controller, clock)

    asyncio.run(scenario())

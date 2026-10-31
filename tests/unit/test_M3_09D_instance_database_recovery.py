"""M3-09D: instance-controller recovery from PostgreSQL interruptions."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import cast
from uuid import UUID, uuid4

import pytest
from holm_instance_controller import (
    ControllerStore,
    InstanceController,
    RuntimeAgentClient,
    RuntimeHealthProbe,
)
from holm_runtimes import RuntimeCompatibility
from sqlalchemy.exc import OperationalError


def _database_error() -> OperationalError:
    return OperationalError(
        "SELECT secret_sql_sentinel",
        {"password": "secret_password_sentinel"},
        OSError("postgresql://secret_dsn_sentinel/holm"),
    )


class _Store:
    async def pending_cleanup(self, after: UUID | None) -> tuple[UUID, ...]:
        return ()

    def __init__(
        self,
        *,
        claim_failures: int = 0,
        stop: asyncio.Event | None = None,
        claim_error: BaseException | None = None,
    ) -> None:
        self.claim_failures = claim_failures
        self.stop = stop
        self.claim_error = claim_error
        self.claim_calls = 0

    async def list_for_reconciliation(self) -> tuple[object, ...]:
        return ()

    async def release_terminal_allocations(self) -> tuple[object, ...]:
        return ()

    async def claim(self, _status: object, *, excluded_ids: tuple[object, ...] = ()) -> None:
        del excluded_ids
        self.claim_calls += 1
        if self.claim_error is not None:
            raise self.claim_error
        if self.claim_calls <= self.claim_failures:
            raise _database_error()
        if self.stop is not None:
            self.stop.set()
        return None


class _Agent:
    async def list(self) -> tuple[object, ...]:
        return ()

    async def list_gpus(self) -> frozenset[str]:
        return frozenset()


class _Health:
    async def ready(self, endpoint: str, served_model_name: str) -> bool:
        del endpoint, served_model_name
        return True

    async def warmup(self, endpoint: str, served_model_name: str) -> bool:
        del endpoint, served_model_name
        return True


def _controller(
    store: _Store,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    initial_window: float = 60.0,
) -> InstanceController:
    return InstanceController(
        store=cast(ControllerStore, store),
        agent=cast(RuntimeAgentClient, _Agent()),
        health=cast(RuntimeHealthProbe, _Health()),
        runtime=cast(RuntimeCompatibility, object()),
        dispatch_seconds=0.001,
        reconciliation_seconds=30.0,
        initial_reconciliation_window_seconds=initial_window,
        sleep=sleep,
    )


def test_M3_09D_dispatch_database_failure_retries_with_bounded_backoff_and_recovers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop = asyncio.Event()
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    store = _Store(claim_failures=6, stop=stop)
    caplog.set_level(logging.WARNING, logger="holm.instance_controller")
    asyncio.run(_controller(store, sleep=fake_sleep).run(stop))

    assert store.claim_calls == 11
    database_sleeps = [seconds for seconds in sleeps if seconds != 30.0]
    assert database_sleeps == [1.0, 2.0, 4.0, 5.0, 5.0, 5.0]
    assert max(database_sleeps) == 5.0
    records = [record for record in caplog.records if record.name == "holm.instance_controller"]
    assert records
    assert {record.component for record in records} == {"instance-controller"}
    assert {record.error_code for record in records} == {"database_unavailable"}
    assert "secret_sql_sentinel" not in caplog.text
    assert "secret_password_sentinel" not in caplog.text
    assert "secret_dsn_sentinel" not in caplog.text


def test_M3_09D_dispatch_database_backoff_is_interrupted_by_stop_signal() -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        attempted = asyncio.Event()

        class SignallingStore(_Store):
            async def claim(
                self, _status: object, *, excluded_ids: tuple[object, ...] = ()
            ) -> None:
                del excluded_ids
                attempted.set()
                raise _database_error()

        running = asyncio.create_task(_controller(SignallingStore()).run(stop))
        await asyncio.wait_for(attempted.wait(), timeout=0.5)
        stop.set()
        await asyncio.wait_for(running, timeout=0.5)

    asyncio.run(scenario())


def test_M3_09D_periodic_database_failure_runs_and_succeeds_next_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()
    calls = 0
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def flaky_recovery(_self: InstanceController) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _database_error()
        stop.set()

    monkeypatch.setattr(InstanceController, "recover_once", flaky_recovery)
    asyncio.run(_controller(_Store(), sleep=fake_sleep)._periodic_reconciliation(stop))

    assert calls == 2
    assert sleeps == [30.0, 1.0]


def test_M3_09D_initial_database_failure_keeps_retrying_after_old_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def flaky_recovery(_self: InstanceController) -> None:
        nonlocal calls
        calls += 1
        if calls < 4:
            raise _database_error()

    monkeypatch.setattr(InstanceController, "recover_once", flaky_recovery)
    asyncio.run(
        _controller(_Store(), sleep=fake_sleep, initial_window=0.001)._initial_reconciliation()
    )

    assert calls == 4
    assert sleeps == [0.001, 0.001, 0.001]


def test_M3_09D_programming_error_is_not_swallowed_by_dispatch_loop() -> None:
    stop = asyncio.Event()
    controller = _controller(_Store(claim_error=ValueError("programming error")))

    with pytest.raises(ValueError, match="programming error"):
        asyncio.run(controller.run(stop))


def test_M3_09D_instance_operation_failure_is_logged_without_stopping_controller(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        controller = _controller(_Store())

        async def failing_operation() -> None:
            raise ValueError("instance_operation_sentinel")

        task = asyncio.create_task(failing_operation())
        await asyncio.gather(task, return_exceptions=True)
        controller._tasks[uuid4()] = task
        controller._harvest()
        assert controller._tasks == {}

    caplog.set_level(logging.WARNING, logger="holm.instance_controller")
    asyncio.run(scenario())

    records = [record for record in caplog.records if record.name == "holm.instance_controller"]
    assert {record.error_code for record in records} == {"instance_operation_failed"}
    assert "instance_operation_sentinel" not in caplog.text


def test_M3_09D_unexpected_reconciliation_item_failure_keeps_periodic_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Instance:
        id = uuid4()

    class _Item:
        instance = _Instance()

    class ItemStore(_Store):
        async def list_for_reconciliation(self) -> tuple[object, ...]:
            return (_Item(),)

    async def failing_item(
        _self: InstanceController,
        _item: object,
        _inventory_gpu_uuids: frozenset[str] | None = None,
    ) -> None:
        raise ValueError("reconciliation_item_sentinel")

    monkeypatch.setattr(InstanceController, "_reconcile_item", failing_item)

    with pytest.raises(RuntimeError, match="instance reconciliation dependency failed"):
        asyncio.run(_controller(ItemStore()).recover_once())

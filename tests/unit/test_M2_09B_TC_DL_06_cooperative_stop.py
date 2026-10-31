"""TC-DL-06: cooperative-stop and command-poll boundaries."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from multiprocessing.context import SpawnProcess
from typing import cast

import pytest
from holm_download_worker import DownloadResolutionJobHandler, DownloadWorkDisposition
from holm_download_worker.resolution_subprocess import (
    DEFAULT_COOPERATIVE_STOP_SECONDS,
    ResolutionSubprocessRequest,
    ResolutionSubprocessResult,
    _cooperatively_stop_process,
)
from holm_persistence import AsyncSessionFactory, JobHandlerContext
from holm_persistence.download_task_repository import DownloadResolutionWorkRecord


@dataclass
class _FakeProcess:
    exits_on_terminate: bool
    alive: bool = True
    calls: list[tuple[str, float | None]] = field(default_factory=list)

    def join(self, timeout: float | None = None) -> None:
        self.calls.append(("join", timeout))

    def is_alive(self) -> bool:
        return self.alive

    def terminate(self) -> None:
        self.calls.append(("terminate", None))
        if self.exits_on_terminate:
            self.alive = False

    def kill(self) -> None:
        self.calls.append(("kill", None))
        self.alive = False


def _stop(process: _FakeProcess, timeout_seconds: float = 0.125) -> None:
    _cooperatively_stop_process(cast(SpawnProcess, process), timeout_seconds)


def test_TC_DL_06_cooperative_exit_before_boundary_is_never_forced() -> None:
    assert DEFAULT_COOPERATIVE_STOP_SECONDS == 10.0
    process = _FakeProcess(exits_on_terminate=True)
    _stop(process)
    assert process.calls == [
        ("join", 0),
        ("terminate", None),
        ("join", 0.125),
    ]
    print("TC-DL-06 cooperative stop: exit inside 10-second boundary avoided force")


def test_TC_DL_06_uncooperative_exit_is_forced_only_after_boundary() -> None:
    process = _FakeProcess(exits_on_terminate=False)
    _stop(process)
    assert process.calls[:3] == [
        ("join", 0),
        ("terminate", None),
        ("join", 0.125),
    ]
    assert process.calls[3][0] == "kill"
    assert process.alive is False
    print("TC-DL-06 cooperative stop: force observed only after injected boundary")


def test_TC_DL_06_positive_control_immediate_force_fails_boundary_assertion() -> None:
    process = _FakeProcess(exits_on_terminate=False)

    def immediate_force(mutant: _FakeProcess) -> None:
        mutant.kill()

    immediate_force(process)
    with pytest.raises(AssertionError):
        assert process.calls[:3] == [
            ("join", 0),
            ("terminate", None),
            ("join", 0.125),
        ]
    print("TC-DL-06 positive control 8: immediate-force mutant failed")


def test_TC_DL_06_positive_control_force_after_success_fails() -> None:
    process = _FakeProcess(exits_on_terminate=True)

    def force_even_after_exit(mutant: _FakeProcess) -> None:
        mutant.terminate()
        mutant.join(timeout=0.125)
        mutant.kill()

    force_even_after_exit(process)
    with pytest.raises(AssertionError):
        assert all(call[0] != "kill" for call in process.calls)
    print("TC-DL-06 positive control 9: force-after-cooperative-exit mutant failed")


class _ResolverTimeout(TimeoutError):
    pass


class _TimeoutResolver:
    async def resolve(self, request: ResolutionSubprocessRequest) -> ResolutionSubprocessResult:
        del request
        raise _ResolverTimeout("resolver-owned timeout")


def test_TC_DL_06_resolver_timeout_is_not_mistaken_for_command_poll_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = False

    async def observe(
        self: DownloadResolutionJobHandler,
        context: JobHandlerContext,
        expected: DownloadResolutionWorkRecord,
    ) -> DownloadWorkDisposition | None:
        del self, context, expected
        nonlocal observed
        observed = True
        return None

    monkeypatch.setattr(DownloadResolutionJobHandler, "_observed_command", observe)
    handler = DownloadResolutionJobHandler(
        session_factory=cast(AsyncSessionFactory, object()),
        resolver=_TimeoutResolver(),
        command_poll_seconds=0.01,
    )

    async def exercise() -> None:
        with pytest.raises(_ResolverTimeout, match="resolver-owned timeout"):
            await asyncio.wait_for(
                handler._resolve_until_commanded(  # noqa: SLF001 - polling boundary under test
                    cast(JobHandlerContext, object()),
                    cast(DownloadResolutionWorkRecord, object()),
                    cast(ResolutionSubprocessRequest, object()),
                ),
                timeout=0.1,
            )

    asyncio.run(exercise())
    assert observed is False

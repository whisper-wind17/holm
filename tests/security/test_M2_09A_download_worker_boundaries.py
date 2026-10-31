"""M2-09A subprocess lifetime and source-error disclosure boundaries."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from uuid import uuid4

import pytest
from holm_domain import (
    Identified,
    JobExecutionKey,
    ModelOriginType,
    RemoteFile,
    ResolvedModel,
    RevisionImmutability,
)
from holm_download_worker import (
    ResolutionSubprocessError,
    ResolutionSubprocessRequest,
    SubprocessModelResolver,
)

RAW_SOURCE_SENTINEL: Final[str] = (
    "SDK exploded while reading https://source.invalid/private/repository?token=secret"
)


def _resolved(request: ResolutionSubprocessRequest) -> ResolvedModel:
    return ResolvedModel(
        source=request.source,
        repository_id=request.repository_id,
        requested_revision_norm=request.requested_revision_norm,
        resolved_revision="a" * 40,
        revision_immutability=RevisionImmutability.IMMUTABLE,
        files=Identified((RemoteFile("config.json", 2),)),
        total_size_bytes=Identified(2),
        architecture=Identified("BoundaryArchitecture"),
        modalities=Identified(("text",)),
        precision=Identified("bf16"),
        quantization=Identified("none"),
        context_length=Identified(4096),
        candidate_runtimes=Identified(("vllm",)),
        license=Identified("apache-2.0"),
    )


def _request() -> ResolutionSubprocessRequest:
    return ResolutionSubprocessRequest(
        source=ModelOriginType.HUGGING_FACE,
        repository_id="Private/Sentinel-Repository",
        requested_revision_norm="secret-revision",
        execution_key=JobExecutionKey(uuid4(), 1),
    )


@dataclass(frozen=True, slots=True)
class _SuccessfulProcess:
    def __call__(self, request: ResolutionSubprocessRequest) -> ResolvedModel:
        return _resolved(request)


@dataclass(frozen=True, slots=True)
class _ExplodingProcess:
    def __call__(self, request: ResolutionSubprocessRequest) -> ResolvedModel:
        del request
        raise RuntimeError(RAW_SOURCE_SENTINEL)


@dataclass(frozen=True, slots=True)
class _BlockingProcess:
    ready_path: Path | None = None
    forbidden_environment_value: str | None = None

    def __call__(self, request: ResolutionSubprocessRequest) -> ResolvedModel:
        if (
            self.forbidden_environment_value is not None
            and self.forbidden_environment_value in os.environ.values()
        ):
            raise RuntimeError("inherited environment reached resolution child")
        if self.ready_path is not None:
            self.ready_path.write_text("ready", encoding="ascii")
        time.sleep(30)
        return _resolved(request)


def _process_exists(pid: int) -> bool:
    return os.path.exists(f"/proc/{pid}")


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("subprocess lifecycle condition was not reached")


def test_M2_09A_resolution_runs_in_a_fresh_spawn_child_and_reaps_it() -> None:
    async def exercise() -> None:
        resolver = SubprocessModelResolver(_SuccessfulProcess())
        result = await resolver.resolve(_request())
        assert result.child_pid != os.getpid()
        assert result.child_pid == resolver.last_child_pid
        assert not _process_exists(result.child_pid)

    asyncio.run(exercise())


def test_M2_09A_positive_control_raw_sdk_exception_is_sanitized_across_ipc(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    async def exercise() -> ResolutionSubprocessError:
        resolver = SubprocessModelResolver(_ExplodingProcess())
        with pytest.raises(ResolutionSubprocessError) as captured:
            await resolver.resolve(_request())
        return captured.value

    error = asyncio.run(exercise())
    rendered = "\n".join((str(error), repr(error), repr(_request()), caplog.text))
    assert error.code == "resolution_process_failed"
    assert error.__context__ is None
    assert RAW_SOURCE_SENTINEL not in rendered
    assert "Private/Sentinel-Repository" not in rendered
    assert "secret-revision" not in rendered


def test_M2_09A_cancelled_resolution_reaps_the_spawn_child_and_scrubs_process_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    environment_sentinel = "M2-09A-HF-TOKEN-MUST-NOT-REACH-CHILD"
    monkeypatch.setenv("HF_TOKEN", environment_sentinel)
    ready_path = tmp_path / "child-ready"

    async def exercise() -> None:
        resolver = SubprocessModelResolver(
            _BlockingProcess(ready_path, environment_sentinel),
            timeout_seconds=60,
        )
        task = asyncio.create_task(resolver.resolve(_request()))
        await _wait_until(lambda: resolver.last_child_pid is not None)
        child_pid = resolver.last_child_pid
        assert child_pid is not None and _process_exists(child_pid)
        await _wait_until(ready_path.exists)
        child_arguments = (Path("/proc") / str(child_pid) / "cmdline").read_bytes()
        assert b"Private/Sentinel-Repository" not in child_arguments
        assert b"secret-revision" not in child_arguments
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _wait_until(lambda: not _process_exists(child_pid))

    asyncio.run(exercise())

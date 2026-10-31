"""M3-09C Host Agent GPU observations and persisted-state compatibility."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from holm_host_agent import runtime_process
from holm_host_agent.gpu_discovery import NvmlCallError, NvmlErrorKind
from holm_host_agent.path_validation import RuntimeIdentity
from holm_host_agent.runtime_process import (
    GpuObservationUnknownReason,
    ObservationKnowledgeState,
    ObservedGpuUuids,
    RuntimeLaunchSpec,
    RuntimeManager,
    RuntimeStateValue,
)

INSTANCE_ID = "01990000-0000-7000-8000-00000000090c"
REQUESTED_GPU = "GPU-requested"
OBSERVED_GPU = "GPU-observed"


class _ProcessNvml:
    def __init__(
        self,
        processes: dict[str, tuple[int, ...]],
        *,
        error: NvmlErrorKind | None = None,
    ) -> None:
        self._processes = processes
        self._error = error

    def initialize(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def device_get_count(self) -> int:
        return len(self._processes)

    def device_get_handle_by_index(self, index: int) -> object:
        return tuple(self._processes)[index]

    def device_get_uuid(self, handle: object) -> str:
        assert isinstance(handle, str)
        return handle

    def device_get_compute_running_process_pids(self, handle: object) -> tuple[int, ...]:
        if self._error is not None:
            raise NvmlCallError(self._error)
        assert isinstance(handle, str)
        return self._processes[handle]


def test_M3_09C_observed_GPU_is_from_NVML_not_the_launch_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = RuntimeLaunchSpec(
        instance_id=INSTANCE_ID,
        runtime_name="vllm",
        model_path="/models/fixture",
        gpu_uuids=(REQUESTED_GPU,),
        tensor_parallel_size=1,
        context_length=4096,
        gpu_memory_utilization=0.8,
        served_model_name="mi_m309c",
        config_version="m3-09c",
    )
    monkeypatch.setattr(runtime_process, "_process_group_pids", lambda _group: frozenset({42}))

    observed = runtime_process._observe_runtime_gpu_uuids(  # noqa: SLF001
        _ProcessNvml({REQUESTED_GPU: (), OBSERVED_GPU: (42,)}), 42
    )

    assert observed.state is ObservationKnowledgeState.KNOWN
    assert observed.values == (OBSERVED_GPU,)
    assert observed.values != spec.gpu_uuids


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (NvmlErrorKind.NOT_SUPPORTED, GpuObservationUnknownReason.SOURCE_UNAVAILABLE),
        (NvmlErrorKind.OTHER, GpuObservationUnknownReason.QUERY_FAILED),
    ],
)
def test_M3_09C_NVML_process_query_failure_is_unknown_not_empty(
    monkeypatch: pytest.MonkeyPatch,
    error: NvmlErrorKind,
    reason: GpuObservationUnknownReason,
) -> None:
    monkeypatch.setattr(runtime_process, "_process_group_pids", lambda _group: frozenset({42}))

    observed = runtime_process._observe_runtime_gpu_uuids(  # noqa: SLF001
        _ProcessNvml({REQUESTED_GPU: ()}, error=error), 42
    )

    assert observed.state is ObservationKnowledgeState.UNKNOWN
    assert observed.values == ()
    assert observed.unknown_reason is reason


def test_M3_09C_legacy_runtime_json_without_GPU_field_remains_readable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    instance_root = state_root / INSTANCE_ID
    instance_root.mkdir(parents=True)
    (instance_root / "runtime.json").write_text(
        json.dumps(
            {
                "instance_id": INSTANCE_ID,
                "pid": 42,
                "start_ticks": 100,
                "config_version": "legacy-v1",
                "endpoint": "127.0.0.1:38123",
                "started_at": "2026-09-15T00:00:00Z",
                "state": "running",
                "exit_code": None,
            }
        ),
        encoding="utf-8",
    )
    known = ObservedGpuUuids(
        ObservationKnowledgeState.KNOWN,
        values=(OBSERVED_GPU,),
    )
    monkeypatch.setattr(runtime_process, "_persisted_is_running", lambda _state: True)
    monkeypatch.setattr(runtime_process, "_observe_runtime_gpu_uuids", lambda *_args: known)
    manager = RuntimeManager(
        compatibility_path=tmp_path / "unused.yaml",
        state_root=state_root,
        model_path_policy=None,
        runtime_identity=RuntimeIdentity(uid=1, gid=1),
        nvml_factory=lambda: _ProcessNvml({}),
    )

    inspected = asyncio.run(manager.inspect(INSTANCE_ID))
    listed = asyncio.run(manager.list())

    assert inspected.state is RuntimeStateValue.RUNNING
    assert inspected.gpu_uuids == known
    assert len(listed) == 1
    assert listed[0].instance_id == INSTANCE_ID
    assert listed[0].gpu_uuids == known

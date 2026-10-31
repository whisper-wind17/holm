"""M3-13A bounded OOM observation and sanitized InspectRuntime contract."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from holm_contracts.agent.v1 import agent_pb2
from holm_host_agent.runtime_process import (
    MAX_OOM_LOG_TAIL_BYTES,
    RuntimeStateValue,
    _detect_out_of_memory,
)
from holm_host_agent.runtime_process import (
    RuntimeObservation as HostRuntimeObservation,
)
from holm_host_agent.server import _runtime_observation_to_proto
from holm_instance_controller.agent_client import (
    RuntimeAgentError,
    RuntimeProcessState,
    _runtime_observation,
)

_SIGNATURES = ("CUDA out of memory", "torch.OutOfMemoryError")


def test_M3_13A_host_agent_scans_only_bounded_log_tail(tmp_path: Path) -> None:
    log_path = tmp_path / "runtime.log"
    log_path.write_bytes(_SIGNATURES[0].encode() + b"x" * MAX_OOM_LOG_TAIL_BYTES)
    assert _detect_out_of_memory(log_path, exit_code=23, signatures=_SIGNATURES) is False

    log_path.write_bytes(b"x" * MAX_OOM_LOG_TAIL_BYTES + b"\xff\xfe" + _SIGNATURES[1].encode())
    assert _detect_out_of_memory(log_path, exit_code=23, signatures=_SIGNATURES) is True


@pytest.mark.parametrize(
    ("exit_code", "write_log", "expected"),
    (
        (-9, True, False),
        (17, True, True),
        (17, False, None),
        (None, True, None),
    ),
    ids=("signal-is-not-oom", "positive-exit-match", "log-unavailable", "exit-unavailable"),
)
def test_M3_13A_OOM_observation_distinguishes_false_from_not_collected(
    tmp_path: Path,
    exit_code: int | None,
    write_log: bool,
    expected: bool | None,
) -> None:
    log_path = tmp_path / "runtime.log"
    if write_log:
        log_path.write_text(_SIGNATURES[0], encoding="utf-8")

    assert _detect_out_of_memory(log_path, exit_code=exit_code, signatures=_SIGNATURES) is expected


def test_M3_13A_InspectRuntime_returns_only_boolean_not_log_body_or_path(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "M3-13A-PROMPT-SENTINEL /sensitive/absolute/model/path"
    log_path = tmp_path / "runtime.log"
    log_path.write_text(f"{sentinel}\n{_SIGNATURES[0]}", encoding="utf-8")
    with caplog.at_level(logging.DEBUG):
        oom_killed = _detect_out_of_memory(
            log_path,
            exit_code=23,
            signatures=_SIGNATURES,
        )
        response = _runtime_observation_to_proto(
            HostRuntimeObservation(
                RuntimeStateValue.EXITED,
                exit_code=23,
                oom_killed=oom_killed,
            )
        )
        observed = _runtime_observation(response)

    assert response.HasField("oom_killed")
    assert response.oom_killed is True
    assert observed.state is RuntimeProcessState.EXITED
    assert observed.exit_code == 23
    assert observed.oom_killed is True
    assert sentinel not in str(response)
    assert sentinel not in caplog.text
    assert "/sensitive/absolute/model/path" not in str(response)
    assert "/sensitive/absolute/model/path" not in caplog.text


def test_M3_13A_InspectRuntime_wire_preserves_not_collected_and_known_false() -> None:
    not_collected = agent_pb2.InspectRuntimeResponse(
        status=agent_pb2.RUNTIME_PROCESS_STATE_EXITED,
        exit_code=17,
        gpu_uuids=agent_pb2.ObservedGpuUuids(
            state=agent_pb2.KNOWLEDGE_STATE_UNKNOWN,
            unknown_reason=agent_pb2.OBSERVATION_UNKNOWN_REASON_NOT_COLLECTED,
        ),
    )
    known_false = agent_pb2.InspectRuntimeResponse()
    known_false.CopyFrom(not_collected)
    known_false.oom_killed = False

    assert _runtime_observation(not_collected).oom_killed is None
    assert _runtime_observation(known_false).oom_killed is False


def test_M3_13A_InspectRuntime_rejects_signal_reported_as_OOM() -> None:
    response = agent_pb2.InspectRuntimeResponse(
        status=agent_pb2.RUNTIME_PROCESS_STATE_EXITED,
        exit_code=-9,
        oom_killed=True,
        gpu_uuids=agent_pb2.ObservedGpuUuids(
            state=agent_pb2.KNOWLEDGE_STATE_UNKNOWN,
            unknown_reason=agent_pb2.OBSERVATION_UNKNOWN_REASON_NOT_COLLECTED,
        ),
    )

    with pytest.raises(RuntimeAgentError, match="Runtime Agent operation failed"):
        _runtime_observation(response)

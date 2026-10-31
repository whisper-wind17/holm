"""TC-HA task-local coverage for bounded container-version discovery."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping

import pytest
from fixtures.fake_host_agent import (
    FX_HA_COMPOSE_VERSION,
    FX_HA_DOCKER_VERSION,
    FX_HA_TOOLKIT_VERSION,
    RawVersionQueryFixture,
)
from fixtures.fake_nvml import FixtureId, scenario_for
from holm_contracts.agent.v1 import agent_pb2
from holm_host_agent.host_info import (
    COMPOSE_VERSION_COMMAND,
    DOCKER_VERSION_COMMAND,
    HOST_VERSION_QUERY_TIMEOUT_SECONDS,
    TOOLKIT_VERSION_COMMAND,
    collect_docker_version,
    collect_toolkit_version,
    get_host_info,
)

RawOutcome = subprocess.CompletedProcess[str] | Exception


class RecordingRawCommandRunner:
    def __init__(self, outcomes: Mapping[tuple[str, ...], RawOutcome]) -> None:
        self._outcomes = outcomes
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def __call__(
        self,
        command: tuple[str, ...],
        *,
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((command, timeout_seconds))
        outcome = self._outcomes[command]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _result(
    command: tuple[str, ...],
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def _assert_unknown(observed: agent_pb2.ObservedString, reason: int) -> None:
    assert observed.state == agent_pb2.KNOWLEDGE_STATE_UNKNOWN
    assert not observed.HasField("value")
    assert observed.unknown_reason == reason
    assert observed.unknown_reason != agent_pb2.OBSERVATION_UNKNOWN_REASON_NOT_COLLECTED


def test_M1_07C_TC_HA_01_FX_HA_raw_outputs_are_parsed_by_production_code() -> None:
    fixtures: tuple[RawVersionQueryFixture, ...] = (
        FX_HA_DOCKER_VERSION,
        FX_HA_TOOLKIT_VERSION,
        FX_HA_COMPOSE_VERSION,
    )
    assert all(fixture.source for fixture in fixtures)
    assert {fixture.review_status for fixture in fixtures} == {"待真机复核"}

    runner = RecordingRawCommandRunner(
        {fixture.command: fixture.completed_process() for fixture in fixtures}
    )

    response = get_host_info(scenario_for(FixtureId.FX_NV_02).open(), runner)
    docker = response.docker_version
    toolkit = response.toolkit_version

    assert docker.state == agent_pb2.KNOWLEDGE_STATE_KNOWN
    assert docker.value == "27.3.1"
    assert docker.value != FX_HA_DOCKER_VERSION.stdout.strip()
    assert toolkit.state == agent_pb2.KNOWLEDGE_STATE_KNOWN
    assert toolkit.value == "1.16.2"
    assert toolkit.value != FX_HA_TOOLKIT_VERSION.stdout.strip()
    assert runner.calls == [
        (DOCKER_VERSION_COMMAND, HOST_VERSION_QUERY_TIMEOUT_SECONDS),
        (TOOLKIT_VERSION_COMMAND, HOST_VERSION_QUERY_TIMEOUT_SECONDS),
        (COMPOSE_VERSION_COMMAND, HOST_VERSION_QUERY_TIMEOUT_SECONDS),
    ]


@pytest.mark.parametrize(
    ("outcome", "reason"),
    (
        (
            FileNotFoundError("version command is absent"),
            agent_pb2.OBSERVATION_UNKNOWN_REASON_SOURCE_UNAVAILABLE,
        ),
        (
            PermissionError("version command is not executable"),
            agent_pb2.OBSERVATION_UNKNOWN_REASON_QUERY_FAILED,
        ),
        (
            _result(DOCKER_VERSION_COMMAND, stderr="permission denied", returncode=1),
            agent_pb2.OBSERVATION_UNKNOWN_REASON_QUERY_FAILED,
        ),
        (
            _result(DOCKER_VERSION_COMMAND, stdout="   \n"),
            agent_pb2.OBSERVATION_UNKNOWN_REASON_EMPTY_VALUE,
        ),
        (
            _result(DOCKER_VERSION_COMMAND, stdout="Docker build metadata only\n"),
            agent_pb2.OBSERVATION_UNKNOWN_REASON_EMPTY_VALUE,
        ),
    ),
)
def test_M1_07C_TC_HA_02_each_raw_docker_path_has_explicit_attribution(
    outcome: RawOutcome,
    reason: int,
) -> None:
    runner = RecordingRawCommandRunner({DOCKER_VERSION_COMMAND: outcome})

    observed = collect_docker_version(runner)

    _assert_unknown(observed, reason)
    assert runner.calls == [(DOCKER_VERSION_COMMAND, HOST_VERSION_QUERY_TIMEOUT_SECONDS)]


def test_M1_07C_TC_HA_03_timeout_is_query_failed_without_retry_or_real_wait() -> None:
    timeout = subprocess.TimeoutExpired(
        cmd=TOOLKIT_VERSION_COMMAND,
        timeout=HOST_VERSION_QUERY_TIMEOUT_SECONDS,
    )
    runner = RecordingRawCommandRunner({TOOLKIT_VERSION_COMMAND: timeout})

    observed = collect_toolkit_version(runner)

    _assert_unknown(observed, agent_pb2.OBSERVATION_UNKNOWN_REASON_QUERY_FAILED)
    assert runner.calls == [(TOOLKIT_VERSION_COMMAND, HOST_VERSION_QUERY_TIMEOUT_SECONDS)]


def test_M1_07C_TC_HA_04_one_query_failure_does_not_fail_GetHostInfo() -> None:
    runner = RecordingRawCommandRunner(
        {
            DOCKER_VERSION_COMMAND: RuntimeError("raw process boundary failed"),
            TOOLKIT_VERSION_COMMAND: FX_HA_TOOLKIT_VERSION.completed_process(),
            COMPOSE_VERSION_COMMAND: FX_HA_COMPOSE_VERSION.completed_process(),
        }
    )

    response = get_host_info(scenario_for(FixtureId.FX_NV_02).open(), runner)

    _assert_unknown(
        response.docker_version,
        agent_pb2.OBSERVATION_UNKNOWN_REASON_QUERY_FAILED,
    )
    assert response.toolkit_version.state == agent_pb2.KNOWLEDGE_STATE_KNOWN
    assert response.toolkit_version.value == "1.16.2"
    assert response.logical_cpu_count.state == agent_pb2.KNOWLEDGE_STATE_KNOWN
    assert response.total_memory_bytes.state == agent_pb2.KNOWLEDGE_STATE_KNOWN
    assert response.total_disk_bytes.state == agent_pb2.KNOWLEDGE_STATE_KNOWN
    assert response.available_disk_bytes.state == agent_pb2.KNOWLEDGE_STATE_KNOWN
    assert response.driver_version.state == agent_pb2.KNOWLEDGE_STATE_KNOWN
    assert response.cuda_driver_version.state == agent_pb2.KNOWLEDGE_STATE_KNOWN


def test_M1_07C_TC_HA_05_unknown_reason_paths_reject_NOT_COLLECTED_positive_control() -> None:
    translated = (
        collect_docker_version(
            RecordingRawCommandRunner({DOCKER_VERSION_COMMAND: FileNotFoundError("missing")})
        ),
        collect_docker_version(
            RecordingRawCommandRunner(
                {DOCKER_VERSION_COMMAND: _result(DOCKER_VERSION_COMMAND, returncode=1)}
            )
        ),
        collect_docker_version(
            RecordingRawCommandRunner({DOCKER_VERSION_COMMAND: _result(DOCKER_VERSION_COMMAND)})
        ),
    )
    assert {item.unknown_reason for item in translated} == {
        agent_pb2.OBSERVATION_UNKNOWN_REASON_SOURCE_UNAVAILABLE,
        agent_pb2.OBSERVATION_UNKNOWN_REASON_QUERY_FAILED,
        agent_pb2.OBSERVATION_UNKNOWN_REASON_EMPTY_VALUE,
    }

    with pytest.raises(AssertionError):
        _assert_unknown(
            agent_pb2.ObservedString(
                state=agent_pb2.KNOWLEDGE_STATE_UNKNOWN,
                unknown_reason=agent_pb2.OBSERVATION_UNKNOWN_REASON_NOT_COLLECTED,
            ),
            agent_pb2.OBSERVATION_UNKNOWN_REASON_SOURCE_UNAVAILABLE,
        )


def test_M1_07C_TC_HA_06_permission_cannot_become_known_or_empty_positive_control() -> None:
    observed = collect_toolkit_version(
        RecordingRawCommandRunner(
            {TOOLKIT_VERSION_COMMAND: PermissionError("not in the required group")}
        )
    )
    _assert_unknown(observed, agent_pb2.OBSERVATION_UNKNOWN_REASON_QUERY_FAILED)

    for invalid in (
        agent_pb2.ObservedString(state=agent_pb2.KNOWLEDGE_STATE_KNOWN, value="1.16.2"),
        agent_pb2.ObservedString(state=agent_pb2.KNOWLEDGE_STATE_KNOWN, value=""),
    ):
        with pytest.raises(AssertionError):
            _assert_unknown(invalid, agent_pb2.OBSERVATION_UNKNOWN_REASON_QUERY_FAILED)


def test_M1_07C_TC_HA_07_default_runner_hands_explicit_timeout_to_subprocess_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the real default runner, not a double, to the explicit query timeout.

    Every other timeout assertion here checks what ``_command_version`` hands to an
    injected runner, so the only production code that actually keeps ``GetHostInfo``
    from hanging forever -- the ``timeout`` argument ``_run_version_command`` gives
    ``subprocess.run`` -- sits below all of those doubles and would stay green if it
    were removed.  This case drives the untouched default runner and inspects what
    ``subprocess.run`` itself receives.
    """
    replies = {
        DOCKER_VERSION_COMMAND: FX_HA_DOCKER_VERSION.completed_process(),
        TOOLKIT_VERSION_COMMAND: FX_HA_TOOLKIT_VERSION.completed_process(),
        COMPOSE_VERSION_COMMAND: FX_HA_COMPOSE_VERSION.completed_process(),
    }
    invocations: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def recording_subprocess_run(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        invocations.append((command, kwargs))
        return replies[command]

    monkeypatch.setattr(subprocess, "run", recording_subprocess_run)

    response = get_host_info(scenario_for(FixtureId.FX_NV_02).open())

    assert isinstance(HOST_VERSION_QUERY_TIMEOUT_SECONDS, float)
    assert 0.0 < HOST_VERSION_QUERY_TIMEOUT_SECONDS < 60.0
    assert [command for command, _ in invocations] == [
        DOCKER_VERSION_COMMAND,
        TOOLKIT_VERSION_COMMAND,
        COMPOSE_VERSION_COMMAND,
    ]
    for command, kwargs in invocations:
        assert "timeout" in kwargs, f"{command} reached subprocess.run without a timeout"
        assert kwargs["timeout"] == HOST_VERSION_QUERY_TIMEOUT_SECONDS
    assert response.docker_version.value == "27.3.1"
    assert response.toolkit_version.value == "1.16.2"

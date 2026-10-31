"""FX-HA-SYS-04-COMPOSE-VERSION: raw command boundary coverage."""

from __future__ import annotations

import subprocess
from dataclasses import replace

import pytest
from fixtures.fake_host_agent import (
    FX_HA_COMPOSE_VERSION,
    FX_HA_COMPOSE_VERSION_PREFIXED,
    FX_HA_DOCKER_VERSION,
    FX_HA_TOOLKIT_VERSION,
)
from fixtures.fake_nvml import FixtureId, scenario_for
from holm_contracts.agent.v1 import agent_pb2
from holm_host_agent.host_info import (
    COMPOSE_VERSION_COMMAND,
    DOCKER_VERSION_COMMAND,
    HOST_VERSION_QUERY_TIMEOUT_SECONDS,
    TOOLKIT_VERSION_COMMAND,
    collect_compose_version,
    get_host_info,
)
from test_M1_07C_TC_HA_container_versions import (
    RawOutcome,
    RecordingRawCommandRunner,
    _assert_unknown,
    _result,
)


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        (FX_HA_COMPOSE_VERSION, "5.1.0"),
        (FX_HA_COMPOSE_VERSION_PREFIXED, "2.32.4"),
        (replace(FX_HA_COMPOSE_VERSION, stdout="2.32.4-desktop.1\n"), "2.32.4-desktop.1"),
    ],
)
def test_SYS_04_TC_HA_01_02_raw_compose_versions(fixture, expected):
    assert fixture.source
    assert fixture.command == COMPOSE_VERSION_COMMAND == ("docker", "compose", "version", "--short")
    runner = RecordingRawCommandRunner({fixture.command: fixture.completed_process()})
    observed = collect_compose_version(runner)
    assert observed.state == agent_pb2.KNOWLEDGE_STATE_KNOWN
    assert observed.value == expected
    assert observed.unknown_reason == agent_pb2.OBSERVATION_UNKNOWN_REASON_UNSPECIFIED
    assert runner.calls == [(COMPOSE_VERSION_COMMAND, HOST_VERSION_QUERY_TIMEOUT_SECONDS)]


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (FileNotFoundError("missing"), agent_pb2.OBSERVATION_UNKNOWN_REASON_SOURCE_UNAVAILABLE),
        (PermissionError("denied"), agent_pb2.OBSERVATION_UNKNOWN_REASON_QUERY_FAILED),
        (RuntimeError("boundary failed"), agent_pb2.OBSERVATION_UNKNOWN_REASON_QUERY_FAILED),
        (
            _result(COMPOSE_VERSION_COMMAND, returncode=1),
            agent_pb2.OBSERVATION_UNKNOWN_REASON_QUERY_FAILED,
        ),
        (
            _result(COMPOSE_VERSION_COMMAND, stdout=" \n"),
            agent_pb2.OBSERVATION_UNKNOWN_REASON_EMPTY_VALUE,
        ),
        (
            _result(COMPOSE_VERSION_COMMAND, stdout="unparseable"),
            agent_pb2.OBSERVATION_UNKNOWN_REASON_EMPTY_VALUE,
        ),
        (
            subprocess.TimeoutExpired(COMPOSE_VERSION_COMMAND, HOST_VERSION_QUERY_TIMEOUT_SECONDS),
            agent_pb2.OBSERVATION_UNKNOWN_REASON_QUERY_FAILED,
        ),
    ],
)
def test_SYS_04_TC_HA_02_03_failure_attribution_without_retry(
    outcome: RawOutcome, reason: int
) -> None:
    runner = RecordingRawCommandRunner({COMPOSE_VERSION_COMMAND: outcome})
    _assert_unknown(collect_compose_version(runner), reason)
    assert runner.calls == [(COMPOSE_VERSION_COMMAND, HOST_VERSION_QUERY_TIMEOUT_SECONDS)]


def test_SYS_04_TC_HA_04_compose_failure_keeps_other_host_fields() -> None:
    runner = RecordingRawCommandRunner(
        {
            DOCKER_VERSION_COMMAND: FX_HA_DOCKER_VERSION.completed_process(),
            TOOLKIT_VERSION_COMMAND: FX_HA_TOOLKIT_VERSION.completed_process(),
            COMPOSE_VERSION_COMMAND: FileNotFoundError("missing"),
        }
    )
    response = get_host_info(scenario_for(FixtureId.FX_NV_02).open(), runner)
    _assert_unknown(
        response.compose_version, agent_pb2.OBSERVATION_UNKNOWN_REASON_SOURCE_UNAVAILABLE
    )
    assert response.docker_version.value == "27.3.1"
    assert response.toolkit_version.value == "1.16.2"
    for field in (
        "logical_cpu_count",
        "total_memory_bytes",
        "total_disk_bytes",
        "available_disk_bytes",
        "driver_version",
        "docker_version",
        "toolkit_version",
        "cuda_driver_version",
    ):
        assert getattr(response, field).state == agent_pb2.KNOWLEDGE_STATE_KNOWN
    assert runner.calls == [
        (DOCKER_VERSION_COMMAND, HOST_VERSION_QUERY_TIMEOUT_SECONDS),
        (TOOLKIT_VERSION_COMMAND, HOST_VERSION_QUERY_TIMEOUT_SECONDS),
        (COMPOSE_VERSION_COMMAND, HOST_VERSION_QUERY_TIMEOUT_SECONDS),
    ]


def test_SYS_04_TC_HA_01_compose_proto_field_round_trip() -> None:
    descriptor = agent_pb2.GetHostInfoResponse.DESCRIPTOR.fields_by_name["compose_version"]
    assert descriptor.number == 9
    assert descriptor.message_type.full_name == agent_pb2.ObservedString.DESCRIPTOR.full_name
    response = agent_pb2.GetHostInfoResponse(
        compose_version=agent_pb2.ObservedString(
            state=agent_pb2.KNOWLEDGE_STATE_KNOWN, value="5.1.0"
        )
    )
    assert agent_pb2.GetHostInfoResponse.FromString(response.SerializeToString()) == response

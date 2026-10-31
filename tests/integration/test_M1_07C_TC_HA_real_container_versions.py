"""TC-HA real negative path for the fixed M1-07C execution environment."""

from holm_contracts.agent.v1 import agent_pb2
from holm_host_agent.host_info import collect_docker_version, collect_toolkit_version


def test_M1_07C_TC_HA_07_real_absent_commands_are_SOURCE_UNAVAILABLE_without_stub() -> None:
    docker = collect_docker_version()
    toolkit = collect_toolkit_version()

    for observed in (docker, toolkit):
        assert observed.state == agent_pb2.KNOWLEDGE_STATE_UNKNOWN
        assert not observed.HasField("value")
        assert observed.unknown_reason == agent_pb2.OBSERVATION_UNKNOWN_REASON_SOURCE_UNAVAILABLE
        assert observed.unknown_reason != agent_pb2.OBSERVATION_UNKNOWN_REASON_NOT_COLLECTED

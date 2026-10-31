"""FX-M2-07C-AGENT: real gRPC/UDS contract without Host Agent privileges."""

from __future__ import annotations

import asyncio
from pathlib import Path

import grpc
import pytest
from holm_contracts.agent.v1 import agent_pb2, agent_pb2_grpc
from holm_download_worker.agent_client import GrpcPathValidator
from holm_persistence.job_executor import JobFailure


class PathAgent(agent_pb2_grpc.HostAgentServicer):
    def __init__(self, mode: str) -> None:
        self.mode = mode

    async def ValidatePath(
        self, request: agent_pb2.ValidatePathRequest, context: grpc.aio.ServicerContext
    ) -> agent_pb2.ValidatePathResponse:
        assert request.path == "/external/path-sentinel"
        if self.mode == "denied":
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "path-sentinel must not escape")
        if self.mode == "invalid":
            return agent_pb2.ValidatePathResponse()
        if self.mode == "unknown":
            value = agent_pb2.ObservedRuntimeReadability(
                state=agent_pb2.KNOWLEDGE_STATE_UNKNOWN,
                unknown_reason=agent_pb2.RUNTIME_READABILITY_UNKNOWN_REASON_RUNTIME_IDENTITY_UNAVAILABLE,
            )
        else:
            value = agent_pb2.ObservedRuntimeReadability(
                state=agent_pb2.KNOWLEDGE_STATE_KNOWN,
                value=agent_pb2.RUNTIME_READABILITY_READABLE
                if self.mode == "readable"
                else agent_pb2.RUNTIME_READABILITY_NOT_READABLE,
            )
        return agent_pb2.ValidatePathResponse(runtime_readability=value)


@pytest.mark.parametrize(
    "mode,expected",
    [("readable", True), ("not_readable", False), ("unknown", None), ("denied", None)],
)
def test_M2_07C_validate_path_three_states(
    tmp_path: Path, mode: str, expected: bool | None
) -> None:
    async def run() -> None:
        socket = tmp_path / "agent.sock"
        server = grpc.aio.server()
        agent_pb2_grpc.add_HostAgentServicer_to_server(PathAgent(mode), server)
        server.add_insecure_port(f"unix:{socket}")
        await server.start()
        try:
            assert await GrpcPathValidator(socket).readable("/external/path-sentinel") is expected
        finally:
            await server.stop(0)

    asyncio.run(run())


def test_M2_07C_validate_path_connection_failure_is_retryable(tmp_path: Path) -> None:
    async def run() -> None:
        with pytest.raises(JobFailure) as exc:
            await GrpcPathValidator(tmp_path / "absent.sock", timeout_seconds=0.1).readable(
                "/external/path-sentinel"
            )
        assert exc.value.code == "external_path_agent_unavailable"
        assert "sentinel" not in str(exc.value)

    asyncio.run(run())


def test_M2_07C_validate_path_malformed_response_fails_loudly(tmp_path: Path) -> None:
    async def run() -> None:
        socket = tmp_path / "invalid.sock"
        server = grpc.aio.server()
        agent_pb2_grpc.add_HostAgentServicer_to_server(PathAgent("invalid"), server)
        server.add_insecure_port(f"unix:{socket}")
        await server.start()
        try:
            with pytest.raises(JobFailure) as exc:
                await GrpcPathValidator(socket).readable("/external/path-sentinel")
            assert exc.value.code == "external_path_agent_invalid_response"
        finally:
            await server.stop(0)

    asyncio.run(run())

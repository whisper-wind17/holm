"""Bounded, path-redacting ValidatePath client for External registration only."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import grpc
from holm_contracts.agent.v1 import agent_pb2, agent_pb2_grpc
from holm_persistence.job_executor import JobFailure


class PathValidator(Protocol):
    async def readable(self, path: str) -> bool | None:
        """True/False are confirmed; None is explicitly unverified."""
        ...


class GrpcPathValidator:
    def __init__(self, socket_path: Path, *, timeout_seconds: float = 5.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Agent timeout must be positive")
        self._socket = socket_path
        self._timeout = timeout_seconds

    async def readable(self, path: str) -> bool | None:
        async with grpc.aio.insecure_channel(
            f"unix:{self._socket}",
            options=(("grpc.use_local_subchannel_pool", 1),),
        ) as channel:
            try:
                response = await agent_pb2_grpc.HostAgentStub(channel).ValidatePath(
                    agent_pb2.ValidatePathRequest(path=path),
                    timeout=self._timeout,
                )
            except grpc.aio.AioRpcError as error:
                if error.code() is grpc.StatusCode.PERMISSION_DENIED:
                    return None
                raise JobFailure("external_path_agent_unavailable") from None
            except TimeoutError:
                raise JobFailure("external_path_agent_unavailable") from None
        value = response.runtime_readability
        if value.state == agent_pb2.KNOWLEDGE_STATE_UNKNOWN and not value.HasField("value"):
            return None
        if value.state == agent_pb2.KNOWLEDGE_STATE_KNOWN and value.HasField("value"):
            if value.value == agent_pb2.RUNTIME_READABILITY_READABLE:
                return True
            if value.value == agent_pb2.RUNTIME_READABILITY_NOT_READABLE:
                return False
        raise JobFailure("external_path_agent_invalid_response")

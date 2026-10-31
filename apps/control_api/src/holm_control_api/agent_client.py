"""Bounded, reconnecting read-only Host Agent client for Control API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

import grpc
from holm_contracts.agent.v1 import agent_pb2, agent_pb2_grpc

DEFAULT_AGENT_SOCKET_PATH: Final[Path] = Path("/run/holm/agent.sock")
DEFAULT_AGENT_TIMEOUT_SECONDS: Final[float] = 2.0


class AgentUnavailableReason(StrEnum):
    """Sanitized connection outcomes safe for the public management API."""

    CONNECTION_FAILED = "connection_failed"
    TIMEOUT = "timeout"
    ACCESS_DENIED = "access_denied"
    RPC_FAILED = "rpc_failed"


@dataclass(frozen=True, slots=True)
class AgentSnapshot:
    host_info: agent_pb2.GetHostInfoResponse
    gpu_inventory: agent_pb2.ListGPUsResponse


@dataclass(frozen=True, slots=True)
class AgentUnavailable:
    reason: AgentUnavailableReason


type AgentObservation = AgentSnapshot | AgentUnavailable


@dataclass(frozen=True, slots=True)
class AgentLogEntry:
    sequence: int
    text: str
    truncated: bool
    original_character_count: int


@dataclass(frozen=True, slots=True)
class AgentLogPage:
    entries: tuple[AgentLogEntry, ...]
    next_cursor_offset: int
    has_more: bool


class AgentLogUnavailableReason(StrEnum):
    CONNECTION_FAILED = "connection_failed"
    TIMEOUT = "timeout"
    ACCESS_DENIED = "access_denied"
    REQUEST_REJECTED = "request_rejected"
    RPC_FAILED = "rpc_failed"


@dataclass(frozen=True, slots=True)
class AgentLogUnavailable:
    reason: AgentLogUnavailableReason


type AgentLogObservation = AgentLogPage | AgentLogUnavailable


class HostAgentClient(Protocol):
    async def observe(self) -> AgentObservation:
        """Read host and GPU facts or return one sanitized unavailability state."""


class HostAgentLogClient(Protocol):
    async def read_logs(
        self,
        instance_id: str,
        *,
        cursor_offset: int,
        page_size: int,
    ) -> AgentLogObservation:
        """Read one bounded Runtime log page or a sanitized failure."""


class GrpcHostAgentClient:
    """Create a fresh bounded channel per observation so Agent restarts recover."""

    __slots__ = ("_socket_path", "_timeout_seconds")

    def __init__(
        self,
        socket_path: Path = DEFAULT_AGENT_SOCKET_PATH,
        *,
        timeout_seconds: float = DEFAULT_AGENT_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Agent timeout must be greater than zero")
        self._socket_path = socket_path
        self._timeout_seconds = timeout_seconds

    async def observe(self) -> AgentObservation:
        channel = grpc.aio.insecure_channel(
            f"unix:{self._socket_path}",
            options=(("grpc.use_local_subchannel_pool", 1),),
        )
        try:
            stub = agent_pb2_grpc.HostAgentStub(channel)
            async with asyncio.timeout(self._timeout_seconds):
                host_info, gpu_inventory = await asyncio.gather(
                    stub.GetHostInfo(
                        agent_pb2.GetHostInfoRequest(),
                        timeout=self._timeout_seconds,
                        wait_for_ready=True,
                    ),
                    stub.ListGPUs(
                        agent_pb2.ListGPUsRequest(),
                        timeout=self._timeout_seconds,
                        wait_for_ready=True,
                    ),
                )
            return AgentSnapshot(host_info=host_info, gpu_inventory=gpu_inventory)
        except TimeoutError:
            return AgentUnavailable(AgentUnavailableReason.TIMEOUT)
        except grpc.aio.AioRpcError as error:
            if error.code() is grpc.StatusCode.PERMISSION_DENIED:
                reason = AgentUnavailableReason.ACCESS_DENIED
            elif error.code() in {
                grpc.StatusCode.UNAVAILABLE,
                grpc.StatusCode.DEADLINE_EXCEEDED,
            }:
                reason = AgentUnavailableReason.CONNECTION_FAILED
            else:
                reason = AgentUnavailableReason.RPC_FAILED
            return AgentUnavailable(reason)
        finally:
            await channel.close()

    async def read_logs(
        self,
        instance_id: str,
        *,
        cursor_offset: int,
        page_size: int,
    ) -> AgentLogObservation:
        channel = grpc.aio.insecure_channel(
            f"unix:{self._socket_path}",
            options=(("grpc.use_local_subchannel_pool", 1),),
        )
        try:
            stub = agent_pb2_grpc.HostAgentStub(channel)
            responses: list[agent_pb2.StreamLogsResponse] = []
            async with asyncio.timeout(self._timeout_seconds):
                async for response in stub.StreamLogs(
                    agent_pb2.StreamLogsRequest(
                        instance_id=instance_id,
                        cursor_offset=cursor_offset,
                        page_size=page_size,
                    ),
                    timeout=self._timeout_seconds,
                    wait_for_ready=True,
                ):
                    responses.append(response)
                    if len(responses) > 1:
                        return AgentLogUnavailable(AgentLogUnavailableReason.RPC_FAILED)
            if len(responses) != 1:
                return AgentLogUnavailable(AgentLogUnavailableReason.RPC_FAILED)
            response = responses[0]
            return AgentLogPage(
                entries=tuple(
                    AgentLogEntry(
                        sequence=entry.sequence,
                        text=entry.text,
                        truncated=entry.truncated,
                        original_character_count=entry.original_character_count,
                    )
                    for entry in response.entries
                ),
                next_cursor_offset=response.next_cursor_offset,
                has_more=response.has_more,
            )
        except TimeoutError:
            return AgentLogUnavailable(AgentLogUnavailableReason.TIMEOUT)
        except grpc.aio.AioRpcError as error:
            if error.code() is grpc.StatusCode.PERMISSION_DENIED:
                reason = AgentLogUnavailableReason.ACCESS_DENIED
            elif error.code() is grpc.StatusCode.INVALID_ARGUMENT:
                reason = AgentLogUnavailableReason.REQUEST_REJECTED
            elif error.code() in {
                grpc.StatusCode.UNAVAILABLE,
                grpc.StatusCode.DEADLINE_EXCEEDED,
            }:
                reason = AgentLogUnavailableReason.CONNECTION_FAILED
            else:
                reason = AgentLogUnavailableReason.RPC_FAILED
            return AgentLogUnavailable(reason)
        finally:
            await channel.close()


__all__ = [
    "DEFAULT_AGENT_SOCKET_PATH",
    "DEFAULT_AGENT_TIMEOUT_SECONDS",
    "AgentObservation",
    "AgentLogEntry",
    "AgentLogObservation",
    "AgentLogPage",
    "AgentLogUnavailable",
    "AgentLogUnavailableReason",
    "AgentSnapshot",
    "AgentUnavailable",
    "AgentUnavailableReason",
    "GrpcHostAgentClient",
    "HostAgentClient",
    "HostAgentLogClient",
]

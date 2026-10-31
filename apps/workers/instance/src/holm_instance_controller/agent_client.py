"""Typed, bounded Host Agent Runtime RPC client."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol
from uuid import UUID

import grpc
from holm_contracts.agent.v1 import agent_pb2, agent_pb2_grpc
from holm_domain.instance_cleanup import CleanupFailure

DEFAULT_RPC_TIMEOUT_SECONDS: Final[float] = 75.0


class RuntimeProcessState(StrEnum):
    RUNNING = "running"
    EXITED = "exited"
    NOT_FOUND = "not_found"


class ObservationKnowledgeState(StrEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"


class GpuObservationUnknownReason(StrEnum):
    NOT_COLLECTED = "not_collected"
    SOURCE_UNAVAILABLE = "source_unavailable"
    EMPTY_VALUE = "empty_value"
    QUERY_FAILED = "query_failed"


_PROTO_STATES: Final[dict[int, RuntimeProcessState]] = {
    agent_pb2.RUNTIME_PROCESS_STATE_RUNNING: RuntimeProcessState.RUNNING,
    agent_pb2.RUNTIME_PROCESS_STATE_EXITED: RuntimeProcessState.EXITED,
    agent_pb2.RUNTIME_PROCESS_STATE_NOT_FOUND: RuntimeProcessState.NOT_FOUND,
}
_PROTO_GPU_UNKNOWN_REASONS: Final[dict[int, GpuObservationUnknownReason]] = {
    agent_pb2.OBSERVATION_UNKNOWN_REASON_NOT_COLLECTED: (GpuObservationUnknownReason.NOT_COLLECTED),
    agent_pb2.OBSERVATION_UNKNOWN_REASON_SOURCE_UNAVAILABLE: (
        GpuObservationUnknownReason.SOURCE_UNAVAILABLE
    ),
    agent_pb2.OBSERVATION_UNKNOWN_REASON_EMPTY_VALUE: GpuObservationUnknownReason.EMPTY_VALUE,
    agent_pb2.OBSERVATION_UNKNOWN_REASON_QUERY_FAILED: GpuObservationUnknownReason.QUERY_FAILED,
}


@dataclass(frozen=True, slots=True)
class ObservedGpuUuids:
    state: ObservationKnowledgeState
    values: tuple[str, ...] = ()
    unknown_reason: GpuObservationUnknownReason | None = None

    def __post_init__(self) -> None:
        if self.state is ObservationKnowledgeState.KNOWN:
            if self.unknown_reason is not None or len(set(self.values)) != len(self.values):
                raise ValueError("invalid known GPU observation")
        elif self.values or self.unknown_reason is None:
            raise ValueError("invalid unknown GPU observation")


_GPU_NOT_COLLECTED: Final[ObservedGpuUuids] = ObservedGpuUuids(
    ObservationKnowledgeState.UNKNOWN,
    unknown_reason=GpuObservationUnknownReason.NOT_COLLECTED,
)


@dataclass(frozen=True, slots=True)
class RuntimeLaunchRequest:
    instance_id: str
    runtime_name: str
    model_path: str
    gpu_uuids: tuple[str, ...]
    tensor_parallel_size: int
    context_length: int
    gpu_memory_utilization: float
    served_model_name: str
    config_version: str


@dataclass(frozen=True, slots=True)
class RuntimeObservation:
    state: RuntimeProcessState
    endpoint: str = ""
    gpu_uuids: ObservedGpuUuids = _GPU_NOT_COLLECTED
    exit_code: int | None = None
    oom_killed: bool | None = None
    started_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RuntimeSummary:
    instance_id: UUID
    state: RuntimeProcessState
    gpu_uuids: ObservedGpuUuids


class RuntimeAgentError(RuntimeError):
    """A sanitized Agent failure that never carries RPC details."""

    def __init__(self, code: str = "runtime_agent_unavailable") -> None:
        self.code = code
        super().__init__("Runtime Agent operation failed")


class RuntimeAgentClient(Protocol):
    async def remove(self, instance_id: str) -> None:
        """Remove the stopped owned Runtime and its instance cache."""

    async def ensure(self, request: RuntimeLaunchRequest) -> RuntimeObservation:
        """Ensure the exact typed Runtime launch specification."""

    async def stop(self, instance_id: str, *, grace_seconds: int) -> RuntimeObservation:
        """Stop one Runtime process."""

    async def inspect(self, instance_id: str) -> RuntimeObservation:
        """Inspect one Runtime process."""

    async def list(self) -> tuple[RuntimeSummary, ...]:
        """List every Runtime owned by the Agent state root."""

    async def list_gpus(self) -> frozenset[str]:
        """List the stable UUIDs in one complete GPU inventory snapshot."""


def _state(value: int) -> RuntimeProcessState:
    try:
        return _PROTO_STATES[value]
    except KeyError:
        raise RuntimeAgentError("runtime_agent_invalid_response") from None


def _gpu_uuids(value: agent_pb2.ObservedGpuUuids) -> ObservedGpuUuids:
    if value.state == agent_pb2.KNOWLEDGE_STATE_KNOWN:
        values = tuple(value.values)
        if len(set(values)) != len(values) or any(
            not item.startswith("GPU-") or any(character.isspace() for character in item)
            for item in values
        ):
            raise RuntimeAgentError("runtime_agent_invalid_response")
        return ObservedGpuUuids(ObservationKnowledgeState.KNOWN, values=values)
    if value.state == agent_pb2.KNOWLEDGE_STATE_UNKNOWN and not value.values:
        try:
            reason = _PROTO_GPU_UNKNOWN_REASONS[value.unknown_reason]
        except KeyError:
            raise RuntimeAgentError("runtime_agent_invalid_response") from None
        return ObservedGpuUuids(
            ObservationKnowledgeState.UNKNOWN,
            unknown_reason=reason,
        )
    raise RuntimeAgentError("runtime_agent_invalid_response")


def _inventory_gpu_uuids(value: agent_pb2.ListGPUsResponse) -> frozenset[str]:
    if value.discovery_state == agent_pb2.GPU_DISCOVERY_STATE_NO_DEVICES:
        if value.devices:
            raise RuntimeAgentError("runtime_agent_invalid_response")
        return frozenset()
    if value.discovery_state != agent_pb2.GPU_DISCOVERY_STATE_READY or not value.devices:
        raise RuntimeAgentError("runtime_agent_gpu_inventory_unavailable")
    uuids = tuple(device.uuid for device in value.devices)
    if len(set(uuids)) != len(uuids) or any(
        not item.startswith("GPU-") or any(character.isspace() for character in item)
        for item in uuids
    ):
        raise RuntimeAgentError("runtime_agent_invalid_response")
    return frozenset(uuids)


def _runtime_observation(value: agent_pb2.InspectRuntimeResponse) -> RuntimeObservation:
    state = _state(value.status)
    exit_code = value.exit_code if value.HasField("exit_code") else None
    oom_killed = value.oom_killed if value.HasField("oom_killed") else None
    if state is not RuntimeProcessState.EXITED and (
        exit_code is not None or oom_killed is not None
    ):
        raise RuntimeAgentError("runtime_agent_invalid_response")
    if oom_killed is True and (exit_code is None or exit_code <= 0):
        raise RuntimeAgentError("runtime_agent_invalid_response")
    started_at = None
    if value.started_at:
        try:
            started_at = datetime.fromisoformat(value.started_at)
            if started_at.tzinfo is None:
                raise ValueError("missing timezone")
            started_at = started_at.astimezone(UTC)
        except (ValueError, OverflowError):
            raise RuntimeAgentError("runtime_agent_invalid_response") from None
    return RuntimeObservation(
        state=state,
        endpoint=value.endpoint,
        gpu_uuids=_gpu_uuids(value.gpu_uuids),
        exit_code=exit_code,
        oom_killed=oom_killed,
        started_at=started_at,
    )


class GrpcRuntimeAgentClient:
    """Use a fresh UDS channel for each bounded call so Agent restarts recover."""

    __slots__ = ("_socket_path", "_timeout_seconds")

    def __init__(
        self,
        socket_path: Path,
        *,
        timeout_seconds: float = DEFAULT_RPC_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Agent timeout must be positive")
        self._socket_path = socket_path
        self._timeout_seconds = timeout_seconds

    def _channel(self) -> grpc.aio.Channel:
        return grpc.aio.insecure_channel(
            f"unix:{self._socket_path}",
            options=(("grpc.use_local_subchannel_pool", 1),),
        )

    async def remove(self, instance_id: str) -> None:
        channel = self._channel()
        try:
            response = await agent_pb2_grpc.HostAgentStub(channel).RemoveRuntime(
                agent_pb2.RemoveRuntimeRequest(instance_id=instance_id),
                timeout=self._timeout_seconds,
                wait_for_ready=True,
            )
            if not isinstance(response, agent_pb2.RemoveRuntimeResponse):
                raise RuntimeAgentError(CleanupFailure.INVALID_RESPONSE.value)
        except grpc.aio.AioRpcError as error:
            detail = error.details()
            allowed = {failure.value for failure in CleanupFailure}
            raise RuntimeAgentError(
                detail if detail in allowed else CleanupFailure.AGENT_UNAVAILABLE.value
            ) from None
        except TimeoutError:
            raise RuntimeAgentError() from None
        finally:
            await channel.close()

    async def ensure(self, request: RuntimeLaunchRequest) -> RuntimeObservation:
        channel = self._channel()
        try:
            response = await agent_pb2_grpc.HostAgentStub(channel).EnsureRuntime(
                agent_pb2.EnsureRuntimeRequest(
                    instance_id=request.instance_id,
                    runtime_name=request.runtime_name,
                    model_path=request.model_path,
                    gpu_uuids=request.gpu_uuids,
                    tensor_parallel_size=request.tensor_parallel_size,
                    context_length=request.context_length,
                    gpu_memory_utilization=request.gpu_memory_utilization,
                    served_model_name=request.served_model_name,
                    config_version=request.config_version,
                ),
                timeout=self._timeout_seconds,
                wait_for_ready=True,
            )
            return RuntimeObservation(state=_state(response.status), endpoint=response.endpoint)
        except (grpc.aio.AioRpcError, TimeoutError):
            raise RuntimeAgentError() from None
        finally:
            await channel.close()

    async def stop(self, instance_id: str, *, grace_seconds: int) -> RuntimeObservation:
        channel = self._channel()
        try:
            response = await agent_pb2_grpc.HostAgentStub(channel).StopRuntime(
                agent_pb2.StopRuntimeRequest(
                    instance_id=instance_id,
                    grace_seconds=grace_seconds,
                ),
                timeout=self._timeout_seconds,
                wait_for_ready=True,
            )
            return RuntimeObservation(state=_state(response.status))
        except (grpc.aio.AioRpcError, TimeoutError):
            raise RuntimeAgentError() from None
        finally:
            await channel.close()

    async def inspect(self, instance_id: str) -> RuntimeObservation:
        channel = self._channel()
        try:
            response = await agent_pb2_grpc.HostAgentStub(channel).InspectRuntime(
                agent_pb2.InspectRuntimeRequest(instance_id=instance_id),
                timeout=self._timeout_seconds,
                wait_for_ready=True,
            )
            return _runtime_observation(response)
        except (grpc.aio.AioRpcError, TimeoutError):
            raise RuntimeAgentError() from None
        finally:
            await channel.close()

    async def list(self) -> tuple[RuntimeSummary, ...]:
        channel = self._channel()
        try:
            response = await agent_pb2_grpc.HostAgentStub(channel).ListRuntimes(
                agent_pb2.ListRuntimesRequest(),
                timeout=self._timeout_seconds,
                wait_for_ready=True,
            )
            summaries: list[RuntimeSummary] = []
            seen: set[UUID] = set()
            for runtime in response.runtimes:
                try:
                    instance_id = UUID(runtime.instance_id)
                except ValueError:
                    raise RuntimeAgentError("runtime_agent_invalid_response") from None
                if str(instance_id) != runtime.instance_id or instance_id in seen:
                    raise RuntimeAgentError("runtime_agent_invalid_response")
                seen.add(instance_id)
                summaries.append(
                    RuntimeSummary(
                        instance_id=instance_id,
                        state=_state(runtime.status),
                        gpu_uuids=_gpu_uuids(runtime.gpu_uuids),
                    )
                )
            return tuple(summaries)
        except (grpc.aio.AioRpcError, TimeoutError):
            raise RuntimeAgentError() from None
        finally:
            await channel.close()

    async def list_gpus(self) -> frozenset[str]:
        channel = self._channel()
        try:
            response = await agent_pb2_grpc.HostAgentStub(channel).ListGPUs(
                agent_pb2.ListGPUsRequest(),
                timeout=self._timeout_seconds,
                wait_for_ready=True,
            )
            return _inventory_gpu_uuids(response)
        except (grpc.aio.AioRpcError, TimeoutError):
            raise RuntimeAgentError() from None
        finally:
            await channel.close()


__all__ = [
    "DEFAULT_RPC_TIMEOUT_SECONDS",
    "GrpcRuntimeAgentClient",
    "GpuObservationUnknownReason",
    "ObservationKnowledgeState",
    "ObservedGpuUuids",
    "RuntimeAgentClient",
    "RuntimeAgentError",
    "RuntimeLaunchRequest",
    "RuntimeObservation",
    "RuntimeProcessState",
    "RuntimeSummary",
]

"""Real-UDS tests for M1-07A's static TC-DP-07 Host Agent slice."""

from __future__ import annotations

import asyncio
import os
import socket
import stat
from pathlib import Path
from typing import Any

import grpc
import pytest
from fixtures.fake_nvml import FakeNvml, FixtureId, scenario_for
from google.protobuf import message_factory
from holm_contracts.agent.v1 import agent_pb2
from holm_host_agent import server as server_module
from holm_host_agent.authorization import IdentityCatalog
from holm_host_agent.gpu_discovery import NvmlErrorKind
from holm_host_agent.host_info import get_host_info
from holm_host_agent.server import (
    AGENT_SOCKET_MODE,
    AGENT_SOCKET_PATH,
    AgentAlreadyRunningError,
    HostAgentServer,
    _set_socket_permissions,
)


def _uds_channel(socket_path: Path) -> grpc.aio.Channel:
    return grpc.aio.insecure_channel(
        f"unix:{socket_path}",
        options=(("grpc.use_local_subchannel_pool", 1),),
    )


def _assert_observed_uint64(observed: Any) -> None:
    assert observed.state in {
        agent_pb2.KNOWLEDGE_STATE_UNKNOWN,
        agent_pb2.KNOWLEDGE_STATE_KNOWN,
    }
    if observed.state == agent_pb2.KNOWLEDGE_STATE_UNKNOWN:
        assert not observed.HasField("value")
    else:
        assert observed.HasField("value")
        assert observed.value >= 0


def _assert_observed_string(observed: Any) -> None:
    assert observed.state in {
        agent_pb2.KNOWLEDGE_STATE_UNKNOWN,
        agent_pb2.KNOWLEDGE_STATE_KNOWN,
    }
    if observed.state == agent_pb2.KNOWLEDGE_STATE_UNKNOWN:
        assert not observed.HasField("value")
    else:
        assert observed.HasField("value")
        assert observed.value != ""


def _assert_explicit_unimplemented(error: object) -> None:
    assert isinstance(error, grpc.aio.AioRpcError)
    assert error.code() == grpc.StatusCode.UNIMPLEMENTED
    assert error.details() == "RPC is reserved but not implemented"


def test_M1_07A_TC_DP_07_real_uds_host_info_and_explicit_unimplemented(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        socket_path = tmp_path / "agent.sock"
        peer_uid = [21_001]
        monkeypatch.setattr(server_module, "_read_peer_uid", lambda _socket: peer_uid[0])
        server = HostAgentServer(
            socket_path,
            identity_catalog=IdentityCatalog.from_pairs(
                (
                    (21_001, "download-worker"),
                    (21_002, "telemetry-worker"),
                    (21_003, "instance-controller"),
                )
            ),
            runtime_state_root=tmp_path / "runtimes",
            nvml_factory=lambda: scenario_for(FixtureId.FX_NV_02).open(),
        )
        original_umask = os.umask(0o000)
        try:
            await server.start()
        finally:
            os.umask(original_umask)

        try:
            assert stat.S_IMODE(socket_path.stat().st_mode) == AGENT_SOCKET_MODE
            service = agent_pb2.DESCRIPTOR.services_by_name["HostAgent"]
            peer_uid_by_method = {
                "GetHostInfo": 21_001,
                "ListGPUs": 21_003,
                "EnsureRuntime": 21_003,
                "StopRuntime": 21_003,
                "RemoveRuntime": 21_003,
                "InspectRuntime": 21_003,
                "ListRuntimes": 21_003,
                "StreamLogs": 21_003,
                "ValidatePath": 21_001,
            }
            for method in service.methods:
                peer_uid[0] = peer_uid_by_method[method.name]
                async with _uds_channel(socket_path) as channel:
                    await asyncio.wait_for(channel.channel_ready(), timeout=2)
                    request_type = message_factory.GetMessageClass(method.input_type)
                    output_type = message_factory.GetMessageClass(method.output_type)
                    if method.name == "GetHostInfo":
                        get_host_info = channel.unary_unary(
                            f"/{service.full_name}/GetHostInfo",
                            request_serializer=request_type.SerializeToString,
                            response_deserializer=output_type.FromString,
                        )
                        response = await get_host_info(request_type(), timeout=2)
                        for observed in (
                            response.logical_cpu_count,
                            response.total_memory_bytes,
                            response.total_disk_bytes,
                            response.available_disk_bytes,
                        ):
                            _assert_observed_uint64(observed)
                            assert observed.state == agent_pb2.KNOWLEDGE_STATE_KNOWN
                        for observed in (
                            response.docker_version,
                            response.toolkit_version,
                        ):
                            _assert_observed_string(observed)
                            assert observed.state == agent_pb2.KNOWLEDGE_STATE_UNKNOWN
                        _assert_observed_string(response.driver_version)
                        assert response.driver_version.state == agent_pb2.KNOWLEDGE_STATE_KNOWN
                        assert response.driver_version.value == "fixture-driver-1"
                        _assert_observed_string(response.cuda_driver_version)
                        assert response.cuda_driver_version.state == agent_pb2.KNOWLEDGE_STATE_KNOWN
                        assert response.cuda_driver_version.value == "12.8"
                        continue

                    if method.name == "ListGPUs":
                        list_gpus = channel.unary_unary(
                            f"/{service.full_name}/ListGPUs",
                            request_serializer=request_type.SerializeToString,
                            response_deserializer=output_type.FromString,
                        )
                        response = await list_gpus(request_type(), timeout=2)
                        assert response.discovery_state == agent_pb2.GPU_DISCOVERY_STATE_READY
                        assert len(response.devices) == 1
                        assert response.devices[0].uuid.startswith("GPU-")
                        continue

                    if method.name == "ValidatePath":
                        # M2-07B implemented it.  It stays inside this loop so
                        # something still fails if it silently reverts to the
                        # reserved placeholder; deleting the case would leave
                        # nothing watching that.
                        validate_path = channel.unary_unary(
                            f"/{service.full_name}/ValidatePath",
                            request_serializer=request_type.SerializeToString,
                            response_deserializer=output_type.FromString,
                        )
                        response = await validate_path(
                            request_type(path="/nonexistent-external-root/model"),
                            timeout=2,
                        )
                        # No External Root is configured on this server, so the
                        # honest answer is a known refusal, not an unknown.
                        assert response.runtime_readability.state == agent_pb2.KNOWLEDGE_STATE_KNOWN
                        assert (
                            response.runtime_readability.value
                            == agent_pb2.RUNTIME_READABILITY_NOT_READABLE
                        )
                        continue

                    if method.name in {
                        "EnsureRuntime",
                        "StopRuntime",
                        "RemoveRuntime",
                        "InspectRuntime",
                    }:
                        invoke = channel.unary_unary(
                            f"/{service.full_name}/{method.name}",
                            request_serializer=request_type.SerializeToString,
                            response_deserializer=output_type.FromString,
                        )
                        with pytest.raises(grpc.aio.AioRpcError) as rejected:
                            await invoke(request_type(), timeout=2)
                        assert rejected.value.code() == grpc.StatusCode.INVALID_ARGUMENT
                        assert rejected.value.details() == "instance_id_invalid"
                        continue

                    if method.name == "ListRuntimes":
                        list_runtimes = channel.unary_unary(
                            f"/{service.full_name}/ListRuntimes",
                            request_serializer=request_type.SerializeToString,
                            response_deserializer=output_type.FromString,
                        )
                        response = await list_runtimes(request_type(), timeout=2)
                        assert list(response.runtimes) == []
                        continue

                    if method.name == "StreamLogs":
                        call = channel.unary_stream(
                            f"/{service.full_name}/StreamLogs",
                            request_serializer=request_type.SerializeToString,
                            response_deserializer=output_type.FromString,
                        )(
                            request_type(
                                instance_id="01990000-0000-7000-8000-000000000308",
                                page_size=1,
                            ),
                            timeout=2,
                        )
                        response = await call.read()
                        assert list(response.entries) == []
                        assert response.next_cursor_offset == 0
                        assert not response.has_more
                        assert await call.read() is grpc.aio.EOF
                        continue

                    if method.server_streaming:
                        call = channel.unary_stream(
                            f"/{service.full_name}/{method.name}",
                            request_serializer=request_type.SerializeToString,
                            response_deserializer=output_type.FromString,
                        )(request_type(), timeout=2)
                        try:
                            await call.read()
                        except grpc.aio.AioRpcError as error:
                            _assert_explicit_unimplemented(error)
                        else:
                            pytest.fail(f"{method.name} returned success")
                    else:
                        invoke = channel.unary_unary(
                            f"/{service.full_name}/{method.name}",
                            request_serializer=request_type.SerializeToString,
                            response_deserializer=output_type.FromString,
                        )
                        try:
                            await invoke(request_type(), timeout=2)
                        except grpc.aio.AioRpcError as error:
                            _assert_explicit_unimplemented(error)
                        else:
                            pytest.fail(f"{method.name} returned a default response")

            peer_uid[0] = 21_003
            async with _uds_channel(socket_path) as channel:
                await asyncio.wait_for(channel.channel_ready(), timeout=2)
                unknown = channel.unary_unary(
                    f"/{service.full_name}/NoSuchMethod",
                    request_serializer=agent_pb2.GetHostInfoRequest.SerializeToString,
                    response_deserializer=agent_pb2.GetHostInfoResponse.FromString,
                )
                with pytest.raises(grpc.aio.AioRpcError) as unknown_error:
                    await unknown(agent_pb2.GetHostInfoRequest(), timeout=2)
                assert unknown_error.value.code() == grpc.StatusCode.PERMISSION_DENIED
                assert unknown_error.value.details() == "Host Agent RPC access denied"
        finally:
            await server.stop(grace=0)

    asyncio.run(scenario())


def test_M1_07A_TC_DP_07_unknown_state_and_empty_response_positive_controls() -> None:
    _assert_observed_uint64(
        agent_pb2.ObservedUInt64(state=agent_pb2.KNOWLEDGE_STATE_KNOWN, value=0)
    )
    with pytest.raises(AssertionError):
        _assert_observed_string(
            agent_pb2.ObservedString(
                state=agent_pb2.KNOWLEDGE_STATE_UNKNOWN,
                value="unknown",
            )
        )
    with pytest.raises(AssertionError):
        _assert_explicit_unimplemented(agent_pb2.ListGPUsResponse())


def test_M1_09A_TC_GPU_17_cuda_driver_version_is_explicit_unknown_when_nvml_is_unavailable() -> (
    None
):
    fake = FakeNvml(
        scenario_for(FixtureId.FX_NV_02).snapshots,
        initialization_error=NvmlErrorKind.DRIVER_UNAVAILABLE,
    )
    response = get_host_info(fake)

    assert response.cuda_driver_version.state == agent_pb2.KNOWLEDGE_STATE_UNKNOWN
    assert not response.cuda_driver_version.HasField("value")
    assert (
        response.cuda_driver_version.unknown_reason
        == agent_pb2.OBSERVATION_UNKNOWN_REASON_SOURCE_UNAVAILABLE
    )


def test_M1_07A_TC_DP_07_socket_mode_is_fixed_under_umask_0000_positive_control(
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "wide.sock"
    raw_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    original_umask = os.umask(0o000)
    try:
        raw_socket.bind(str(socket_path))
    finally:
        os.umask(original_umask)
    try:
        with pytest.raises(AssertionError):
            assert stat.S_IMODE(socket_path.stat().st_mode) == AGENT_SOCKET_MODE
        _set_socket_permissions(socket_path)
        assert stat.S_IMODE(socket_path.stat().st_mode) == AGENT_SOCKET_MODE
    finally:
        raw_socket.close()


def test_M1_07A_TC_DP_07_stale_socket_recovers(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket_path = tmp_path / "agent.sock"
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(socket_path))
        stale.close()
        server = HostAgentServer(socket_path)
        await server.start()
        try:
            assert stat.S_ISSOCK(socket_path.stat().st_mode)
        finally:
            await server.stop(grace=0)

    asyncio.run(scenario())


def test_M1_07A_TC_DP_07_live_instance_cannot_be_replaced(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket_path = tmp_path / "agent.sock"
        first = HostAgentServer(socket_path)
        second = HostAgentServer(socket_path)
        await first.start()
        original_inode = socket_path.stat().st_ino
        try:
            with pytest.raises(AgentAlreadyRunningError):
                await second.start()
            assert socket_path.stat().st_ino == original_inode
            async with _uds_channel(socket_path) as channel:
                await asyncio.wait_for(channel.channel_ready(), timeout=2)
        finally:
            await first.stop(grace=0)

    asyncio.run(scenario())


def test_M1_07A_TC_DP_07_unconditional_unlink_takeover_positive_control(
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "agent.sock"
    first = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    second = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    first.bind(str(socket_path))
    first.listen()
    original_inode = socket_path.stat().st_ino
    try:
        socket_path.unlink()
        second.bind(str(socket_path))
        with pytest.raises(AssertionError):
            assert socket_path.stat().st_ino == original_inode
    finally:
        first.close()
        second.close()


def test_M1_07A_TC_DP_07_production_socket_path_is_frozen() -> None:
    assert Path("/run/holm/agent.sock") == AGENT_SOCKET_PATH

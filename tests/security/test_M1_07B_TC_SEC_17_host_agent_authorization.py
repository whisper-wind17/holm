"""TC-SEC-17 checks for the Host Agent kernel and method boundary."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

import grpc
import pytest
from google.protobuf import message_factory
from holm_contracts.agent.v1 import agent_pb2
from holm_host_agent import server as server_module
from holm_host_agent.authorization import (
    ROLE_METHOD_ALLOWLIST,
    AuthorizationDenialReason,
    AuthorizationInterceptor,
    DenialReason,
    IdentityCatalog,
    ProtocolRejectionReason,
    StructuredDenialAuditor,
    allowed_methods_for_service,
    assert_method_policy_complete,
    assert_protocol_request_surface_safe,
    authorize_rpc,
    descriptor_method_names,
    load_identity_catalog,
)
from holm_host_agent.server import HostAgentServer

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVICE = agent_pb2.DESCRIPTOR.services_by_name["HostAgent"]
HASH_A = "a" * 64
HASH_B = "b" * 64
GENERIC_DENIAL = "Host Agent RPC access denied"
EXPECTED_ROLE_METHOD_ALLOWLIST: Mapping[str, frozenset[str]] = {
    "control-api": frozenset({"GetHostInfo", "ListGPUs", "StreamLogs"}),
    "download-worker": frozenset({"GetHostInfo", "ValidatePath"}),
    "instance-controller": frozenset(
        {
            "EnsureRuntime",
            "StopRuntime",
            "RemoveRuntime",
            "InspectRuntime",
            "ListRuntimes",
            "ListGPUs",
            "StreamLogs",
        }
    ),
}
AGENT_SOCKET_LITERAL = "/run/holm/agent.sock"
DOCKER_SOCKET_LITERALS = frozenset({"/var/run/docker.sock", "/run/docker.sock"})
EXPECTED_DOCKER_SOCKET_FILES = frozenset(
    {"apps/host_agent/src/holm_host_agent/runtime_container.py"}
)
EXPECTED_AGENT_SOCKET_FILES = frozenset(
    {
        "apps/control_api/src/holm_control_api/agent_client.py",
        "apps/host_agent/src/holm_host_agent/server.py",
        "apps/workers/instance/src/holm_instance_controller/__main__.py",
    }
)


@dataclass(slots=True)
class RecordingAuditor:
    records: list[tuple[int, str, DenialReason]] = field(default_factory=list)
    dropped_reason: DenialReason | None = None

    def record(self, *, peer_uid: int, rpc_method: str, reason: DenialReason) -> None:
        if reason != self.dropped_reason:
            self.records.append((peer_uid, rpc_method, reason))


class HandlerDetails(NamedTuple):
    method: str
    invocation_metadata: tuple[tuple[str, str], ...] = ()


def _uds_channel(socket_path: Path) -> grpc.aio.Channel:
    return grpc.aio.insecure_channel(
        f"unix:{socket_path}",
        options=(("grpc.use_local_subchannel_pool", 1),),
    )


def _manifest_data(service_identities: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "build_version": "0.0.0+gm107b0000000",
        "commit": "7" * 40,
        "image_digests": [{"name": "platform", "digest": f"sha256:{HASH_A}"}],
        "artifact_hashes": {
            "bundle": HASH_A,
            "compose": HASH_B,
            "caddyfile": HASH_A,
            "systemd_units": [{"name": "holm.service", "sha256": HASH_B}],
        },
        "service_identities": service_identities,
        "default_resource_limits": [
            {
                "service": "gateway",
                "cpu_millicores": 1000,
                "memory_mib": 512,
                "pids": 256,
                "log_rotation_mib": 10,
                "tmpfs_mib": 64,
            }
        ],
        "runtime_compat_hash": HASH_A,
        "python_lockfile_hash": HASH_A,
        "node_lockfile_hash": HASH_B,
        "gateway": {
            "max_inflight_requests": 512,
            "runtime_pool_max_connections_per_instance": 512,
            "boundary_semantics": "reject_when_current_is_greater_than_or_equal_to_limit",
        },
        "retention": {
            "request_records_days": 30,
            "download_tasks_days": 30,
            "telemetry_raw_seconds": 3600,
            "telemetry_aggregate_days": 30,
        },
        "default_timezone": "Asia/Shanghai",
    }


def _write_manifest(path: Path, identities: list[dict[str, object]]) -> None:
    path.write_text(json.dumps(_manifest_data(identities)), encoding="utf-8")


async def _call_unary(
    channel: grpc.aio.Channel,
    method_name: str,
    *,
    payload: bytes = b"",
    metadata: tuple[tuple[str, str], ...] = (),
) -> Any:
    method = SERVICE.methods_by_name[method_name]
    output_type = message_factory.GetMessageClass(method.output_type)
    call = channel.unary_unary(
        f"/{SERVICE.full_name}/{method_name}",
        request_serializer=lambda value: value,
        response_deserializer=output_type.FromString,
    )
    return await call(payload, timeout=2, metadata=metadata)


async def _call_method(
    channel: grpc.aio.Channel,
    method_name: str,
    *,
    payload: bytes = b"",
) -> Any:
    method = SERVICE.methods_by_name[method_name]
    if not method.server_streaming:
        return await _call_unary(channel, method_name, payload=payload)
    output_type = message_factory.GetMessageClass(method.output_type)
    call = channel.unary_stream(
        f"/{SERVICE.full_name}/{method_name}",
        request_serializer=lambda value: value,
        response_deserializer=output_type.FromString,
    )(payload, timeout=2)
    return await call.read()


def _assert_audit_recorded(
    records: list[tuple[int, str, DenialReason]],
    reason: AuthorizationDenialReason,
) -> None:
    assert any(record[2] == reason for record in records)


def _assert_role_policy_exact(policy: Mapping[str, frozenset[str]]) -> None:
    assert dict(policy) == dict(EXPECTED_ROLE_METHOD_ALLOWLIST)


def test_FIX_08_TC_SEC_17_removed_telemetry_identity_is_unknown_caller() -> None:
    identities = IdentityCatalog.from_pairs(((21_001, "control-api"),))
    decision = authorize_rpc(peer_uid=21_002, rpc_method="ListGPUs", identities=identities)
    assert not decision.allowed
    assert decision.reason is AuthorizationDenialReason.UNKNOWN_UID
    assert allowed_methods_for_service("telemetry-worker") == frozenset()


def _assert_sanitized_denial(detail: str) -> None:
    assert detail == GENERIC_DENIAL
    assert not any(method in detail for method in descriptor_method_names())
    assert "manifest" not in detail.casefold()
    assert "/" not in detail


def _assert_no_forbidden_socket_references(source_root: Path) -> None:
    agent_socket_files: set[str] = set()
    docker_socket_files: set[str] = set()
    for component_root in (source_root / "apps", source_root / "packages"):
        for path in sorted(component_root.glob("**/*")):
            if not path.is_file() or path.suffix not in {
                ".js",
                ".jsx",
                ".py",
                ".toml",
                ".ts",
                ".tsx",
            }:
                continue
            relative = path.relative_to(source_root).as_posix()
            content = path.read_text(encoding="utf-8")
            if AGENT_SOCKET_LITERAL in content:
                agent_socket_files.add(relative)
            if any(value in content for value in DOCKER_SOCKET_LITERALS):
                docker_socket_files.add(relative)
    assert agent_socket_files == EXPECTED_AGENT_SOCKET_FILES, (
        "Agent Socket references must equal the exact allowlist; "
        f"expected={sorted(EXPECTED_AGENT_SOCKET_FILES)}, "
        f"actual={sorted(agent_socket_files)}"
    )
    assert docker_socket_files == EXPECTED_DOCKER_SOCKET_FILES, (
        "Docker Socket references must equal the Host Agent allowlist; "
        f"expected={sorted(EXPECTED_DOCKER_SOCKET_FILES)}, "
        f"actual={sorted(docker_socket_files)}"
    )


async def _wait_until(predicate: Callable[[], bool]) -> None:
    for _attempt in range(100):
        if predicate():
            return
        await asyncio.sleep(0.01)
    pytest.fail("asynchronous Host Agent state did not converge")


def test_M1_07B_TC_SEC_17_real_uds_kernel_peer_uid_ignores_spoofed_metadata(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        actual_uid = os.getuid()
        spoofed_uid = max(actual_uid + 10_000, 21_001)
        identities = IdentityCatalog.from_pairs(((spoofed_uid, "download-worker"),))
        auditor = RecordingAuditor()
        socket_path = tmp_path / "real-peer.sock"
        server = HostAgentServer(
            socket_path,
            identity_catalog=identities,
            auditor=auditor,
        )
        await server.start()
        try:
            async with _uds_channel(socket_path) as channel:
                with pytest.raises(grpc.aio.AioRpcError) as captured:
                    await _call_unary(
                        channel,
                        "GetHostInfo",
                        metadata=(
                            ("caller-uid", str(spoofed_uid)),
                            ("caller-service", "download-worker"),
                        ),
                    )
            assert captured.value.code() == grpc.StatusCode.PERMISSION_DENIED
            _assert_sanitized_denial(captured.value.details())
            expected_reason = (
                AuthorizationDenialReason.CONTAINER_ROOT
                if actual_uid == 0
                else AuthorizationDenialReason.UNKNOWN_UID
            )
            assert auditor.records == [(actual_uid, "GetHostInfo", expected_reason)]

            spoofed_decision = authorize_rpc(
                peer_uid=spoofed_uid,
                rpc_method="GetHostInfo",
                identities=identities,
            )
            with pytest.raises(AssertionError):
                assert not spoofed_decision.allowed
        finally:
            await server.stop(grace=0)

    asyncio.run(scenario())


def test_M1_07B_TC_SEC_17_peer_connection_limit_fails_closed_and_stop_converges(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        socket_path = tmp_path / "bounded-connections.sock"
        server = HostAgentServer(
            socket_path,
            identity_catalog=IdentityCatalog.unavailable(),
            max_peer_connections=1,
        )
        await server.start()
        backend_root = server._backend_root
        assert backend_root is not None
        first_reader, first_writer = await asyncio.open_unix_connection(socket_path)
        await _wait_until(lambda: len(server._connection_tasks) == 1)

        second_reader, second_writer = await asyncio.open_unix_connection(socket_path)
        assert await asyncio.wait_for(second_reader.read(1), timeout=1) == b""
        assert len(server._connection_tasks) == 1

        await server.stop(grace=0)
        await asyncio.wait_for(first_reader.read(), timeout=1)
        assert await first_reader.read(1) == b""
        assert not server._connection_tasks
        assert not backend_root.exists()
        assert not socket_path.exists()

        first_writer.close()
        second_writer.close()
        await asyncio.gather(
            first_writer.wait_closed(),
            second_writer.wait_closed(),
            return_exceptions=True,
        )

    asyncio.run(scenario())


def test_M1_07B_TC_SEC_17_peer_connection_limit_rejects_disabled_guard() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        HostAgentServer(max_peer_connections=0)


@pytest.mark.parametrize("manifest_state", ("missing", "malformed", "empty"))
def test_M1_07B_TC_SEC_17_manifest_unavailable_is_deny_all(
    tmp_path: Path,
    manifest_state: str,
) -> None:
    manifest_path = tmp_path / "release-manifest.json"
    if manifest_state == "malformed":
        manifest_path.write_text("{not-json", encoding="utf-8")
    elif manifest_state == "empty":
        manifest_path.write_text(json.dumps(_manifest_data([])), encoding="utf-8")

    catalog = load_identity_catalog(manifest_path)
    assert not catalog.available
    for method in SERVICE.methods:
        decision = authorize_rpc(peer_uid=21_001, rpc_method=method.name, identities=catalog)
        assert not decision.allowed
        assert decision.reason == AuthorizationDenialReason.MANIFEST_UNAVAILABLE


@pytest.mark.parametrize(
    ("additional", "available"),
    (
        ([{"service": "control-api", "uid": 21_001, "gid": 21_002}], False),
        ([{"service": "gateway", "uid": 21_001, "gid": 21_002}], False),
        ([{"service": "runtime", "uid": 21_001, "gid": 21_002}], False),
        (
            [
                {"service": "gateway", "uid": 21_002, "gid": 21_002},
                {"service": "ledger-writer", "uid": 21_002, "gid": 21_002},
            ],
            True,
        ),
    ),
)
def test_FIX_12_TC_SEC_17_catalog_ignores_only_unprivileged_shared_uids(
    tmp_path: Path, additional: list[dict[str, object]], available: bool
) -> None:
    manifest_path = tmp_path / "release-manifest.json"
    _write_manifest(
        manifest_path,
        [{"service": "download-worker", "uid": 21_001, "gid": 21_001}, *additional],
    )
    catalog = load_identity_catalog(manifest_path)
    assert catalog.available is available
    if available:
        assert dict(catalog.services_by_uid) == {21_001: "download-worker"}
    else:
        decision = authorize_rpc(peer_uid=21_001, rpc_method="GetHostInfo", identities=catalog)
        assert not decision.allowed
        assert decision.reason is AuthorizationDenialReason.MANIFEST_UNAVAILABLE


def test_M1_07B_TC_SEC_17_missing_manifest_rejects_every_rpc_over_uds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(server_module, "_read_peer_uid", lambda _socket: 21_001)
        auditor = RecordingAuditor()
        socket_path = tmp_path / "missing-manifest.sock"
        server = HostAgentServer(
            socket_path,
            release_manifest_path=tmp_path / "absent-release-manifest.json",
            auditor=auditor,
        )
        await server.start()
        try:
            async with _uds_channel(socket_path) as channel:
                for method in SERVICE.methods:
                    with pytest.raises(grpc.aio.AioRpcError) as captured:
                        await _call_method(channel, method.name)
                    assert captured.value.code() == grpc.StatusCode.PERMISSION_DENIED
                    _assert_sanitized_denial(captured.value.details())

            assert {record[1] for record in auditor.records} == descriptor_method_names()
            assert {record[2] for record in auditor.records} == {
                AuthorizationDenialReason.MANIFEST_UNAVAILABLE
            }
        finally:
            await server.stop(grace=0)

    asyncio.run(scenario())


def test_M1_07B_TC_SEC_17_release_manifest_uid_change_revokes_caller(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "release-manifest.json"
    _write_manifest(
        manifest_path,
        [{"service": "download-worker", "uid": 21_001, "gid": 21_001}],
    )
    original = load_identity_catalog(manifest_path)
    assert authorize_rpc(
        peer_uid=21_001,
        rpc_method="GetHostInfo",
        identities=original,
    ).allowed

    _write_manifest(
        manifest_path,
        [{"service": "download-worker", "uid": 21_002, "gid": 21_002}],
    )
    changed = load_identity_catalog(manifest_path)
    decision = authorize_rpc(
        peer_uid=21_001,
        rpc_method="GetHostInfo",
        identities=changed,
    )
    assert not decision.allowed
    assert decision.reason == AuthorizationDenialReason.UNKNOWN_UID


def test_M1_07B_TC_SEC_17_uid_zero_is_denied_even_in_handcrafted_catalog() -> None:
    catalog = IdentityCatalog.from_pairs(((0, "download-worker"),))
    assert catalog.available

    decision = authorize_rpc(peer_uid=0, rpc_method="GetHostInfo", identities=catalog)

    assert not decision.allowed
    assert decision.reason == AuthorizationDenialReason.CONTAINER_ROOT


def test_M1_07B_TC_SEC_17_descriptor_policy_is_complete_with_bidirectional_controls() -> None:
    assert_method_policy_complete()
    assert descriptor_method_names() == frozenset().union(*ROLE_METHOD_ALLOWLIST.values())

    with pytest.raises(AssertionError, match="FutureMethod"):
        assert_method_policy_complete(
            descriptor_methods=descriptor_method_names() | {"FutureMethod"}
        )

    missing_policy = dict(ROLE_METHOD_ALLOWLIST)
    missing_policy["download-worker"] = missing_policy["download-worker"] - {"ValidatePath"}
    with pytest.raises(AssertionError, match="ValidatePath"):
        assert_method_policy_complete(policy=missing_policy)


def test_M1_07B_TC_SEC_17_role_method_sets_are_exact_with_both_controls() -> None:
    _assert_role_policy_exact(ROLE_METHOD_ALLOWLIST)

    wider = dict(ROLE_METHOD_ALLOWLIST)
    wider["download-worker"] = wider["download-worker"] | {"ListGPUs"}
    with pytest.raises(AssertionError):
        _assert_role_policy_exact(wider)

    narrower = dict(ROLE_METHOD_ALLOWLIST)
    narrower["instance-controller"] = narrower["instance-controller"] - {"StopRuntime"}
    with pytest.raises(AssertionError):
        _assert_role_policy_exact(narrower)


@pytest.mark.parametrize(
    "service",
    (
        "gateway",
        "edge",
        "postgres",
        "ledger-writer",
        "maintenance-worker",
    ),
)
def test_M1_07B_TC_SEC_17_other_manifest_services_have_no_methods(service: str) -> None:
    assert allowed_methods_for_service(service) == frozenset()


def test_M1_09A_TC_SEC_17_control_api_role_is_exact_and_fails_closed() -> None:
    assert allowed_methods_for_service("control-api") == frozenset(
        {"GetHostInfo", "ListGPUs", "StreamLogs"}
    )
    wider = dict(ROLE_METHOD_ALLOWLIST)
    wider["control-api"] = wider["control-api"] | {"EnsureRuntime"}
    with pytest.raises(AssertionError):
        _assert_role_policy_exact(wider)

    unavailable = IdentityCatalog.unavailable()
    for method in ("GetHostInfo", "ListGPUs", "StreamLogs"):
        decision = authorize_rpc(peer_uid=21_004, rpc_method=method, identities=unavailable)
        assert not decision.allowed
        assert decision.reason is AuthorizationDenialReason.MANIFEST_UNAVAILABLE


@pytest.mark.parametrize(
    "method",
    (
        "EnsureRuntime",
        "StopRuntime",
        "RemoveRuntime",
    ),
)
def test_M3_08_TC_SEC_17_control_api_lifecycle_methods_are_denied_and_audited(
    method: str,
) -> None:
    catalog = IdentityCatalog.from_pairs(((21_004, "control-api"),))
    decision = authorize_rpc(peer_uid=21_004, rpc_method=method, identities=catalog)
    assert not decision.allowed
    assert decision.reason is AuthorizationDenialReason.METHOD_NOT_ALLOWED

    async def continuation(_details: grpc.HandlerCallDetails) -> grpc.RpcMethodHandler:
        return grpc.unary_unary_rpc_method_handler(lambda _request, _context: None)

    auditor = RecordingAuditor()
    interceptor = AuthorizationInterceptor(
        peer_uid=21_004,
        identities=catalog,
        auditor=auditor,
    )
    asyncio.run(
        interceptor.intercept_service(
            continuation,
            HandlerDetails(f"/{SERVICE.full_name}/{method}"),
        )
    )
    assert auditor.records == [(21_004, method, AuthorizationDenialReason.METHOD_NOT_ALLOWED)]


@pytest.mark.parametrize(
    ("peer_uid", "rpc_method", "catalog", "reason"),
    (
        (
            0,
            "GetHostInfo",
            IdentityCatalog.from_pairs(((0, "download-worker"),)),
            AuthorizationDenialReason.CONTAINER_ROOT,
        ),
        (
            21_001,
            "GetHostInfo",
            IdentityCatalog.unavailable(),
            AuthorizationDenialReason.MANIFEST_UNAVAILABLE,
        ),
        (
            21_001,
            "GetHostInfo",
            IdentityCatalog.from_pairs(((21_002, "download-worker"),)),
            AuthorizationDenialReason.UNKNOWN_UID,
        ),
        (
            21_001,
            "EnsureRuntime",
            IdentityCatalog.from_pairs(((21_001, "download-worker"),)),
            AuthorizationDenialReason.METHOD_NOT_ALLOWED,
        ),
    ),
)
def test_M1_07B_TC_SEC_17_each_denial_reason_is_audited_with_drop_control(
    peer_uid: int,
    rpc_method: str,
    catalog: IdentityCatalog,
    reason: AuthorizationDenialReason,
) -> None:
    async def continuation(_details: grpc.HandlerCallDetails) -> grpc.RpcMethodHandler:
        return grpc.unary_unary_rpc_method_handler(lambda _request, _context: None)

    async def exercise(auditor: RecordingAuditor) -> None:
        interceptor = AuthorizationInterceptor(
            peer_uid=peer_uid,
            identities=catalog,
            auditor=auditor,
        )
        await interceptor.intercept_service(
            continuation,
            HandlerDetails(f"/{SERVICE.full_name}/{rpc_method}"),
        )

    auditor = RecordingAuditor()
    asyncio.run(exercise(auditor))
    _assert_audit_recorded(auditor.records, reason)

    dropped = RecordingAuditor(dropped_reason=reason)
    asyncio.run(exercise(dropped))
    with pytest.raises(AssertionError):
        _assert_audit_recorded(dropped.records, reason)


def test_M1_07B_TC_SEC_17_structured_denial_log_has_required_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    auditor = StructuredDenialAuditor()

    auditor.record(
        peer_uid=21_009,
        rpc_method="EnsureRuntime",
        reason=AuthorizationDenialReason.METHOD_NOT_ALLOWED,
    )

    payload = json.loads(caplog.records[-1].message)
    assert payload.keys() == {"event", "peer_uid", "rpc_method", "reason", "timestamp"}
    assert payload["event"] == "host_agent_rpc_denied"
    assert payload["peer_uid"] == 21_009
    assert payload["rpc_method"] == "EnsureRuntime"
    assert payload["reason"] == "method_not_allowed"
    assert datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00")).tzinfo


def test_M1_07B_TC_SEC_17_denial_error_cannot_leak_allowlist_positive_control() -> None:
    _assert_sanitized_denial(GENERIC_DENIAL)
    with pytest.raises(AssertionError):
        _assert_sanitized_denial("Allowed methods: GetHostInfo, ValidatePath")


def test_M1_07B_TC_SEC_17_malformed_payload_cannot_reveal_known_method(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        actual_uid = os.getuid()
        authorized_uid = max(actual_uid + 10_000, 21_001)
        auditor = RecordingAuditor()
        socket_path = tmp_path / "opaque-denial.sock"
        server = HostAgentServer(
            socket_path,
            identity_catalog=IdentityCatalog.from_pairs(((authorized_uid, "download-worker"),)),
            auditor=auditor,
        )
        await server.start()
        try:
            async with _uds_channel(socket_path) as channel:
                with pytest.raises(grpc.aio.AioRpcError) as known:
                    await _call_unary(channel, "GetHostInfo", payload=b"\xff")
                unknown_call = channel.unary_unary(
                    f"/{SERVICE.full_name}/NoSuchMethod",
                    request_serializer=lambda value: value,
                    response_deserializer=agent_pb2.GetHostInfoResponse.FromString,
                )
                with pytest.raises(grpc.aio.AioRpcError) as unknown:
                    await unknown_call(b"\xff", timeout=2)

            assert (known.value.code(), known.value.details()) == (
                unknown.value.code(),
                unknown.value.details(),
            )
            assert known.value.code() == grpc.StatusCode.PERMISSION_DENIED
            _assert_sanitized_denial(known.value.details())
            expected_reason = (
                AuthorizationDenialReason.CONTAINER_ROOT
                if actual_uid == 0
                else AuthorizationDenialReason.UNKNOWN_UID
            )
            assert auditor.records == [
                (actual_uid, "GetHostInfo", expected_reason),
                (actual_uid, "NoSuchMethod", expected_reason),
            ]
        finally:
            await server.stop(grace=0)

    asyncio.run(scenario())


def test_M1_07B_TC_SEC_17_authorized_unknown_and_reserved_are_distinct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(server_module, "_read_peer_uid", lambda _socket: 21_003)
        socket_path = tmp_path / "dispatch-order.sock"
        server = HostAgentServer(
            socket_path,
            identity_catalog=IdentityCatalog.from_pairs(((21_003, "instance-controller"),)),
        )
        await server.start()
        try:
            async with _uds_channel(socket_path) as channel:
                with pytest.raises(grpc.aio.AioRpcError) as reserved:
                    await _call_unary(channel, "RemoveRuntime")
                unknown = channel.unary_unary(
                    f"/{SERVICE.full_name}/NoSuchMethod",
                    request_serializer=lambda value: value,
                    response_deserializer=agent_pb2.GetHostInfoResponse.FromString,
                )
                with pytest.raises(grpc.aio.AioRpcError) as absent:
                    await unknown(b"", timeout=2)

            assert reserved.value.code() == grpc.StatusCode.INVALID_ARGUMENT
            assert reserved.value.details() == "instance_id_invalid"
            assert absent.value.code() == grpc.StatusCode.PERMISSION_DENIED
            _assert_sanitized_denial(absent.value.details())
        finally:
            await server.stop(grace=0)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("method_name", "peer_uid"),
    (
        ("GetHostInfo", 21_001),
        ("ListGPUs", 21_003),
        ("EnsureRuntime", 21_003),
        ("StopRuntime", 21_003),
        ("RemoveRuntime", 21_003),
        ("InspectRuntime", 21_003),
        ("ListRuntimes", 21_003),
        ("StreamLogs", 21_003),
        ("ValidatePath", 21_001),
    ),
)
def test_M1_07B_TC_SEC_17_unknown_protobuf_fields_are_rejected_and_audited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    peer_uid: int,
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(server_module, "_read_peer_uid", lambda _socket: peer_uid)
        auditor = RecordingAuditor()
        socket_path = tmp_path / "strict-wire.sock"
        server = HostAgentServer(
            socket_path,
            identity_catalog=IdentityCatalog.from_pairs(
                (
                    (21_001, "download-worker"),
                    (21_002, "telemetry-worker"),
                    (21_003, "instance-controller"),
                )
            ),
            auditor=auditor,
        )
        await server.start()
        try:
            async with _uds_channel(socket_path) as channel:
                with pytest.raises(grpc.aio.AioRpcError) as captured:
                    await _call_method(
                        channel,
                        method_name,
                        payload=b"\x98\x06\x01",
                    )
            assert captured.value.code() == grpc.StatusCode.INVALID_ARGUMENT
            assert captured.value.details() == "Host Agent RPC request rejected"
            assert auditor.records == [
                (peer_uid, method_name, ProtocolRejectionReason.UNKNOWN_FIELDS)
            ]
        finally:
            await server.stop(grace=0)

    asyncio.run(scenario())


# The assertion below stays an exact set as typed RPCs gain fields.  Lifecycle
# and read-only requests remain closed: there is no free-form command, argv,
# environment map, or caller-selected identity.
EXPECTED_REQUEST_FIELDS: frozenset[str] = frozenset(
    {
        "holm.agent.v1.EnsureRuntimeRequest.config_version",
        "holm.agent.v1.EnsureRuntimeRequest.context_length",
        "holm.agent.v1.EnsureRuntimeRequest.gpu_memory_utilization",
        "holm.agent.v1.EnsureRuntimeRequest.gpu_uuids",
        "holm.agent.v1.EnsureRuntimeRequest.instance_id",
        "holm.agent.v1.EnsureRuntimeRequest.model_path",
        "holm.agent.v1.EnsureRuntimeRequest.runtime_name",
        "holm.agent.v1.EnsureRuntimeRequest.served_model_name",
        "holm.agent.v1.EnsureRuntimeRequest.tensor_parallel_size",
        "holm.agent.v1.InspectRuntimeRequest.instance_id",
        "holm.agent.v1.RemoveRuntimeRequest.instance_id",
        "holm.agent.v1.StopRuntimeRequest.grace_seconds",
        "holm.agent.v1.StopRuntimeRequest.instance_id",
        "holm.agent.v1.StreamLogsRequest.cursor_offset",
        "holm.agent.v1.StreamLogsRequest.instance_id",
        "holm.agent.v1.StreamLogsRequest.page_size",
        "holm.agent.v1.ValidatePathRequest.path",
    }
)


def test_M1_07B_TC_SEC_17_request_surface_admits_only_declared_typed_fields() -> None:
    assert_protocol_request_surface_safe()
    assert {
        field.full_name for method in SERVICE.methods for field in method.input_type.fields
    } == EXPECTED_REQUEST_FIELDS

    with pytest.raises(AssertionError, match="args"):
        assert_protocol_request_surface_safe(extra_field_names=("args",))


def _write_socket_scan_fixture(root: Path) -> None:
    for relative in EXPECTED_AGENT_SOCKET_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'SOCKET = "{AGENT_SOCKET_LITERAL}"\n', encoding="utf-8")
    for relative in EXPECTED_DOCKER_SOCKET_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('DOCKER = "/var/run/docker.sock"\n', encoding="utf-8")


def test_M1_07B_TC_SEC_17_privileged_socket_source_scan_has_hit_control_a(
    tmp_path: Path,
) -> None:
    _assert_no_forbidden_socket_references(PROJECT_ROOT)
    _write_socket_scan_fixture(tmp_path)

    injected_gateway = tmp_path / "apps" / "gateway" / "probe.py"
    injected_gateway.parent.mkdir(parents=True)
    injected_gateway.write_text(f'AGENT_SOCKET = "{AGENT_SOCKET_LITERAL}"\n', encoding="utf-8")
    with pytest.raises(AssertionError, match="apps/gateway/probe.py"):
        _assert_no_forbidden_socket_references(tmp_path)


def test_M1_07B_TC_SEC_17_agent_socket_allowlist_rejects_other_control_file_b(
    tmp_path: Path,
) -> None:
    _write_socket_scan_fixture(tmp_path)
    other_control_file = tmp_path / "apps" / "control_api" / "src" / "holm_control_api" / "probe.py"
    other_control_file.write_text(f'AGENT_SOCKET = "{AGENT_SOCKET_LITERAL}"\n', encoding="utf-8")

    with pytest.raises(AssertionError, match="holm_control_api/probe.py"):
        _assert_no_forbidden_socket_references(tmp_path)


def test_M1_07B_TC_SEC_17_agent_socket_holder_cannot_reference_docker_socket_c(
    tmp_path: Path,
) -> None:
    _write_socket_scan_fixture(tmp_path)
    agent_client = tmp_path / "apps/control_api/src/holm_control_api/agent_client.py"
    agent_client.write_text(
        f'SOCKET = "{AGENT_SOCKET_LITERAL}"\nDOCKER = "/var/run/docker.sock"\n',
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="agent_client.py"):
        _assert_no_forbidden_socket_references(tmp_path)


def test_M2_09A_TC_SEC_17_download_worker_is_inside_the_docker_socket_scan(
    tmp_path: Path,
) -> None:
    _write_socket_scan_fixture(tmp_path)
    injected_worker = tmp_path / "apps/workers/download/src/holm_download_worker/probe.py"
    injected_worker.parent.mkdir(parents=True, exist_ok=True)
    injected_worker.write_text('DOCKER = "/var/run/docker.sock"\n', encoding="utf-8")

    with pytest.raises(AssertionError, match="apps/workers/download"):
        _assert_no_forbidden_socket_references(tmp_path)


def test_M1_07B_TC_SEC_17_unused_agent_socket_allowlist_entry_fails_d(
    tmp_path: Path,
) -> None:
    _write_socket_scan_fixture(tmp_path)
    server = tmp_path / "apps/host_agent/src/holm_host_agent/server.py"
    server.write_text('SOCKET_NAME = "agent.sock"\n', encoding="utf-8")

    with pytest.raises(AssertionError, match="holm_host_agent/server.py"):
        _assert_no_forbidden_socket_references(tmp_path)

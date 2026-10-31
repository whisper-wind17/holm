"""M3-09C Host Agent contract, authorization, and fake-fixture coverage."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

import grpc
import pytest
from fixtures.fake_host_agent import (
    INSTANCE_CONTROLLER_UID,
    TELEMETRY_WORKER_UID,
    ContainerStatus,
    FakeHostAgent,
    LaunchSpec,
    RpcMethod,
)
from holm_contracts.agent.v1 import agent_pb2
from holm_host_agent.authorization import (
    ROLE_METHOD_ALLOWLIST,
    AuthorizationDenialReason,
    AuthorizationInterceptor,
    DenialReason,
    IdentityCatalog,
    assert_method_policy_complete,
)
from holm_host_agent.runtime_process import (
    ObservationKnowledgeState,
    ObservedGpuUuids,
    RuntimeStateValue,
    RuntimeSummary,
)
from holm_host_agent.server import HostAgentService

INSTANCE_ID = UUID("01990000-0000-7000-8000-00000000090c")


@dataclass(frozen=True, slots=True)
class _HandlerDetails:
    method: str
    invocation_metadata: object = None


class _Auditor:
    def __init__(self) -> None:
        self.records: list[tuple[int, str, DenialReason]] = []

    def record(self, *, peer_uid: int, rpc_method: str, reason: DenialReason) -> None:
        self.records.append((peer_uid, rpc_method, reason))


class _RuntimeManager:
    async def list(self) -> tuple[RuntimeSummary, ...]:
        return (
            RuntimeSummary(
                instance_id=str(INSTANCE_ID),
                state=RuntimeStateValue.RUNNING,
                gpu_uuids=ObservedGpuUuids(
                    ObservationKnowledgeState.KNOWN,
                    values=("GPU-observed",),
                ),
            ),
        )


def test_M3_09C_ListRuntimes_has_no_filter_surface_and_serializes_observed_GPU() -> None:
    request = agent_pb2.ListRuntimesRequest()
    assert len(request.DESCRIPTOR.fields) == 0
    service = HostAgentService(runtime_manager=_RuntimeManager())  # type: ignore[arg-type]

    response = asyncio.run(service.ListRuntimes(request, object()))  # type: ignore[arg-type]

    assert len(response.runtimes) == 1
    assert response.runtimes[0].instance_id == str(INSTANCE_ID)
    assert response.runtimes[0].status == agent_pb2.RUNTIME_PROCESS_STATE_RUNNING
    assert response.runtimes[0].gpu_uuids.state == agent_pb2.KNOWLEDGE_STATE_KNOWN
    assert tuple(response.runtimes[0].gpu_uuids.values) == ("GPU-observed",)


def test_M3_09C_ListRuntimes_is_authorized_only_for_instance_controller() -> None:
    assert "ListRuntimes" in ROLE_METHOD_ALLOWLIST["instance-controller"]
    assert all(
        "ListRuntimes" not in methods
        for role, methods in ROLE_METHOD_ALLOWLIST.items()
        if role != "instance-controller"
    )
    assert_method_policy_complete()


@pytest.mark.parametrize(
    ("uid", "role"),
    [(21_001, "control-api"), (21_002, "download-worker"), (21_003, "telemetry-worker")],
)
def test_M3_09C_non_controller_ListRuntimes_calls_are_denied_and_audited(
    uid: int,
    role: str,
) -> None:
    auditor = _Auditor()
    interceptor = AuthorizationInterceptor(
        peer_uid=uid,
        identities=IdentityCatalog.from_pairs(((uid, role),)),
        auditor=auditor,
    )

    async def continuation(_details: grpc.HandlerCallDetails) -> grpc.RpcMethodHandler:
        return grpc.unary_unary_rpc_method_handler(lambda _request, _context: None)

    asyncio.run(
        interceptor.intercept_service(
            continuation,
            _HandlerDetails("/holm.agent.v1.HostAgent/ListRuntimes"),  # type: ignore[arg-type]
        )
    )

    assert auditor.records == [(uid, "ListRuntimes", AuthorizationDenialReason.METHOD_NOT_ALLOWED)]


def test_M3_09C_fake_Host_Agent_lists_and_injects_observed_GPU_independently() -> None:
    agent = FakeHostAgent(allowed_image_digests=("sha256:" + "a" * 64,))
    spec = LaunchSpec(
        runtime_id="m3-09c-runtime",
        image_digest="sha256:" + "a" * 64,
        gpu_uuids=("GPU-requested",),
    )
    agent.ensure_runtime(spec)
    agent.inject_observed_gpu_uuids(spec.runtime_id, ("GPU-observed",))

    assert agent.require_allowed(INSTANCE_CONTROLLER_UID, "ListRuntimes") is RpcMethod.LIST_RUNTIMES
    assert agent.list_runtimes()[0].status is ContainerStatus.RUNNING
    assert agent.list_runtimes()[0].gpu_uuids == ("GPU-observed",)
    try:
        agent.require_allowed(TELEMETRY_WORKER_UID, "ListRuntimes")
    except RuntimeError:
        pass
    else:
        raise AssertionError("telemetry-worker unexpectedly gained ListRuntimes")

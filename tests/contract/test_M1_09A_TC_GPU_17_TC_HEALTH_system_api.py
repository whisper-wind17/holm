"""M1-09A consumer contracts for System server/components endpoints."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Final, cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from fixtures.asgi_client import (
    NO_CONFIGURED_CREDENTIAL_ROWS,
    AsgiResponse,
    AsgiTestClient,
)
from holm_contracts import VersionPayload
from holm_contracts.agent.v1 import agent_pb2
from holm_control_api import (
    COMPONENT_SIGNAL_RULES,
    READY_PATH,
    SYSTEM_COMPONENTS_PATH,
    SYSTEM_SERVER_PATH,
    AgentSnapshot,
    AgentUnavailable,
    AgentUnavailableReason,
    AuthenticatedSession,
    AuthenticationService,
    ComponentSignalSource,
    DependencyCheck,
    GrpcHostAgentClient,
    ReadinessReason,
    SystemComponentId,
    SystemComponentStatus,
    SystemStatusService,
    create_app,
    summarize_component_statuses,
)
from holm_control_api.system_status import RequestLedgerWriterDetails
from holm_domain import UserRole, UserStatus
from holm_persistence import (
    AuditEventDraft,
    AuditEventRecord,
    SessionRecord,
    SystemStatusSnapshot,
    UserRecord,
)
from sqlalchemy.ext.asyncio import AsyncSession

NOW: Final[datetime] = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
ADMIN_ID: Final[UUID] = UUID("00000000-0000-7000-8000-000000000901")


async def _readiness_ok() -> ReadinessReason:
    return ReadinessReason.OK


class _Agent:
    def __init__(self, observation: AgentSnapshot | AgentUnavailable) -> None:
        self.observation = observation

    async def observe(self) -> AgentSnapshot | AgentUnavailable:
        return self.observation


class _Database:
    def __init__(self, snapshot: SystemStatusSnapshot | BaseException) -> None:
        self.snapshot = snapshot

    async def read(self) -> SystemStatusSnapshot:
        if isinstance(self.snapshot, BaseException):
            raise self.snapshot
        return self.snapshot


class _AuditWriter:
    def __init__(self) -> None:
        self.events: list[AuditEventDraft] = []

    async def add(self, session: AsyncSession, event: AuditEventDraft) -> AuditEventRecord:
        raise AssertionError("contract authenticator has no business transaction")

    async def write(self, event: AuditEventDraft) -> AuditEventRecord:
        self.events.append(event)
        return cast(AuditEventRecord, object())


class _Authenticator:
    def __init__(self, role: UserRole) -> None:
        user = UserRecord(
            id=ADMIN_ID,
            username_norm=f"{role.value}-system-test",
            display_name="System Test",
            role=role,
            status=UserStatus.ACTIVE,
            must_change_password=False,
            created_at=NOW,
            updated_at=NOW,
            deleted_at=None,
            password_hash="$argon2id$not-returned",
        )
        session = SessionRecord(
            id=UUID("00000000-0000-7000-8000-000000000902"),
            user_id=user.id,
            session_hash="a" * 64,
            csrf_hash="b" * 64,
            created_at=NOW,
            last_seen_at=NOW,
            expires_at=NOW + timedelta(hours=1),
            revoked_at=None,
        )
        self.authenticated = AuthenticatedSession(user=user, session=session)
        self.audit_writer = _AuditWriter()

    async def authenticate_session(
        self,
        *,
        session_token: str | None,
        csrf_token: str | None = None,
        require_csrf: bool = False,
    ) -> AuthenticatedSession:
        return self.authenticated


def _version() -> VersionPayload:
    return VersionPayload(
        build_version="1.0.0+gm109a000001",
        commit="9" * 40,
        schema_version=1,
        runtime_compat_hash="8" * 64,
    )


def _database_snapshot() -> SystemStatusSnapshot:
    return SystemStatusSnapshot(
        postgres_version="16.15",
        heartbeats=MappingProxyType({}),
        oldest_executable_job_age_seconds=None,
        expired_lease_count=0,
        running_job_count=0,
    )


def _host_info() -> agent_pb2.GetHostInfoResponse:
    return agent_pb2.GetHostInfoResponse(
        logical_cpu_count=agent_pb2.ObservedUInt64(state=agent_pb2.KNOWLEDGE_STATE_KNOWN, value=32),
        total_memory_bytes=agent_pb2.ObservedUInt64(
            state=agent_pb2.KNOWLEDGE_STATE_KNOWN, value=128 * 1024**3
        ),
        total_disk_bytes=agent_pb2.ObservedUInt64(
            state=agent_pb2.KNOWLEDGE_STATE_KNOWN, value=2 * 1024**4
        ),
        available_disk_bytes=agent_pb2.ObservedUInt64(
            state=agent_pb2.KNOWLEDGE_STATE_KNOWN, value=1024**4
        ),
        driver_version=agent_pb2.ObservedString(
            state=agent_pb2.KNOWLEDGE_STATE_KNOWN, value="595.45.04"
        ),
        cuda_driver_version=agent_pb2.ObservedString(
            state=agent_pb2.KNOWLEDGE_STATE_KNOWN, value="13.0"
        ),
    )


def _snapshot(inventory: agent_pb2.ListGPUsResponse) -> AgentSnapshot:
    return AgentSnapshot(host_info=_host_info(), gpu_inventory=inventory)


def _app(
    observation: AgentSnapshot | AgentUnavailable,
    *,
    role: UserRole | None = UserRole.ADMIN,
) -> FastAPI:
    service = SystemStatusService(
        agent=_Agent(observation),
        database=_Database(_database_snapshot()),
        version=_version(),
    )
    authentication = None if role is None else cast(AuthenticationService, _Authenticator(role))
    return create_app(
        version_payload=_version(),
        readiness_checks=[DependencyCheck(name="database", probe=_readiness_ok)],
        credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
        authentication=authentication,
        system_status=service,
    )


def _get(app: FastAPI, *paths: str) -> tuple[AsgiResponse, ...]:
    async def scenario() -> tuple[AsgiResponse, ...]:
        client = AsgiTestClient(app)
        async with client.lifespan():
            responses: list[AsgiResponse] = []
            for path in paths:
                responses.append(await client.get(path))
                await asyncio.sleep(0.001)
            return tuple(responses)

    return asyncio.run(scenario())


def _four_observations() -> tuple[AgentUnavailable | AgentSnapshot, ...]:
    unreachable = AgentUnavailable(AgentUnavailableReason.CONNECTION_FAILED)
    nvml_unavailable = _snapshot(
        agent_pb2.ListGPUsResponse(
            discovery_state=agent_pb2.GPU_DISCOVERY_STATE_UNAVAILABLE,
            failure_reason=agent_pb2.GPU_DISCOVERY_FAILURE_REASON_LIBRARY_UNAVAILABLE,
        )
    )
    no_devices = _snapshot(
        agent_pb2.ListGPUsResponse(
            discovery_state=agent_pb2.GPU_DISCOVERY_STATE_NO_DEVICES,
            failure_reason=agent_pb2.GPU_DISCOVERY_FAILURE_REASON_NO_DEVICES,
        )
    )
    ready = _snapshot(
        agent_pb2.ListGPUsResponse(
            discovery_state=agent_pb2.GPU_DISCOVERY_STATE_READY,
            devices=(
                agent_pb2.GpuDevice(
                    uuid="GPU-0001",
                    display_index=0,
                    model="L20",
                    memory_bytes=48 * 1024**3,
                    health=agent_pb2.GPU_HEALTH_STATE_UNKNOWN,
                    health_reasons=(agent_pb2.GPU_HEALTH_REASON_QUERY_UNSUPPORTED,),
                ),
            ),
        )
    )
    return unreachable, nvml_unavailable, no_devices, ready


def _assert_distinct(payloads: list[dict[str, object]]) -> None:
    rendered = {repr(payload) for payload in payloads}
    assert len(rendered) == 4


def test_M1_09A_TC_GPU_17_four_agent_gpu_states_are_pairwise_distinct() -> None:
    payloads = [
        _get(_app(observation), SYSTEM_SERVER_PATH)[0].json()
        for observation in _four_observations()
    ]
    for payload in payloads:
        payload.pop("observed_at")
    _assert_distinct(payloads)
    assert payloads[0]["agent"] == {
        "status": "unavailable",
        "reason": "connection_failed",
    }
    available = cast(dict[str, object], payloads[1]["agent"])
    assert cast(dict[str, object], available["gpus"])["failure_reason"] == "library_unavailable"
    assert cast(dict[str, object], payloads[2]["agent"])["gpus"] != available["gpus"]
    ready_gpu = cast(dict[str, object], cast(dict[str, object], payloads[3]["agent"])["gpus"])
    assert cast(list[dict[str, object]], ready_gpu["devices"])[0]["health"] == "unknown"
    forbidden = {"utilization", "memory_used", "temperature", "power", "allocation_status"}
    assert not forbidden.intersection(repr(payloads))

    collapsed = list(payloads)
    collapsed[0] = collapsed[2]
    with pytest.raises(AssertionError):
        _assert_distinct(collapsed)


def test_M1_09A_TC_HEALTH_12_agent_outage_does_not_change_readiness_same_observation() -> None:
    app = _app(AgentUnavailable(AgentUnavailableReason.CONNECTION_FAILED))
    ready, server = _get(app, READY_PATH, SYSTEM_SERVER_PATH)
    assert ready.status == 200
    assert all(item["name"] != "agent" for item in ready.json()["dependencies"])
    assert server.status == 200
    assert server.json()["agent"]["status"] == "unavailable"


def test_M1_09A_TC_HEALTH_13_system_endpoints_enforce_view_system_and_observation_time() -> None:
    observation = _four_observations()[-1]
    unauthenticated = _get(_app(observation, role=None), SYSTEM_SERVER_PATH, SYSTEM_COMPONENTS_PATH)
    user = _get(_app(observation, role=UserRole.USER), SYSTEM_SERVER_PATH, SYSTEM_COMPONENTS_PATH)
    admin = _get(_app(observation), SYSTEM_SERVER_PATH, SYSTEM_COMPONENTS_PATH, SYSTEM_SERVER_PATH)
    assert [response.status for response in unauthenticated] == [401, 401]
    assert [response.status for response in user] == [403, 403]
    assert [response.status for response in admin] == [200, 200, 200]
    first = datetime.fromisoformat(admin[0].json()["observed_at"].replace("Z", "+00:00"))
    second = datetime.fromisoformat(admin[2].json()["observed_at"].replace("Z", "+00:00"))
    assert second > first
    assert all(
        any(name.lower() == "x-request-id" for name, _ in response.headers) for response in admin
    )


def test_M1_09A_TC_HEALTH_14_all_declared_signal_sources_drive_component_state() -> None:
    expected_sources = {
        SystemComponentId.CONTROL_PLANE: (ComponentSignalSource.SELF,),
        SystemComponentId.GATEWAY: (ComponentSignalSource.PROBE,),
        SystemComponentId.DATABASE: (ComponentSignalSource.DATABASE,),
        SystemComponentId.REQUEST_LEDGER_WRITER: (
            ComponentSignalSource.HEARTBEAT,
            ComponentSignalSource.LEDGER_BACKLOG,
        ),
        SystemComponentId.SCHEDULER: (
            ComponentSignalSource.HEARTBEAT,
            ComponentSignalSource.JOBS,
        ),
        SystemComponentId.TELEMETRY_COLLECTOR: (ComponentSignalSource.HEARTBEAT,),
        SystemComponentId.DOWNLOAD_MANAGER: (ComponentSignalSource.HEARTBEAT,),
        SystemComponentId.EDGE: (ComponentSignalSource.PROBE,),
        SystemComponentId.MAINTENANCE: (ComponentSignalSource.HEARTBEAT,),
        SystemComponentId.HOST_AGENT: (ComponentSignalSource.AGENT,),
    }
    assert {
        component_id: rule.sources for component_id, rule in COMPONENT_SIGNAL_RULES.items()
    } == (expected_sources)

    payload = _get(_app(_four_observations()[-1]), SYSTEM_COMPONENTS_PATH)[0].json()
    components = {SystemComponentId(item["id"]): item for item in payload["components"]}
    assert set(components) == set(expected_sources)
    for component_id, sources in expected_sources.items():
        component = components[component_id]
        assert component["signal_sources"] == [source.value for source in sources]
        assert component["status"] == (
            SystemComponentStatus.READY.value
            if sources
            in {
                (ComponentSignalSource.SELF,),
                (ComponentSignalSource.DATABASE,),
                (ComponentSignalSource.AGENT,),
            }
            else SystemComponentStatus.NOT_DEPLOYED.value
        )


def test_M1_09A_TC_HEALTH_15_not_deployed_is_neither_ready_nor_abnormal_and_ledger_is_unknown() -> (
    None
):
    response = _get(_app(_four_observations()[-1]), SYSTEM_COMPONENTS_PATH)[0]
    payload = response.json()
    assert payload["summary"] == {
        "status": "ready",
        "ready_count": 3,
        "abnormal_count": 0,
        "not_deployed_count": 7,
    }
    components = {item["id"]: item for item in payload["components"]}
    assert set(components) == {component.value for component in SystemComponentId}
    ledger = components[SystemComponentId.REQUEST_LEDGER_WRITER.value]
    assert ledger["status"] == SystemComponentStatus.NOT_DEPLOYED.value
    details = ledger["details"]
    assert details["pending_event_count"] == {"state": "unknown", "reason": "not_collected"}
    assert details["oldest_event_at"] == {"state": "unknown", "reason": "not_collected"}

    degraded_and_not_deployed = summarize_component_statuses(
        (SystemComponentStatus.DEGRADED, SystemComponentStatus.NOT_DEPLOYED)
    )
    assert degraded_and_not_deployed.model_dump(mode="json") == {
        "status": "degraded",
        "ready_count": 0,
        "abnormal_count": 1,
        "not_deployed_count": 1,
    }
    assert summarize_component_statuses(
        (SystemComponentStatus.READY, SystemComponentStatus.NOT_DEPLOYED)
    ).model_dump(mode="json") == {
        "status": "ready",
        "ready_count": 1,
        "abnormal_count": 0,
        "not_deployed_count": 1,
    }
    assert summarize_component_statuses(
        (SystemComponentStatus.DEGRADED, SystemComponentStatus.READY)
    ).model_dump(mode="json") == {
        "status": "degraded",
        "ready_count": 1,
        "abnormal_count": 1,
        "not_deployed_count": 0,
    }


@pytest.mark.parametrize(
    ("field", "known_value"),
    (
        ("pending_event_count", 0),
        ("oldest_event_at", "1970-01-01T00:00:00Z"),
    ),
)
def test_SYS_01_M1_09A_TC_HEALTH_15_each_ledger_observation_accepts_measured_values(
    field: str,
    known_value: object,
) -> None:
    payload: dict[str, object] = {
        "kind": "request_ledger_writer",
        "pending_event_count": {"state": "unknown", "reason": "not_collected"},
        "oldest_event_at": {"state": "unknown", "reason": "not_collected"},
    }
    payload[field] = {"state": "known", "value": known_value}
    observed = RequestLedgerWriterDetails.model_validate(payload).model_dump(mode="json")
    assert observed[field] == payload[field]


def test_M1_09A_TC_GPU_17_agent_client_rejects_nonpositive_timeout() -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        GrpcHostAgentClient(timeout_seconds=0)

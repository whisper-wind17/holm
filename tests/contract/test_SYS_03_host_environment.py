"""FX-SYS-03: typed Agent observations and the repository runtime catalog.

Extends FX-M1-09A's authenticated HTTP harness; no host commands or GPU are used.
"""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from fixtures.asgi_client import NO_CONFIGURED_CREDENTIAL_ROWS
from holm_contracts.agent.v1 import agent_pb2
from holm_contracts.release import HostRequirements
from holm_control_api import (
    READY_PATH,
    SYSTEM_COMPONENTS_PATH,
    AuthenticationService,
    DependencyCheck,
    SystemStatusService,
    create_app,
)
from holm_control_api.agent_client import (
    AgentSnapshot,
    AgentUnavailable,
    AgentUnavailableReason,
)
from holm_control_api.diagnostics_service import SystemHealthDiagnosticSource
from holm_control_api.system_status import (
    host_environment_response,
    inference_engines_response,
)
from holm_control_api.version_comparison import compare_versions
from holm_domain import UserRole
from holm_runtimes import load_runtime_compatibility
from security.test_M7_11A_diagnostics_authorization import _diagnostics
from test_M1_09A_TC_GPU_17_TC_HEALTH_system_api import (
    _Agent,
    _Authenticator,
    _Database,
    _database_snapshot,
    _get,
    _readiness_ok,
    _version,
)


def _observation(version: str | None = "595.45.04") -> AgentSnapshot:
    value = (
        agent_pb2.ObservedString(state=agent_pb2.KNOWLEDGE_STATE_KNOWN, value=version)
        if version is not None
        else agent_pb2.ObservedString()
    )
    return AgentSnapshot(
        agent_pb2.GetHostInfoResponse(
            driver_version=value,
            docker_version=value,
            toolkit_version=value,
            compose_version=value,
            cuda_driver_version=agent_pb2.ObservedString(
                state=agent_pb2.KNOWLEDGE_STATE_KNOWN, value="13.0"
            ),
        ),
        agent_pb2.ListGPUsResponse(),
    )


@pytest.mark.parametrize(
    ("current", "minimum", "expected"),
    [
        ("580.65.06", "580.65.6", 0),
        ("24", "24.0.0.0", 0),
        ("24.10", "24.9", 1),
        ("579.99", "580", -1),
        ("580.65-rc1", "580.65", None),
        ("580+cuda", "580", None),
        ("580", "580.x", None),
        ("", "1", None),
    ],
)
def test_SYS_03_TC_HEALTH_26_27_version_comparison(current, minimum, expected):
    assert compare_versions(current, minimum) == expected


@pytest.mark.parametrize(
    ("version", "minimum", "status", "reason"),
    [
        ("595.45.04", "580.65.06", "satisfied", "meets_minimum"),
        ("580.65.06", "580.65.06", "satisfied", "meets_minimum"),
        ("579.99", "580.65.06", "unsatisfied", "below_minimum"),
        (None, "580.65.06", "unknown", "current_unavailable"),
        (None, None, "unknown", "current_unavailable"),
        ("595.45.04", None, "unknown", "minimum_not_declared"),
        ("595.45+cuda", "580.65.06", "unknown", "version_unparseable"),
    ],
)
def test_SYS_03_TC_HEALTH_26_host_table(version, minimum, status, reason):
    requirements = HostRequirements(
        nvidia_driver=minimum, docker=minimum, nvidia_container_toolkit=minimum, compose=minimum
    )
    response = host_environment_response(_observation(version), requirements)
    assert [row.id for row in response.items] == [
        "nvidia_driver",
        "docker",
        "compose",
        "nvidia_container_toolkit",
    ]
    for row in response.items:
        assert (row.minimum, row.status, row.reason) == (minimum, status, reason)
    assert response.items[2].current.model_dump() == (
        {"state": "known", "value": version}
        if version is not None
        else {"state": "unknown", "reason": "protocol_invalid"}
    )
    assert response.cuda_driver_version.model_dump() == {"state": "known", "value": "13.0"}


@pytest.mark.parametrize(
    ("version", "minimum", "status", "reason"),
    [
        ("595.45.04", "580.65.06", "compatible", "meets_minimum"),
        ("580.65.06", "580.65.06", "compatible", "meets_minimum"),
        ("579.99", "580.65.06", "incompatible", "below_minimum"),
        (None, "580.65.06", "unknown", "driver_unavailable"),
        (None, None, "unknown", "driver_unavailable"),
        ("595.45.04", None, "unknown", "minimum_not_declared"),
        ("595.45-rc1", "580.65.06", "unknown", "version_unparseable"),
    ],
)
def test_SYS_03_TC_HEALTH_27_engine_table(version, minimum, status, reason):
    catalog = load_runtime_compatibility(Path("runtime-compat.yaml"))
    catalog = replace(
        catalog,
        runtimes={
            name: replace(runtime, minimum_driver_version=minimum)
            for name, runtime in catalog.runtimes.items()
        },
    )
    snapshot = replace(_database_snapshot(), active_instance_counts={"vllm": 2})
    rows = inference_engines_response(_observation(version), catalog, snapshot)
    assert [row.name for row in rows] == ["vllm", "sglang"]
    assert [row.active_instance_count for row in rows] == [2, 0]
    for row in rows:
        assert (row.minimum_driver_version, row.driver_compatibility, row.reason) == (
            minimum,
            status,
            reason,
        )
        assert row.image_digest is None
        assert row.launcher == "process"
    assert all(
        row.active_instance_count is None
        for row in inference_engines_response(_observation(version), catalog, None)
    )


@pytest.mark.parametrize("database_failed", [False, True])
@pytest.mark.parametrize("agent_failed", [False, True])
def test_SYS_03_TC_HEALTH_21_26_27_http_single_observation_and_safe_diagnostics(
    database_failed: bool,
    agent_failed: bool,
) -> None:
    class CountingAgent(_Agent):
        calls = 0

        async def observe(self):
            self.calls += 1
            return await super().observe()

    class CountingDatabase(_Database):
        calls = 0

        async def read(self):
            self.calls += 1
            return await super().read()

    agent = CountingAgent(
        AgentUnavailable(AgentUnavailableReason.TIMEOUT) if agent_failed else _observation()
    )
    database = CountingDatabase(
        OSError("FX-SYS-03-private-db-path")
        if database_failed
        else replace(_database_snapshot(), active_instance_counts={"vllm": 3})
    )
    catalog = load_runtime_compatibility(Path("runtime-compat.yaml"))
    service = SystemStatusService(
        agent=agent,
        database=database,
        version=_version(),
        runtime_catalog=catalog,
        host_requirements=HostRequirements(nvidia_driver="580.65.06", docker="999"),
    )
    app = create_app(
        version_payload=_version(),
        readiness_checks=[DependencyCheck(name="database", probe=_readiness_ok)],
        credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
        authentication=cast(AuthenticationService, _Authenticator(UserRole.ADMIN)),
        system_status=service,
    )
    ready, response = _get(app, READY_PATH, SYSTEM_COMPONENTS_PATH)
    assert ready.status == response.status == 200
    assert (agent.calls, database.calls) == (1, 1)
    payload = response.json()
    assert set(payload) == {
        "observed_at",
        "platform",
        "summary",
        "components",
        "host_environment",
        "inference_engines",
    }
    baseline = SystemStatusService(agent=agent, database=database, version=_version())
    assert payload["summary"] == asyncio.run(baseline.read_components()).summary.model_dump()
    if agent_failed:
        for row in payload["host_environment"]["items"]:
            assert (row["status"], row["reason"], row["current"]) == (
                "unknown",
                "agent_unavailable",
                {"state": "unknown", "reason": "source_unavailable"},
            )
        assert payload["host_environment"]["cuda_driver_version"] == {
            "state": "unknown",
            "reason": "source_unavailable",
        }
    for row in payload["inference_engines"]:
        assert (row["driver_compatibility"], row["reason"]) == (
            ("unknown", "agent_unavailable") if agent_failed else ("compatible", "meets_minimum")
        )
        assert row["active_instance_count"] == (
            None if database_failed else (3 if row["name"] == "vllm" else 0)
        )
    diagnostic = _diagnostics()
    diagnostic._health = SystemHealthDiagnosticSource(service)
    bundle = asyncio.run(diagnostic.assemble(diagnostic.query()))
    with zipfile.ZipFile(io.BytesIO(bundle.archive_bytes())) as archive:
        health = json.loads(archive.read("health/components.json"))
        assert health["inference_engines"] == payload["inference_engines"]
        contents = "\n".join(archive.read(name).decode() for name in archive.namelist())
    for forbidden in [
        "executable",
        "supported_architectures",
        "FX-SYS-03-private-db-path",
        "/opt/holm-runtimes",
        "/run/holm/agent.sock",
    ]:
        assert forbidden not in response.body.decode()
        assert forbidden not in contents
    for runtime in catalog.runtimes.values():
        assert str(runtime.executable) not in contents

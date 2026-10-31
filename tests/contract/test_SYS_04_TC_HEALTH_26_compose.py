"""Compose raw FX-HA output reaches the authenticated components API."""

from dataclasses import replace
from typing import cast

import pytest
from fixtures.asgi_client import NO_CONFIGURED_CREDENTIAL_ROWS
from fixtures.fake_host_agent import FX_HA_COMPOSE_VERSION
from holm_contracts.release import HostRequirements
from holm_control_api import (
    SYSTEM_COMPONENTS_PATH,
    AuthenticationService,
    DependencyCheck,
    SystemStatusService,
    create_app,
)
from holm_domain import UserRole
from holm_host_agent.host_info import COMPOSE_VERSION_COMMAND, collect_compose_version
from test_M1_07C_TC_HA_container_versions import RecordingRawCommandRunner
from test_M1_09A_TC_GPU_17_TC_HEALTH_system_api import (
    _Agent,
    _Authenticator,
    _Database,
    _database_snapshot,
    _get,
    _readiness_ok,
    _version,
)
from test_SYS_03_host_environment import _observation


@pytest.mark.parametrize(
    ("output", "minimum", "current", "status", "reason"),
    [
        ("5.1.0\n", "2.20", {"state": "known", "value": "5.1.0"}, "satisfied", "meets_minimum"),
        (
            "v2.32.4\n",
            "2.32.4",
            {"state": "known", "value": "2.32.4"},
            "satisfied",
            "meets_minimum",
        ),
        ("2.19.0\n", "2.20", {"state": "known", "value": "2.19.0"}, "unsatisfied", "below_minimum"),
        (
            None,
            "2.20",
            {"state": "unknown", "reason": "source_unavailable"},
            "unknown",
            "current_unavailable",
        ),
        ("5.1.0\n", None, {"state": "known", "value": "5.1.0"}, "unknown", "minimum_not_declared"),
        (
            "2.32.4-desktop.1\n",
            "2.20",
            {"state": "known", "value": "2.32.4-desktop.1"},
            "unknown",
            "version_unparseable",
        ),
    ],
)
def test_SYS_04_TC_HEALTH_26_compose_http(output, minimum, current, status, reason):
    outcome = (
        FileNotFoundError("missing")
        if output is None
        else replace(FX_HA_COMPOSE_VERSION, stdout=output).completed_process()
    )
    observation = _observation()
    observation.host_info.compose_version.CopyFrom(
        collect_compose_version(RecordingRawCommandRunner({COMPOSE_VERSION_COMMAND: outcome}))
    )
    service = SystemStatusService(
        agent=_Agent(observation),
        database=_Database(_database_snapshot()),
        version=_version(),
        host_requirements=HostRequirements(compose=minimum),
    )
    app = create_app(
        version_payload=_version(),
        readiness_checks=[DependencyCheck(name="database", probe=_readiness_ok)],
        credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
        authentication=cast(AuthenticationService, _Authenticator(UserRole.ADMIN)),
        system_status=service,
    )
    (response,) = _get(app, SYSTEM_COMPONENTS_PATH)
    assert response.status == 200
    compose = next(
        row for row in response.json()["host_environment"]["items"] if row["id"] == "compose"
    )
    assert compose == {
        "id": "compose",
        "current": current,
        "minimum": minimum,
        "status": status,
        "reason": reason,
    }

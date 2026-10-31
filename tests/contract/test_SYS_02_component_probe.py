"""FX-SYS-02: deterministic probes extend the existing System API fixtures."""

from __future__ import annotations

import asyncio
import logging
from typing import cast

import httpx
import pytest
from fixtures.asgi_client import NO_CONFIGURED_CREDENTIAL_ROWS, AsgiTestClient
from holm_control_api import (
    READY_PATH,
    SYSTEM_COMPONENTS_PATH,
    AuthenticationService,
    DependencyCheck,
    SystemStatusService,
    create_app,
)
from holm_control_api.component_probe import ComponentProbeClient
from holm_domain import UserRole
from test_M1_09A_TC_GPU_17_TC_HEALTH_system_api import (
    _Agent,
    _Authenticator,
    _Database,
    _database_snapshot,
    _four_observations,
    _readiness_ok,
    _version,
)


def _service(probes: ComponentProbeClient) -> SystemStatusService:
    return SystemStatusService(
        agent=_Agent(_four_observations()[-1]),
        database=_Database(_database_snapshot()),
        version=_version(),
        probes=probes,
    )


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("ready", "probe_ready"),
        ("route_snapshot_unavailable", "gateway_route_snapshot_unavailable"),
        ("ledger_unavailable", "gateway_ledger_unavailable"),
        ("timeout", "probe_unreachable"),
        ("connection", "probe_unreachable"),
        ("unconfigured", "no_signal_source"),
        ("redirect", "probe_unreachable"),
        ("bad_json", "probe_unreachable"),
        ("bad_shape", "probe_unreachable"),
        ("bad_reason", "probe_unreachable"),
        ("error", "probe_unreachable"),
    ],
)
def test_SYS_02_TC_HEALTH_12_14_15_28_probe_states_and_control_readiness(
    case: str, reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        assert request.method == "GET"
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        assert request.extensions["timeout"] == dict.fromkeys(
            ("connect", "read", "write", "pool"), 2.0
        )
        if case == "timeout":
            raise httpx.ReadTimeout("secret upstream exception")
        if case == "connection":
            raise httpx.ConnectError("secret upstream exception")
        if request.url.path == "/internal/version":
            return httpx.Response(200, json=_version().model_dump(mode="json"))
        if request.url.path == "/internal/health/live":
            return httpx.Response(200, json={"status": "live"})
        if case == "ready":
            return httpx.Response(200, json={"status": "ready"})
        if case == "redirect":
            return httpx.Response(302, headers={"Location": "http://redirect-secret.test"})
        if case == "bad_json":
            return httpx.Response(200, content=b"not-json secret")
        if case == "bad_shape":
            return httpx.Response(200, json={"status": "ready", "secret": "body"})
        if case == "error":
            return httpx.Response(500, json={"reason": "ledger_unavailable"})
        return httpx.Response(503, json={"reason": case})

    async def exercise() -> None:
        probes = ComponentProbeClient(
            gateway_url=None if case == "unconfigured" else "http://gateway-secret.test",
            edge_url=None if case == "unconfigured" else "http://edge-secret.test",
            transport=httpx.MockTransport(handler),
        )
        app = create_app(
            version_payload=_version(),
            readiness_checks=[DependencyCheck(name="database", probe=_readiness_ok)],
            credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
            authentication=cast(AuthenticationService, _Authenticator(UserRole.ADMIN)),
            system_status=_service(probes),
        )
        client = AsgiTestClient(app)
        async with client.lifespan():
            body = (await client.get(SYSTEM_COMPONENTS_PATH)).json()
            assert (await client.get(READY_PATH)).status == 200
        components = {item["id"]: item for item in body["components"]}
        gateway = components["Gateway"]
        assert gateway["signal_sources"] == ["probe"]
        assert gateway["reason"] == reason
        assert gateway["status"] == (
            "not_deployed"
            if case == "unconfigured"
            else "ready"
            if case == "ready"
            else "unavailable"
        )
        edge = components["Edge"]
        assert edge["reason"] == (
            "no_signal_source"
            if case == "unconfigured"
            else "probe_unreachable"
            if case in {"timeout", "connection"}
            else "probe_ready"
        )
        assert edge["status"] == (
            "not_deployed"
            if case == "unconfigured"
            else "unavailable"
            if case in {"timeout", "connection"}
            else "ready"
        )
        assert edge["version"] == {
            "state": "unknown",
            "reason": "not_deployed" if case == "unconfigured" else "not_collected",
        }
        if case in {"unconfigured", "timeout", "connection"}:
            assert gateway["version"] == {
                "state": "unknown",
                "reason": "not_deployed" if case == "unconfigured" else "source_unavailable",
            }
        else:
            assert gateway["version"] == {"state": "known", "value": _version().build_version}
        assert body["summary"]["abnormal_count"] == (
            0
            if case in {"ready", "unconfigured"}
            else 2
            if case in {"timeout", "connection"}
            else 1
        )
        assert len(seen) == (0 if case == "unconfigured" else 3)

    asyncio.run(exercise())
    assert "secret" not in caplog.text
    assert "http://" not in caplog.text


@pytest.mark.parametrize("version", [None, {}, {"build_version": "fake"}])
def test_SYS_02_TC_HEALTH_07_15_version_is_unknown_when_invalid(version: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/version":
            return httpx.Response(503 if version is None else 200, json=version)
        return httpx.Response(200, json={"status": "ready"})

    async def exercise() -> None:
        result = await _service(
            ComponentProbeClient(
                gateway_url="http://gateway.test", transport=httpx.MockTransport(handler)
            )
        ).read_components()
        gateway = next(item for item in result.components if item.id == "Gateway")
        assert gateway.status == "ready"
        assert gateway.version.model_dump() == {"state": "unknown", "reason": "source_unavailable"}

    asyncio.run(exercise())


def test_SYS_02_TC_HEALTH_06_14_database_agent_and_three_requests_run_concurrently() -> None:
    async def exercise() -> None:
        started: set[str] = set()
        all_started = asyncio.Event()

        async def delay(name: str) -> None:
            started.add(name)
            if len(started) == 5:
                all_started.set()
            # A sequential implementation cannot reach the barrier and times out.
            await all_started.wait()
            await asyncio.sleep(0.01)

        class Database(_Database):
            async def read(self):
                await delay("database")
                return await super().read()

        class Agent(_Agent):
            async def observe(self):
                await delay("agent")
                return await super().observe()

        async def handler(request: httpx.Request) -> httpx.Response:
            await delay(request.url.path)
            return httpx.Response(
                200,
                json=(
                    _version().model_dump(mode="json")
                    if request.url.path == "/internal/version"
                    else {"status": "live" if request.url.path.endswith("live") else "ready"}
                ),
            )

        service = SystemStatusService(
            agent=Agent(_four_observations()[-1]),
            database=Database(_database_snapshot()),
            version=_version(),
            probes=ComponentProbeClient(
                gateway_url="http://gateway.test",
                edge_url="http://edge.test",
                transport=httpx.MockTransport(handler),
            ),
        )
        async with asyncio.timeout(1):
            result = await service.read_components()
        assert len(started) == 5
        assert result.summary.abnormal_count == 0

    asyncio.run(exercise())


def test_SYS_02_TC_HEALTH_06_total_request_timeout_is_two_seconds() -> None:
    async def exercise() -> None:
        cancelled: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(request.url.path)
            raise AssertionError("unreachable")

        probes = ComponentProbeClient(
            gateway_url="http://gateway.test",
            edge_url="http://edge.test",
            transport=httpx.MockTransport(handler),
        )
        async with asyncio.timeout(3):
            gateway, edge = await probes.read()
        assert gateway is not None and gateway.reason == "probe_unreachable"
        assert edge is not None and edge.reason == "probe_unreachable"
        assert len(cancelled) == 3

    asyncio.run(exercise())


@pytest.mark.parametrize("payload", [b'{"status":"live","extra":1}', b'"live"', b"x" * 16385])
def test_SYS_02_TC_HEALTH_14_edge_rejects_malformed_or_oversized_body(payload: bytes) -> None:
    async def exercise() -> None:
        probes = ComponentProbeClient(
            edge_url="http://edge.test",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload)),
        )
        gateway, edge = await probes.read()
        assert gateway is None
        assert edge is not None and edge.reason == "probe_unreachable"

    asyncio.run(exercise())


def test_SYS_02_probe_rejects_embedded_credentials_without_sending_or_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def forbidden(_: httpx.Request) -> httpx.Response:
        raise AssertionError("credential-bearing URL must not be requested")

    async def exercise() -> None:
        probes = ComponentProbeClient(
            gateway_url="http://secret-user:secret-password@gateway.test",
            edge_url="http://edge.test?token=secret-query",
            transport=httpx.MockTransport(forbidden),
        )
        gateway, edge = await probes.read()
        assert gateway is not None and gateway.reason == "probe_unreachable"
        assert edge is not None and edge.reason == "probe_unreachable"

    asyncio.run(exercise())
    assert "secret" not in caplog.text
    assert "http://" not in caplog.text


def test_SYS_02_TC_HEALTH_10_probe_never_replays_response_cookie() -> None:
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.path] = request.headers.get("cookie")
        if request.url.path == "/internal/health/live":
            # FX-SYS-02: loopback services share a cookie host despite distinct ports.
            return httpx.Response(
                200,
                json={"status": "live"},
                headers={"Set-Cookie": "probe-session=must-not-forward; Path=/"},
            )
        return httpx.Response(
            200,
            json=(
                _version().model_dump(mode="json")
                if request.url.path == "/internal/version"
                else {"status": "ready"}
            ),
        )

    async def exercise() -> None:
        probes = ComponentProbeClient(
            gateway_url="http://probe.test:8401",
            edge_url="http://probe.test:8480",
            transport=httpx.MockTransport(handler),
        )
        gateway, edge = await probes.read()
        assert gateway is not None and gateway.reason == "probe_ready"
        assert edge is not None and edge.reason == "probe_ready"

    asyncio.run(exercise())
    assert seen == dict.fromkeys(
        ("/internal/health/live", "/internal/health/ready", "/internal/version")
    )

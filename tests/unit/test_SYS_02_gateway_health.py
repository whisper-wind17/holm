"""FX-SYS-02: reuse FX-M4-04 clock/snapshot and PROTO-06 Gateway assembly fixtures."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import httpx
import pytest
from holm_contracts import VersionPayload
from holm_domain import LedgerHealth
from holm_gateway.application import create_app
from holm_gateway.route_snapshot import RouteSnapshotManager
from holm_ledger import LedgerHealthMonitor
from test_M4_04_snapshot_unit import _Clock, _Source
from test_PROTO_06_gateway import VLLM_RUNTIME, _authenticator

VERSION = VersionPayload(
    build_version="1.0.0+sys02", commit="9" * 40, schema_version=1, runtime_compat_hash="8" * 64
)


@pytest.mark.parametrize(
    ("expired", "health", "code", "body"),
    [
        (False, LedgerHealth.AVAILABLE, 200, {"status": "ready"}),
        (False, LedgerHealth.DELAYED, 200, {"status": "ready"}),
        (True, LedgerHealth.AVAILABLE, 503, {"reason": "route_snapshot_unavailable"}),
        (False, LedgerHealth.UNAVAILABLE, 503, {"reason": "ledger_unavailable"}),
        (True, LedgerHealth.UNAVAILABLE, 503, {"reason": "route_snapshot_unavailable"}),
    ],
)
def test_SYS_02_TC_HEALTH_01_02_03_07_10_gateway_internal_routes(
    expired: bool, health: LedgerHealth, code: int, body: dict[str, str]
) -> None:
    async def exercise() -> None:
        clock = _Clock()
        source = _Source(clock)
        manager = RouteSnapshotManager(source, listener=None, clock=clock, max_age_seconds=60)
        await manager.refresh()
        source.fail = True  # Probes must not consult the database or Runtime.
        if expired:
            clock.value += 61
        authenticator, _ = _authenticator()
        authenticator = Mock(wraps=authenticator)
        ledger = Mock()
        metering = Mock()
        metrics = Mock()
        upstream = httpx.AsyncClient(
            transport=httpx.MockTransport(Mock(side_effect=AssertionError))
        )
        app = create_app(
            authenticator=authenticator,
            routes=manager,
            runtime_capabilities={"vllm": VLLM_RUNTIME.request_capabilities},
            upstream_client=upstream,
            request_ledger=ledger,
            ledger_health=cast(
                LedgerHealthMonitor, SimpleNamespace(health=health, latch_unavailable=Mock())
            ),
            snapshot_available=manager.ensure_available,
            version_payload=VERSION,
            metering=metering,
            service_metrics=metrics,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
        ) as client:
            live = await client.get("/internal/health/live")
            ready = await client.get("/internal/health/ready")
            version = await client.get("/internal/version")
        await upstream.aclose()
        assert (live.status_code, live.json()) == (200, {"status": "live"})
        assert (ready.status_code, ready.json()) == (code, body)
        assert version.status_code == 200
        assert VersionPayload.model_validate(version.json()) == VERSION
        assert source.loads == 1
        assert authenticator.mock_calls == []
        assert ledger.mock_calls == []
        assert metering.mock_calls == []
        assert metrics.mock_calls == []

    asyncio.run(exercise())

"""FX-FIX-26: context errors through Gateway, real SQLite WAL and PG replay."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from holm_gateway.application import create_app  # type: ignore[import-untyped]
from holm_gateway.routing import CachedRouteDirectory  # type: ignore[import-untyped]
from holm_gateway.service_metrics import (  # type: ignore[import-untyped]
    GatewayServiceMetrics,
)
from holm_ledger import EventType, SpoolWriter, open_ledger
from migration_test_support import upgrade_head
from test_M5_03_gateway_ledger_path import (
    VLLM_RUNTIME,
    _authenticator,
    _events,
    _ledger_path,
    _route,
    _RouteSource,
)
from test_M5_05_ledger_writer_postgres import _with_writer


@pytest.mark.parametrize("stream", [False, True])
def test_FIX_26_TC_GW_34_context_details_never_enter_storage_logs_or_metrics(
    tmp_path: Path,
    ephemeral_postgres: EphemeralPostgres,
    caplog: pytest.LogCaptureFixture,
    stream: bool,
) -> None:
    upgrade_head(ephemeral_postgres.engine)
    caplog.set_level(logging.DEBUG)
    sentinels = [
        "FX26_PRIVATE_RUNTIME",
        "max_context_tokens",
        # Seven digits: a four-digit sentinel collided with timestamp microseconds
        # (BRAND-02 ci, 819287) and made this privacy scan flaky.
        "1048573",
        "BadRequestError",
        "context_length_exceeded",
    ]
    payload = {
        "model": "m5-03-model",
        "stream": stream,
        "messages": [{"role": "user", "content": "FX26_PRIVATE_RUNTIME"}],
    }

    async def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "param": "input_tokens",
                    "type": "BadRequestError",
                    "message": (
                        "This model's maximum context length is 1048573 tokens. "
                        "FX26_PRIVATE_RUNTIME"
                    ),
                }
            },
        )

    async def scenario() -> None:
        assert VLLM_RUNTIME is not None
        path = _ledger_path(tmp_path)
        writer = SpoolWriter(path)
        writer.start()
        authenticator, key = _authenticator()
        metrics = GatewayServiceMetrics()
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as runtime:
                app = create_app(
                    authenticator=authenticator,
                    routes=CachedRouteDirectory(_RouteSource((_route(),))),
                    runtime_capabilities={"vllm": VLLM_RUNTIME.request_capabilities},
                    upstream_client=runtime,
                    request_ledger=writer,
                    service_metrics=metrics,
                )
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://gateway"
                ) as client:
                    response = await client.post(
                        "/v1/chat/completions",
                        json=payload,
                        headers={"Authorization": f"Bearer {key}"},
                    )
            assert response.status_code == 400
            assert response.json()["error"]["details"] == {
                "reason": "context_length_exceeded",
                "max_context_tokens": 1048573,
            }
            assert "FX26_PRIVATE_RUNTIME" not in response.text
            events = _events(path)
            assert events[-1].event_type is EventType.FAILED
            request_id = events[-1].request_id
            assert isinstance(request_id, UUID)
            for sentinel in sentinels:
                assert sentinel not in repr(events)
                assert sentinel not in repr(metrics.snapshot(sampled_at=datetime.now(UTC)))
                assert sentinel not in caplog.text
                assert sentinel not in repr([record.__dict__ for record in caplog.records])
                for target in (path, Path(f"{path}-wal")):
                    if target.exists():
                        assert sentinel.encode() not in target.read_bytes()
        finally:
            await writer.close()
        with open_ledger(path) as ledger:
            replayer, engine = await _with_writer(ephemeral_postgres, ledger)
            try:
                assert await replayer.replay_once() == len(events)
                assert ledger.list_unacked() == ()
            finally:
                await engine.dispose()
        with ephemeral_postgres.engine.connect() as connection:
            row = (
                connection.execute(
                    sa.text("SELECT * FROM usage.request_records WHERE request_id = :id"),
                    {"id": request_id},
                )
                .mappings()
                .one()
            )
        assert row["record_state"] == "failed"
        assert row["error_code"] == "invalid_request"
        for sentinel in sentinels:
            assert sentinel not in repr(dict(row))
            assert sentinel not in caplog.text
            assert sentinel not in repr([record.__dict__ for record in caplog.records])

    asyncio.run(scenario())

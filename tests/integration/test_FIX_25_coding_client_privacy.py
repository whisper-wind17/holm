"""FX-FIX-25: synthetic tool payloads through Gateway, real SQLite WAL and PG replay."""

from __future__ import annotations

import asyncio
import json
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
@pytest.mark.parametrize("unsupported", [False, True])
def test_FIX_25_TC_GW_33_client_content_never_enters_storage_logs_or_metrics(
    tmp_path: Path,
    ephemeral_postgres: EphemeralPostgres,
    caplog: pytest.LogCaptureFixture,
    stream: bool,
    unsupported: bool,
) -> None:
    upgrade_head(ephemeral_postgres.engine)
    caplog.set_level(logging.DEBUG)
    sentinels = [
        f"FX25_PRIVATE_{name}_4ec91"
        for name in (
            "FIELD",
            "VALUE",
            "TOOL",
            "PROPERTY",
            "ARGUMENT",
            "RESULT",
            "CALL_ID",
            "USER",
            "OUTPUT",
        )
    ]
    field, value, tool, prop, argument, result, call_id, user, output = sentinels
    schema = {
        "type": "object",
        "properties": {
            prop: {"type": "array", "items": {"anyOf": [{"enum": [argument]}, {"type": "null"}]}}
        },
        "required": [prop],
        "additionalProperties": False,
    }
    payload: dict[str, object] = {
        "model": "m5-03-model",
        "stream": stream,
        "messages": [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": tool,
                            "arguments": json.dumps({prop: [argument]}),
                        },
                    }
                ],
            },
            {"role": "tool", "content": result, "tool_call_id": call_id},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": tool,
                    "description": value,
                    "parameters": schema,
                    "strict": True,
                },
            }
        ],
        "tool_choice": "auto",
        "user": user,
    }
    if unsupported:
        payload[field] = value
    captured: list[dict[str, object]] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        choice: dict[str, object] = {"finish_reason": "stop"}
        choice["delta"] = {"content": output}
        body = {"choices": [choice], "usage": {"prompt_tokens": 4, "completion_tokens": 2}}
        return httpx.Response(
            200,
            content=b"data: " + json.dumps(body).encode() + b"\n\ndata: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
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
            assert response.status_code == (400 if unsupported else 200)
            if unsupported:
                assert response.json()["error"]["details"] == {"unsupported_fields": [field]}
                assert value not in response.text
                assert captured == []
            else:
                assert len(captured) == 1
                assert captured[0]["tools"] == payload["tools"]
                assert captured[0]["messages"] == payload["messages"]
                assert "user" not in captured[0]
            events = _events(path)
            assert events[-1].event_type is (
                EventType.REJECTED if unsupported else EventType.COMPLETED
            )
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
        assert row["record_state"] == ("rejected" if unsupported else "completed")
        for sentinel in sentinels:
            assert sentinel not in repr(dict(row))
            assert sentinel not in caplog.text
            assert sentinel not in repr([record.__dict__ for record in caplog.records])

    asyncio.run(scenario())

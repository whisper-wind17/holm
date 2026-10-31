"""FX-M4-19: real SQLite WAL and PostgreSQL replay; synthetic private body sentinels."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import cast
from uuid import UUID

import httpx
import pytest
import sqlalchemy as sa
import test_M5_03_gateway_ledger_path as f
import test_M5_04_request_record_upsert as projection
from conftest import EphemeralPostgres
from holm_gateway.application import create_app  # type: ignore[import-untyped]
from holm_gateway.routing import CachedRouteDirectory  # type: ignore[import-untyped]
from holm_ledger import ReceivedEvent, RequestOperation, RequestProtocol, SpoolWriter
from holm_protocols import AdmissionController  # type: ignore[import-untyped]
from holm_security import ValidatedApiKey
from migration_test_support import upgrade_head
from sqlalchemy.exc import IntegrityError
from starlette.types import Message, Scope

SENTINEL = "M4_19_PRIVATE_BODY_SENTINEL"


@pytest.mark.parametrize("outcome", ["completed", "forbidden", "limited", "failed", "aborted"])
def test_M4_19_TC_GW_46_request_records_and_zero_body(
    tmp_path: Path,
    ephemeral_postgres: EphemeralPostgres,
    outcome: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    upgrade_head(ephemeral_postgres.engine)

    async def scenario() -> None:
        admission = AdmissionController(8)
        occupied = None
        if outcome in {"limited", "aborted"}:
            occupied = await admission.acquire(
                f.INSTANCE_ID, user_id=f.USER_ID, service_id=f.SERVICE_ID, limits=lambda: (1, 1)
            )
            assert occupied is not None
        auth, key = f._authenticator()

        class Authenticator:
            async def authenticate(self, credential: str | None) -> ValidatedApiKey:
                principal = cast(ValidatedApiKey, await auth.authenticate(credential))
                return replace(
                    principal,
                    authorized_service_ids=frozenset()
                    if outcome == "forbidden"
                    else principal.authorized_service_ids,
                    service_limits={f.SERVICE_ID: (1, 1 if outcome == "limited" else None)},
                )

        async def upstream(request: httpx.Request) -> httpx.Response:
            if outcome == "failed":
                raise httpx.ConnectError(SENTINEL)
            return httpx.Response(
                200,
                content=b"data: "
                + json.dumps(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "reasoning_content": SENTINEL,
                                    "content": SENTINEL,
                                    "tool_calls": [
                                        {
                                            "id": "call_private",
                                            "function": {
                                                "name": "read",
                                                "arguments": json.dumps({"path": SENTINEL}),
                                            },
                                        }
                                    ],
                                },
                                "finish_reason": "tool_calls",
                            }
                        ],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 7},
                    }
                ).encode()
                + b"\n\ndata: [DONE]\n\n",
            )

        path = f._ledger_path(tmp_path)
        writer = SpoolWriter(path)
        writer.start()
        assert f.VLLM_RUNTIME is not None
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as runtime:
                app = create_app(
                    authenticator=Authenticator(),
                    routes=CachedRouteDirectory(f._RouteSource((f._route(),))),
                    runtime_capabilities={"vllm": f.VLLM_RUNTIME.request_capabilities},
                    upstream_client=runtime,
                    admission=admission,
                    request_ledger=writer,
                )
                payload = {
                    "model": "m5-03-model",
                    "max_tokens": 32,
                    "messages": [{"role": "user", "content": SENTINEL}],
                }
                if outcome == "aborted":
                    sent = False
                    disconnected = asyncio.Event()
                    responses: list[Message] = []

                    async def receive() -> Message:
                        nonlocal sent
                        if not sent:
                            sent = True
                            return {
                                "type": "http.request",
                                "body": json.dumps(payload).encode(),
                                "more_body": False,
                            }
                        await disconnected.wait()
                        return {"type": "http.disconnect"}

                    async def send(message: Message) -> None:
                        responses.append(message)

                    scope: Scope = {
                        "type": "http",
                        "asgi": {"version": "3.0"},
                        "http_version": "1.1",
                        "method": "POST",
                        "scheme": "http",
                        "path": "/v1/messages",
                        "raw_path": b"/v1/messages",
                        "query_string": b"",
                        "root_path": "",
                        "headers": [
                            (b"x-api-key", key.encode()),
                            (b"content-type", b"application/json"),
                        ],
                        "client": ("127.0.0.1", 1234),
                        "server": ("gateway", 80),
                    }
                    task = asyncio.create_task(app(scope, receive, send))
                    async with asyncio.timeout(3):
                        while not (await admission.grant_counts())[1]:
                            await asyncio.sleep(0.001)
                    assert not responses
                    disconnected.set()
                    await asyncio.wait_for(task, 3)
                else:
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
                    ) as client:
                        response = await client.post(
                            "/v1/messages", headers={"x-api-key": key}, json=payload
                        )
                    assert (
                        response.status_code
                        == {"completed": 200, "forbidden": 403, "limited": 429, "failed": 503}[
                            outcome
                        ]
                    )
                    if outcome == "limited":
                        assert response.headers["Retry-After"]
                        assert response.json()["error"]["type"] == "rate_limit_error"
                events = f._events(path)
                assert isinstance(events[0], ReceivedEvent)
                assert (events[0].protocol, events[0].operation, events[0].endpoint) == (
                    RequestProtocol.ANTHROPIC,
                    RequestOperation.MESSAGES,
                    "/v1/messages",
                )
                expected = "rejected" if outcome in {"forbidden", "limited"} else outcome
                assert events[-1].event_type.value.lower() == expected
                await projection._upsert(ephemeral_postgres, events)
                await projection._upsert(ephemeral_postgres, events)
                with ephemeral_postgres.engine.connect() as connection:
                    row = connection.execute(
                        sa.text(
                            "SELECT protocol, operation, endpoint, record_state, "
                            "input_tokens, output_tokens "
                            "FROM usage.request_records WHERE request_id = :id"
                        ),
                        {"id": events[0].request_id},
                    ).one()
                    assert row[:4] == ("anthropic", "messages", "/v1/messages", expected)
                    if outcome == "completed":
                        assert row[4:] == (5, 7)
                    else:
                        assert row[4:] == (None, None)
                    persisted = connection.execute(
                        sa.text(
                            "SELECT row_to_json(r)::text FROM usage.request_records r "
                            "WHERE request_id = :id"
                        ),
                        {"id": events[0].request_id},
                    ).scalar_one()
                    assert SENTINEL not in persisted
                assert SENTINEL not in json.dumps(
                    [event.model_dump(mode="json") for event in events]
                )
                for target in (path, Path(f"{path}-wal")):
                    if target.exists():
                        assert SENTINEL.encode() not in target.read_bytes()
        finally:
            if occupied is not None:
                await occupied.release()
            await writer.close()
        assert await admission.active_counts() == (0, {})
        expected_minutes = (
            {(f.USER_ID, f.SERVICE_ID): 1} if outcome in {"completed", "failed"} else {}
        )
        assert await admission.grant_counts() == ({}, {}, expected_minutes)

    asyncio.run(scenario())
    assert SENTINEL not in caplog.text


def test_M4_19_TC_GW_46_migration_retains_closed_vocabulary(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    upgrade_head(ephemeral_postgres.engine)
    request_id = UUID("41900000-0000-4000-8000-000000000046")
    asyncio.run(projection._upsert(ephemeral_postgres, (projection._received(request_id),)))
    for column in ("protocol", "operation"):
        # Updating a proven-valid row isolates the CHECK from unrelated NOT NULL
        # or partition failures. Verify the actual rejected constraint as well.
        with (
            pytest.raises(IntegrityError) as failure,
            ephemeral_postgres.engine.begin() as connection,
        ):
            connection.execute(
                sa.text(
                    f"UPDATE usage.request_records SET {column} = 'unknown' WHERE request_id = :id"
                ),
                {"id": request_id},
            )
        assert f"ck_request_records_{column}" in str(failure.value.orig)

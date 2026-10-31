"""M4-17 / FX-CALL-LIMITS: real SQLite and PG, HTTP disconnect and body sentinel."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import httpx
import sqlalchemy as sa
import test_M5_03_gateway_ledger_path as f
import test_M5_04_request_record_upsert as projection
from conftest import EphemeralPostgres
from holm_gateway.application import create_app  # type: ignore[import-untyped]
from holm_gateway.routing import CachedRouteDirectory  # type: ignore[import-untyped]
from holm_ledger import AbortedEvent, EventType, SpoolWriter
from holm_protocols import AdmissionController  # type: ignore[import-untyped]
from holm_security import ValidatedApiKey
from migration_test_support import upgrade_head
from starlette.types import Message, Scope


def test_M4_17_TC_GW_35_38_http_queued_disconnect_persists_aborted_without_body(
    tmp_path: Path,
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    upgrade_head(ephemeral_postgres.engine)

    async def scenario() -> None:
        admission = AdmissionController(8)
        occupied = await admission.acquire(
            f.INSTANCE_ID, user_id=f.USER_ID, service_id=f.SERVICE_ID, limits=lambda: (1, 3)
        )
        assert occupied is not None
        authenticator, key = f._authenticator()

        class LimitedAuthenticator:
            async def authenticate(self, credential: str | None) -> ValidatedApiKey:
                return replace(
                    cast(ValidatedApiKey, await authenticator.authenticate(credential)),
                    service_limits={f.SERVICE_ID: (1, 3)},
                )

        async def upstream(request: httpx.Request) -> httpx.Response:
            raise AssertionError("queued call must not reach Runtime")

        path = f._ledger_path(tmp_path)
        writer = SpoolWriter(path)
        writer.start()
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            assert f.VLLM_RUNTIME is not None
            app = create_app(
                authenticator=LimitedAuthenticator(),
                routes=CachedRouteDirectory(f._RouteSource((f._route(),))),
                runtime_capabilities={"vllm": f.VLLM_RUNTIME.request_capabilities},
                upstream_client=client,
                admission=admission,
                request_ledger=writer,
            )
            body = json.dumps(
                {"model": "m5-03-model", "messages": [{"role": "user", "content": f.SENTINEL}]}
            ).encode()
            sent = False
            disconnected = asyncio.Event()
            responses: list[Message] = []

            async def receive() -> Message:
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
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
                "path": "/v1/chat/completions",
                "raw_path": b"/v1/chat/completions",
                "query_string": b"",
                "root_path": "",
                "headers": [
                    (b"authorization", f"Bearer {key}".encode()),
                    (b"content-type", b"application/json"),
                ],
                "client": ("127.0.0.1", 1234),
                "server": ("gateway", 80),
            }
            try:
                task = asyncio.create_task(app(scope, receive, send))
                async with asyncio.timeout(2):
                    while not (await admission.grant_counts())[1]:
                        await asyncio.sleep(0.001)
                assert not responses
                disconnected.set()
                await asyncio.wait_for(task, 2)
                events = f._events(path)
                assert [event.event_type for event in events] == [
                    EventType.RECEIVED,
                    EventType.ABORTED,
                ]
                aborted = events[-1]
                assert isinstance(aborted, AbortedEvent)
                assert aborted.user_id == f.USER_ID and aborted.service_id == f.SERVICE_ID
                assert aborted.input_tokens is None and aborted.output_tokens is None
                await projection._upsert(ephemeral_postgres, events)
                with ephemeral_postgres.engine.connect() as connection:
                    row = connection.execute(
                        sa.text(
                            "SELECT record_state, user_id, service_id, input_tokens, "
                            "output_tokens FROM usage.request_records WHERE request_id = "
                            ":id"
                        ),
                        {"id": aborted.request_id},
                    ).one()
                assert row == ("aborted", f.USER_ID, f.SERVICE_ID, None, None)
                for target in (path, Path(f"{path}-wal")):
                    if target.exists():
                        assert f.SENTINEL.encode() not in target.read_bytes()
                assert f.SENTINEL not in json.dumps(
                    [event.model_dump(mode="json") for event in events]
                )
            finally:
                await occupied.release()
                await writer.close()
            assert await admission.active_counts() == (0, {})
            assert await admission.grant_counts() == ({}, {}, {})

    asyncio.run(scenario())

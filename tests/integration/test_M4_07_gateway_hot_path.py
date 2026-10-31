"""M4-07 integration guard: authentication and routing stay off PostgreSQL."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import MappingProxyType
from typing import cast

import httpx
import test_M4_04_snapshot_integration as m404
from conftest import EphemeralPostgres
from holm_gateway.application import create_app  # type: ignore[import-untyped]
from holm_gateway.authentication import ApiKeyAuthenticator  # type: ignore[import-untyped]
from holm_gateway.route_snapshot import (  # type: ignore[import-untyped]
    SnapshotApiKeyValidationLookup,
)
from holm_persistence import AsyncSessionFactory
from holm_protocols import AdmissionController  # type: ignore[import-untyped]
from holm_runtimes import RuntimeRequestCapabilities
from holm_security import ApiKeyPepperKeyring
from starlette.types import ASGIApp


async def _request(app: ASGIApp, key: str, model: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        return await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "messages": []},
        )


def test_M4_07_loaded_snapshot_serves_200_401_403_404_and_overload_without_postgres(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    m404._seed(ephemeral_postgres)

    async def scenario() -> None:
        async with m404._environment(ephemeral_postgres, listen=False) as environment:
            # Freeze the already-loaded Snapshot so its normal background proof
            # loop cannot be mistaken for a request-path database access.
            await environment.manager.stop()
            session_attempts = 0

            def explode() -> object:
                nonlocal session_attempts
                session_attempts += 1
                raise AssertionError("Gateway hot path opened a PostgreSQL session")

            environment.snapshot_source._session_factory = cast(AsyncSessionFactory, explode)
            original = environment.manager.snapshot

            ok = await _request(environment.gateway, environment.plaintext_key, "m4-04-model")
            invalid = await _request(environment.gateway, "invalid-key", "m4-04-model")

            member = original.users_by_id[m404.MEMBER_ID]
            environment.manager._snapshot = replace(
                original,
                users_by_id=MappingProxyType(
                    {
                        **original.users_by_id,
                        m404.MEMBER_ID: replace(member, authorized_service_ids=frozenset()),
                    }
                ),
            )
            forbidden = await _request(
                environment.gateway, environment.plaintext_key, "m4-04-model"
            )

            environment.manager._snapshot = original
            missing = await _request(environment.gateway, environment.plaintext_key, "missing")

            admission = AdmissionController(1, 1)
            occupied = await admission.try_acquire(m404.INSTANCE_A)
            assert occupied is not None
            overloaded_gateway = create_app(
                authenticator=ApiKeyAuthenticator(
                    lookup=SnapshotApiKeyValidationLookup(environment.manager),
                    keyring=ApiKeyPepperKeyring(current_key_id="m4-04", keys={"m4-04": b"K" * 32}),
                    availability_check=environment.manager.ensure_available,
                ),
                routes=environment.manager,
                runtime_capabilities={
                    "vllm": RuntimeRequestCapabilities(
                        openai_chat_completions=frozenset({"model", "messages"})
                    )
                },
                upstream_client=environment.upstream,
                admission=admission,
            )
            try:
                overloaded = await _request(
                    overloaded_gateway, environment.plaintext_key, "m4-04-model"
                )
            finally:
                await occupied.release()

            assert ok.status_code == 200
            assert invalid.status_code == 401
            assert invalid.json()["error"]["code"] == "invalid_api_key"
            assert forbidden.status_code == 403
            assert forbidden.json()["error"]["code"] == "service_not_authorized"
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "model_not_found"
            assert overloaded.status_code == 503
            assert overloaded.json()["error"]["code"] == "gateway_overloaded"
            assert session_attempts == 0

    asyncio.run(scenario())

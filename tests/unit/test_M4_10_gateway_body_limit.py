"""M4-10 Gateway request body default aligned with TDD 9.3 (16 MiB)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import UUID

import httpx
from holm_domain import ModelInstanceStatus
from holm_gateway.__main__ import _parse_args
from holm_gateway.application import create_app
from holm_gateway.authentication import ApiKeyAuthenticator
from holm_gateway.configuration import GatewayConfiguration
from holm_gateway.routing import CachedRouteDirectory, GatewayRoute
from holm_runtimes import load_runtime_compatibility
from holm_security import ApiKeyPepperKeyring, ApiKeyValidationSnapshot, issue_api_key

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[2]
MIB: Final = 1024 * 1024
SERVICE_ID: Final = UUID("00000000-0000-7000-8000-000000041001")
INSTANCE_ID: Final = UUID("00000000-0000-7000-8000-000000041002")
USER_ID: Final = UUID("00000000-0000-7000-8000-000000041003")
KEY_ID: Final = UUID("00000000-0000-7000-8000-000000041004")


class _RouteSource:
    async def list_routes(self) -> tuple[GatewayRoute, ...]:
        return (
            GatewayRoute(
                service_id=SERVICE_ID,
                instance_id=INSTANCE_ID,
                public_model_name="qwen-m410",
                enabled=True,
                instance_status=ModelInstanceStatus.RUNNING.value,
                runtime_name="vllm",
                internal_endpoint="private-runtime:8000",
                internal_model_name="mi_000000041006",
            ),
        )


def _authenticator() -> tuple[ApiKeyAuthenticator, str]:
    keyring = ApiKeyPepperKeyring(current_key_id="m4-10", keys={"m4-10": b"K" * 32})
    issued = issue_api_key(keyring)

    async def lookup(lookup_id: str) -> ApiKeyValidationSnapshot | None:
        if lookup_id != issued.lookup_id:
            return None
        return ApiKeyValidationSnapshot(
            user_id=USER_ID,
            api_key_id=KEY_ID,
            pepper_key_id=issued.pepper_key_id,
            secret_hmac=issued.secret_hmac,
            revoked=False,
            expired_status=False,
            expires_at=None,
            user_active=True,
            scoped_service_ids=frozenset(),
            database_now=datetime(2026, 9, 17, tzinfo=UTC),
            user_is_admin=True,
            authorized_service_ids=frozenset({SERVICE_ID}),
        )

    return ApiKeyAuthenticator(lookup=lookup, keyring=keyring), issued.plaintext


def test_M4_10_gateway_body_default_is_16_mib() -> None:
    assert GatewayConfiguration().max_request_body_bytes == 16 * MIB
    args = _parse_args(
        [
            "--database-url-file",
            "/tmp/database-url",
            "--api-key-pepper-keyring-file",
            "/tmp/keyring",
            "--runtime-compatibility",
            "/tmp/runtime-compat.yaml",
        ]
    )
    assert args.max_request_body_bytes == 16 * MIB


def test_M4_10_gateway_accepts_body_just_under_16_mib() -> None:
    forwarded: list[int] = []

    async def runtime(request: httpx.Request) -> httpx.Response:
        forwarded.append(len(await request.aread()))
        return httpx.Response(
            200,
            content=b"data: "
            + json.dumps(
                {
                    "model": "mi_000000041006",
                    "choices": [{"finish_reason": "stop", "delta": {"content": "ok"}}],
                }
            ).encode()
            + b"\n\ndata: [DONE]\n\n",
        )

    async def scenario() -> None:
        runtime_compat = load_runtime_compatibility(REPOSITORY_ROOT / "runtime-compat.yaml")
        vllm = runtime_compat.runtime("vllm")
        assert vllm is not None
        authenticator, key = _authenticator()
        upstream = httpx.AsyncClient(transport=httpx.MockTransport(runtime), timeout=None)
        app = create_app(
            authenticator=authenticator,
            routes=CachedRouteDirectory(_RouteSource(), ttl_seconds=2),
            runtime_capabilities={"vllm": vllm.request_capabilities},
            upstream_client=upstream,
        )
        content = "a" * (16 * MIB - 1024)
        body = json.dumps(
            {"model": "qwen-m410", "messages": [{"role": "user", "content": content}]}
        ).encode()
        assert 15 * MIB < len(body) < 16 * MIB
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as gateway:
                response = await gateway.post(
                    "/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    content=body,
                )
        finally:
            await upstream.aclose()
        assert response.status_code == 200, response.text
        assert len(forwarded) == 1

    asyncio.run(scenario())

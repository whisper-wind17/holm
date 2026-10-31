"""M4-03D / FX-M4-03D: PROTO-05 accounts and services on isolated real PG."""

from __future__ import annotations

import asyncio
import json
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from holm_gateway.application import (  # type: ignore[import-untyped]
    create_app as create_gateway_app,
)
from holm_gateway.authentication import ApiKeyAuthenticator  # type: ignore[import-untyped]
from holm_gateway.route_snapshot import (  # type: ignore[import-untyped]
    PostgresRouteSnapshotSource,
    RouteSnapshotManager,
    SnapshotApiKeyValidationLookup,
)
from holm_gateway.routing import CachedRouteDirectory  # type: ignore[import-untyped]
from holm_persistence import PostgresApiKeyValidationLookup
from holm_security import validate_api_key
from migration_test_support import upgrade_head
from test_PROTO_05_services_api_keys import (
    ADMIN_USERNAME,
    INSTANCE_1,
    MEMBER_USERNAME,
    VLLM_CAPABILITIES,
    _create_accounts,
    _create_service,
    _environment,
    _gateway_route,
    _GatewayRoutes,
    _login,
    _seed_instances,
)

PATH = "/api/v1/me/api-keys"


@pytest.fixture(autouse=True)
def _database(ephemeral_postgres: EphemeralPostgres) -> None:
    upgrade_head(ephemeral_postgres.engine)
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(
            sa.text(
                "TRUNCATE core.ai_services, core.models, core.users, "
                "core.audit_events, core.domain_events CASCADE"
            )
        )
    _seed_instances(ephemeral_postgres.engine)


@pytest.mark.parametrize("snapshot", [False, True])
def test_M4_03D_TC_SEC_11_12_admin_requires_grant_and_scope(
    ephemeral_postgres: EphemeralPostgres,
    snapshot: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as env:
            ids = await _create_accounts(env)
            admin = await _login(env, ADMIN_USERNAME)
            admin_id = ids[ADMIN_USERNAME]
            service_id = await _create_service(
                env, admin, name="m403d-public", instance_id=INSTANCE_1
            )
            permission_path = f"/api/v1/users/{admin_id}/permissions"
            denied = await env.client.request(
                "POST",
                PATH,
                cookies=admin,
                json_body={"name": "ungranted", "service_ids": [str(service_id)]},
            )
            assert denied.status == 403
            assert denied.json()["error"]["code"] == "service_not_authorized"
            created = await env.client.request(
                "POST", PATH, cookies=admin, json_body={"name": "all grants"}
            )
            assert created.status == 201
            plaintext = created.json()["api_key"]
            assert (await env.client.get(PATH, cookies=admin)).json()["items"][0][
                "effective_services"
            ] == []
            detail = await env.client.get(f"/api/v1/users/{admin_id}", cookies=admin)
            assert detail.json()["authorized_services"] == []
            assert detail.json()["authorized_service_count"] == 0
            manager = RouteSnapshotManager(
                PostgresRouteSnapshotSource(env.session_factory), listener=None
            )
            await manager.refresh()
            lookup = (
                SnapshotApiKeyValidationLookup(manager)
                if snapshot
                else PostgresApiKeyValidationLookup(env.session_factory)
            )
            upstream_calls: list[str] = []

            async def upstream(request: httpx.Request) -> httpx.Response:
                upstream_calls.append(request.url.path)
                return httpx.Response(
                    200,
                    content=b"data: "
                    + json.dumps(
                        {
                            "id": "m403d",
                            "object": "chat.completion.chunk",
                            "model": "m403d-public",
                            "choices": [],
                        }
                    ).encode()
                    + b"\n\ndata: [DONE]\n\n",
                )

            async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as backend:
                gateway = create_gateway_app(
                    authenticator=ApiKeyAuthenticator(lookup=lookup, keyring=env.keyring),
                    routes=CachedRouteDirectory(
                        _GatewayRoutes((_gateway_route(service_id, "m403d-public"),)), ttl_seconds=2
                    ),
                    runtime_capabilities={"vllm": VLLM_CAPABILITIES},
                    upstream_client=backend,
                )
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=gateway), base_url="http://gateway"
                ) as client:

                    async def call(expected: int) -> None:
                        response = await client.post(
                            "/v1/chat/completions",
                            headers={"Authorization": f"Bearer {plaintext}"},
                            json={"model": "m403d-public", "messages": []},
                        )
                        assert response.status_code == expected
                        if expected == 403:
                            assert response.json()["error"]["code"] == "service_not_authorized"
                        validated = await validate_api_key(
                            plaintext, lookup=lookup, keyring=env.keyring
                        )
                        assert validated.allows_service(service_id) is (expected == 200)

                    await call(403)
                    assert upstream_calls == []
                    grant = await env.client.request(
                        "PUT",
                        permission_path,
                        cookies=admin,
                        json_body={"service_ids": [str(service_id)]},
                    )
                    assert grant.status == 200
                    await manager.refresh()
                    await call(200)
                    scoped = await env.client.request(
                        "POST",
                        PATH,
                        cookies=admin,
                        json_body={"name": "scoped", "service_ids": [str(service_id)]},
                    )
                    assert scoped.status == 201
                    items = (await env.client.get(PATH, cookies=admin)).json()["items"]
                    assert all(
                        [entry["id"] for entry in key["effective_services"]] == [str(service_id)]
                        for key in items
                    )
                    detail = (
                        await env.client.get(f"/api/v1/users/{admin_id}", cookies=admin)
                    ).json()
                    assert detail["authorized_service_count"] == 1
                    assert [entry["id"] for entry in detail["authorized_services"]] == [
                        str(service_id)
                    ]
                    listed = (await env.client.get("/api/v1/users", cookies=admin)).json()["items"]
                    assert (
                        next(item for item in listed if item["id"] == str(admin_id))[
                            "authorized_service_count"
                        ]
                        == 1
                    )
                    revoke = await env.client.request(
                        "PUT", permission_path, cookies=admin, json_body={"service_ids": []}
                    )
                    assert revoke.status == 200
                    await manager.refresh()
                    await call(403)
                    plaintext = scoped.json()["api_key"]
                    await call(403)
                    assert len(upstream_calls) == 1
                    assert all(
                        key["effective_services"] == []
                        for key in (await env.client.get(PATH, cookies=admin)).json()["items"]
                    )
            assert plaintext not in caplog.text
            assert created.json()["api_key"] not in caplog.text

    asyncio.run(scenario())


def test_M4_03D_grant_targets_include_disabled_admins_and_reject_deleted_or_missing(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as env:
            ids = await _create_accounts(env)
            admin = await _login(env, ADMIN_USERNAME)
            service_id = await _create_service(
                env, admin, name="m403d-targets", instance_id=INSTANCE_1
            )
            created = await env.client.request(
                "POST",
                "/api/v1/users",
                cookies=admin,
                json_body={"username": "other-admin", "role": "admin"},
            )
            assert created.status == 201
            other_id = UUID(created.json()["id"])
            for target in (other_id, ids[MEMBER_USERNAME]):
                assert (
                    await env.client.request(
                        "POST", f"/api/v1/users/{target}:disable", cookies=admin
                    )
                ).status == 200
                assert (
                    await env.client.request(
                        "PUT",
                        f"/api/v1/users/{target}/permissions",
                        cookies=admin,
                        json_body={"service_ids": [str(service_id)]},
                    )
                ).status == 200
            by_service = f"/api/v1/services/{service_id}/permissions"
            assert (
                await env.client.request(
                    "PUT",
                    by_service,
                    cookies=admin,
                    json_body={
                        "user_ids": [
                            str(other_id),
                            str(ids[ADMIN_USERNAME]),
                            str(ids[MEMBER_USERNAME]),
                        ]
                    },
                )
            ).status == 200
            assert (
                await env.client.request("POST", f"/api/v1/users/{other_id}:delete", cookies=admin)
            ).status == 200
            for target in (other_id, uuid4()):
                rejected = await env.client.request(
                    "PUT",
                    f"/api/v1/users/{target}/permissions",
                    cookies=admin,
                    json_body={"service_ids": [str(service_id)]},
                )
                assert rejected.status == 400
                rejected = await env.client.request(
                    "PUT", by_service, cookies=admin, json_body={"user_ids": [str(target)]}
                )
                assert rejected.status == 400
            rejected = await env.client.request(
                "PUT",
                f"/api/v1/users/{ids[ADMIN_USERNAME]}/permissions",
                cookies=admin,
                json_body={"service_ids": [str(uuid4())]},
            )
            assert rejected.status == 400

    asyncio.run(scenario())

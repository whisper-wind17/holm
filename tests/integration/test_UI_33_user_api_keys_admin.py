"""UI-33: administrator view/revoke of a user's API keys and effective key counts."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from holm_persistence import (
    API_KEYS,
    AUDIT_EVENTS,
    PostgresApiKeyValidationLookup,
)
from holm_security import ApiKeyValidationError, validate_api_key
from migration_test_support import upgrade_head
from test_PROTO_05_services_api_keys import (
    ADMIN_USERNAME,
    MEMBER_USERNAME,
    _create_accounts,
    _environment,
    _login,
    _seed_instances,
)

ME_KEYS = "/api/v1/me/api-keys"
DETAIL_KEY_FIELDS = {
    "id",
    "name",
    "display_prefix",
    "last4",
    "status",
    "expires_at",
    "last_used_at",
}


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


def test_UI_33_admin_detail_keys_revoke_and_effective_count(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as env:
            ids = await _create_accounts(env)
            admin = await _login(env, ADMIN_USERNAME)
            member = await _login(env, MEMBER_USERNAME)
            member_id = ids[MEMBER_USERNAME]
            admin_id = ids[ADMIN_USERNAME]
            keys: dict[str, UUID] = {}
            plaintexts: dict[str, str] = {}
            for name in ("one", "two", "revoked", "expired"):
                response = await env.client.request(
                    "POST", ME_KEYS, cookies=member, json_body={"name": name}
                )
                assert response.status == 201
                keys[name] = UUID(response.json()["id"])
                plaintexts[name] = response.json()["api_key"]
            admin_key = await env.client.request(
                "POST", ME_KEYS, cookies=admin, json_body={"name": "admin-own"}
            )
            assert admin_key.status == 201
            admin_key_id = UUID(admin_key.json()["id"])
            assert (
                await env.client.request(
                    "POST", f"{ME_KEYS}/{keys['revoked']}:revoke", cookies=member
                )
            ).status == 200
            async with env.session_factory() as session, session.begin():
                await session.execute(
                    sa.update(API_KEYS)
                    .where(API_KEYS.c.id == keys["expired"])
                    .values(
                        created_at=datetime.now(UTC) - timedelta(days=2),
                        expires_at=datetime.now(UTC) - timedelta(days=1),
                    )
                )

            # Target 1 + 3: seven keys, expired shows Expired, only live keys counted.
            detail = await env.client.get(f"/api/v1/users/{member_id}", cookies=admin)
            assert detail.status == 200
            body = detail.json()
            assert {item["name"]: item["status"] for item in body["api_keys"]} == {
                "one": "Active",
                "two": "Active",
                "revoked": "Revoked",
                "expired": "Expired",
            }
            assert all(set(item) == DETAIL_KEY_FIELDS for item in body["api_keys"])
            assert body["api_key_count"] == 2
            for plaintext in plaintexts.values():
                assert plaintext not in detail.text
            listed = (await env.client.get("/api/v1/users", cookies=admin)).json()["items"]
            assert {item["id"]: item["api_key_count"] for item in listed}[str(member_id)] == 2

            # Permission and not-found failures.
            path = f"/api/v1/users/{member_id}/api-keys/{keys['one']}:revoke"
            assert (await env.client.request("POST", path, cookies=member)).status == 403
            foreign = await env.client.request(
                "POST",
                f"/api/v1/users/{member_id}/api-keys/{admin_key_id}:revoke",
                cookies=admin,
            )
            assert foreign.status == 404
            assert foreign.json()["error"]["code"] == "api_key_not_found"
            missing_user = await env.client.request(
                "POST",
                f"/api/v1/users/{uuid4()}/api-keys/{keys['one']}:revoke",
                cookies=admin,
            )
            assert missing_user.status == 404
            assert missing_user.json()["error"]["code"] == "user_not_found"

            # Disabled users' keys are still listed as live and can be revoked by admin.
            assert (
                await env.client.request(
                    "POST", f"/api/v1/users/{member_id}:disable", cookies=admin
                )
            ).status == 200
            after_disable = await env.client.get(f"/api/v1/users/{member_id}", cookies=admin)
            assert after_disable.json()["api_key_count"] == 2

            lookup = PostgresApiKeyValidationLookup(env.session_factory)
            revoked = await env.client.request("POST", path, cookies=admin)
            assert revoked.status == 200
            assert set(revoked.json()) == DETAIL_KEY_FIELDS
            assert revoked.json()["id"] == str(keys["one"])
            assert revoked.json()["status"] == "Revoked"
            with pytest.raises(ApiKeyValidationError):
                await validate_api_key(plaintexts["one"], lookup=lookup, keyring=env.keyring)
            again = await env.client.request("POST", path, cookies=admin)
            assert again.status == 200
            assert again.json()["status"] == "Revoked"

            async with env.session_factory() as session:
                audit = (
                    await session.execute(
                        sa.select(AUDIT_EVENTS.c.actor_id, AUDIT_EVENTS.c.object_type).where(
                            AUDIT_EVENTS.c.action == "api_key.change",
                            AUDIT_EVENTS.c.object_id == keys["one"],
                            AUDIT_EVENTS.c.actor_id == admin_id,
                        )
                    )
                ).all()
            assert audit
            assert all(row.object_type == "api_key" for row in audit)
            final = await env.client.get(f"/api/v1/users/{member_id}", cookies=admin)
            assert final.json()["api_key_count"] == 1

            # Deleted users answer user_not_found.
            assert (
                await env.client.request("POST", f"/api/v1/users/{member_id}:delete", cookies=admin)
            ).status == 200
            gone = await env.client.request(
                "POST",
                f"/api/v1/users/{member_id}/api-keys/{keys['two']}:revoke",
                cookies=admin,
            )
            assert gone.status == 404
            assert gone.json()["error"]["code"] == "user_not_found"

    asyncio.run(scenario())

"""UI-36: FX-PROTO-05 accounts, detail creation time and deleted-user history."""

from __future__ import annotations

import asyncio
from datetime import datetime

from conftest import EphemeralPostgres
from test_PROTO_05_services_api_keys import (
    ADMIN_USERNAME,
    MEMBER_USERNAME,
    _create_accounts,
    _environment,
    _login,
)
from test_UI_33_user_api_keys_admin import _database  # noqa: F401


def test_UI_36_detail_created_at_and_deleted_history(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as env:
            ids = await _create_accounts(env)
            admin = await _login(env, ADMIN_USERNAME)
            path = f"/api/v1/users/{ids[MEMBER_USERNAME]}"
            response = await env.client.get(path, cookies=admin)
            assert response.status == 200
            created = response.json()["created_at"]
            assert datetime.fromisoformat(created).tzinfo is not None
            listed = await env.client.get("/api/v1/users", cookies=admin)
            assert all("created_at" not in item for item in listed.json()["items"])
            deleted = await env.client.request("POST", f"{path}:delete", cookies=admin)
            assert deleted.status == 200
            history = await env.client.get(path, cookies=admin)
            assert history.status == 200
            assert history.json()["status"] == "deleted"
            assert history.json()["created_at"] == created

    asyncio.run(scenario())

"""UI-34 activity; FX-UI-34 reuses PROTO-05's real PostgreSQL/ASGI environment."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlencode
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from holm_control_api.http_constants import CSRF_HEADER_NAME
from holm_persistence import AUDIT_EVENTS, USERS
from migration_test_support import upgrade_head
from test_PROTO_05_services_api_keys import (
    ADMIN_USERNAME,
    INSTANCE_1,
    MEMBER_USERNAME,
    _all_cookies,
    _create_accounts,
    _create_service,
    _environment,
    _login,
    _login_response,
    _seed_instances,
)


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


def test_UI_34_lifecycle_subject_filter_paging_and_privacy(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as env:
            ids = await _create_accounts(env)
            admin = await _login(env, ADMIN_USERNAME)
            member = await _login(env, MEMBER_USERNAME)
            created = await env.client.request(
                "POST",
                "/api/v1/users",
                cookies=admin,
                json_body={"username": "activity-user", "display_name": "Activity User"},
            )
            assert created.status == 201
            user_id = created.json()["id"]
            path = f"/api/v1/users/{user_id}"
            temporary = created.json()["temporary_password"]
            login = await _login_response(env, "activity-user", temporary)
            assert login.status == 200
            changed = await env.client.request(
                "POST",
                "/api/v1/auth/password",
                cookies=_all_cookies(login),
                headers={CSRF_HEADER_NAME: login.json()["csrf_token"]},
                json_body={
                    "new_password": "Violet-River!6429-Quartz",
                    "confirm_password": "Violet-River!6429-Quartz",
                },
            )
            assert changed.status == 200
            owner = _all_cookies(changed)
            service_id = await _create_service(
                env, admin, name="activity-service", instance_id=INSTANCE_1
            )
            assert (
                await env.client.request(
                    "PUT",
                    path + "/permissions",
                    cookies=admin,
                    json_body={"service_ids": [str(service_id)]},
                )
            ).status == 200
            key = await env.client.request(
                "POST", "/api/v1/me/api-keys", cookies=owner, json_body={"name": "activity-key"}
            )
            assert key.status == 201
            key_id = key.json()["id"]
            plaintext = key.json()["api_key"]
            assert (
                await env.client.request("POST", f"{path}/api-keys/{key_id}:revoke", cookies=admin)
            ).status == 200
            assert (
                await env.client.request(
                    "PUT",
                    f"/api/v1/services/{service_id}/permissions",
                    cookies=admin,
                    json_body={"user_ids": []},
                )
            ).status == 200
            assert (
                await env.client.request(
                    "PUT",
                    f"/api/v1/services/{service_id}/permissions",
                    cookies=admin,
                    json_body={"user_ids": [user_id]},
                )
            ).status == 200
            for operation in ("reset-password", "disable", "enable", "delete"):
                assert (
                    await env.client.request("POST", path + ":" + operation, cookies=admin)
                ).status == 200
            response = await env.client.get(path + "/activity", cookies=admin)
            assert response.status == 200  # Deleted subject remains readable.
            body = response.json()
            items = body["items"]
            assert body["next_cursor"] is None
            assert [(item["action"], item["operation"]) for item in reversed(items)] == [
                ("account.change", "create"),
                ("auth.login", None),
                ("account.password_change", "change"),
                ("permission.change", "replace"),
                ("api_key.change", "create"),
                ("api_key.change", "revoke"),
                ("permission.change", "replace_by_service"),
                ("permission.change", "replace_by_service"),
                ("account.password_change", "reset"),
                ("account.change", "disable"),
                ("account.change", "enable"),
                ("account.change", "delete"),
            ]
            assert [(i["created_at"], i["id"]) for i in items] == sorted(
                [(i["created_at"], i["id"]) for i in items], reverse=True
            )
            for item in items:
                own = (
                    item["action"] == "auth.login"
                    or item["operation"] == "change"
                    or (item["action"] == "api_key.change" and item["operation"] == "create")
                )
                assert item["actor"] == {
                    "type": "user",
                    "id": user_id if own else str(ids[ADMIN_USERNAME]),
                    "username": "activity-user" if own else ADMIN_USERNAME,
                    "display_name": "Activity User" if own else ADMIN_USERNAME,
                }
                if item["action"] == "api_key.change":
                    assert item["api_key"] == {
                        "id": key_id,
                        "name": "activity-key",
                        "display_prefix": "holm_sk_",
                        "last4": plaintext[-4:],
                    }
                else:
                    assert item["api_key"] is None
            grants = [i for i in reversed(items) if i["action"] == "permission.change"]
            assert [(i["added_service_ids"], i["removed_service_ids"]) for i in grants] == [
                ([str(service_id)], []),
                ([], [str(service_id)]),
                ([str(service_id)], []),
            ]
            rendered = json.dumps(body)
            for secret in (
                plaintext,
                temporary,
                "Violet-River!6429-Quartz",
                "secret_hmac",
                "lookup_id",
                "details",
            ):
                assert secret not in rendered
            # Repeated filters produce six rows: three pages, no overlap or omissions.
            query = [("action", "account.change"), ("action", "api_key.change"), ("limit", "2")]
            seen: list[str] = []
            cursor = None
            for page in range(3):
                suffix = query + ([("cursor", cursor)] if cursor else [])
                result = await env.client.get(
                    path + "/activity?" + urlencode(suffix), cookies=admin
                )
                assert result.status == 200
                seen.extend(i["id"] for i in result.json()["items"])
                cursor = result.json()["next_cursor"]
                assert (cursor is None) == (page == 2)
            assert seen == [
                i["id"] for i in items if i["action"] in {"account.change", "api_key.change"}
            ]
            assert len(set(seen)) == 6
            for query_string in (
                "cursor=garbage",
                "cursor=",
                "action=auth.logout",
                "limit=0",
                "limit=101",
            ):
                bad = await env.client.get(path + "/activity?" + query_string, cookies=admin)
                assert bad.status == 400
                assert bad.json()["error"]["code"] == "invalid_request"
            missing = await env.client.get(f"/api/v1/users/{uuid4()}/activity", cookies=admin)
            assert missing.status == 404
            assert missing.json()["error"]["code"] == "user_not_found"
            assert (await env.client.get(path + "/activity", cookies=member)).status == 403
            assert (await env.client.get(path + "/activity")).status == 401
            other = await env.client.get(
                f"/api/v1/users/{ids[MEMBER_USERNAME]}/activity", cookies=admin
            )
            assert {i["id"] for i in other.json()["items"]}.isdisjoint(i["id"] for i in items)
            # Legacy events and deleted actors remain readable.
            async with env.session_factory() as session, session.begin():
                await session.execute(
                    sa.update(USERS)
                    .where(USERS.c.id == UUID(user_id))
                    .values(display_name="Deleted Actor")
                )
                await session.execute(
                    sa.update(AUDIT_EVENTS)
                    .where(AUDIT_EVENTS.c.id == UUID(items[-1]["id"]))
                    .values(details={})
                )
            legacy = await env.client.get(path + "/activity?action=account.change", cookies=admin)
            assert legacy.json()["items"][-1]["operation"] is None
            login_activity = await env.client.get(
                path + "/activity?action=auth.login", cookies=admin
            )
            assert login_activity.json()["items"][0]["actor"]["display_name"] == "Deleted Actor"

    asyncio.run(scenario())


def test_UI_34_permission_batches_preserve_all_33_services(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as env:
            ids = await _create_accounts(env)
            admin = await _login(env, ADMIN_USERNAME)
            services = [
                str(await _create_service(env, admin, name=f"batch-{n}", instance_id=INSTANCE_1))
                for n in range(33)
            ]
            path = f"/api/v1/users/{ids[MEMBER_USERNAME]}"
            for selected, field in ((services, "added_service_ids"), ([], "removed_service_ids")):
                result = await env.client.request(
                    "PUT", path + "/permissions", cookies=admin, json_body={"service_ids": selected}
                )
                assert result.status == 200
                activity = await env.client.get(
                    path + "/activity?action=permission.change", cookies=admin
                )
                rows = list(reversed(activity.json()["items"][:2]))
                assert [row["operation"] for row in rows] == ["replace", "replace"]
                assert [len(row[field]) for row in rows] == [32, 1]
                assert [value for row in rows for value in row[field]] == sorted(services)

    asyncio.run(scenario())


def test_UI_34_hidden_keys_tied_timestamps_and_actor_subject_scope(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as env:
            ids = await _create_accounts(env)
            admin = await _login(env, ADMIN_USERNAME)
            member = await _login(env, MEMBER_USERNAME)
            created = await env.client.request(
                "POST", "/api/v1/me/api-keys", cookies=member, json_body={"name": "hidden-key"}
            )
            assert created.status == 201
            key_id = created.json()["id"]
            key_path = f"/api/v1/me/api-keys/{key_id}"
            assert (
                await env.client.request("POST", key_path + ":revoke", cookies=member)
            ).status == 200
            assert (await env.client.request("DELETE", key_path, cookies=member)).status == 200
            async with env.session_factory() as session, session.begin():
                await session.execute(
                    sa.update(AUDIT_EVENTS)
                    .where(AUDIT_EVENTS.c.object_id == UUID(key_id))
                    .values(created_at=sa.func.date_trunc("day", sa.func.now()))
                )
            path = f"/api/v1/users/{ids[MEMBER_USERNAME]}/activity?action=api_key.change&limit=1"
            seen: list[str] = []
            cursor = None
            for n in range(3):
                page = await env.client.get(
                    path + ("&" + urlencode({"cursor": cursor}) if cursor else ""), cookies=admin
                )
                assert page.status == 200
                row = page.json()["items"][0]
                assert row["api_key"]["id"] == key_id
                assert row["api_key"]["name"] == "hidden-key"
                seen.append(row["id"])
                cursor = page.json()["next_cursor"]
                assert (cursor is None) == (n == 2)
            assert len(set(seen)) == 3
            assert seen == sorted(seen, reverse=True)
            # Administrator acting on another user is not a subject event for the administrator.
            assert (
                await env.client.request(
                    "POST", f"/api/v1/users/{ids[MEMBER_USERNAME]}:disable", cookies=admin
                )
            ).status == 200
            own = await env.client.get(
                f"/api/v1/users/{ids[ADMIN_USERNAME]}/activity", cookies=admin
            )
            assert [row["action"] for row in own.json()["items"]] == ["auth.login"]

    asyncio.run(scenario())


@pytest.mark.parametrize("side", ["user", "service"])
def test_M4_18_limit_activity_excludes_grants_and_revocations(
    ephemeral_postgres: EphemeralPostgres, side: str
) -> None:
    """FX-M4-18: reuse UI-34 real PG accounts, grants and audit HTTP reads."""

    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as env:
            ids = await _create_accounts(env)
            admin = await _login(env, ADMIN_USERNAME)
            user_id = str(ids[MEMBER_USERNAME])
            service_id = str(
                await _create_service(env, admin, name="m418-activity", instance_id=INSTANCE_1)
            )
            path = (
                f"/api/v1/users/{user_id}/permissions"
                if side == "user"
                else f"/api/v1/services/{service_id}/permissions"
            )
            field, peer = ("service_ids", service_id) if side == "user" else ("user_ids", user_id)
            bodies: tuple[dict[str, object], ...] = (
                {field: [peer], "limits": {peer: {"max_concurrent_requests": 2}}},
                {field: [peer], "limits": {peer: {"max_requests_per_minute": 10}}},
                {field: []},
            )
            for body in bodies:
                response = await env.client.request("PUT", path, cookies=admin, json_body=body)
                assert response.status == 200
            response = await env.client.get(
                f"/api/v1/users/{user_id}/activity?action=permission.change", cookies=admin
            )
            assert response.status == 200
            events = list(reversed(response.json()["items"]))
            assert len(events) == 3
            assert [item["limit_changed_service_ids"] for item in events] == [[], [service_id], []]
            assert [item["added_service_ids"] for item in events] == [[service_id], [], []]
            assert [item["removed_service_ids"] for item in events] == [[], [], [service_id]]
            assert all(
                item["operation"] == ("replace" if side == "user" else "replace_by_service")
                for item in events
            )
            with ephemeral_postgres.engine.connect() as connection:
                details = connection.execute(
                    sa.select(AUDIT_EVENTS.c.details).where(
                        AUDIT_EVENTS.c.id == UUID(events[1]["id"])
                    )
                ).scalar_one()
            assert "limits" in details["changed_fields"]
            assert [json.loads(value) for value in details["limits_before"]] == [
                [service_id, [2, None]]
            ]
            assert [json.loads(value) for value in details["limits_after"]] == [
                [service_id, [2, 10]]
            ]
            # Account events without limit metadata remain readable.
            disabled = await env.client.request(
                "POST", f"/api/v1/users/{user_id}:disable", cookies=admin
            )
            assert disabled.status == 200
            old = await env.client.get(
                f"/api/v1/users/{user_id}/activity?action=account.change", cookies=admin
            )
            assert old.status == 200
            assert old.json()["items"]
            assert all(item["limit_changed_service_ids"] == [] for item in old.json()["items"])

    asyncio.run(scenario())

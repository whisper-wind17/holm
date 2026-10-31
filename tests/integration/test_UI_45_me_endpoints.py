"""UI-45; FX-UI-45 reuses the PROTO-05 PostgreSQL/ASGI fixtures."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from holm_domain import ModelInstanceStatus
from holm_persistence import AUDIT_EVENTS, ensure_request_record_partitions
from holm_persistence.ledger_request_records import LEDGER_REQUEST_RECORDS
from migration_test_support import upgrade_head
from sqlalchemy.ext.asyncio import AsyncEngine
from test_PROTO_05_services_api_keys import (
    ADMIN_USERNAME,
    INSTANCE_1,
    INSTANCE_2,
    MEMBER_USERNAME,
    _create_accounts,
    _create_service,
    _environment,
    _login,
    _seed_instances,
    _set_instance_status,
)


@pytest.fixture(autouse=True)
def _database(ephemeral_postgres: EphemeralPostgres) -> None:
    upgrade_head(ephemeral_postgres.engine)
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(
            sa.text(
                "TRUNCATE core.ai_services, core.models, core.users, core.audit_events, "
                "usage.request_records CASCADE"
            )
        )
    _seed_instances(ephemeral_postgres.engine)


def test_UI_46_UI_45_services_authorization_activity_order_and_privacy(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as env:
            ids = await _create_accounts(env)
            admin = await _login(env, ADMIN_USERNAME)
            member = await _login(env, MEMBER_USERNAME)
            services = []
            for name, instance in (
                ("z-recent", INSTANCE_1),
                ("a-unused", INSTANCE_2),
                ("b-unused", INSTANCE_2),
                ("a-disabled", INSTANCE_1),
            ):
                services.append(await _create_service(env, admin, name=name, instance_id=instance))
            await _set_instance_status(env, INSTANCE_1, ModelInstanceStatus.RUNNING)
            await _set_instance_status(env, INSTANCE_2, ModelInstanceStatus.RUNNING)
            path = "/api/v1/me/services"
            assert (await env.client.get(path, cookies=admin)).json() == {"items": []}
            assert (await env.client.get(path, cookies=member)).json() == {"items": []}
            assert (await env.client.get(path)).status == 401
            for username, grants in ((MEMBER_USERNAME, services), (ADMIN_USERNAME, services[1:2])):
                response = await env.client.request(
                    "PUT",
                    f"/api/v1/users/{ids[username]}/permissions",
                    cookies=admin,
                    json_body={"service_ids": [str(identity) for identity in grants]},
                )
                assert response.status == 200
            for identity in (services[0], services[3]):
                assert (
                    await env.client.request(
                        "PATCH",
                        f"/api/v1/services/{identity}",
                        cookies=admin,
                        json_body={"enabled": False},
                    )
                ).status == 200
            assert (await env.client.get("/api/v1/me/profile", cookies=member)).json()[
                "authorized_service_count"
            ] == 4
            assert (await env.client.get("/api/v1/me/profile", cookies=admin)).json()[
                "authorized_service_count"
            ] == 1
            now = datetime.now(UTC).replace(microsecond=0)
            success = now - timedelta(minutes=10)
            async with env.session_factory() as session:
                assert isinstance(session.bind, AsyncEngine)
                await ensure_request_record_partitions(session.bind, (now, success))
            async with env.session_factory() as session, session.begin():
                for user_id, identity, received, terminal, state in (
                    (
                        ids[MEMBER_USERNAME],
                        services[0],
                        success - timedelta(seconds=1),
                        success,
                        "completed",
                    ),
                    (ids[MEMBER_USERNAME], services[0], now - timedelta(minutes=5), None, "failed"),
                    (ids[ADMIN_USERNAME], services[0], now, now, "completed"),
                ):
                    await session.execute(
                        sa.insert(LEDGER_REQUEST_RECORDS).values(
                            request_id=uuid4(),
                            event_seq=1,
                            user_id=user_id,
                            service_id=identity,
                            received_at=received,
                            terminal_at=terminal,
                            record_state=state,
                            updated_at=now,
                        )
                    )
            items = (await env.client.get(path, cookies=member)).json()["items"]
            assert [item["id"] for item in items] == [str(identity) for identity in services]
            assert all(
                set(item)
                == {
                    "id",
                    "name",
                    "description",
                    "public_model_name",
                    "available",
                    "enabled",
                    "last_used_at",
                    "last_succeeded_at",
                }
                for item in items
            )
            assert [item["enabled"] for item in items] == [False, True, True, False]
            assert items[0]["available"] is False
            assert datetime.fromisoformat(items[0]["last_used_at"]) == now - timedelta(minutes=5)
            assert datetime.fromisoformat(items[0]["last_succeeded_at"]) == success
            assert items[1]["last_used_at"] is items[1]["last_succeeded_at"] is None
            assert [item["available"] for item in items] == [False, True, True, False]
            admin_items = (await env.client.get(path, cookies=admin)).json()["items"]
            assert [item["id"] for item in admin_items] == [str(services[1])]
            assert admin_items[0]["last_used_at"] is None
            async with env.session_factory() as session, session.begin():
                await session.execute(
                    sa.insert(LEDGER_REQUEST_RECORDS).values(
                        request_id=uuid4(),
                        event_seq=1,
                        user_id=ids[MEMBER_USERNAME],
                        service_id=services[2],
                        received_at=now,
                        terminal_at=None,
                        record_state="completed",
                        updated_at=now,
                    )
                )
            fallback = (await env.client.get(path, cookies=member)).json()["items"][0]
            assert fallback["id"] == str(services[2])
            assert datetime.fromisoformat(fallback["last_succeeded_at"]) == now

    asyncio.run(scenario())


def test_UI_45_profile_login_audit_normalization_csrf_and_closed_fields(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as env:
            ids = await _create_accounts(env)
            member = await _login(env, MEMBER_USERNAME)
            path = "/api/v1/me/profile"
            assert (await env.client.get(path)).status == 401
            first = await env.client.get(path, cookies=member)
            assert first.status == 200
            profile = first.json()
            assert set(profile) == {
                "username",
                "display_name",
                "role",
                "current_login_at",
                "previous_login_at",
                "created_at",
                "authorized_service_count",
            }
            assert profile["previous_login_at"] is None
            assert profile["authorized_service_count"] == 0
            current = datetime.fromisoformat(profile["current_login_at"])
            previous = current - timedelta(days=1)
            async with env.session_factory() as session, session.begin():
                for actor, moment, result, action in (
                    (ids[MEMBER_USERNAME], previous, "succeeded", "auth.login"),
                    (ids[MEMBER_USERNAME], previous + timedelta(hours=1), "failed", "auth.login"),
                    (ids[ADMIN_USERNAME], previous + timedelta(hours=2), "succeeded", "auth.login"),
                    (
                        ids[MEMBER_USERNAME],
                        previous + timedelta(hours=3),
                        "succeeded",
                        "account.change",
                    ),
                ):
                    await session.execute(
                        sa.insert(AUDIT_EVENTS).values(
                            id=uuid4(),
                            actor_type="user",
                            actor_id=actor,
                            action=action,
                            object_type="user",
                            object_id=actor,
                            result=result,
                            request_id=uuid4(),
                            details={},
                            created_at=moment,
                        )
                    )
            profile = (await env.client.get(path, cookies=member)).json()
            assert datetime.fromisoformat(profile["previous_login_at"]) == previous
            assert datetime.fromisoformat(profile["current_login_at"]) == current
            assert (
                await env.client.request(
                    "PATCH",
                    path,
                    cookies=dict(member),
                    json_body={"display_name": "unsafe"},
                )
            ).status == 403
            assert (
                await env.client.request(
                    "PATCH", path, cookies=member, json_body={"display_name": "a" * 81}
                )
            ).status == 400
            for body in (
                {"display_name": "ok", "role": "admin"},
                {"display_name": "ok", "username": "other"},
                {},
                {"display_name": None},
            ):
                assert (
                    await env.client.request("PATCH", path, cookies=member, json_body=body)
                ).status == 400
            assert (await env.client.get(path, cookies=member)).json() == profile
            for raw, expected in (
                ("  Ａlice  ", "Alice"),
                (" " * 100, MEMBER_USERNAME),
                ("a" * 80 + "  ", "a" * 80),
            ):
                changed = await env.client.request(
                    "PATCH", path, cookies=member, json_body={"display_name": raw}
                )
                assert changed.status == 200
                assert changed.json() == {**profile, "display_name": expected}
                assert (await env.client.get(path, cookies=member)).json() == changed.json()
                async with env.session_factory() as session:
                    audit = (
                        (
                            await session.execute(
                                sa.select(AUDIT_EVENTS).where(
                                    AUDIT_EVENTS.c.request_id
                                    == dict(changed.headers)["x-request-id"],
                                )
                            )
                        )
                        .mappings()
                        .one()
                    )
                assert audit["actor_id"] == audit["object_id"] == ids[MEMBER_USERNAME]
                assert audit["action"] == "account.change"
                assert audit["result"] == "succeeded"
                assert audit["details"] == {"changed_fields": ["display_name"]}

    asyncio.run(scenario())

"""FX-M4-11B: reuse PROTO-05 accounts/instances with real isolated PostgreSQL."""

from __future__ import annotations

import asyncio
from uuid import UUID

import httpx
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from conftest import EphemeralPostgres, temporary_postgres
from holm_domain import ModelInstanceStatus
from holm_persistence import AuditWriter
from migration_test_support import ALEMBIC_CONFIG_PATH, upgrade_head
from sqlalchemy.exc import IntegrityError
from test_M4_04_snapshot_integration import ADMIN_ID, MEMBER_ID, _seed
from test_M4_04_snapshot_integration import _environment as snapshot_environment
from test_M4_11A_public_model_rename import database as database
from test_M4_11A_public_model_rename import state
from test_PROTO_05_services_api_keys import (
    ADMIN_USERNAME,
    INSTANCE_1,
    INSTANCE_2,
    MEMBER_USERNAME,
    _create_accounts,
    _create_service,
    _environment,
    _login,
    _set_instance_status,
)


def test_M4_11B_TC_DB_01_upgrade_legacy_names_and_constraints() -> None:
    with temporary_postgres() as database:
        with database.engine.connect() as connection:
            config = Config(str(ALEMBIC_CONFIG_PATH))
            config.attributes["connection"] = connection
            command.upgrade(config, "20260930_0034")
        names = ["x", "相同前缀的公共名" * 20, "相同前缀的公共名" * 20 + "尾"]
        with database.engine.begin() as connection:
            for i, name in enumerate(names, 1):
                connection.execute(
                    sa.text(
                        "INSERT INTO core.ai_services "
                        "(id, public_model_name, description, enabled, "
                        "route_version, created_at, updated_at) "
                        "VALUES (:id, :name, '', true, 1, now(), now())"
                    ),
                    {"id": UUID(int=i), "name": name},
                )
        upgrade_head(database.engine)
        upgrade_head(database.engine)
        with database.engine.connect() as connection:
            rows = (
                connection.execute(
                    sa.text("SELECT id, name, public_model_name FROM core.ai_services ORDER BY id")
                )
                .mappings()
                .all()
            )
        assert [row["public_model_name"] for row in rows] == names
        assert [row["name"] for row in rows] == [
            name[:7] + "-" + UUID(int=i).hex for i, name in enumerate(names, 1)
        ]
        for invalid in (None, "x", "x" * 41, " trailing ", rows[0]["name"]):
            with pytest.raises(IntegrityError), database.engine.begin() as connection:
                connection.execute(
                    sa.text("UPDATE core.ai_services SET name=:name WHERE id=:id"),
                    {"name": invalid, "id": UUID(int=2)},
                )


def test_M4_11B_TC_SEC_11_12_names_independent_atomic_and_authorized(
    database: EphemeralPostgres,
) -> None:
    async def scenario() -> None:
        async with _environment(database) as env:
            accounts = await _create_accounts(env)
            admin = await _login(env, ADMIN_USERNAME)
            member = await _login(env, MEMBER_USERNAME)
            await _set_instance_status(env, INSTANCE_1, ModelInstanceStatus.RUNNING)
            base = {"public_model_name": "legacy-route", "primary_instance_id": str(INSTANCE_1)}
            response = await env.client.request(
                "POST",
                "/api/v1/services",
                cookies=admin,
                json_body={**base, "name": "  研发助手😀  "},
            )
            assert response.status == 201
            created = response.json()
            assert created["name"] == "研发助手😀"
            identity = created["id"]
            path = f"/api/v1/services/{identity}:rename-display-name"
            legacy_id = await _create_service(env, admin, name="x", instance_id=INSTANCE_2)
            legacy = (await env.client.get(f"/api/v1/services/{legacy_id}", cookies=admin)).json()
            assert legacy["name"] == "x-" + legacy_id.hex
            baseline = await state(env.session_factory)
            for invalid in ("", " ", "x", "😀" * 41, "bad\x00name", None):
                bad = await env.client.request(
                    "POST",
                    "/api/v1/services",
                    cookies=admin,
                    json_body={**base, "public_model_name": "unused", "name": invalid},
                )
                assert bad.status == 400
                bad = await env.client.request(
                    "POST",
                    path,
                    cookies=admin,
                    json_body={"name": invalid, "expected_route_version": 1},
                )
                assert bad.status == 400
            duplicate = await env.client.request(
                "POST",
                "/api/v1/services",
                cookies=admin,
                json_body={**base, "public_model_name": "unused", "name": "研发助手😀"},
            )
            assert duplicate.status == 409
            assert duplicate.json()["error"]["code"] == "service_display_name_conflict"
            for name, version, code in (
                (legacy["name"], 1, "service_display_name_conflict"),
                ("新版名称", 2, "version_conflict"),
            ):
                bad = await env.client.request(
                    "POST",
                    path,
                    cookies=admin,
                    json_body={"name": name, "expected_route_version": version},
                )
                assert bad.status == 409
                assert bad.json()["error"]["code"] == code
                assert bad.json()["error"]["request_id"]
                assert "UPDATE" not in bad.text and "uq_ai_services" not in bad.text
            assert await state(env.session_factory) == baseline
            for cookies, status in ((member, 403), (None, 401), (dict(admin), 403)):
                bad = await env.client.request(
                    "POST",
                    path,
                    cookies=cookies,
                    json_body={"name": "新版名称", "expected_route_version": 1},
                )
                assert bad.status == status
            missing = await env.client.request(
                "POST",
                f"/api/v1/services/{UUID(int=19)}:rename-display-name",
                cookies=admin,
                json_body={"name": "新版名称", "expected_route_version": 1},
            )
            assert missing.status == 404
            granted = await env.client.request(
                "PUT",
                f"/api/v1/users/{accounts[MEMBER_USERNAME]}/permissions",
                cookies=admin,
                json_body={"service_ids": [identity]},
            )
            assert granted.status == 200
            key = await env.client.request(
                "POST",
                "/api/v1/me/api-keys",
                cookies=member,
                json_body={"name": "FX-M4-11B key", "service_ids": [identity]},
            )
            assert key.status == 201
            before = await state(env.session_factory)
            changed = await env.client.request(
                "POST",
                path,
                cookies=admin,
                json_body={"name": "😀" * 40, "expected_route_version": 1},
            )
            assert changed.status == 200
            assert changed.json() == {**created, "name": "😀" * 40, "route_version": 2}
            persisted = await env.client.get(f"/api/v1/services/{identity}", cookies=admin)
            assert persisted.json() == changed.json()
            after = await state(env.session_factory)
            for table in before.keys() - {"ai_services", "audit_events", "domain_events"}:
                assert before[table] == after[table]
            assert len(after["audit_events"]) == len(before["audit_events"]) + 1
            assert len(after["domain_events"]) == len(before["domain_events"]) + 1
            stale = await env.client.request(
                "POST",
                path,
                cookies=admin,
                json_body={"name": "陈旧编辑名称", "expected_route_version": 1},
            )
            assert stale.status == 409
            assert stale.json()["error"]["code"] == "version_conflict"
            assert await state(env.session_factory) == after
            personal = (await env.client.get("/api/v1/me/services", cookies=member)).json()["items"]
            assert len(personal) == 1
            assert personal[0]["name"] == "😀" * 40
            assert personal[0]["public_model_name"] == "legacy-route"
            assert set(personal[0]) == {
                "name",
                "public_model_name",
                "description",
                "available",
                "enabled",
                "id",
                "last_used_at",
                "last_succeeded_at",
            }
            keys = (await env.client.get("/api/v1/me/api-keys", cookies=member)).json()["items"]
            assert keys[0]["effective_services"] == [
                {"id": identity, "name": "😀" * 40, "public_model_name": "legacy-route"}
            ]
            await env.client.request(
                "PATCH", f"/api/v1/services/{identity}", cookies=admin, json_body={"enabled": False}
            )
            with database.engine.begin() as connection:
                connection.execute(
                    sa.text("UPDATE core.model_instances SET status='Stopped' WHERE id=:id"),
                    {"id": INSTANCE_1},
                )
            renamed = await env.client.request(
                "POST",
                path,
                cookies=admin,
                json_body={"name": "停用服务新名称", "expected_route_version": 2},
            )
            assert renamed.status == 200 and renamed.json()["enabled"] is False
            public = await env.client.request(
                "POST",
                f"/api/v1/services/{identity}:rename",
                cookies=admin,
                json_body={
                    "public_model_name": "new-route",
                    "confirm_public_model_name": "new-route",
                    "expected_route_version": 3,
                },
            )
            assert public.status == 200
            assert public.json()["name"] == "停用服务新名称"

    asyncio.run(scenario())


def test_M4_11B_TC_GW_03_display_name_does_not_change_public_route_and_audit_is_atomic(
    ephemeral_postgres: EphemeralPostgres,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(ephemeral_postgres)

    async def scenario() -> None:
        async with snapshot_environment(ephemeral_postgres) as env:
            before = await state(env.factory)
            old = await env.manager.find("m4-04-model")
            assert old is not None

            async def fail_audit(*args: object, **kwargs: object) -> None:
                raise RuntimeError("FX-M4-11B audit unavailable")

            with monkeypatch.context() as patch:
                patch.setattr(AuditWriter, "add", fail_audit)
                with pytest.raises(RuntimeError, match="audit unavailable"):
                    await env.ai_service.rename_display_name(
                        env.service_id,
                        name="新版服务名称",
                        expected_route_version=1,
                        actor_id=ADMIN_ID,
                        request_id=UUID(int=4111),
                    )
            assert await state(env.factory) == before
            changed = await env.ai_service.rename_display_name(
                env.service_id,
                name="新版服务名称",
                expected_route_version=1,
                actor_id=ADMIN_ID,
                request_id=UUID(int=4112),
            )
            assert changed.public_model_name == "m4-04-model"
            after = await state(env.factory)
            for table in before.keys() - {"ai_services", "audit_events", "domain_events"}:
                assert before[table] == after[table]
            await env.manager.refresh()
            route = await env.manager.find("m4-04-model")
            assert route is not None
            assert (route.service_id, route.instance_id, route.public_model_name) == (
                old.service_id,
                old.instance_id,
                old.public_model_name,
            )
            assert await env.manager.find("新版服务名称") is None
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=env.gateway), base_url="http://gateway"
            ) as client:
                headers = {"Authorization": f"Bearer {env.plaintext_key}"}
                models = await client.get("/v1/models", headers=headers)
                assert [item["id"] for item in models.json()["data"]] == ["m4-04-model"]
                assert "新版服务名称" not in models.text
                response = await client.post(
                    "/v1/chat/completions",
                    headers=headers,
                    json={"model": "m4-04-model", "messages": []},
                )
                assert response.status_code == 200
                assert response.json()["model"] == "m4-04-model"
                assert "新版服务名称" not in response.text
                missing = await client.post(
                    "/v1/chat/completions",
                    headers=headers,
                    json={"model": "新版服务名称", "messages": []},
                )
                assert missing.status_code == 404
                await env.user_service.replace_user_permissions(
                    MEMBER_ID, (), actor_id=ADMIN_ID, request_id=UUID(int=4113)
                )
                await env.manager.refresh()
                denied = await client.post(
                    "/v1/chat/completions",
                    headers=headers,
                    json={"model": "m4-04-model", "messages": []},
                )
                assert denied.status_code == 403

    asyncio.run(scenario())

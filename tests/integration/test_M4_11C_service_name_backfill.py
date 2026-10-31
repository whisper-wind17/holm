"""FX-M4-11C: legacy service rows and reused PROTO-05 API fixtures on real PG."""

from __future__ import annotations

import asyncio
from uuid import UUID

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from conftest import EphemeralPostgres, temporary_postgres
from holm_domain import ModelInstanceStatus
from migration_test_support import ALEMBIC_CONFIG_PATH, upgrade_head
from test_M4_11A_public_model_rename import database as database
from test_PROTO_05_services_api_keys import (
    ADMIN_USERNAME,
    INSTANCE_1,
    _create_accounts,
    _environment,
    _login,
    _set_instance_status,
)


def test_M4_11C_TC_DB_01_backfill_preserves_conflicts_and_unrelated_columns() -> None:
    with temporary_postgres() as db:
        with db.engine.connect() as connection:
            config = Config(str(ALEMBIC_CONFIG_PATH))
            config.attributes["connection"] = connection
            command.upgrade(config, "20260930_0034")
        names = [
            "  公共模型😀  ",
            "manual-route",
            "x",
            "长" * 41,
            "duplicate",
            "duplicate",
            "occupied",
            "holder",
            "manual-duplicate",
            "manual-duplicate",
            "界" * 40,
        ]
        with db.engine.begin() as connection:
            for i, name in enumerate(names, 1):
                connection.execute(
                    sa.text(
                        "INSERT INTO core.ai_services "
                        "(id, public_model_name, description, enabled, route_version, "
                        "created_at, updated_at, deleted_at) "
                        "VALUES (:id, :name, 'FX-M4-11C', true, 1, now(), now(), "
                        "CASE WHEN :deleted THEN now() ELSE NULL END)"
                    ),
                    {"id": UUID(int=i), "name": name, "deleted": i in (6, 8, 10)},
                )
        with db.engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "20260930_0035")
        with db.engine.begin() as connection:
            for i, name in ((2, "管理员名称"), (8, "occupied"), (10, "手工名称")):
                connection.execute(
                    sa.text("UPDATE core.ai_services SET name=:name WHERE id=:id"),
                    {"id": UUID(int=i), "name": name},
                )
            before = (
                connection.execute(sa.text("SELECT * FROM core.ai_services ORDER BY id"))
                .mappings()
                .all()
            )
            history = connection.execute(
                sa.text("SELECT count(*) FROM core.audit_events")
            ).scalar_one()
        upgrade_head(db.engine)
        upgrade_head(db.engine)
        with db.engine.connect() as connection:
            after = (
                connection.execute(sa.text("SELECT * FROM core.ai_services ORDER BY id"))
                .mappings()
                .all()
            )
            assert (
                connection.execute(sa.text("SELECT count(*) FROM core.audit_events")).scalar_one()
                == history
            )
        for i, (old, new) in enumerate(zip(before, after, strict=True), 1):
            expected = names[i - 1].strip() if i in (1, 11) else old["name"]
            assert dict(new) == {**old, "name": expected}


def test_M4_11C_omitted_name_prefers_public_name_or_falls_back(
    database: EphemeralPostgres,
) -> None:
    async def scenario() -> None:
        async with _environment(database) as env:
            await _create_accounts(env)
            admin = await _login(env, ADMIN_USERNAME)
            await _set_instance_status(env, INSTANCE_1, ModelInstanceStatus.RUNNING)
            base = {"primary_instance_id": str(INSTANCE_1)}
            for public_name in ("readable-route", "x", "long" * 11):
                response = await env.client.request(
                    "POST",
                    "/api/v1/services",
                    cookies=admin,
                    json_body={**base, "public_model_name": public_name},
                )
                assert response.status == 201
                body = response.json()
                expected = (
                    public_name
                    if 2 <= len(public_name) <= 40
                    else public_name[:7] + "-" + UUID(body["id"]).hex
                )
                assert body["name"] == expected
                assert body["public_model_name"] == public_name
            holder = await env.client.request(
                "POST",
                "/api/v1/services",
                cookies=admin,
                json_body={**base, "public_model_name": "holder", "name": "occupied"},
            )
            assert holder.status == 201
            # Deleted services release their display names for reuse.
            with database.engine.begin() as connection:
                connection.execute(
                    sa.text("UPDATE core.ai_services SET deleted_at=now() WHERE id=:id"),
                    {"id": UUID(holder.json()["id"])},
                )
            response = await env.client.request(
                "POST",
                "/api/v1/services",
                cookies=admin,
                json_body={**base, "public_model_name": "occupied"},
            )
            assert response.status == 201
            assert response.json()["name"] == "occupied"
            for invalid in ("", " ", "x", "😀" * 41, None):
                response = await env.client.request(
                    "POST",
                    "/api/v1/services",
                    cookies=admin,
                    json_body={**base, "public_model_name": "unused", "name": invalid},
                )
                assert response.status == 400

    asyncio.run(scenario())

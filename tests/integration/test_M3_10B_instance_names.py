"""M3-10B names: FX-PROTO-01 real PostgreSQL/API, fake GPU inventory only."""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from conftest import EphemeralPostgres, temporary_postgres
from migration_test_support import ALEMBIC_CONFIG_PATH, upgrade_head
from sqlalchemy.exc import IntegrityError
from test_PROTO_01_model_instances import (
    ADMIN_USERNAME,
    GPU_0,
    MEMBER_USERNAME,
    READY_ARTIFACT_ID,
    _create_accounts,
    _create_body,
    _environment,
    _login,
    _seed_model_and_artifacts,
)


def test_M3_10B_TC_DB_01_legacy_backfill_is_unique_deterministic_and_idempotent() -> None:
    with temporary_postgres() as database:
        with database.engine.connect() as connection:
            config = Config(str(ALEMBIC_CONFIG_PATH))
            config.attributes["connection"] = connection
            command.upgrade(config, "20260929_0032")
        _seed_model_and_artifacts(database.engine)
        ids = [
            UUID("01aa0000-0000-7000-8000-000000000001"),
            UUID("01aa0000-0000-7000-8000-000000000002"),
        ]
        with database.engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE core.models SET display_name = :name"), {"name": "模型" * 50}
            )
            for index, identity in enumerate(ids):
                connection.execute(
                    sa.text(
                        "INSERT INTO core.model_instances "
                        "(id,artifact_id,runtime_type,expected_gpu_uuids,tensor_parallel_size,"
                        "context_length,gpu_memory_utilization,internal_model_name,status,"
                        "config_version,created_at,updated_at) VALUES "
                        "(:id,:artifact,'vllm','[\"FX-GPU\"]',1,4096,0.9,:internal,'Stopped',"
                        "1,now(),now())"
                    ),
                    {"id": identity, "artifact": READY_ARTIFACT_ID, "internal": f"mi_{index:012x}"},
                )
        upgrade_head(database.engine)
        upgrade_head(database.engine)
        with database.engine.connect() as connection:
            rows = (
                connection.execute(
                    sa.text(
                        "SELECT id,name,internal_model_name FROM core.model_instances ORDER BY id"
                    )
                )
                .mappings()
                .all()
            )
            assert [row["name"] for row in rows] == ["模型" * 32, "模型" * 31 + "-2"]
            assert [row["internal_model_name"] for row in rows] == [
                "mi_000000000000",
                "mi_000000000001",
            ]
        for invalid in (None, "x", "x" * 65, " trailing ", rows[0]["name"]):
            with pytest.raises(IntegrityError), database.engine.begin() as connection:
                connection.execute(
                    sa.text("UPDATE core.model_instances SET name=:name WHERE id=:id"),
                    {"name": invalid, "id": ids[1]},
                )


def _prepare(database: EphemeralPostgres) -> None:
    upgrade_head(database.engine)
    with database.engine.begin() as connection:
        connection.execute(sa.text("TRUNCATE core.models, core.users CASCADE"))
    _seed_model_and_artifacts(database.engine)


def test_M3_10B_TC_GPU_01_04_09_names_roundtrip_conflict_and_internal_isolation(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _prepare(ephemeral_postgres)

    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as env:
            await _create_accounts(env)
            admin = await _login(env, ADMIN_USERNAME)
            body = _create_body(READY_ARTIFACT_ID, [GPU_0], 1)
            created = await env.client.request(
                "POST",
                "/api/v1/instances",
                cookies=admin,
                json_body={**body, "name": "  研发助手·一号  "},
            )
            assert created.status == 201
            item = created.json()
            assert item["name"] == "研发助手·一号"
            assert item["internal_model_name"].startswith("mi_")
            assert item["name"] not in item["internal_model_name"]
            duplicate = await env.client.request(
                "POST",
                "/api/v1/instances",
                cookies=admin,
                json_body={**body, "name": "研发助手·一号"},
            )
            assert duplicate.status == 409
            assert duplicate.json()["error"]["code"] == "instance_name_conflict"
            assert duplicate.json()["error"]["request_id"]
            assert "研发助手" not in str(duplicate.json())
            assert "INSERT" not in str(duplicate.json())
            with ephemeral_postgres.engine.connect() as connection:
                assert connection.scalar(sa.text("SELECT count(*) FROM core.model_instances")) == 1
                assert connection.scalar(sa.text("SELECT count(*) FROM core.gpu_allocations")) == 1
            automatic = []
            for _ in range(2):
                response = await env.client.request(
                    "POST", "/api/v1/instances", cookies=admin, json_body=body
                )
                assert response.status == 201
                automatic.append(response.json())
            assert automatic[0]["name"] != automatic[1]["name"]
            assert [row["name"] for row in automatic] == ["PROTO-01 Model", "PROTO-01 Model-2"]
            listed = await env.client.get("/api/v1/instances", cookies=admin)
            assert {row["name"] for row in listed.json()["items"]} == {
                item["name"],
                *(row["name"] for row in automatic),
            }
            identity = item["id"]
            detail = await env.client.get(f"/api/v1/instances/{identity}", cookies=admin)
            assert detail.json()["name"] == item["name"]
            # Simulate a completed stop; lifecycle start/retry must retain both names.
            with ephemeral_postgres.engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "UPDATE core.gpu_allocations SET status='Released', released_at=now() "
                        "WHERE instance_id=:id"
                    ),
                    {"id": identity},
                )
                connection.execute(
                    sa.text("UPDATE core.model_instances SET status='Stopped' WHERE id=:id"),
                    {"id": identity},
                )
            started = await env.client.request(
                "POST", f"/api/v1/instances/{identity}:start", cookies=admin
            )
            assert started.status == 200
            assert started.json()["name"] == item["name"]
            with ephemeral_postgres.engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "UPDATE core.gpu_allocations SET status='Released', released_at=now() "
                        "WHERE instance_id=:id"
                    ),
                    {"id": identity},
                )
                connection.execute(
                    sa.text("UPDATE core.model_instances SET status='Failed' WHERE id=:id"),
                    {"id": identity},
                )
            retried = await env.client.request(
                "POST", f"/api/v1/instances/{identity}:retry", cookies=admin
            )
            assert retried.status == 200
            assert retried.json()["name"] == item["name"]
            assert retried.json()["internal_model_name"] == item["internal_model_name"]

    asyncio.run(scenario())


def test_M3_10B_invalid_names_and_authorization_leave_no_rows(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _prepare(ephemeral_postgres)

    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as env:
            await _create_accounts(env)
            admin = await _login(env, ADMIN_USERNAME)
            member = await _login(env, MEMBER_USERNAME)
            body = _create_body(READY_ARTIFACT_ID, [GPU_0], 1)
            for name in ("", " ", "x", "x" * 65, "NUL\x00name", None, 123):
                response = await env.client.request(
                    "POST", "/api/v1/instances", cookies=admin, json_body={**body, "name": name}
                )
                assert response.status == (422 if isinstance(name, str) else 400)
                assert "NUL" not in str(response.json())
            for cookies, status in ((None, 401), (member, 403), (dict(admin), 403)):
                response = await env.client.request(
                    "POST",
                    "/api/v1/instances",
                    cookies=cookies,
                    json_body={**body, "name": "private name"},
                )
                assert response.status == status
                assert "private name" not in str(response.json())
                invalid = await env.client.request(
                    "POST", "/api/v1/instances", cookies=cookies, json_body={**body, "name": " "}
                )
                assert invalid.status == status
            with ephemeral_postgres.engine.connect() as connection:
                assert connection.scalar(sa.text("SELECT count(*) FROM core.model_instances")) == 0
                assert connection.scalar(sa.text("SELECT count(*) FROM core.gpu_allocations")) == 0
            for name in ("𠮷" * 64, "大小写", "大小寫", "Case", "case"):
                response = await env.client.request(
                    "POST", "/api/v1/instances", cookies=admin, json_body={**body, "name": name}
                )
                assert response.status == 201
                assert response.json()["name"] == name

    asyncio.run(scenario())

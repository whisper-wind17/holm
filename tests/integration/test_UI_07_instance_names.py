"""FX-UI-07: real PG rows/API using the existing PROTO-01 model fixture."""

import asyncio
import importlib.util
from pathlib import Path
from uuid import UUID

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from conftest import EphemeralPostgres, temporary_postgres
from migration_test_support import ALEMBIC_CONFIG_PATH, upgrade_head
from test_M3_10B_instance_names import _prepare
from test_PROTO_01_model_instances import (
    ADMIN_USERNAME,
    GPU_0,
    READY_ARTIFACT_ID,
    _create_accounts,
    _create_body,
    _environment,
    _login,
    _seed_model_and_artifacts,
)


def test_UI_07_TC_DB_01_migration_order_custom_names_and_repeated_upgrade() -> None:
    with temporary_postgres() as db:
        with db.engine.connect() as connection:
            config = Config(str(ALEMBIC_CONFIG_PATH))
            config.attributes["connection"] = connection
            command.upgrade(config, "20261008_0037")
        _seed_model_and_artifacts(db.engine)
        model_name = "Qwen/Qwen2.5-0.5B-Instruct"
        # IDs deliberately run opposite to creation order; suffix need not equal ID.
        old_names = [model_name + "-" + f"{i:032x}" for i in range(1, 4)]
        names = [*old_names, "自定义名称", "Qwen2.5-0.5B-Instruct-2", model_name + "-" + "A" * 32]
        with db.engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE core.models SET display_name=:name"), {"name": model_name}
            )
            for index, name in enumerate(names):
                connection.execute(
                    sa.text(
                        "INSERT INTO core.model_instances "
                        "(id,name,artifact_id,runtime_type,expected_gpu_uuids,tensor_parallel_size,"
                        "context_length,gpu_memory_utilization,internal_model_name,status,"
                        "config_version,created_at,updated_at) VALUES "
                        "(:id,:name,:artifact,'vllm','[\"FX-GPU\"]',1,4096,0.9,:internal,'Stopped',"
                        "1, '2026-10-09'::timestamptz + :index * interval '1 second',now())"
                    ),
                    {
                        "id": UUID(int=100 - index),
                        "name": name,
                        "artifact": READY_ARTIFACT_ID,
                        "internal": f"mi_{index:012x}",
                        "index": index,
                    },
                )
            before = (
                connection.execute(
                    sa.text("SELECT * FROM core.model_instances ORDER BY created_at")
                )
                .mappings()
                .all()
            )
        upgrade_head(db.engine)
        with db.engine.begin() as connection:
            after = (
                connection.execute(
                    sa.text("SELECT * FROM core.model_instances ORDER BY created_at")
                )
                .mappings()
                .all()
            )
            expected = [
                "Qwen2.5-0.5B-Instruct",
                "Qwen2.5-0.5B-Instruct-3",
                "Qwen2.5-0.5B-Instruct-4",
                *names[3:],
            ]
            for old, new, name in zip(before, after, expected, strict=True):
                assert dict(new) == {**old, "name": name}
            path = (
                Path(__file__).parents[2]
                / "migrations/versions/20261009_0038_instance_default_names.py"
            )
            spec = importlib.util.spec_from_file_location("ui07_migration", path)
            assert spec is not None and spec.loader is not None
            migration = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(migration)
            # Actually execute the body twice, rather than relying on Alembic's version no-op.
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                migration.downgrade()
            repeated = (
                connection.execute(
                    sa.text("SELECT * FROM core.model_instances ORDER BY created_at")
                )
                .mappings()
                .all()
            )
            assert repeated == after


def test_UI_07_default_name_retry_limit_and_explicit_conflict(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _prepare(ephemeral_postgres)
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(sa.text("UPDATE core.models SET display_name='Org/Readable'"))

    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as env:
            await _create_accounts(env)
            admin = await _login(env, ADMIN_USERNAME)
            body = _create_body(READY_ARTIFACT_ID, [GPU_0], 1)
            for sequence in range(1, 21):
                response = await env.client.request(
                    "POST", "/api/v1/instances", cookies=admin, json_body=body
                )
                assert response.status == 201
                expected = "Readable" if sequence == 1 else f"Readable-{sequence}"
                assert response.json()["name"] == expected
                assert response.json()["internal_model_name"].startswith("mi_")
            for payload in (body, {**body, "name": "Readable"}):
                conflict = await env.client.request(
                    "POST", "/api/v1/instances", cookies=admin, json_body=payload
                )
                assert conflict.status == 409
                assert conflict.json()["error"]["code"] == "instance_name_conflict"
            with ephemeral_postgres.engine.connect() as connection:
                assert connection.scalar(sa.text("SELECT count(*) FROM core.model_instances")) == 20

    asyncio.run(scenario())

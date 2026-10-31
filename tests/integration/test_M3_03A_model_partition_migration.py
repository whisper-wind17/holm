"""M3-03A: real-PostgreSQL coverage for nullable partition metadata."""

from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from conftest import EphemeralPostgres
from migration_test_support import ALEMBIC_CONFIG_PATH
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

PREVIOUS_REVISION = "20260921_0024"
ARTIFACT_ID = UUID("52000000-0000-4000-8000-000000000001")
MODEL_ID = UUID("51000000-0000-4000-8000-000000000001")


def _config(connection: Connection) -> Config:
    return Config(ALEMBIC_CONFIG_PATH, attributes={"connection": connection})


def test_M3_03A_migration_preserves_existing_artifact_as_uncollected_and_downgrades(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    engine = ephemeral_postgres.engine
    with engine.connect() as connection:
        command.upgrade(_config(connection), PREVIOUS_REVISION)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO core.models "
                "(id, source_type, source_key, display_name, external_path, metadata_status, "
                "created_at, updated_at) VALUES "
                "(:id, 'Local Path', '/models/m303a', 'M3-03A', '/models/m303a', "
                "'CONFIRMED', clock_timestamp(), clock_timestamp())"
            ),
            {"id": MODEL_ID},
        )
        connection.execute(
            text(
                "INSERT INTO core.model_artifacts "
                "(id, model_id, source_type, repository_id, resolved_revision, storage_type, "
                "status, path, manifest_hash, size_bytes, created_at, updated_at) VALUES "
                "(:id, :model_id, 'Local Path', 'm303a', :revision, 'External', 'Ready', "
                "'/models/m303a', :revision, 1, clock_timestamp(), clock_timestamp())"
            ),
            {"id": ARTIFACT_ID, "model_id": MODEL_ID, "revision": "a" * 64},
        )
    with engine.connect() as connection:
        command.upgrade(_config(connection), "head")
        row = connection.execute(
            text("SELECT attention_heads, kv_heads FROM core.model_artifacts WHERE id = :id"),
            {"id": ARTIFACT_ID},
        ).one()
    assert row == (None, None)

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text("UPDATE core.model_artifacts SET attention_heads = 0 WHERE id = :id"),
            {"id": ARTIFACT_ID},
        )

    with engine.connect() as connection:
        command.downgrade(_config(connection), PREVIOUS_REVISION)
    columns = {
        column["name"] for column in inspect(engine).get_columns("model_artifacts", schema="core")
    }
    assert "attention_heads" not in columns
    assert "kv_heads" not in columns
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM core.model_artifacts WHERE id = :id"),
                {"id": ARTIFACT_ID},
            ).scalar_one()
            == 1
        )

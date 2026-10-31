"""TC-DB-11: model-domain Migration downgrade and re-upgrade closure."""

from __future__ import annotations

from typing import Final

from alembic import command
from alembic.config import Config
from migration_test_support import (
    ALEMBIC_CONFIG_PATH,
    PostgresFixture,
    assert_head_structure,
    database_structure,
    upgrade_head,
)
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

PREVIOUS_REVISION: Final[str] = "20260901_0007"
MODEL_TABLES: Final[tuple[str, ...]] = (
    "download_subscriptions",
    "download_tasks",
    "model_artifacts",
    "models",
)


def _alembic_config(connection: Connection) -> Config:
    return Config(ALEMBIC_CONFIG_PATH, attributes={"connection": connection})


def _model_domain_snapshot(engine: Engine) -> tuple[tuple[object, ...], ...]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT object_kind, table_name, object_name, definition
                FROM (
                    SELECT
                        'column'::text AS object_kind,
                        table_name,
                        column_name AS object_name,
                        data_type || ':' || is_nullable AS definition
                    FROM information_schema.columns
                    WHERE table_schema = 'core'
                      AND table_name = ANY(:tables)

                    UNION ALL

                    SELECT
                        'constraint'::text,
                        relation.relname,
                        constraint_record.conname,
                        pg_get_constraintdef(constraint_record.oid, true)
                    FROM pg_constraint AS constraint_record
                    JOIN pg_class AS relation ON relation.oid = constraint_record.conrelid
                    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                    WHERE namespace.nspname = 'core'
                      AND relation.relname = ANY(:tables)

                    UNION ALL

                    SELECT
                        'index'::text,
                        tablename,
                        indexname,
                        indexdef
                    FROM pg_indexes
                    WHERE schemaname = 'core'
                      AND tablename = ANY(:tables)
                ) AS objects
                ORDER BY object_kind, table_name, object_name, definition
                """
            ),
            {"tables": list(MODEL_TABLES)},
        ).tuples()
        snapshot = tuple(tuple(row) for row in rows)
    assert snapshot, "model-domain Migration snapshot discovery returned no objects"
    return snapshot


def _model_domain_residuals(engine: Engine) -> tuple[tuple[str, str], ...]:
    with engine.connect() as connection:
        return tuple(
            connection.execute(
                text(
                    """
                    SELECT namespace.nspname, relation.relname
                    FROM pg_class AS relation
                    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                    WHERE namespace.nspname = 'core'
                      AND (
                          relation.relname = ANY(:tables)
                          OR relation.relname LIKE 'uq_model_%'
                          OR relation.relname LIKE 'uq_%download%'
                          OR relation.relname LIKE 'pk_model_%'
                          OR relation.relname LIKE 'pk_download_%'
                      )
                    ORDER BY namespace.nspname, relation.relname
                    """
                ),
                {"tables": list(MODEL_TABLES)},
            ).tuples()
        )


def test_TC_DB_11_upgrade_downgrade_upgrade_is_exact_and_leaves_no_objects(
    ephemeral_postgres: PostgresFixture,
) -> None:
    engine = ephemeral_postgres.engine
    upgrade_head(engine)
    first_snapshot = _model_domain_snapshot(engine)
    assert_head_structure(database_structure(engine))

    with engine.connect() as connection:
        command.downgrade(_alembic_config(connection), PREVIOUS_REVISION)

    assert _model_domain_residuals(engine) == ()
    with engine.connect() as connection:
        assert (
            tuple(
                connection.execute(
                    text(
                        """
                    SELECT constraint_record.conname
                    FROM pg_constraint AS constraint_record
                    JOIN pg_class AS relation ON relation.oid = constraint_record.conrelid
                    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                    WHERE namespace.nspname = 'core'
                      AND relation.relname = ANY(:tables)
                    """
                    ),
                    {"tables": list(MODEL_TABLES)},
                ).scalars()
            )
            == ()
        )

    with engine.connect() as connection:
        command.upgrade(_alembic_config(connection), "head")

    assert _model_domain_snapshot(engine) == first_snapshot
    assert_head_structure(database_structure(engine))

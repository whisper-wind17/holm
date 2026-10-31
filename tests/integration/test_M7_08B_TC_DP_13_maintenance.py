"""M7-08B / TC-DP-13 / TC-HEALTH: actual login grants and worker lifecycle."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from conftest import EphemeralPostgres
from holm_persistence.role_grants import (
    M7_07_RUNTIME_ROLES,
    MAINTENANCE,
    RUNTIME_ROLES,
    TABLE_GRANTS,
)
from migration_test_support import ALEMBIC_CONFIG_PATH, upgrade_head
from sqlalchemy.exc import DBAPIError


def test_M7_08B_TC_DP_13_upgrade_0030_and_real_login_minimal_grants(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    with ephemeral_postgres.engine.connect() as connection:
        config = Config(str(ALEMBIC_CONFIG_PATH))
        config.attributes["connection"] = connection
        command.upgrade(config, "20260923_0030")
    with ephemeral_postgres.engine.begin() as connection:
        assert (
            connection.scalar(
                sa.text("SELECT count(*) FROM pg_roles WHERE rolname = :role"),
                {"role": MAINTENANCE},
            )
            == 0
        )
        assert len(M7_07_RUNTIME_ROLES) == 5
        # Existing installer-created roles must lose accidental direct DDL grants.
        connection.execute(sa.text(f"CREATE ROLE {MAINTENANCE} NOLOGIN"))
        connection.execute(sa.text(f"GRANT CREATE, TEMP ON DATABASE postgres TO {MAINTENANCE}"))
        connection.execute(sa.text(f"GRANT CREATE ON SCHEMA public TO {MAINTENANCE}"))
    upgrade_head(ephemeral_postgres.engine)
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(sa.text("CREATE ROLE m708b_grant_login LOGIN"))
        connection.execute(sa.text(f"GRANT {MAINTENANCE} TO m708b_grant_login"))
    engine = sa.create_engine(ephemeral_postgres.engine.url.set(username="m708b_grant_login"))
    try:
        with engine.begin() as connection:
            assert connection.scalar(sa.text("SELECT current_user")) == "m708b_grant_login"
            connection.execute(
                sa.text(
                    "INSERT INTO core.component_heartbeats "
                    "(component, instance_id, version, status, last_heartbeat_at) VALUES "
                    "('maintenance-worker', 'FX-M7-08B-grants', 'fixture', 'Healthy', now())"
                )
            )
            connection.execute(
                sa.text(
                    "UPDATE core.component_heartbeats SET status='Degraded' "
                    "WHERE instance_id='FX-M7-08B-grants'"
                )
            )
            assert (
                connection.scalar(
                    sa.text(
                        "SELECT status FROM core.component_heartbeats "
                        "WHERE instance_id='FX-M7-08B-grants'"
                    )
                )
                == "Degraded"
            )
        for statement in (
            "DELETE FROM core.component_heartbeats WHERE false",
            "SELECT secret_hmac FROM core.api_keys LIMIT 0",
            "SELECT * FROM core.users LIMIT 0",
            "UPDATE core.model_artifacts SET status=status WHERE false",
            "SELECT * FROM usage.request_records LIMIT 0",
            "CREATE TABLE core.m708b_forbidden (id int)",
            "CREATE TABLE public.m708b_forbidden (id int)",
            "CREATE TEMP TABLE m708b_forbidden (id int)",
            "SELECT core.holm_drop_expired_month_partitions('usage.request_records', now())",
            "SET ROLE holm_control",
        ):
            with pytest.raises(DBAPIError) as error, engine.begin() as connection:
                connection.execute(sa.text(statement))
            assert getattr(error.value.orig, "sqlstate", None) == "42501"
        with ephemeral_postgres.engine.connect() as connection:
            assert not connection.scalar(
                sa.text("SELECT has_schema_privilege(:role, 'telemetry', 'USAGE')"),
                {"role": MAINTENANCE},
            )
            assert not connection.scalar(
                sa.text("SELECT has_schema_privilege(:role, 'usage', 'USAGE')"),
                {"role": MAINTENANCE},
            )
            row = connection.execute(
                sa.text(
                    "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, "
                    "rolreplication, rolbypassrls FROM pg_roles WHERE rolname=:role"
                ),
                {"role": MAINTENANCE},
            ).one()
            assert not any(row)
        assert set(RUNTIME_ROLES) == {*M7_07_RUNTIME_ROLES, MAINTENANCE}
        assert {
            table: grants[MAINTENANCE]
            for table, grants in TABLE_GRANTS.items()
            if MAINTENANCE in grants
        } == {"core.component_heartbeats": "SIU"}
    finally:
        engine.dispose()

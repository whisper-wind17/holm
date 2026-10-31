"""M4-17 / TC-GW-40 old-head grant fixture: no backfill, defaults or broad grants."""

from uuid import UUID

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from conftest import EphemeralPostgres
from holm_persistence.role_grants import CONTROL, GATEWAY
from migration_test_support import ALEMBIC_CONFIG_PATH
from sqlalchemy.exc import DBAPIError, IntegrityError


def test_M4_17_TC_GW_40_existing_grants_remain_unlimited(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    engine = ephemeral_postgres.engine
    with engine.connect() as connection:
        command.upgrade(
            Config(ALEMBIC_CONFIG_PATH, attributes={"connection": connection}), "20261016_0041"
        )
    user, service = UUID(int=417001), UUID(int=417002)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO core.users (id, username_norm, display_name, role, status, "
                "password_hash, must_change_password, created_at, updated_at) VALUES (:id, "
                "'m417', 'M417', 'admin', 'active', '$argon2id$m417-fixture', false, now(), "
                "now())"
            ),
            {"id": user},
        )
        connection.execute(
            sa.text(
                "INSERT INTO core.ai_services (id, name, public_model_name, description, "
                "enabled, route_version, created_at, updated_at) VALUES (:id, 'M417', "
                "'m417', '', true, 1, now(), now())"
            ),
            {"id": service},
        )
        connection.execute(
            sa.text(
                "INSERT INTO core.user_service_permissions (user_id, service_id, "
                "created_by, created_at) VALUES (:user, :service, :user, now())"
            ),
            {"user": user, "service": service},
        )
    with engine.connect() as connection:
        command.upgrade(Config(ALEMBIC_CONFIG_PATH, attributes={"connection": connection}), "head")
        assert connection.execute(
            sa.text(
                "SELECT max_concurrent_requests, max_requests_per_minute FROM "
                "core.user_service_permissions"
            )
        ).one() == (None, None)
        columns = sa.inspect(connection).get_columns("user_service_permissions", schema="core")
        limits = [column for column in columns if column["name"].startswith("max_")]
        assert len(limits) == 2
        assert all(column["nullable"] and column["default"] is None for column in limits)
    for column, value in [
        ("max_concurrent_requests", 0),
        ("max_concurrent_requests", 65),
        ("max_requests_per_minute", 0),
        ("max_requests_per_minute", 601),
    ]:
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                sa.text(f"UPDATE core.user_service_permissions SET {column} = :value"),
                {"value": value},
            )
    with engine.begin() as connection:
        connection.execute(sa.text(f'SET LOCAL ROLE "{CONTROL}"'))
        connection.execute(
            sa.text(
                "UPDATE core.user_service_permissions SET max_concurrent_requests = 2, "
                "max_requests_per_minute = 3"
            )
        )
    with engine.begin() as connection:
        connection.execute(sa.text(f'SET LOCAL ROLE "{GATEWAY}"'))
        assert connection.execute(
            sa.text(
                "SELECT max_concurrent_requests, max_requests_per_minute FROM "
                "core.user_service_permissions"
            )
        ).one() == (2, 3)
    with pytest.raises(DBAPIError), engine.begin() as connection:
        connection.execute(sa.text(f'SET LOCAL ROLE "{CONTROL}"'))
        connection.execute(sa.text("UPDATE core.user_service_permissions SET created_at = now()"))

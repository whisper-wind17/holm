"""FX-BRAND-03-OLD-DB: a key persisted under the pre-BRAND-03 CHECK constraint."""

from uuid import UUID

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from conftest import temporary_postgres
from migration_test_support import ALEMBIC_CONFIG_PATH


def test_BRAND_03_existing_key_upgrade_preserves_credentials_and_enforces_prefix() -> None:
    key_id = UUID("00000000-0000-7000-8000-000000000303")
    with temporary_postgres() as database:
        with database.engine.connect() as connection:
            config = Config(ALEMBIC_CONFIG_PATH, attributes={"connection": connection})
            command.upgrade(config, "20261020_0044")
        with database.engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO core.users "
                    "(id, username_norm, display_name, role, status, password_hash, "
                    "must_change_password, created_at, updated_at) "
                    "VALUES (:id, 'brand03', 'BRAND-03', 'admin', 'active', "
                    "'$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$c2FsdA', false, now(), now())"
                ),
                {"id": key_id},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO core.api_keys "
                    "(id, user_id, name, lookup_id, display_prefix, last4, pepper_key_id, "
                    "secret_hmac, status, created_at) "
                    "VALUES (:id, :id, 'BRAND-03', 'AAAAAAAAAAAAAAAAAAAAAAAAAA', 'pwsk_', "
                    "'AAAA', 'test', decode(repeat('00', 32), 'hex'), 'Active', now())"
                ),
                {"id": key_id},
            )
            before = dict(
                connection.execute(sa.text("SELECT * FROM core.api_keys")).mappings().one()
            )
        with database.engine.connect() as connection:
            config = Config(ALEMBIC_CONFIG_PATH, attributes={"connection": connection})
            command.upgrade(config, "head")
            after = dict(
                connection.execute(sa.text("SELECT * FROM core.api_keys")).mappings().one()
            )
            assert after == {**before, "display_prefix": "holm_sk_"}
        with (
            database.engine.begin() as connection,
            pytest.raises(sa.exc.IntegrityError, match="ck_api_keys_display_prefix"),
        ):
            connection.execute(sa.text("UPDATE core.api_keys SET display_prefix = 'pwsk_'"))

"""M7-07: a precreated runtime group with LOGIN must stop migration."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from migration_test_support import upgrade_head
from sqlalchemy.exc import DBAPIError


def test_M7_07_TC_DP_13_login_group_role_makes_migration_fail_loudly(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(sa.text("CREATE ROLE holm_control LOGIN"))
    with pytest.raises(DBAPIError, match="runtime group role has elevated attributes"):
        upgrade_head(ephemeral_postgres.engine)
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(sa.text("ALTER ROLE holm_control NOLOGIN"))
        connection.execute(sa.text("CREATE ROLE m7_07_parent NOLOGIN"))
        connection.execute(sa.text("GRANT m7_07_parent TO holm_control"))
    with pytest.raises(DBAPIError, match="runtime group role inherits another role"):
        upgrade_head(ephemeral_postgres.engine)

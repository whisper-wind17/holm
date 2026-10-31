"""M7-04 / TC-DP-13: actual installer LOGIN identities and group privileges."""

from __future__ import annotations

import secrets
import time

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from holm_persistence.install_roles import LOGIN_GROUPS, provision_login_roles
from migration_test_support import upgrade_head
from sqlalchemy.exc import DBAPIError


def test_M7_04_TC_DP_13_real_login_inherits_grants_and_no_ddl(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    upgrade_head(ephemeral_postgres.engine)
    passwords = {name: secrets.token_urlsafe(32) for name in LOGIN_GROUPS}
    with ephemeral_postgres.engine.begin() as connection:
        provision_login_roles(connection, passwords)
        provision_login_roles(connection, passwords)
    # This fixture defaults to trust. Require SCRAM for these exact new roles,
    # retaining the fixture owner's administration path and Unix-only topology.
    hba = ephemeral_postgres.data_directory / "pg_hba.conf"
    original_hba = hba.read_text()
    users = ",".join(group + "_login" for group in LOGIN_GROUPS.values())
    hba.write_text(f"local all {users} scram-sha-256\n" + original_hba)
    with ephemeral_postgres.engine.connect() as connection:
        previous = connection.scalar(sa.text("SELECT pg_conf_load_time()"))
        assert connection.scalar(sa.text("SELECT pg_reload_conf()"))
        deadline = time.monotonic() + 5
        while connection.scalar(sa.text("SELECT pg_conf_load_time()")) <= previous:
            assert time.monotonic() < deadline
            time.sleep(0.01)
    for name, group in LOGIN_GROUPS.items():
        wrong = sa.create_engine(
            ephemeral_postgres.engine.url.set(
                username=group + "_login", password="FX-M7-04-wrong-password"
            ),
            hide_parameters=True,
        )
        try:
            with pytest.raises(DBAPIError):
                wrong.connect()
        finally:
            wrong.dispose()
        login = sa.create_engine(
            ephemeral_postgres.engine.url.set(username=group + "_login", password=passwords[name]),
            hide_parameters=True,
        )
        try:
            with login.connect() as connection:
                assert connection.scalar(sa.text("SELECT current_user")) == group + "_login"
                assert connection.scalar(
                    sa.text("SELECT pg_has_role(current_user, :role, 'USAGE')"), {"role": group}
                )
                assert connection.scalar(
                    sa.text("SELECT has_schema_privilege(current_user, 'core', 'USAGE')")
                )
            with pytest.raises(DBAPIError), login.begin() as connection:
                connection.execute(sa.text("CREATE TABLE core.m704_forbidden (id int)"))
        finally:
            login.dispose()
    with ephemeral_postgres.engine.connect() as connection:
        for name, group in LOGIN_GROUPS.items():
            verifier = connection.scalar(
                sa.text("SELECT rolpassword FROM pg_authid WHERE rolname=:name"),
                {"name": group + "_login"},
            )
            assert verifier.startswith("SCRAM-SHA-256$") and passwords[name] not in verifier

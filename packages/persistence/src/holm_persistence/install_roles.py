"""Installer-owned LOGIN roles; runtime grants remain in role_grants."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from collections.abc import Mapping

import sqlalchemy as sa
from psycopg import sql

from holm_persistence.role_grants import (
    CONTROL,
    DOWNLOAD,
    GATEWAY,
    INSTANCE,
    LEDGER,
    MAINTENANCE,
    RUNTIME_ROLES,
)

LOGIN_GROUPS: Mapping[str, str] = {
    "control_database_url": CONTROL,
    "gateway_database_url": GATEWAY,
    "download_database_url": DOWNLOAD,
    "instance_database_url": INSTANCE,
    "ledger_database_url": LEDGER,
    "maintenance_database_url": MAINTENANCE,
}


class InstallRoleError(RuntimeError):
    pass


def scram_verifier(password: str) -> str:
    """Avoid sending a plaintext password as a SQL statement or logging parameter."""
    salt = secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 4096)
    client = hmac.digest(salted, b"Client Key", "sha256")
    stored = hashlib.sha256(client).digest()
    server = hmac.digest(salted, b"Server Key", "sha256")

    def encode(value: bytes) -> str:
        return base64.b64encode(value).decode("ascii")

    return f"SCRAM-SHA-256$4096:{encode(salt)}${encode(stored)}:{encode(server)}"


def provision_login_roles(connection: sa.Connection, passwords: Mapping[str, str]) -> None:
    if set(passwords) != set(LOGIN_GROUPS) or set(LOGIN_GROUPS.values()) != set(RUNTIME_ROLES):
        raise InstallRoleError("login_role_set_mismatch")
    for secret_name, group in LOGIN_GROUPS.items():
        login = group + "_login"
        parent = connection.execute(
            sa.text("SELECT rolcanlogin FROM pg_roles WHERE rolname=:name"), {"name": group}
        ).first()
        if parent is None or parent.rolcanlogin:
            raise InstallRoleError("runtime_group_missing")
        row = connection.execute(
            sa.text(
                "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, "
                "rolreplication, rolbypassrls FROM pg_roles WHERE rolname=:name"
            ),
            {"name": login},
        ).first()
        if row is None:
            statement = sql.SQL(
                "CREATE ROLE {} LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOREPLICATION NOBYPASSRLS PASSWORD {}"
            ).format(sql.Identifier(login), sql.Literal(scram_verifier(passwords[secret_name])))
            connection.exec_driver_sql(statement.as_string())
        elif not row.rolcanlogin or any(
            (row.rolsuper, row.rolcreatedb, row.rolcreaterole, row.rolreplication, row.rolbypassrls)
        ):
            raise InstallRoleError("unsafe_existing_login")
        memberships = set(
            connection.scalars(
                sa.text(
                    "SELECT parent.rolname FROM pg_auth_members m "
                    "JOIN pg_roles parent ON parent.oid=m.roleid "
                    "JOIN pg_roles member ON member.oid=m.member WHERE member.rolname=:name"
                ),
                {"name": login},
            )
        )
        if memberships - {group}:
            raise InstallRoleError("unexpected_login_membership")
        connection.exec_driver_sql(
            sql.SQL("GRANT {} TO {}")
            .format(sql.Identifier(group), sql.Identifier(login))
            .as_string()
        )

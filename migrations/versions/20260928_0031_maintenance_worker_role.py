"""Dedicated heartbeat-only maintenance group.

Revision ID: 20260928_0031
Revises: 20260923_0030
"""

from collections.abc import Sequence

from alembic import op
from holm_persistence.role_grants import M7_08B_TABLE_GRANTS, MAINTENANCE

revision: str = "20260928_0031"
down_revision: str | None = "20260923_0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(f"""DO $role$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{MAINTENANCE}') THEN
            CREATE ROLE {MAINTENANCE} NOLOGIN;
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{MAINTENANCE}' AND
            (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole
             OR rolreplication OR rolbypassrls))
        OR EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member
                   WHERE r.rolname = '{MAINTENANCE}') THEN
            RAISE EXCEPTION 'maintenance group role has elevated privileges';
        END IF;
    END $role$""")
    # Revoke possible prior direct privileges before applying the narrow matrix.
    op.execute(f"""DO $database$ BEGIN EXECUTE format(
        'REVOKE ALL ON DATABASE %I FROM {MAINTENANCE}', current_database()); END $database$""")
    for schema in ("public", "core", "usage", "telemetry"):
        op.execute(f"REVOKE ALL ON SCHEMA {schema} FROM {MAINTENANCE}")
        op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM {MAINTENANCE}")
        op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {schema} FROM {MAINTENANCE}")
        op.execute(f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA {schema} FROM {MAINTENANCE}")
    op.execute(f"GRANT USAGE ON SCHEMA core TO {MAINTENANCE}")
    operations = {"S": "SELECT", "I": "INSERT", "U": "UPDATE"}
    for table, grants in M7_08B_TABLE_GRANTS.items():
        for role, letters in grants.items():
            privileges = ", ".join(operations[letter] for letter in letters)
            op.execute(f"GRANT {privileges} ON TABLE {table} TO {role}")


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON TABLE core.component_heartbeats FROM {MAINTENANCE}")
    op.execute(f"REVOKE USAGE ON SCHEMA core FROM {MAINTENANCE}")
    # Cluster roles and installer-owned login memberships outlive the revision.

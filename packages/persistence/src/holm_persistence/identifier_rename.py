"""Compatibility for databases at any pre-brand revision."""

import sqlalchemy as sa
from sqlalchemy.engine import Connection

_ROLES = (
    "control",
    "gateway",
    "download_worker",
    "instance_controller",
    "ledger_writer",
    "maintenance_worker",
)
_FUNCTIONS = (
    "update_api_key_last_used",
    "drop_expired_month_partitions",
    "ensure_month_partition",
    "schedule_artifact_checks",
)


def _needs_rename(old: str, new: str, *, old_exists: bool, new_exists: bool) -> bool:
    if old_exists and new_exists:
        raise RuntimeError(f"Identifier rename failed: both identifiers exist: {old}, {new}")
    return old_exists


def rename_legacy_identifiers(connection: Connection) -> None:
    """Rename existing objects in the caller's transaction before historical grants run.

    Early revisions may not have created these objects yet. RENAME preserves OIDs,
    memberships and ACLs; conflicting names abort the entire migration transaction.
    """
    for suffix in _ROLES:
        old, new = f"privatews_{suffix}", f"holm_{suffix}"
        names = set(
            connection.execute(
                sa.text("SELECT rolname FROM pg_catalog.pg_roles WHERE rolname IN (:old, :new)"),
                {"old": old, "new": new},
            ).scalars()
        )
        if _needs_rename(old, new, old_exists=old in names, new_exists=new in names):
            connection.exec_driver_sql(f'ALTER ROLE "{old}" RENAME TO "{new}"')

    for suffix in _FUNCTIONS:
        old, new = f"privatews_{suffix}", f"holm_{suffix}"
        functions = connection.execute(
            sa.text(
                "SELECT p.proname, pg_catalog.pg_get_function_identity_arguments(p.oid) "
                "FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n "
                "ON n.oid = p.pronamespace WHERE n.nspname = 'core' "
                "AND p.proname IN (:old, :new) ORDER BY p.oid"
            ),
            {"old": old, "new": new},
        ).all()
        names = {row[0] for row in functions}
        if _needs_rename(old, new, old_exists=old in names, new_exists=new in names):
            # Signatures come from PostgreSQL, preserving every overload and its OID/ACL.
            for _, signature in functions:
                connection.exec_driver_sql(
                    f'ALTER FUNCTION core."{old}"({signature}) RENAME TO "{new}"'
                )

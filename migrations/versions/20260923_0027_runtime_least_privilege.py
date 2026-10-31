"""Runtime role grants and narrow DDL-owner functions.

Revision ID: 20260923_0027
Revises: 20260923_0026
"""

from collections.abc import Sequence

from alembic import op
from holm_persistence.role_grants import (
    CONTROL,
    DOWNLOAD,
    INSTANCE,
    LEDGER,
)
from holm_persistence.role_grants import (
    M7_07_COLUMN_SELECTS as COLUMN_SELECTS,
)
from holm_persistence.role_grants import (
    M7_07_RUNTIME_ROLES as RUNTIME_ROLES,
)
from holm_persistence.role_grants import (
    M7_07_TABLE_GRANTS as TABLE_GRANTS,
)

revision: str = "20260923_0027"
down_revision: str | None = "20260923_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SQL_OPERATIONS = {"S": "SELECT", "I": "INSERT", "U": "UPDATE", "D": "DELETE"}
_ENSURE = "core.holm_ensure_month_partition(text, date)"
_DROP = "core.holm_drop_expired_month_partitions(text, timestamptz)"
_LAST_USED = "core.holm_update_api_key_last_used(uuid, timestamptz)"


def _quote(value: str) -> str:
    # All inputs originate from the frozen matrix and validated metadata.
    return '"' + value.replace('"', '""') + '"'


def _qualified(value: str) -> str:
    schema, table = value.split(".", 1)
    return f"{_quote(schema)}.{_quote(table)}"


def _create_roles() -> None:
    for role in RUNTIME_ROLES:
        # The installer's DDL Owner precreates the roles. No CREATEROLE is
        # needed when they already exist; local superuser fixtures create them.
        op.execute(
            "DO $role$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '"
            + role
            + "') THEN CREATE ROLE "
            + _quote(role)
            + " NOLOGIN; END IF; END $role$"
        )
        op.execute(
            "DO $role$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles r WHERE r.rolname = '"
            + role
            + "' AND (r.rolcanlogin OR r.rolsuper OR r.rolcreatedb OR "
            "r.rolcreaterole OR r.rolreplication OR r.rolbypassrls)) "
            "THEN RAISE EXCEPTION 'runtime group role has elevated attributes'; "
            "END IF; IF EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles r "
            "ON r.oid = m.member WHERE r.rolname = '"
            + role
            + "') THEN RAISE EXCEPTION 'runtime group role inherits another role'; "
            "END IF; END $role$"
        )


def _grant_tables() -> None:
    # PostgreSQL grants TEMP on new databases to PUBLIC by default. Removing
    # that inherited DDL capability is required before any runtime role is
    # useful as a no-DDL boundary.
    op.execute(
        "DO $database$ BEGIN EXECUTE format("
        "'REVOKE TEMPORARY, CREATE ON DATABASE %I FROM PUBLIC', current_database()); "
        "END $database$"
    )
    op.execute("REVOKE CREATE ON SCHEMA public, core, usage, telemetry FROM PUBLIC")
    for role in RUNTIME_ROLES:
        op.execute(f"GRANT USAGE ON SCHEMA core, usage, telemetry TO {_quote(role)}")
    for table, decisions in TABLE_GRANTS.items():
        target = _qualified(table)
        for role in RUNTIME_ROLES:
            op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {target} FROM {_quote(role)}")
        for role, operations in decisions.items():
            columns = COLUMN_SELECTS.get((table, role))
            if columns is not None:
                quoted = ", ".join(map(_quote, columns))
                op.execute(f"GRANT SELECT ({quoted}) ON TABLE {target} TO {_quote(role)}")
            for operation in operations:
                if operation != "S" or columns is None:
                    op.execute(
                        f"GRANT {_SQL_OPERATIONS[operation]} ON TABLE {target} TO {_quote(role)}"
                    )
    op.execute(f"GRANT SELECT ON TABLE alembic_version TO {_quote(CONTROL)}")


def _create_functions() -> None:
    op.execute(
        """
CREATE FUNCTION core.holm_ensure_month_partition(parent_name text, month_start date)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    parent_schema text;
    parent_table text;
    child_name text;
    next_month date;
BEGIN
    IF parent_name = 'usage.request_records' THEN
        parent_schema := 'usage'; parent_table := 'request_records';
    ELSIF parent_name = 'telemetry.instance_metrics_1m' THEN
        parent_schema := 'telemetry'; parent_table := 'instance_metrics_1m';
    ELSIF parent_name = 'telemetry.service_metrics_1m' THEN
        parent_schema := 'telemetry'; parent_table := 'service_metrics_1m';
    ELSIF parent_name = 'telemetry.gpu_metrics_1m' THEN
        parent_schema := 'telemetry'; parent_table := 'gpu_metrics_1m';
    ELSE
        RAISE EXCEPTION 'unmanaged partition parent' USING ERRCODE = '22023';
    END IF;
    IF month_start IS NULL OR extract(day FROM month_start) <> 1 THEN
        RAISE EXCEPTION 'month_start must be the first day of a month'
            USING ERRCODE = '22023';
    END IF;
    next_month := (month_start + interval '1 month')::date;
    child_name := parent_table || '_' || to_char(month_start, 'YYYYMM');
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS %I.%I PARTITION OF %I.%I '
        || 'FOR VALUES FROM (%L) TO (%L)',
        parent_schema, child_name, parent_schema, parent_table,
        month_start::timestamp AT TIME ZONE 'UTC',
        next_month::timestamp AT TIME ZONE 'UTC'
    );
    IF NOT EXISTS (
        SELECT 1 FROM pg_inherits i
        JOIN pg_class p ON p.oid = i.inhparent
        JOIN pg_namespace pn ON pn.oid = p.relnamespace
        JOIN pg_class c ON c.oid = i.inhrelid
        JOIN pg_namespace cn ON cn.oid = c.relnamespace
        WHERE pn.nspname = parent_schema AND p.relname = parent_table
          AND cn.nspname = parent_schema AND c.relname = child_name
    ) THEN
        RAISE EXCEPTION 'managed partition name belongs to another table'
            USING ERRCODE = '22023';
    END IF;
    RETURN child_name;
END $function$
"""
    )
    op.execute(
        """
CREATE FUNCTION core.holm_drop_expired_month_partitions(
    parent_name text, cutoff timestamptz
) RETURNS text[] LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    parent_schema text;
    parent_table text;
    row record;
    parts text[];
    year_number integer;
    month_number integer;
    start_at timestamptz;
    end_at timestamptz;
    dropped text[] := ARRAY[]::text[];
BEGIN
    IF parent_name = 'usage.request_records' THEN
        parent_schema := 'usage'; parent_table := 'request_records';
    ELSIF parent_name = 'telemetry.instance_metrics_1m' THEN
        parent_schema := 'telemetry'; parent_table := 'instance_metrics_1m';
    ELSIF parent_name = 'telemetry.service_metrics_1m' THEN
        parent_schema := 'telemetry'; parent_table := 'service_metrics_1m';
    ELSIF parent_name = 'telemetry.gpu_metrics_1m' THEN
        parent_schema := 'telemetry'; parent_table := 'gpu_metrics_1m';
    ELSE
        RAISE EXCEPTION 'unmanaged partition parent' USING ERRCODE = '22023';
    END IF;
    IF cutoff IS NULL THEN
        RAISE EXCEPTION 'cutoff is required' USING ERRCODE = '22023';
    END IF;
    FOR row IN
        SELECT c.relname AS child_name, pg_get_expr(c.relpartbound, c.oid) AS bounds
        FROM pg_inherits i
        JOIN pg_class p ON p.oid = i.inhparent
        JOIN pg_namespace pn ON pn.oid = p.relnamespace
        JOIN pg_class c ON c.oid = i.inhrelid
        JOIN pg_namespace cn ON cn.oid = c.relnamespace
        WHERE pn.nspname = parent_schema AND p.relname = parent_table
          AND cn.nspname = parent_schema ORDER BY c.relname
    LOOP
        IF left(row.child_name, length(parent_table) + 1) <> parent_table || '_'
           OR right(row.child_name, 6) !~ '^[0-9]{6}$'
           OR length(row.child_name) <> length(parent_table) + 7 THEN
            CONTINUE;
        END IF;
        year_number := left(right(row.child_name, 6), 4)::integer;
        month_number := right(row.child_name, 2)::integer;
        IF year_number < 1 OR year_number > 9998 OR month_number < 1 OR month_number > 12 THEN
            CONTINUE;
        END IF;
        parts := regexp_match(row.bounds, '^FOR VALUES FROM \\(''([^'']+)''\\) TO \\(''([^'']+)''\\)$');
        IF parts IS NULL THEN CONTINUE; END IF;
        start_at := make_timestamptz(year_number, month_number, 1, 0, 0, 0, 'UTC');
        end_at := start_at + interval '1 month';
        IF parts[1]::timestamptz <> start_at OR parts[2]::timestamptz <> end_at
           OR end_at >= cutoff THEN
            CONTINUE;
        END IF;
        EXECUTE format('DROP TABLE %I.%I', parent_schema, row.child_name);
        dropped := array_append(dropped, row.child_name);
    END LOOP;
    RETURN dropped;
END $function$
"""
    )
    op.execute(
        """
CREATE FUNCTION core.holm_update_api_key_last_used(key_id uuid, used_at timestamptz)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF key_id IS NULL OR used_at IS NULL THEN
        RAISE EXCEPTION 'key and time are required' USING ERRCODE = '22023';
    END IF;
    UPDATE core.api_keys
    SET last_used_at = greatest(coalesce(last_used_at, '-infinity'::timestamptz), used_at)
    WHERE id = key_id AND created_at <= used_at;
    RETURN FOUND;
END $function$
"""
    )
    for signature in (_ENSURE, _DROP, _LAST_USED):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    for role in (CONTROL, LEDGER):
        op.execute(f"GRANT EXECUTE ON FUNCTION {_ENSURE} TO {_quote(role)}")
        op.execute(f"GRANT EXECUTE ON FUNCTION {_DROP} TO {_quote(role)}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_LAST_USED} TO {_quote(LEDGER)}")


def upgrade() -> None:
    _create_roles()
    _grant_tables()
    _create_functions()


def downgrade() -> None:
    for signature in (_LAST_USED, _DROP, _ENSURE):
        op.execute(f"DROP FUNCTION {signature}")
    for table in TABLE_GRANTS:
        for role in RUNTIME_ROLES:
            op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {_qualified(table)} FROM {_quote(role)}")
    op.execute(f"REVOKE SELECT ON TABLE alembic_version FROM {_quote(CONTROL)}")
    for role in RUNTIME_ROLES:
        op.execute(f"REVOKE USAGE ON SCHEMA core, usage, telemetry FROM {_quote(role)}")
    # Cluster roles outlive this database migration by design.

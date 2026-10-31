"""Enforce the minimum retention period in the privileged partition drop."""

from collections.abc import Sequence

from alembic import op

revision: str = "20260923_0030"
down_revision: str | None = "20260923_0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION core.holm_drop_expired_month_partitions(
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

_CUTOFF_REQUIRED = """    IF cutoff IS NULL THEN
        RAISE EXCEPTION 'cutoff is required' USING ERRCODE = '22023';
    END IF;
"""
_CUTOFF_FLOOR = """    IF cutoff > now() - interval '30 days' THEN
        RAISE EXCEPTION 'cutoff exceeds minimum retention period' USING ERRCODE = '22023';
    END IF;
"""


def upgrade() -> None:
    op.execute(_FUNCTION_SQL.replace(_CUTOFF_REQUIRED, _CUTOFF_REQUIRED + _CUTOFF_FLOOR))


def downgrade() -> None:
    op.execute(_FUNCTION_SQL)

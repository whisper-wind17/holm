"""Bounded maintenance coordination for read-only Artifact checks.

Revision ID: 20260930_0034
Revises: 20260930_0033
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260930_0034"
down_revision: str | None = "20260930_0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name in ("light_check_requested_at", "light_checked_at"):
        op.add_column("model_artifacts", sa.Column(name, sa.DateTime(timezone=True)), schema="core")
    # No input parameters: callers cannot select a path, target, payload, actor,
    # job kind, clock or batch size. Existing table grants remain unchanged.
    op.execute("""
        CREATE FUNCTION core.holm_schedule_artifact_checks()
        RETURNS TABLE (scheduled integer, pending boolean, failed boolean)
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog
        SET statement_timeout = '4s'
        SET lock_timeout = '1s'
        AS $function$
        DECLARE
            checked_now timestamptz := clock_timestamp();
            candidate uuid;
        BEGIN
            scheduled := 0;
            FOR candidate IN
                SELECT a.id FROM core.model_artifacts a
                WHERE a.status = 'Ready'
                  AND greatest(a.created_at, a.light_check_requested_at, a.light_checked_at)
                      <= checked_now - interval '24 hours'
                  AND NOT EXISTS (
                      SELECT 1 FROM core.jobs j WHERE j.target_object_id = a.id
                      AND j.job_type IN ('artifact.light_verify', 'artifact.delete')
                      AND j.status IN ('Queued', 'Running')
                  )
                ORDER BY greatest(a.created_at, a.light_check_requested_at, a.light_checked_at), a.id
                LIMIT 32 FOR UPDATE OF a SKIP LOCKED
            LOOP
                INSERT INTO core.jobs
                    (id, job_type, payload, target_object_id, target_object_version,
                     status, priority, attempt, max_attempts, run_after, created_at, updated_at)
                VALUES (gen_random_uuid(), 'artifact.light_verify', '{}'::jsonb, candidate, 0,
                        'Queued', -10, 0, 3, checked_now, checked_now, checked_now);
                UPDATE core.model_artifacts SET light_check_requested_at = checked_now
                    WHERE id = candidate;
                scheduled := scheduled + 1;
            END LOOP;
            SELECT EXISTS (
                SELECT 1 FROM core.jobs j
                WHERE j.job_type = 'artifact.light_verify' AND j.status IN ('Queued', 'Running')
            ) INTO pending;
            SELECT EXISTS (
                SELECT 1 FROM core.jobs j JOIN core.model_artifacts a ON a.id = j.target_object_id
                WHERE j.job_type = 'artifact.light_verify' AND j.status = 'Failed'
                  AND j.created_at = a.light_check_requested_at
            ) INTO failed;
            RETURN NEXT;
        END;
        $function$;
        REVOKE ALL ON FUNCTION core.holm_schedule_artifact_checks() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION core.holm_schedule_artifact_checks()
            TO holm_maintenance_worker;
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION core.holm_schedule_artifact_checks()")
    op.drop_column("model_artifacts", "light_checked_at", schema="core")
    op.drop_column("model_artifacts", "light_check_requested_at", schema="core")

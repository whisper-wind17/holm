"""Create core.jobs and core.component_heartbeats.

Revision ID: 20260829_0005
Revises: 20260829_0004
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260829_0005"
down_revision: str | None = "20260829_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "core"


def upgrade() -> None:
    """Add the PostgreSQL job lease and worker heartbeat relations."""
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("job_type", sa.Text(), nullable=False),
        # Operational metadata only. The platform's request/response body and
        # generated-content categories are forbidden from every persistence path.
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("target_object_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("target_object_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_jobs"),
        sa.CheckConstraint(
            "char_length(job_type) BETWEEN 1 AND 128",
            name="ck_jobs_job_type_length",
        ),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_jobs_payload_object"),
        sa.CheckConstraint(
            "target_object_version >= 0",
            name="ck_jobs_target_version_nonnegative",
        ),
        sa.CheckConstraint(
            "status IN ('Queued', 'Running', 'Succeeded', 'Failed')",
            name="ck_jobs_status",
        ),
        sa.CheckConstraint(
            "attempt >= 0 AND attempt <= max_attempts",
            name="ck_jobs_attempt_bounded",
        ),
        sa.CheckConstraint("max_attempts >= 1", name="ck_jobs_max_attempts_positive"),
        sa.CheckConstraint(
            "(status = 'Running') = (lease_owner IS NOT NULL AND lease_until IS NOT NULL)",
            name="ck_jobs_running_has_lease",
        ),
        sa.CheckConstraint(
            "(status IN ('Succeeded', 'Failed')) = (completed_at IS NOT NULL)",
            name="ck_jobs_terminal_has_completion",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_jobs_claim_order",
        "jobs",
        ["status", "run_after", sa.text("priority DESC"), "created_at"],
        schema=SCHEMA,
    )

    op.create_table(
        "component_heartbeats",
        sa.Column("component", sa.Text(), nullable=False),
        sa.Column("instance_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint(
            "component",
            "instance_id",
            name="pk_component_heartbeats",
        ),
        sa.CheckConstraint(
            "component IN ('download-worker', 'instance-controller', 'telemetry-worker', "
            "'ledger-writer', 'maintenance-worker')",
            name="ck_component_heartbeats_component",
        ),
        sa.CheckConstraint(
            "status IN ('Healthy', 'Degraded', 'Stopping')",
            name="ck_component_heartbeats_status",
        ),
        sa.CheckConstraint(
            "char_length(instance_id) BETWEEN 1 AND 128",
            name="ck_component_heartbeats_instance_id_length",
        ),
        sa.CheckConstraint(
            "char_length(version) BETWEEN 1 AND 128",
            name="ck_component_heartbeats_version_length",
        ),
        sa.CheckConstraint(
            "reason IS NULL OR char_length(reason) <= 512",
            name="ck_component_heartbeats_reason_length",
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    """Remove only the job lease and heartbeat relations."""
    op.drop_table("component_heartbeats", schema=SCHEMA)
    op.drop_table("jobs", schema=SCHEMA)

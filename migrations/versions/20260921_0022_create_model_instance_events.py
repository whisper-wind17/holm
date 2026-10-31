"""Create structured Model Instance lifecycle events.

Revision ID: 20260921_0022
Revises: 20260918_0021
Create Date: 2026-09-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260921_0022"
down_revision: str | None = "20260918_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "core"
TABLE = "model_instance_events"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("instance_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("from_status", sa.Text(), nullable=True),
        sa.Column("to_status", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_model_instance_events"),
        sa.CheckConstraint(
            "event_type IN ('StatusChanged', 'RestartRequested', 'RetryRequested', "
            "'DeploymentCancelled', 'GpuSelectionChanged', 'WaitingForGpu')",
            name="ck_model_instance_events_event_type",
        ),
        sa.CheckConstraint(
            "actor IN ('admin', 'controller', 'system')",
            name="ck_model_instance_events_actor",
        ),
        sa.CheckConstraint(
            "from_status IS NULL OR from_status IN "
            "('WaitingForResource', 'Starting', 'Running', 'Restarting', 'Unhealthy', "
            "'Stopping', 'Stopped', 'Failed')",
            name="ck_model_instance_events_from_status",
        ),
        sa.CheckConstraint(
            "to_status IS NULL OR to_status IN "
            "('WaitingForResource', 'Starting', 'Running', 'Restarting', 'Unhealthy', "
            "'Stopping', 'Stopped', 'Failed')",
            name="ck_model_instance_events_to_status",
        ),
        sa.CheckConstraint(
            "detail = '{}'::jsonb",
            name="ck_model_instance_events_detail_allowlist",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_model_instance_events_instance_occurred",
        TABLE,
        ["instance_id", sa.text("occurred_at DESC"), sa.text("id DESC")],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table(TABLE, schema=SCHEMA)

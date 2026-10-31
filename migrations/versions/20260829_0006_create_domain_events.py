"""Create core.domain_events.

Revision ID: 20260829_0006
Revises: 20260829_0005
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260829_0006"
down_revision: str | None = "20260829_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "core"


def upgrade() -> None:
    """Add the transaction-ordered event log used by replay and invalidation."""
    op.create_table(
        "domain_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column(
            "xact_id",
            sa.BigInteger(),
            server_default=sa.text("(pg_current_xact_id()::text::bigint)"),
            nullable=False,
        ),
        sa.Column("aggregate_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_domain_events"),
        sa.UniqueConstraint(
            "aggregate_id",
            "version",
            name="uq_domain_events_aggregate_id_version",
        ),
        sa.CheckConstraint(
            "char_length(event) BETWEEN 1 AND 128",
            name="ck_domain_events_event_length",
        ),
        sa.CheckConstraint("version > 0", name="ck_domain_events_version_positive"),
        sa.CheckConstraint("xact_id > 0", name="ck_domain_events_xact_id_positive"),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_domain_events_payload_object",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_domain_events_occurred_at",
        "domain_events",
        ["occurred_at"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    """Remove only the event log introduced by this revision."""
    op.drop_table("domain_events", schema=SCHEMA)

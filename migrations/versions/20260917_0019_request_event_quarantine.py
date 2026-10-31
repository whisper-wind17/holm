"""Create metadata-only Request Ledger conflict quarantine.

Revision ID: 20260917_0019
Revises: 20260917_0018
Create Date: 2026-09-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260917_0019"
down_revision: str | None = "20260917_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "usage"
EVENT_TYPES = ("Received", "Rejected", "InProgress", "Completed", "Failed", "Aborted", "Unknown")


def upgrade() -> None:
    event_types = ", ".join(f"'{event_type}'" for event_type in EVENT_TYPES)
    op.create_table(
        "request_event_quarantine",
        sa.Column("request_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("event_seq", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("event_received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("registered_received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "request_id",
            "event_seq",
            name="pk_request_event_quarantine",
        ),
        sa.CheckConstraint(
            "event_seq > 0",
            name="ck_request_event_quarantine_event_seq",
        ),
        sa.CheckConstraint(
            f"event_type IN ({event_types})",
            name="ck_request_event_quarantine_event_type",
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("request_event_quarantine", schema=SCHEMA)

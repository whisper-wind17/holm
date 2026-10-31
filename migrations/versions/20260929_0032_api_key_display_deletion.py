"""Retain API Key history while hiding inactive display records.

Revision ID: 20260929_0032
Revises: 20260928_0031
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0032"
down_revision: str | None = "20260928_0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        schema="core",
    )


def downgrade() -> None:
    op.drop_column("api_keys", "deleted_at", schema="core")

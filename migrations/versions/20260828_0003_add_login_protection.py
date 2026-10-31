"""Persist login failure counters and exponential-backoff locks.

Revision ID: 20260828_0003
Revises: 20260828_0002
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260828_0003"
down_revision: str | None = "20260828_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "core"
TOKEN_HASH_PATTERN = "^[0-9a-f]{64}$"


def upgrade() -> None:
    """Add the identifier-neutral, process-independent lockout state."""
    op.create_table(
        "login_protection",
        sa.Column("principal_hash", sa.Text(), nullable=False),
        sa.Column("failed_attempts", sa.Integer(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("principal_hash", name="pk_login_protection"),
        sa.CheckConstraint(
            f"principal_hash ~ '{TOKEN_HASH_PATTERN}'",
            name="ck_login_protection_principal_hash_sha256",
        ),
        sa.CheckConstraint(
            "failed_attempts >= 0",
            name="ck_login_protection_failed_attempts_nonnegative",
        ),
        sa.CheckConstraint(
            "(failed_attempts < 5) = (locked_until IS NULL)",
            name="ck_login_protection_lock_matches_threshold",
        ),
        sa.CheckConstraint(
            "locked_until IS NULL OR locked_until > updated_at",
            name="ck_login_protection_lock_after_update",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_login_protection_updated_at",
        "login_protection",
        ["updated_at"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    """Remove only login-protection state."""
    op.drop_index(
        "ix_login_protection_updated_at",
        table_name="login_protection",
        schema=SCHEMA,
    )
    op.drop_table("login_protection", schema=SCHEMA)

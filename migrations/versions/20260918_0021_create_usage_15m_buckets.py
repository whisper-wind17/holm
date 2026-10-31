"""Create the sparse UTC 15-minute Usage aggregate.

Revision ID: 20260918_0021
Revises: 20260918_0020
Create Date: 2026-09-18 06:28:54.754719
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260918_0021"
down_revision: str | None = "20260918_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "usage"
TABLE = "usage_15m"


def upgrade() -> None:
    # Historical Request Records are deliberately not backfilled. The periodic
    # writer starts by recomputing only the current and previous UTC buckets.
    op.create_table(
        TABLE,
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("api_key_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("service_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("requests", sa.BigInteger(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("error_requests", sa.BigInteger(), nullable=False),
        sa.Column("aborted_requests", sa.BigInteger(), nullable=False),
        sa.Column("partial_token_requests", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "bucket_start",
            "user_id",
            "api_key_id",
            "service_id",
            name="pk_usage_15m",
        ),
        sa.CheckConstraint(
            "bucket_start = date_bin(INTERVAL '15 minutes', bucket_start, "
            "TIMESTAMPTZ '1970-01-01 00:00:00+00')",
            name="ck_usage_15m_bucket_start_aligned",
        ),
        sa.CheckConstraint("requests >= 0", name="ck_usage_15m_requests"),
        sa.CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0",
            name="ck_usage_15m_input_tokens",
        ),
        sa.CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0",
            name="ck_usage_15m_output_tokens",
        ),
        sa.CheckConstraint(
            "error_requests >= 0",
            name="ck_usage_15m_error_requests",
        ),
        sa.CheckConstraint(
            "aborted_requests >= 0",
            name="ck_usage_15m_aborted_requests",
        ),
        sa.CheckConstraint(
            "partial_token_requests >= 0",
            name="ck_usage_15m_partial_token_requests",
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table(TABLE, schema=SCHEMA)

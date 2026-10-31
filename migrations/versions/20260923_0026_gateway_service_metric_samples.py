"""Store bounded cumulative Gateway service metric samples.

Revision ID: 20260923_0026
Revises: 20260922_0025
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260923_0026"
down_revision: str | None = "20260922_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "gateway_service_metric_samples",
        sa.Column("service_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("gateway_start_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ttft_buckets", sa.ARRAY(sa.BigInteger()), nullable=False),
        sa.Column("tpot_buckets", sa.ARRAY(sa.BigInteger()), nullable=False),
        sa.Column("e2e_buckets", sa.ARRAY(sa.BigInteger()), nullable=False),
        sa.Column("output_tokens_total", sa.BigInteger(), nullable=False),
        sa.Column("accepted_requests", sa.BigInteger(), nullable=False),
        sa.Column("rejected_requests", sa.BigInteger(), nullable=False),
        sa.Column("client_disconnects", sa.BigInteger(), nullable=False),
        sa.Column("gateway_errors", sa.BigInteger(), nullable=False),
        sa.Column("runtime_errors", sa.BigInteger(), nullable=False),
        sa.Column("timeout_errors", sa.BigInteger(), nullable=False),
        sa.Column("inflight", sa.Integer(), nullable=False),
        schema="telemetry",
    )


def downgrade() -> None:
    op.drop_table("gateway_service_metric_samples", schema="telemetry")

"""Store service end-to-end latency P50 and P99.

Revision ID: 20260923_0029
Revises: 20260923_0028
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260923_0029"
down_revision: str | None = "20260923_0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "service_metrics_1m",
        sa.Column("e2e_latency_p50_ms", sa.Float(), nullable=True),
        schema="telemetry",
    )
    op.add_column(
        "service_metrics_1m",
        sa.Column("e2e_latency_p99_ms", sa.Float(), nullable=True),
        schema="telemetry",
    )


def downgrade() -> None:
    op.drop_column("service_metrics_1m", "e2e_latency_p99_ms", schema="telemetry")
    op.drop_column("service_metrics_1m", "e2e_latency_p50_ms", schema="telemetry")

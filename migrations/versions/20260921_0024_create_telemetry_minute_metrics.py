"""Create partitioned minute telemetry wide tables.

Revision ID: 20260921_0024
Revises: 20260921_0023
Create Date: 2026-09-21 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260921_0024"
down_revision: str | None = "20260921_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "telemetry"


def _common_columns(
    identity_name: str, identity_type: sa.types.TypeEngine[object]
) -> list[sa.Column[object]]:
    return [
        sa.Column("bucket_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(identity_name, identity_type, nullable=False),
        sa.Column("quality", sa.Text(), nullable=False),
        sa.Column("telemetry_status", sa.Text(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "instance_metrics_1m",
        *_common_columns("instance_id", sa.Uuid(as_uuid=True)),
        sa.Column("ttft_p50_ms", sa.Float(), nullable=True),
        sa.Column("ttft_p95_ms", sa.Float(), nullable=True),
        sa.Column("ttft_p99_ms", sa.Float(), nullable=True),
        sa.Column("output_tokens_delta", sa.Float(), nullable=True),
        sa.Column("output_tps_avg", sa.Float(), nullable=True),
        sa.Column("output_tps_max", sa.Float(), nullable=True),
        sa.Column("requests_delta", sa.Float(), nullable=True),
        sa.Column("running_avg", sa.Float(), nullable=True),
        sa.Column("running_max", sa.Float(), nullable=True),
        sa.Column("running_last", sa.Float(), nullable=True),
        sa.Column("waiting_avg", sa.Float(), nullable=True),
        sa.Column("waiting_max", sa.Float(), nullable=True),
        sa.Column("waiting_last", sa.Float(), nullable=True),
        sa.Column("queue_time_p95_ms", sa.Float(), nullable=True),
        sa.Column("kv_cache_avg", sa.Float(), nullable=True),
        sa.Column("kv_cache_max", sa.Float(), nullable=True),
        sa.Column("kv_cache_last", sa.Float(), nullable=True),
        sa.Column("errors_delta", sa.Float(), nullable=True),
        sa.Column("error_rate", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("bucket_at", "instance_id", name="pk_instance_metrics_1m"),
        schema=SCHEMA,
        postgresql_partition_by="RANGE (bucket_at)",
    )
    op.create_table(
        "service_metrics_1m",
        *_common_columns("service_id", sa.Uuid(as_uuid=True)),
        sa.Column("ttft_p50_ms", sa.Float(), nullable=True),
        sa.Column("ttft_p95_ms", sa.Float(), nullable=True),
        sa.Column("ttft_p99_ms", sa.Float(), nullable=True),
        sa.Column("tpot_p50_ms", sa.Float(), nullable=True),
        sa.Column("tpot_p95_ms", sa.Float(), nullable=True),
        sa.Column("e2e_latency_p95_ms", sa.Float(), nullable=True),
        sa.Column("output_tokens_delta", sa.Float(), nullable=True),
        sa.Column("output_tps_avg", sa.Float(), nullable=True),
        sa.Column("output_tps_max", sa.Float(), nullable=True),
        sa.Column("inflight_avg", sa.Float(), nullable=True),
        sa.Column("inflight_max", sa.Float(), nullable=True),
        sa.Column("inflight_last", sa.Float(), nullable=True),
        sa.Column("request_rate_avg", sa.Float(), nullable=True),
        sa.Column("request_rate_max", sa.Float(), nullable=True),
        sa.Column("accepted_requests", sa.BigInteger(), nullable=True),
        sa.Column("rejected_requests", sa.BigInteger(), nullable=True),
        sa.Column("client_disconnects", sa.BigInteger(), nullable=True),
        sa.Column("error_requests", sa.BigInteger(), nullable=True),
        sa.Column("error_rate", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("bucket_at", "service_id", name="pk_service_metrics_1m"),
        schema=SCHEMA,
        postgresql_partition_by="RANGE (bucket_at)",
    )
    op.create_table(
        "gpu_metrics_1m",
        *_common_columns("gpu_id", sa.Text()),
        sa.Column("utilization_avg", sa.Float(), nullable=True),
        sa.Column("utilization_max", sa.Float(), nullable=True),
        sa.Column("utilization_last", sa.Float(), nullable=True),
        sa.Column("memory_used_bytes_avg", sa.Float(), nullable=True),
        sa.Column("memory_used_bytes_max", sa.BigInteger(), nullable=True),
        sa.Column("memory_used_bytes_last", sa.BigInteger(), nullable=True),
        sa.Column("temperature_celsius_avg", sa.Float(), nullable=True),
        sa.Column("temperature_celsius_max", sa.Float(), nullable=True),
        sa.Column("temperature_celsius_last", sa.Float(), nullable=True),
        sa.Column("power_watts_avg", sa.Float(), nullable=True),
        sa.Column("power_watts_max", sa.Float(), nullable=True),
        sa.Column("power_watts_last", sa.Float(), nullable=True),
        sa.Column("health_last", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("bucket_at", "gpu_id", name="pk_gpu_metrics_1m"),
        schema=SCHEMA,
        postgresql_partition_by="RANGE (bucket_at)",
    )


def downgrade() -> None:
    op.drop_table("gpu_metrics_1m", schema=SCHEMA)
    op.drop_table("service_metrics_1m", schema=SCHEMA)
    op.drop_table("instance_metrics_1m", schema=SCHEMA)

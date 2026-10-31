"""Persist Model Instance automatic-restart reconciliation state.

Revision ID: 20260916_0016
Revises: 20260915_0015
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260916_0016"
down_revision: str | None = "20260915_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "core"
MODEL_INSTANCE_STATUSES = (
    "WaitingForResource",
    "Starting",
    "Running",
    "Restarting",
    "Unhealthy",
    "Stopping",
    "Stopped",
    "Failed",
)
LEGACY_MODEL_INSTANCE_STATUSES = (
    "WaitingForResource",
    "Starting",
    "Running",
    "Stopping",
    "Stopped",
    "Failed",
)


def _values_clause(column: str, values: Sequence[str]) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    op.drop_constraint(
        "ck_model_instances_status",
        "model_instances",
        schema=SCHEMA,
        type_="check",
    )
    op.create_check_constraint(
        "ck_model_instances_status",
        "model_instances",
        _values_clause("status", MODEL_INSTANCE_STATUSES),
        schema=SCHEMA,
    )
    op.add_column(
        "model_instances",
        sa.Column("restart_count", sa.Integer(), server_default="0", nullable=False),
        schema=SCHEMA,
    )
    op.add_column(
        "model_instances",
        sa.Column("restart_window_started_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "model_instances",
        sa.Column("last_healthy_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "model_instances",
        sa.Column("last_crashed_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_model_instances_restart_count_nonnegative",
        "model_instances",
        "restart_count >= 0",
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE core.gpu_allocations AS allocation "
            "SET status = 'Released', released_at = clock_timestamp(), "
            "updated_at = clock_timestamp() "
            "FROM core.model_instances AS instance "
            "WHERE allocation.instance_id = instance.id "
            "AND allocation.status <> 'Released' "
            "AND instance.status IN ('Unhealthy', 'Restarting')"
        )
    )
    op.execute(
        sa.text(
            "UPDATE core.model_instances SET status = 'Failed', "
            "error_code = 'migration_downgrade', internal_endpoint = NULL, "
            "updated_at = clock_timestamp() "
            "WHERE status IN ('Unhealthy', 'Restarting')"
        )
    )
    op.drop_constraint(
        "ck_model_instances_restart_count_nonnegative",
        "model_instances",
        schema=SCHEMA,
        type_="check",
    )
    for column in (
        "last_crashed_at",
        "last_healthy_at",
        "restart_window_started_at",
        "restart_count",
    ):
        op.drop_column("model_instances", column, schema=SCHEMA)
    op.drop_constraint(
        "ck_model_instances_status",
        "model_instances",
        schema=SCHEMA,
        type_="check",
    )
    op.create_check_constraint(
        "ck_model_instances_status",
        "model_instances",
        _values_clause("status", LEGACY_MODEL_INSTANCE_STATUSES),
        schema=SCHEMA,
    )

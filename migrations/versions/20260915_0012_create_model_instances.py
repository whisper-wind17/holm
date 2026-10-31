"""Create Model Instance and GPU Allocation relations.

Revision ID: 20260915_0012
Revises: 20260907_0011
Create Date: 2026-09-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260915_0012"
down_revision: str | None = "20260907_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "core"
RUNTIME_TYPES = ("vllm", "sglang")
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
GPU_ALLOCATION_STATUSES = ("Reserved", "Allocated", "Released")


def _values_clause(column: str, values: Sequence[str]) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    """Add the prototype instance data plane and its concurrency guard."""

    op.create_table(
        "model_instances",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("artifact_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("runtime_type", sa.Text(), nullable=False),
        sa.Column(
            "expected_gpu_uuids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("tensor_parallel_size", sa.Integer(), nullable=False),
        sa.Column("context_length", sa.Integer(), nullable=False),
        sa.Column("gpu_memory_utilization", sa.Float(), nullable=False),
        sa.Column("internal_model_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("internal_endpoint", sa.Text(), nullable=True),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_model_instances"),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            [f"{SCHEMA}.model_artifacts.id"],
            name="fk_model_instances_artifact_id_model_artifacts",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("internal_model_name", name="uq_model_instances_internal_name"),
        sa.CheckConstraint(
            _values_clause("runtime_type", RUNTIME_TYPES),
            name="ck_model_instances_runtime_type",
        ),
        sa.CheckConstraint(
            _values_clause("status", MODEL_INSTANCE_STATUSES),
            name="ck_model_instances_status",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(expected_gpu_uuids) = 'array'",
            name="ck_model_instances_expected_gpu_uuids_array",
        ),
        sa.CheckConstraint(
            "tensor_parallel_size = jsonb_array_length(expected_gpu_uuids) "
            "AND tensor_parallel_size > 0",
            name="ck_model_instances_tp_matches_gpu_count",
        ),
        sa.CheckConstraint(
            "context_length > 0",
            name="ck_model_instances_context_length_positive",
        ),
        sa.CheckConstraint(
            "gpu_memory_utilization > 0 AND gpu_memory_utilization <= 1",
            name="ck_model_instances_gpu_memory_utilization_range",
        ),
        sa.CheckConstraint(
            "internal_model_name ~ '^mi_[0-9a-f]{12}$'",
            name="ck_model_instances_internal_name",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR char_length(error_code) > 0",
            name="ck_model_instances_error_code_nonempty",
        ),
        sa.CheckConstraint(
            "internal_endpoint IS NULL OR char_length(internal_endpoint) > 0",
            name="ck_model_instances_internal_endpoint_nonempty",
        ),
        sa.CheckConstraint(
            "config_version > 0",
            name="ck_model_instances_config_version_positive",
        ),
        schema=SCHEMA,
    )

    op.create_table(
        "gpu_allocations",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("instance_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("gpu_uuid", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_gpu_allocations"),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            [f"{SCHEMA}.model_instances.id"],
            name="fk_gpu_allocations_instance_id_model_instances",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            _values_clause("status", GPU_ALLOCATION_STATUSES),
            name="ck_gpu_allocations_status",
        ),
        sa.CheckConstraint(
            "char_length(gpu_uuid) > 0",
            name="ck_gpu_allocations_gpu_uuid_nonempty",
        ),
        sa.CheckConstraint(
            "(status = 'Released') = (released_at IS NOT NULL)",
            name="ck_gpu_allocations_release_pair",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_gpu_allocations_active_gpu_uuid",
        "gpu_allocations",
        ["gpu_uuid"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("status <> 'Released'"),
    )
    op.create_index(
        "ix_gpu_allocations_instance_id",
        "gpu_allocations",
        ["instance_id"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    """Remove the prototype instance data plane."""

    op.drop_table("gpu_allocations", schema=SCHEMA)
    op.drop_table("model_instances", schema=SCHEMA)

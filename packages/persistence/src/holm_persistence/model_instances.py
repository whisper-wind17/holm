"""SQLAlchemy tables for Model Instances and their GPU Allocations."""

from __future__ import annotations

from typing import Final

import sqlalchemy as sa
from holm_domain import GpuAllocationStatus, ModelInstanceStatus, RuntimeType
from sqlalchemy.dialects.postgresql import JSONB

from holm_persistence.accounts import METADATA, _values_clause
from holm_persistence.model_domain import MODEL_ARTIFACTS_TABLE_NAME
from holm_persistence.schemas import CORE_SCHEMA

MODEL_INSTANCES_TABLE_NAME: Final[str] = "model_instances"
GPU_ALLOCATIONS_TABLE_NAME: Final[str] = "gpu_allocations"

MODEL_INSTANCES: Final[sa.Table] = sa.Table(
    MODEL_INSTANCES_TABLE_NAME,
    METADATA,
    sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
    sa.Column(
        "artifact_id",
        sa.Uuid(as_uuid=True),
        sa.ForeignKey(
            f"{CORE_SCHEMA}.{MODEL_ARTIFACTS_TABLE_NAME}.id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    ),
    sa.Column("runtime_type", sa.Text(), nullable=False),
    sa.Column("expected_gpu_uuids", JSONB, nullable=False),
    sa.Column("tensor_parallel_size", sa.Integer(), nullable=False),
    sa.Column("context_length", sa.Integer(), nullable=False),
    sa.Column("gpu_memory_utilization", sa.Float(), nullable=False),
    sa.Column("name", sa.Text(), nullable=False),
    sa.Column("internal_model_name", sa.Text(), nullable=False),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("error_code", sa.Text(), nullable=True),
    sa.Column("internal_endpoint", sa.Text(), nullable=True),
    sa.Column("config_version", sa.Integer(), nullable=False),
    sa.Column("restart_count", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("restart_window_started_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("last_healthy_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("last_crashed_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("name", name="uq_model_instances_name"),
    sa.CheckConstraint("char_length(name) BETWEEN 2 AND 64 AND name = btrim(name)", name="name"),
    sa.UniqueConstraint("internal_model_name", name="internal_name"),
    sa.CheckConstraint(_values_clause("runtime_type", RuntimeType), name="runtime_type"),
    sa.CheckConstraint(_values_clause("status", ModelInstanceStatus), name="status"),
    sa.CheckConstraint(
        "jsonb_typeof(expected_gpu_uuids) = 'array'",
        name="expected_gpu_uuids_array",
    ),
    sa.CheckConstraint(
        "tensor_parallel_size = jsonb_array_length(expected_gpu_uuids) "
        "AND tensor_parallel_size > 0",
        name="tp_matches_gpu_count",
    ),
    sa.CheckConstraint("context_length > 0", name="context_length_positive"),
    sa.CheckConstraint(
        "gpu_memory_utilization > 0 AND gpu_memory_utilization <= 1",
        name="gpu_memory_utilization_range",
    ),
    sa.CheckConstraint(
        "internal_model_name ~ '^mi_[0-9a-f]{12}$'",
        name="internal_name",
    ),
    sa.CheckConstraint(
        "error_code IS NULL OR char_length(error_code) > 0",
        name="error_code_nonempty",
    ),
    sa.CheckConstraint(
        "internal_endpoint IS NULL OR char_length(internal_endpoint) > 0",
        name="internal_endpoint_nonempty",
    ),
    sa.CheckConstraint("config_version > 0", name="config_version_positive"),
    sa.CheckConstraint("restart_count >= 0", name="restart_count_nonnegative"),
    schema=CORE_SCHEMA,
)

GPU_ALLOCATIONS: Final[sa.Table] = sa.Table(
    GPU_ALLOCATIONS_TABLE_NAME,
    METADATA,
    sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
    sa.Column(
        "instance_id",
        sa.Uuid(as_uuid=True),
        sa.ForeignKey(
            f"{CORE_SCHEMA}.{MODEL_INSTANCES_TABLE_NAME}.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    ),
    sa.Column("gpu_uuid", sa.Text(), nullable=False),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint(_values_clause("status", GpuAllocationStatus), name="status"),
    sa.CheckConstraint("char_length(gpu_uuid) > 0", name="gpu_uuid_nonempty"),
    sa.CheckConstraint(
        "(status = 'Released') = (released_at IS NOT NULL)",
        name="release_pair",
    ),
    sa.Index(
        "uq_gpu_allocations_active_gpu_uuid",
        "gpu_uuid",
        unique=True,
        postgresql_where=sa.column("status") != GpuAllocationStatus.RELEASED.value,
    ),
    sa.Index("ix_gpu_allocations_instance_id", "instance_id"),
    schema=CORE_SCHEMA,
)

MODEL_INSTANCE_TABLES: Final[tuple[sa.Table, ...]] = (MODEL_INSTANCES, GPU_ALLOCATIONS)

__all__ = [
    "GPU_ALLOCATIONS",
    "GPU_ALLOCATIONS_TABLE_NAME",
    "MODEL_INSTANCES",
    "MODEL_INSTANCES_TABLE_NAME",
    "MODEL_INSTANCE_TABLES",
]

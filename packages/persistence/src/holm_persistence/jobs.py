"""SQLAlchemy definitions for PostgreSQL jobs and component heartbeats.

``jobs.payload`` is restricted to operational metadata. Request/response bodies,
messages, prompts, tool arguments, code, and generated output must never be put
there; those values are memory-only under the repository privacy invariant.
"""

from __future__ import annotations

from typing import Final

import sqlalchemy as sa
from holm_domain import Component, ComponentStatus, JobStatus
from sqlalchemy.dialects.postgresql import JSONB

from holm_persistence.accounts import METADATA, _values_clause
from holm_persistence.schemas import CORE_SCHEMA

JOBS_TABLE_NAME: Final[str] = "jobs"
COMPONENT_HEARTBEATS_TABLE_NAME: Final[str] = "component_heartbeats"

JOBS: Final[sa.Table] = sa.Table(
    JOBS_TABLE_NAME,
    METADATA,
    sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
    sa.Column("job_type", sa.Text(), nullable=False),
    sa.Column("payload", JSONB(), nullable=False),
    sa.Column("target_object_id", sa.Uuid(as_uuid=True), nullable=False),
    sa.Column("target_object_version", sa.Integer(), nullable=False),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("priority", sa.Integer(), nullable=False),
    sa.Column("lease_owner", sa.Text(), nullable=True),
    sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
    sa.Column("attempt", sa.Integer(), nullable=False),
    sa.Column("max_attempts", sa.Integer(), nullable=False),
    sa.Column("run_after", sa.DateTime(timezone=True), nullable=False),
    sa.Column("last_error_code", sa.Text(), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("char_length(job_type) BETWEEN 1 AND 128", name="job_type_length"),
    sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_object"),
    sa.CheckConstraint("target_object_version >= 0", name="target_version_nonnegative"),
    sa.CheckConstraint(_values_clause("status", JobStatus), name="status"),
    sa.CheckConstraint("attempt >= 0 AND attempt <= max_attempts", name="attempt_bounded"),
    sa.CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
    sa.CheckConstraint(
        "(status = 'Running') = (lease_owner IS NOT NULL AND lease_until IS NOT NULL)",
        name="running_has_lease",
    ),
    sa.CheckConstraint(
        "(status IN ('Succeeded', 'Failed')) = (completed_at IS NOT NULL)",
        name="terminal_has_completion",
    ),
    sa.Index(
        "ix_jobs_claim_order",
        "status",
        "run_after",
        sa.desc("priority"),
        "created_at",
    ),
    schema=CORE_SCHEMA,
)

COMPONENT_HEARTBEATS: Final[sa.Table] = sa.Table(
    COMPONENT_HEARTBEATS_TABLE_NAME,
    METADATA,
    sa.Column("component", sa.Text(), primary_key=True),
    sa.Column("instance_id", sa.Text(), primary_key=True),
    sa.Column("version", sa.Text(), nullable=False),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("reason", sa.Text(), nullable=True),
    sa.CheckConstraint(_values_clause("component", Component), name="component"),
    sa.CheckConstraint(_values_clause("status", ComponentStatus), name="status"),
    sa.CheckConstraint("char_length(instance_id) BETWEEN 1 AND 128", name="instance_id_length"),
    sa.CheckConstraint("char_length(version) BETWEEN 1 AND 128", name="version_length"),
    sa.CheckConstraint("reason IS NULL OR char_length(reason) <= 512", name="reason_length"),
    schema=CORE_SCHEMA,
)

JOB_TABLES: Final[tuple[sa.Table, ...]] = (JOBS, COMPONENT_HEARTBEATS)

__all__ = [
    "COMPONENT_HEARTBEATS",
    "COMPONENT_HEARTBEATS_TABLE_NAME",
    "JOBS",
    "JOBS_TABLE_NAME",
    "JOB_TABLES",
]

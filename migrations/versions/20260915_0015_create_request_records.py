"""Create allowlisted Gateway Request Records.

Revision ID: 20260915_0015
Revises: 20260915_0014
Create Date: 2026-09-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260915_0015"
down_revision: str | None = "20260915_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "core"
REQUEST_RECORD_STATUSES = ("aborted", "completed", "failed", "rejected")
GATEWAY_ERROR_CODES = (
    "api_key_expired",
    "backend_unavailable",
    "control_plane_unavailable",
    "gateway_overloaded",
    "inference_timeout",
    "invalid_api_key",
    "invalid_request",
    "model_not_found",
    "rate_limit_exceeded",
    "service_disabled",
    "service_not_authorized",
    "usage_storage_unavailable",
)
REQUEST_FINISH_REASONS = ("content_filter", "length", "other", "stop", "tool_calls")


def _values_clause(column: str, values: Sequence[str]) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    """Add the metadata-only Request Record projection used by the prototype."""

    op.create_table(
        "request_records",
        sa.Column("request_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("api_key_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("service_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("instance_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("stream", sa.Boolean(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("ttft_ms", sa.BigInteger(), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=False),
        sa.Column("finish_reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("request_id", name="pk_request_records"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            [f"{SCHEMA}.users.id"],
            name="fk_request_records_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["api_key_id"],
            [f"{SCHEMA}.api_keys.id"],
            name="fk_request_records_api_key_id_api_keys",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            [f"{SCHEMA}.ai_services.id"],
            name="fk_request_records_service_id_ai_services",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            [f"{SCHEMA}.model_instances.id"],
            name="fk_request_records_instance_id_model_instances",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "completed_at >= received_at",
            name="ck_request_records_completed_after_received",
        ),
        sa.CheckConstraint(
            _values_clause("status", REQUEST_RECORD_STATUSES),
            name="ck_request_records_status",
        ),
        sa.CheckConstraint(
            "http_status BETWEEN 100 AND 599",
            name="ck_request_records_http_status",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR " + _values_clause("error_code", GATEWAY_ERROR_CODES),
            name="ck_request_records_error_code",
        ),
        sa.CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0",
            name="ck_request_records_input_tokens",
        ),
        sa.CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0",
            name="ck_request_records_output_tokens",
        ),
        sa.CheckConstraint(
            "ttft_ms IS NULL OR ttft_ms >= 0",
            name="ck_request_records_ttft_ms",
        ),
        sa.CheckConstraint("duration_ms >= 0", name="ck_request_records_duration_ms"),
        sa.CheckConstraint(
            "finish_reason IS NULL OR " + _values_clause("finish_reason", REQUEST_FINISH_REASONS),
            name="ck_request_records_finish_reason",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_request_records_received_at_desc",
        "request_records",
        [sa.text("received_at DESC")],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_request_records_service_id_received_at",
        "request_records",
        ["service_id", sa.text("received_at DESC")],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_request_records_user_id_received_at",
        "request_records",
        ["user_id", sa.text("received_at DESC")],
        schema=SCHEMA,
    )


def downgrade() -> None:
    """Remove the prototype Request Record projection."""

    op.drop_table("request_records", schema=SCHEMA)

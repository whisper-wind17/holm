"""Create partitioned Request Records and the global Request ID registry.

Revision ID: 20260917_0018
Revises: 20260917_0017
Create Date: 2026-09-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260917_0018"
down_revision: str | None = "20260917_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "usage"
REQUEST_RECORD_STATES = (
    "aborted",
    "completed",
    "failed",
    "in_progress",
    "received",
    "rejected",
    "unknown",
)
REQUEST_PROTOCOLS = ("openai",)
REQUEST_OPERATIONS = ("chat.completions",)
REQUEST_FINISH_REASONS = ("content_filter", "length", "other", "stop", "tool_calls")
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
EVENT_TYPES = ("Aborted", "Completed", "Failed", "InProgress", "Received", "Rejected", "Unknown")


def _values_clause(column: str, values: Sequence[str]) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    op.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"'))
    op.create_table(
        "request_id_registry",
        sa.Column("request_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("highest_event_seq", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("request_id", name="pk_request_id_registry"),
        sa.CheckConstraint(
            "highest_event_seq > 0",
            name="ck_request_id_registry_highest_event_seq",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "request_records",
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("event_seq", sa.BigInteger(), nullable=False),
        sa.Column("record_state", sa.Text(), nullable=False),
        sa.Column("protocol", sa.Text(), nullable=True),
        sa.Column("operation", sa.Text(), nullable=True),
        sa.Column("endpoint", sa.Text(), nullable=True),
        sa.Column("stream", sa.Boolean(), nullable=True),
        sa.Column("input_bytes", sa.BigInteger(), nullable=True),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("api_key_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("service_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("instance_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("requested_max_tokens", sa.BigInteger(), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("ttft_ms", sa.BigInteger(), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("finish_reason", sa.Text(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("client_disconnected", sa.Boolean(), nullable=True),
        sa.Column("last_confirmed_event_type", sa.Text(), nullable=True),
        sa.Column("last_confirmed_event_seq", sa.BigInteger(), nullable=True),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "received_at",
            "request_id",
            name="uq_request_records_received_at_request_id",
        ),
        sa.CheckConstraint("event_seq > 0", name="ck_request_records_event_seq"),
        sa.CheckConstraint(
            _values_clause("record_state", REQUEST_RECORD_STATES),
            name="ck_request_records_record_state",
        ),
        sa.CheckConstraint(
            "protocol IS NULL OR " + _values_clause("protocol", REQUEST_PROTOCOLS),
            name="ck_request_records_protocol",
        ),
        sa.CheckConstraint(
            "operation IS NULL OR " + _values_clause("operation", REQUEST_OPERATIONS),
            name="ck_request_records_operation",
        ),
        sa.CheckConstraint(
            "input_bytes IS NULL OR input_bytes >= 0",
            name="ck_request_records_input_bytes",
        ),
        sa.CheckConstraint(
            "requested_max_tokens IS NULL OR requested_max_tokens >= 0",
            name="ck_request_records_requested_max_tokens",
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
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_request_records_duration_ms",
        ),
        sa.CheckConstraint(
            "finish_reason IS NULL OR " + _values_clause("finish_reason", REQUEST_FINISH_REASONS),
            name="ck_request_records_finish_reason",
        ),
        sa.CheckConstraint(
            "http_status IS NULL OR http_status BETWEEN 100 AND 599",
            name="ck_request_records_http_status",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR " + _values_clause("error_code", GATEWAY_ERROR_CODES),
            name="ck_request_records_error_code",
        ),
        sa.CheckConstraint(
            "client_disconnected IS NULL OR client_disconnected",
            name="ck_request_records_client_disconnected",
        ),
        sa.CheckConstraint(
            "last_confirmed_event_type IS NULL OR "
            + _values_clause("last_confirmed_event_type", EVENT_TYPES),
            name="ck_request_records_last_confirmed_event_type",
        ),
        sa.CheckConstraint(
            "last_confirmed_event_seq IS NULL OR last_confirmed_event_seq > 0",
            name="ck_request_records_last_confirmed_event_seq",
        ),
        schema=SCHEMA,
        postgresql_partition_by="RANGE (received_at)",
    )
    op.create_index(
        "ix_usage_request_records_received_at_desc",
        "request_records",
        [sa.text("received_at DESC")],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_usage_request_records_user_id_received_at",
        "request_records",
        ["user_id", sa.text("received_at DESC")],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_usage_request_records_service_id_received_at",
        "request_records",
        ["service_id", sa.text("received_at DESC")],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("request_records", schema=SCHEMA)
    op.drop_table("request_id_registry", schema=SCHEMA)

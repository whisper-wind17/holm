"""Align persisted enum CHECK constraints with their frozen vocabularies.

Revision ID: 20260921_0023
Revises: 20260921_0022
Create Date: 2026-09-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260921_0023"
down_revision: str | None = "20260921_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

type CheckSpec = tuple[str, str, str, str, tuple[str, ...], bool]

ARTIFACT_STORAGE_TYPES = ("Managed", "External")
MODEL_ARTIFACT_STATUSES = ("Ready", "Corrupted", "Missing")
DOWNLOAD_TASK_STATUSES = (
    "Queued",
    "Resolving",
    "Downloading",
    "Paused",
    "Verifying",
    "Publishing",
    "Completed",
    "Failed",
    "Cancelled",
)
DOWNLOAD_SUBSCRIBER_TYPES = ("Instance", "Standalone")
MODEL_SOURCE_TYPES = ("Hugging Face", "ModelScope")
MODEL_SOURCE_VALIDATION_STATUSES = (
    "accessible",
    "invalid_credential",
    "permission_denied",
    "not_found",
    "rate_limited",
)
MODEL_ORIGIN_TYPES = ("Hugging Face", "ModelScope", "Local Path")
METADATA_STATUSES = ("CONFIRMED", "UNCONFIRMED")
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
API_KEY_STATUSES = ("Active", "Revoked", "Expired")
REQUEST_RECORD_STATUSES = ("aborted", "completed", "failed", "rejected")
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
QUARANTINE_EVENT_TYPES = (
    "Received",
    "Rejected",
    "InProgress",
    "Completed",
    "Failed",
    "Aborted",
    "Unknown",
)
METERING_SOURCES = (
    "runtime_metrics",
    "runtime_usage",
    "tokenizer_fallback",
    "unavailable",
)
METRICS_CAPABILITIES = ("full", "partial", "unsupported")

CHECKS: tuple[CheckSpec, ...] = (
    (
        "core",
        "model_artifacts",
        "ck_model_artifacts_storage_type",
        "storage_type",
        ARTIFACT_STORAGE_TYPES,
        False,
    ),
    (
        "core",
        "model_artifacts",
        "ck_model_artifacts_status",
        "status",
        MODEL_ARTIFACT_STATUSES,
        False,
    ),
    (
        "core",
        "download_tasks",
        "ck_download_tasks_status",
        "status",
        DOWNLOAD_TASK_STATUSES,
        False,
    ),
    (
        "core",
        "download_subscriptions",
        "ck_download_subscriptions_subscriber_type",
        "subscriber_type",
        DOWNLOAD_SUBSCRIBER_TYPES,
        False,
    ),
    (
        "core",
        "model_sources",
        "ck_model_sources_type",
        "type",
        MODEL_SOURCE_TYPES,
        False,
    ),
    (
        "core",
        "model_sources",
        "ck_model_sources_verification_status",
        "verification_status",
        MODEL_SOURCE_VALIDATION_STATUSES,
        True,
    ),
    (
        "core",
        "models",
        "ck_models_source_type",
        "source_type",
        MODEL_ORIGIN_TYPES,
        False,
    ),
    (
        "core",
        "models",
        "ck_models_metadata_status",
        "metadata_status",
        METADATA_STATUSES,
        False,
    ),
    (
        "core",
        "model_instances",
        "ck_model_instances_runtime_type",
        "runtime_type",
        RUNTIME_TYPES,
        False,
    ),
    (
        "core",
        "model_instances",
        "ck_model_instances_status",
        "status",
        MODEL_INSTANCE_STATUSES,
        False,
    ),
    (
        "core",
        "gpu_allocations",
        "ck_gpu_allocations_status",
        "status",
        GPU_ALLOCATION_STATUSES,
        False,
    ),
    ("core", "api_keys", "ck_api_keys_status", "status", API_KEY_STATUSES, False),
    (
        "core",
        "request_records",
        "ck_request_records_status",
        "status",
        REQUEST_RECORD_STATUSES,
        False,
    ),
    (
        "core",
        "request_records",
        "ck_request_records_error_code",
        "error_code",
        GATEWAY_ERROR_CODES,
        True,
    ),
    (
        "core",
        "request_records",
        "ck_request_records_finish_reason",
        "finish_reason",
        REQUEST_FINISH_REASONS,
        True,
    ),
    (
        "usage",
        "request_records",
        "ck_request_records_record_state",
        "record_state",
        REQUEST_RECORD_STATES,
        False,
    ),
    (
        "usage",
        "request_records",
        "ck_request_records_protocol",
        "protocol",
        REQUEST_PROTOCOLS,
        True,
    ),
    (
        "usage",
        "request_records",
        "ck_request_records_operation",
        "operation",
        REQUEST_OPERATIONS,
        True,
    ),
    (
        "usage",
        "request_records",
        "ck_request_records_finish_reason",
        "finish_reason",
        REQUEST_FINISH_REASONS,
        True,
    ),
    (
        "usage",
        "request_records",
        "ck_request_records_error_code",
        "error_code",
        GATEWAY_ERROR_CODES,
        True,
    ),
    (
        "usage",
        "request_records",
        "ck_request_records_last_confirmed_event_type",
        "last_confirmed_event_type",
        EVENT_TYPES,
        True,
    ),
    (
        "usage",
        "request_event_quarantine",
        "ck_request_event_quarantine_event_type",
        "event_type",
        QUARANTINE_EVENT_TYPES,
        False,
    ),
    (
        "core",
        "request_records",
        "ck_request_records_metering_source",
        "metering_source",
        METERING_SOURCES,
        True,
    ),
    (
        "core",
        "request_records",
        "ck_request_records_metrics_capability",
        "metrics_capability",
        METRICS_CAPABILITIES,
        True,
    ),
    (
        "usage",
        "request_records",
        "ck_request_records_metering_source",
        "metering_source",
        METERING_SOURCES,
        True,
    ),
    (
        "usage",
        "request_records",
        "ck_request_records_metrics_capability",
        "metrics_capability",
        METRICS_CAPABILITIES,
        True,
    ),
)


def _values_clause(column: str, values: tuple[str, ...], *, nullable: bool) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    clause = f"{column} IN ({rendered})"
    return f"{column} IS NULL OR {clause}" if nullable else clause


def _replace_check(spec: CheckSpec) -> None:
    schema, table, name, column, values, nullable = spec
    op.execute(sa.text(f'ALTER TABLE "{schema}"."{table}" DROP CONSTRAINT IF EXISTS "{name}"'))
    op.create_check_constraint(
        name,
        table,
        _values_clause(column, values, nullable=nullable),
        schema=schema,
    )


def _align_checks() -> None:
    for spec in CHECKS:
        _replace_check(spec)


def upgrade() -> None:
    """Repair every database, including ones migrated before an enum expanded."""
    _align_checks()


def downgrade() -> None:
    """Restore the schema emitted by the now-frozen 0022 revision chain."""
    # The repaired vocabularies must remain in place at 0022: reverting only
    # RuntimeType to its once-live value would recreate the journey-dependent
    # schema this revision exists to remove.
    _align_checks()

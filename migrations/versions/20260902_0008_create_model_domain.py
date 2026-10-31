"""Create the model-acquisition domain relations.

Revision ID: 20260902_0008
Revises: 20260901_0007
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260902_0008"
down_revision: str | None = "20260901_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "core"
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


def _values_clause(column: str, values: Sequence[str]) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    """Add exactly the four model-domain tables and their database guards."""
    op.create_table(
        "models",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("repository_id", sa.Text(), nullable=True),
        sa.Column("external_path", sa.Text(), nullable=True),
        sa.Column("architecture", sa.Text(), nullable=True),
        sa.Column("modalities", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("precision", sa.Text(), nullable=True),
        sa.Column("quantization", sa.Text(), nullable=True),
        sa.Column("context_length", sa.Integer(), nullable=True),
        sa.Column(
            "supported_runtimes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("estimated_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("license", sa.Text(), nullable=True),
        sa.Column("metadata_status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_models"),
        sa.CheckConstraint(
            "(repository_id IS NOT NULL) <> (external_path IS NOT NULL)",
            name="ck_models_repository_or_external_path",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(modalities) = 'array'",
            name="ck_models_modalities_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(supported_runtimes) = 'array'",
            name="ck_models_supported_runtimes_array",
        ),
        sa.CheckConstraint(
            "context_length IS NULL OR context_length > 0",
            name="ck_models_context_length_positive",
        ),
        sa.CheckConstraint(
            "estimated_size_bytes IS NULL OR estimated_size_bytes >= 0",
            name="ck_models_estimated_size_nonnegative",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_model_source_key",
        "models",
        ["source_type", "source_key"],
        unique=True,
        schema=SCHEMA,
    )

    op.create_table(
        "model_artifacts",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("model_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("repository_id", sa.Text(), nullable=False),
        sa.Column("resolved_revision", sa.Text(), nullable=False),
        sa.Column("storage_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("manifest_hash", sa.Text(), nullable=False),
        sa.Column("tokenizer_bundle_hash", sa.Text(), nullable=True),
        sa.Column("tokenizer_bundle_path", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_model_artifacts"),
        sa.ForeignKeyConstraint(
            ["model_id"],
            [f"{SCHEMA}.models.id"],
            name="fk_model_artifacts_model_id_models",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            _values_clause("storage_type", ARTIFACT_STORAGE_TYPES),
            name="ck_model_artifacts_storage_type",
        ),
        sa.CheckConstraint(
            _values_clause("status", MODEL_ARTIFACT_STATUSES),
            name="ck_model_artifacts_status",
        ),
        sa.CheckConstraint("size_bytes >= 0", name="ck_model_artifacts_size_nonnegative"),
        sa.CheckConstraint(
            "(tokenizer_bundle_hash IS NULL) = (tokenizer_bundle_path IS NULL)",
            name="ck_model_artifacts_tokenizer_bundle_pair",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_model_artifact_revision",
        "model_artifacts",
        ["source_type", "repository_id", "resolved_revision"],
        unique=True,
        schema=SCHEMA,
    )

    op.create_table(
        "download_tasks",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("model_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("artifact_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("repository_id", sa.Text(), nullable=False),
        sa.Column("requested_revision_norm", sa.Text(), nullable=False),
        sa.Column("resolved_revision", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("downloaded_bytes", sa.BigInteger(), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), nullable=True),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("staging_path", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_download_tasks"),
        sa.ForeignKeyConstraint(
            ["model_id"],
            [f"{SCHEMA}.models.id"],
            name="fk_download_tasks_model_id_models",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            [f"{SCHEMA}.model_artifacts.id"],
            name="fk_download_tasks_artifact_id_model_artifacts",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            _values_clause("status", DOWNLOAD_TASK_STATUSES),
            name="ck_download_tasks_status",
        ),
        sa.CheckConstraint("attempt >= 1", name="ck_download_tasks_attempt_positive"),
        sa.CheckConstraint(
            "downloaded_bytes >= 0",
            name="ck_download_tasks_downloaded_bytes_nonnegative",
        ),
        sa.CheckConstraint(
            "total_bytes IS NULL OR total_bytes >= 0",
            name="ck_download_tasks_total_bytes_nonnegative",
        ),
        sa.CheckConstraint(
            "total_bytes IS NULL OR downloaded_bytes <= total_bytes",
            name="ck_download_tasks_downloaded_bytes_bounded",
        ),
        sa.CheckConstraint(
            "(lease_owner IS NULL) = (lease_until IS NULL)",
            name="ck_download_tasks_lease_pair",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_active_download_identity",
        "download_tasks",
        ["source_type", "repository_id", "resolved_revision"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text(
            "resolved_revision IS NOT NULL "
            "AND status IN ('Downloading', 'Paused', 'Verifying', 'Publishing')"
        ),
    )
    op.create_index(
        "uq_pre_resolution_download_request",
        "download_tasks",
        ["source_type", "repository_id", "requested_revision_norm"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("status IN ('Queued', 'Resolving')"),
    )

    op.create_table(
        "download_subscriptions",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("task_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("subscriber_type", sa.Text(), nullable=False),
        sa.Column("subscriber_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_download_subscriptions"),
        sa.ForeignKeyConstraint(
            ["task_id"],
            [f"{SCHEMA}.download_tasks.id"],
            name="fk_download_subscriptions_task_id_download_tasks",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "task_id",
            "subscriber_type",
            "subscriber_id",
            name="uq_download_subscription_identity",
        ),
        sa.CheckConstraint(
            _values_clause("subscriber_type", DOWNLOAD_SUBSCRIBER_TYPES),
            name="ck_download_subscriptions_subscriber_type",
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    """Remove the four relations in reverse dependency order."""
    op.drop_table("download_subscriptions", schema=SCHEMA)
    op.drop_table("download_tasks", schema=SCHEMA)
    op.drop_table("model_artifacts", schema=SCHEMA)
    op.drop_table("models", schema=SCHEMA)

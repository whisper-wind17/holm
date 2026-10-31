"""SQLAlchemy model for the four model-acquisition relations.

Persisted enums are text plus CHECK constraints, matching the account-table
precedent. PostgreSQL native ENUM types make clean downgrade and later value
changes needlessly stateful; the domain enums below remain the sole vocabulary
definition while the database still rejects unknown values.
"""

from __future__ import annotations

import unicodedata
from typing import Final

import sqlalchemy as sa
from holm_domain import (
    ArtifactStorageType,
    DownloadSubscriberType,
    DownloadTaskStatus,
    MetadataStatus,
    ModelArtifactStatus,
    ModelOriginType,
)
from sqlalchemy.dialects.postgresql import JSONB

from holm_persistence.accounts import METADATA, _values_clause
from holm_persistence.schemas import CORE_SCHEMA

MODELS_TABLE_NAME: Final[str] = "models"
MODEL_ARTIFACTS_TABLE_NAME: Final[str] = "model_artifacts"
DOWNLOAD_TASKS_TABLE_NAME: Final[str] = "download_tasks"
DOWNLOAD_SUBSCRIPTIONS_TABLE_NAME: Final[str] = "download_subscriptions"

DEFAULT_REQUESTED_REVISION: Final[str] = "@default"


def _normalize_identifier(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def normalize_source_key(source_key: str) -> str:
    """Return the only persisted spelling accepted for a Model source key."""

    return _normalize_identifier(source_key, field_name="source_key")


def normalize_requested_revision(requested_revision: str | None) -> str:
    """Normalize an optional requested Revision without ever returning NULL."""

    if requested_revision is None:
        return DEFAULT_REQUESTED_REVISION
    if not isinstance(requested_revision, str):
        raise TypeError("requested_revision must be a string or None")
    normalized = unicodedata.normalize("NFKC", requested_revision).strip()
    return normalized or DEFAULT_REQUESTED_REVISION


MODELS: Final[sa.Table] = sa.Table(
    MODELS_TABLE_NAME,
    METADATA,
    sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
    sa.Column("source_id", sa.Uuid(as_uuid=True), nullable=True),
    sa.Column("source_type", sa.Text(), nullable=False),
    sa.Column("source_key", sa.Text(), nullable=False),
    sa.Column("display_name", sa.Text(), nullable=False),
    sa.Column("repository_id", sa.Text(), nullable=True),
    sa.Column("external_path", sa.Text(), nullable=True),
    sa.Column("architecture", sa.Text(), nullable=True),
    sa.Column("modalities", JSONB(none_as_null=True), nullable=True),
    sa.Column("precision", sa.Text(), nullable=True),
    sa.Column("quantization", sa.Text(), nullable=True),
    sa.Column("context_length", sa.Integer(), nullable=True),
    sa.Column("supported_runtimes", JSONB(none_as_null=True), nullable=True),
    sa.Column("estimated_size_bytes", sa.BigInteger(), nullable=True),
    sa.Column("license", sa.Text(), nullable=True),
    sa.Column("metadata_status", sa.Text(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint(_values_clause("source_type", ModelOriginType), name="source_type"),
    sa.CheckConstraint(_values_clause("metadata_status", MetadataStatus), name="metadata_status"),
    sa.CheckConstraint(
        "(source_type = 'Local Path' AND source_id IS NULL) OR "
        "(source_type IN ('Hugging Face', 'ModelScope') AND source_id IS NOT NULL)",
        name="source_scope",
    ),
    sa.ForeignKeyConstraint(
        ["source_id", "source_type"],
        [f"{CORE_SCHEMA}.model_sources.id", f"{CORE_SCHEMA}.model_sources.type"],
        name="fk_models_source_identity_model_sources",
        ondelete="RESTRICT",
    ),
    sa.CheckConstraint(
        "(repository_id IS NOT NULL) <> (external_path IS NOT NULL)",
        name="repository_or_external_path",
    ),
    sa.CheckConstraint(
        "modalities IS NULL OR jsonb_typeof(modalities) = 'array'",
        name="modalities_array",
    ),
    sa.CheckConstraint(
        "supported_runtimes IS NULL OR jsonb_typeof(supported_runtimes) = 'array'",
        name="supported_runtimes_array",
    ),
    sa.CheckConstraint(
        "context_length IS NULL OR context_length > 0",
        name="context_length_positive",
    ),
    sa.CheckConstraint(
        "estimated_size_bytes IS NULL OR estimated_size_bytes >= 0",
        name="estimated_size_nonnegative",
    ),
    sa.Index("uq_model_source_key", "source_type", "source_key", unique=True),
    schema=CORE_SCHEMA,
)

MODEL_ARTIFACTS: Final[sa.Table] = sa.Table(
    MODEL_ARTIFACTS_TABLE_NAME,
    METADATA,
    sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
    sa.Column(
        "model_id",
        sa.Uuid(as_uuid=True),
        sa.ForeignKey(f"{CORE_SCHEMA}.{MODELS_TABLE_NAME}.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("source_type", sa.Text(), nullable=False),
    sa.Column("repository_id", sa.Text(), nullable=False),
    sa.Column("resolved_revision", sa.Text(), nullable=False),
    sa.Column("storage_type", sa.Text(), nullable=False),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("path", sa.Text(), nullable=False),
    sa.Column("manifest_hash", sa.Text(), nullable=False),
    sa.Column("manifest_files", JSONB(none_as_null=True), nullable=True),
    sa.Column("tokenizer_bundle_hash", sa.Text(), nullable=True),
    sa.Column("tokenizer_bundle_path", sa.Text(), nullable=True),
    sa.Column("attention_heads", sa.Integer(), nullable=True),
    sa.Column("kv_heads", sa.Integer(), nullable=True),
    sa.Column("light_check_requested_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("light_checked_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("size_bytes", sa.BigInteger(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint(_values_clause("storage_type", ArtifactStorageType), name="storage_type"),
    sa.CheckConstraint(_values_clause("status", ModelArtifactStatus), name="status"),
    sa.CheckConstraint("size_bytes >= 0", name="size_nonnegative"),
    sa.CheckConstraint(
        "attention_heads IS NULL OR attention_heads > 0",
        name="attention_heads_positive",
    ),
    sa.CheckConstraint("kv_heads IS NULL OR kv_heads > 0", name="kv_heads_positive"),
    sa.CheckConstraint(
        "(tokenizer_bundle_hash IS NULL) = (tokenizer_bundle_path IS NULL)",
        name="tokenizer_bundle_pair",
    ),
    sa.Index(
        "uq_model_artifact_revision",
        "source_type",
        "repository_id",
        "resolved_revision",
        unique=True,
    ),
    schema=CORE_SCHEMA,
)

DOWNLOAD_TASKS: Final[sa.Table] = sa.Table(
    DOWNLOAD_TASKS_TABLE_NAME,
    METADATA,
    sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
    sa.Column(
        "model_id",
        sa.Uuid(as_uuid=True),
        sa.ForeignKey(f"{CORE_SCHEMA}.{MODELS_TABLE_NAME}.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "artifact_id",
        sa.Uuid(as_uuid=True),
        sa.ForeignKey(f"{CORE_SCHEMA}.{MODEL_ARTIFACTS_TABLE_NAME}.id", ondelete="RESTRICT"),
        nullable=True,
    ),
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
    sa.CheckConstraint(_values_clause("status", DownloadTaskStatus), name="status"),
    sa.CheckConstraint("attempt >= 1", name="attempt_positive"),
    sa.CheckConstraint("downloaded_bytes >= 0", name="downloaded_bytes_nonnegative"),
    sa.CheckConstraint("total_bytes IS NULL OR total_bytes >= 0", name="total_bytes_nonnegative"),
    sa.CheckConstraint(
        "total_bytes IS NULL OR downloaded_bytes <= total_bytes",
        name="downloaded_bytes_bounded",
    ),
    sa.CheckConstraint(
        "(lease_owner IS NULL) = (lease_until IS NULL)",
        name="lease_pair",
    ),
    sa.Index(
        "uq_active_download_identity",
        "source_type",
        "repository_id",
        "resolved_revision",
        unique=True,
        postgresql_where=sa.and_(
            sa.column("resolved_revision").is_not(None),
            sa.column("status").in_(
                tuple(
                    status.value
                    for status in (
                        DownloadTaskStatus.DOWNLOADING,
                        DownloadTaskStatus.PAUSED,
                        DownloadTaskStatus.VERIFYING,
                        DownloadTaskStatus.PUBLISHING,
                    )
                )
            ),
        ),
    ),
    sa.Index(
        "uq_pre_resolution_download_request",
        "source_type",
        "repository_id",
        "requested_revision_norm",
        unique=True,
        postgresql_where=sa.column("status").in_(
            (DownloadTaskStatus.QUEUED.value, DownloadTaskStatus.RESOLVING.value)
        ),
    ),
    schema=CORE_SCHEMA,
)

DOWNLOAD_SUBSCRIPTIONS: Final[sa.Table] = sa.Table(
    DOWNLOAD_SUBSCRIPTIONS_TABLE_NAME,
    METADATA,
    sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
    sa.Column(
        "task_id",
        sa.Uuid(as_uuid=True),
        sa.ForeignKey(f"{CORE_SCHEMA}.{DOWNLOAD_TASKS_TABLE_NAME}.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("subscriber_type", sa.Text(), nullable=False),
    sa.Column("subscriber_id", sa.Uuid(as_uuid=True), nullable=False),
    sa.Column("actor_id", sa.Uuid(as_uuid=True), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint(
        "task_id",
        "subscriber_type",
        "subscriber_id",
        name="uq_download_subscription_identity",
    ),
    sa.CheckConstraint(
        _values_clause("subscriber_type", DownloadSubscriberType),
        name="subscriber_type",
    ),
    schema=CORE_SCHEMA,
)

MODEL_DOMAIN_TABLES: Final[tuple[sa.Table, ...]] = (
    MODELS,
    MODEL_ARTIFACTS,
    DOWNLOAD_TASKS,
    DOWNLOAD_SUBSCRIPTIONS,
)

__all__ = [
    "DEFAULT_REQUESTED_REVISION",
    "DOWNLOAD_SUBSCRIPTIONS",
    "DOWNLOAD_SUBSCRIPTIONS_TABLE_NAME",
    "DOWNLOAD_TASKS",
    "DOWNLOAD_TASKS_TABLE_NAME",
    "MODELS",
    "MODELS_TABLE_NAME",
    "MODEL_ARTIFACTS",
    "MODEL_ARTIFACTS_TABLE_NAME",
    "MODEL_DOMAIN_TABLES",
    "normalize_requested_revision",
    "normalize_source_key",
]

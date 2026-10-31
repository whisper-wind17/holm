"""SQLAlchemy tables for AI Services, API Keys, and Key service scopes."""

from __future__ import annotations

from typing import Final

import sqlalchemy as sa
from holm_domain import ApiKeyStatus

from holm_persistence.accounts import METADATA, USERS_TABLE_NAME, _values_clause
from holm_persistence.model_instances import MODEL_INSTANCES_TABLE_NAME
from holm_persistence.schemas import CORE_SCHEMA

AI_SERVICES_TABLE_NAME: Final[str] = "ai_services"
AI_SERVICE_BACKENDS_TABLE_NAME: Final[str] = "ai_service_backends"
API_KEYS_TABLE_NAME: Final[str] = "api_keys"
API_KEY_SERVICE_SCOPES_TABLE_NAME: Final[str] = "api_key_service_scopes"
USER_SERVICE_PERMISSIONS_TABLE_NAME: Final[str] = "user_service_permissions"

AI_SERVICES: Final[sa.Table] = sa.Table(
    AI_SERVICES_TABLE_NAME,
    METADATA,
    sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
    sa.Column("name", sa.Text(), nullable=False),
    sa.Column("public_model_name", sa.Text(), nullable=False),
    sa.Column("description", sa.Text(), nullable=False),
    sa.Column("enabled", sa.Boolean(), nullable=False),
    sa.Column("route_version", sa.Integer(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("char_length(public_model_name) BETWEEN 1 AND 255", name="name_length"),
    sa.CheckConstraint("char_length(description) <= 2000", name="description_length"),
    sa.CheckConstraint(
        "char_length(name) BETWEEN 2 AND 40 AND name = btrim(name)", name="display_name"
    ),
    sa.CheckConstraint("route_version > 0", name="route_version_positive"),
    sa.CheckConstraint(
        "deleted_at IS NULL OR deleted_at >= created_at", name="deleted_after_created"
    ),
    sa.Index(
        "uq_ai_services_name",
        "name",
        unique=True,
        postgresql_where=sa.column("deleted_at").is_(None),
    ),
    sa.Index(
        "uq_ai_services_active_public_model_name",
        "public_model_name",
        unique=True,
        postgresql_where=sa.column("deleted_at").is_(None),
    ),
    schema=CORE_SCHEMA,
)

AI_SERVICE_BACKENDS: Final[sa.Table] = sa.Table(
    AI_SERVICE_BACKENDS_TABLE_NAME,
    METADATA,
    sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
    sa.Column(
        "service_id",
        sa.Uuid(as_uuid=True),
        sa.ForeignKey(f"{CORE_SCHEMA}.{AI_SERVICES_TABLE_NAME}.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "instance_id",
        sa.Uuid(as_uuid=True),
        sa.ForeignKey(
            f"{CORE_SCHEMA}.{MODEL_INSTANCES_TABLE_NAME}.id",
            ondelete="SET NULL",
        ),
        nullable=True,
    ),
    sa.Column("instance_name_snapshot", sa.Text(), nullable=False),
    sa.Column("runtime_snapshot", sa.Text(), nullable=False),
    sa.Column("backend_key", sa.Text(), nullable=False),
    sa.Column("weight", sa.Integer(), nullable=False),
    sa.Column("enabled", sa.Boolean(), nullable=False),
    sa.Column("health_gate_passed", sa.Boolean(), nullable=False),
    sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint(
        "retired_at IS NOT NULL OR instance_id IS NOT NULL",
        name=sa.schema.conv("ck_active_backend_has_instance"),
    ),
    sa.CheckConstraint("backend_key = 'primary'", name="backend_key_primary"),
    sa.CheckConstraint(
        "retired_at IS NOT NULL OR (weight = 100 AND enabled)",
        name="active_primary_shape",
    ),
    sa.CheckConstraint(
        "retired_at IS NULL OR retired_at >= joined_at",
        name="retired_after_joined",
    ),
    sa.Index(
        "uq_active_service_backend_key",
        "service_id",
        "backend_key",
        unique=True,
        postgresql_where=sa.column("retired_at").is_(None),
    ),
    sa.Index(
        "uq_active_service_backend_instance",
        "service_id",
        "instance_id",
        unique=True,
        postgresql_where=sa.column("retired_at").is_(None),
    ),
    sa.Index("ix_ai_service_backends_instance_id", "instance_id"),
    schema=CORE_SCHEMA,
)

API_KEYS: Final[sa.Table] = sa.Table(
    API_KEYS_TABLE_NAME,
    METADATA,
    sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
    sa.Column(
        "user_id",
        sa.Uuid(as_uuid=True),
        sa.ForeignKey(f"{CORE_SCHEMA}.{USERS_TABLE_NAME}.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("name", sa.Text(), nullable=False),
    sa.Column("lookup_id", sa.Text(), nullable=False, unique=True),
    sa.Column("display_prefix", sa.Text(), nullable=False),
    sa.Column("last4", sa.Text(), nullable=False),
    sa.Column("pepper_key_id", sa.Text(), nullable=False),
    sa.Column("secret_hmac", sa.LargeBinary(), nullable=False),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("char_length(name) BETWEEN 1 AND 128", name="name_length"),
    sa.CheckConstraint("lookup_id ~ '^[A-Z2-7]{26}$'", name="lookup_id_base32"),
    sa.CheckConstraint("display_prefix = 'holm_sk_'", name="display_prefix"),
    sa.CheckConstraint("last4 ~ '^[A-Za-z0-9_-]{4}$'", name="last4_base64url"),
    sa.CheckConstraint(
        "pepper_key_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'",
        name="pepper_key_id",
    ),
    sa.CheckConstraint("octet_length(secret_hmac) = 32", name="secret_hmac_sha256"),
    sa.CheckConstraint(_values_clause("status", ApiKeyStatus), name="status"),
    sa.CheckConstraint(
        "expires_at IS NULL OR expires_at > created_at", name="expires_after_created"
    ),
    sa.CheckConstraint(
        "last_used_at IS NULL OR last_used_at >= created_at", name="last_used_after_created"
    ),
    sa.CheckConstraint(
        f"(status = '{ApiKeyStatus.REVOKED.value}') = (revoked_at IS NOT NULL)",
        name="revoked_pair",
    ),
    sa.CheckConstraint(
        "revoked_at IS NULL OR revoked_at >= created_at", name="revoked_after_created"
    ),
    sa.Index("ix_api_keys_user_id", "user_id"),
    schema=CORE_SCHEMA,
)

API_KEY_SERVICE_SCOPES: Final[sa.Table] = sa.Table(
    API_KEY_SERVICE_SCOPES_TABLE_NAME,
    METADATA,
    sa.Column(
        "key_id",
        sa.Uuid(as_uuid=True),
        sa.ForeignKey(f"{CORE_SCHEMA}.{API_KEYS_TABLE_NAME}.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column(
        "service_id",
        sa.Uuid(as_uuid=True),
        sa.ForeignKey(f"{CORE_SCHEMA}.{AI_SERVICES_TABLE_NAME}.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Index("ix_api_key_service_scopes_service_id", "service_id"),
    schema=CORE_SCHEMA,
)

USER_SERVICE_PERMISSIONS: Final[sa.Table] = sa.Table(
    USER_SERVICE_PERMISSIONS_TABLE_NAME,
    METADATA,
    sa.Column(
        "user_id",
        sa.Uuid(as_uuid=True),
        sa.ForeignKey(f"{CORE_SCHEMA}.{USERS_TABLE_NAME}.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column(
        "service_id",
        sa.Uuid(as_uuid=True),
        sa.ForeignKey(f"{CORE_SCHEMA}.{AI_SERVICES_TABLE_NAME}.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column(
        "created_by",
        sa.Uuid(as_uuid=True),
        sa.ForeignKey(f"{CORE_SCHEMA}.{USERS_TABLE_NAME}.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("max_concurrent_requests", sa.Integer(), nullable=True),
    sa.Column("max_requests_per_minute", sa.Integer(), nullable=True),
    sa.CheckConstraint("max_concurrent_requests BETWEEN 1 AND 64", name="ck_permission_concurrent"),
    sa.CheckConstraint(
        "max_requests_per_minute BETWEEN 1 AND 600", name="ck_permission_per_minute"
    ),
    sa.Index("ix_user_service_permissions_service_id", "service_id"),
    schema=CORE_SCHEMA,
)

AI_SERVICE_TABLES: Final[tuple[sa.Table, ...]] = (
    AI_SERVICES,
    AI_SERVICE_BACKENDS,
    API_KEYS,
    API_KEY_SERVICE_SCOPES,
    USER_SERVICE_PERMISSIONS,
)

__all__ = [
    "AI_SERVICES",
    "AI_SERVICES_TABLE_NAME",
    "AI_SERVICE_BACKENDS",
    "AI_SERVICE_BACKENDS_TABLE_NAME",
    "AI_SERVICE_TABLES",
    "API_KEYS",
    "API_KEYS_TABLE_NAME",
    "API_KEY_SERVICE_SCOPES",
    "API_KEY_SERVICE_SCOPES_TABLE_NAME",
    "USER_SERVICE_PERMISSIONS",
    "USER_SERVICE_PERMISSIONS_TABLE_NAME",
]

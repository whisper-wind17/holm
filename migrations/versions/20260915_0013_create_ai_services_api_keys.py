"""Create AI Service and API Key relations.

Revision ID: 20260915_0013
Revises: 20260915_0012
Create Date: 2026-09-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260915_0013"
down_revision: str | None = "20260915_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "core"
API_KEY_STATUSES = ("Active", "Revoked", "Expired")


def _values_clause(column: str, values: Sequence[str]) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    """Add the prototype AI Service and one-time-plaintext API Key data plane."""

    op.create_table(
        "ai_services",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("public_model_name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("primary_instance_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("route_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_ai_services"),
        sa.ForeignKeyConstraint(
            ["primary_instance_id"],
            [f"{SCHEMA}.model_instances.id"],
            name="fk_ai_services_primary_instance_id_model_instances",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "char_length(public_model_name) BETWEEN 1 AND 255",
            name="ck_ai_services_name_length",
        ),
        sa.CheckConstraint(
            "char_length(description) <= 2000",
            name="ck_ai_services_description_length",
        ),
        sa.CheckConstraint(
            "route_version > 0",
            name="ck_ai_services_route_version_positive",
        ),
        sa.CheckConstraint(
            "deleted_at IS NULL OR deleted_at >= created_at",
            name="ck_ai_services_deleted_after_created",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_ai_services_active_public_model_name",
        "ai_services",
        ["public_model_name"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_ai_services_primary_instance_id",
        "ai_services",
        ["primary_instance_id"],
        schema=SCHEMA,
    )

    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("lookup_id", sa.Text(), nullable=False),
        sa.Column("display_prefix", sa.Text(), nullable=False),
        sa.Column("last4", sa.Text(), nullable=False),
        sa.Column("pepper_key_id", sa.Text(), nullable=False),
        sa.Column("secret_hmac", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_api_keys"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            [f"{SCHEMA}.users.id"],
            name="fk_api_keys_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("lookup_id", name="uq_api_keys_lookup_id"),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 128",
            name="ck_api_keys_name_length",
        ),
        sa.CheckConstraint(
            "lookup_id ~ '^[A-Z2-7]{26}$'",
            name="ck_api_keys_lookup_id_base32",
        ),
        sa.CheckConstraint("display_prefix = 'pwsk_'", name="ck_api_keys_display_prefix"),
        sa.CheckConstraint(
            "last4 ~ '^[A-Za-z0-9_-]{4}$'",
            name="ck_api_keys_last4_base64url",
        ),
        sa.CheckConstraint(
            "pepper_key_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'",
            name="ck_api_keys_pepper_key_id",
        ),
        sa.CheckConstraint(
            "octet_length(secret_hmac) = 32",
            name="ck_api_keys_secret_hmac_sha256",
        ),
        sa.CheckConstraint(
            _values_clause("status", API_KEY_STATUSES),
            name="ck_api_keys_status",
        ),
        sa.CheckConstraint(
            "expires_at IS NULL OR expires_at > created_at",
            name="ck_api_keys_expires_after_created",
        ),
        sa.CheckConstraint(
            "last_used_at IS NULL OR last_used_at >= created_at",
            name="ck_api_keys_last_used_after_created",
        ),
        sa.CheckConstraint(
            "(status = 'Revoked') = (revoked_at IS NOT NULL)",
            name="ck_api_keys_revoked_pair",
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="ck_api_keys_revoked_after_created",
        ),
        schema=SCHEMA,
    )
    op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"], schema=SCHEMA)

    op.create_table(
        "api_key_service_scopes",
        sa.Column("key_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("service_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "key_id",
            "service_id",
            name="pk_api_key_service_scopes",
        ),
        sa.ForeignKeyConstraint(
            ["key_id"],
            [f"{SCHEMA}.api_keys.id"],
            name="fk_api_key_service_scopes_key_id_api_keys",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            [f"{SCHEMA}.ai_services.id"],
            name="fk_api_key_service_scopes_service_id_ai_services",
            ondelete="CASCADE",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_api_key_service_scopes_service_id",
        "api_key_service_scopes",
        ["service_id"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    """Remove API Keys before their referenced AI Services."""

    op.drop_table("api_key_service_scopes", schema=SCHEMA)
    op.drop_table("api_keys", schema=SCHEMA)
    op.drop_table("ai_services", schema=SCHEMA)

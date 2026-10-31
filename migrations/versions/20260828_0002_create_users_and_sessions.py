"""Create core.users and core.sessions.

Revision ID: 20260828_0002
Revises: 20260828_0001
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260828_0002"
down_revision: str | None = "20260828_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "core"
TOKEN_HASH_PATTERN = "^[0-9a-f]{64}$"

# The DDL below is written out rather than generated from
# ``holm_persistence.accounts``. A migration is a historical fact: it has to
# keep describing the schema it actually produced even after the models move on.
# The models and this file are kept from drifting apart by the schema
# reflection test, which compares the migrated database against the metadata --
# a check, not a shared definition.


def upgrade() -> None:
    """Add the two account tables to the existing core Schema."""
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("username_norm", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("must_change_password", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("username_norm", name="uq_users_username_norm"),
        sa.CheckConstraint("role IN ('admin', 'user')", name="ck_users_role"),
        sa.CheckConstraint(
            "status IN ('active', 'disabled', 'deleted')",
            name="ck_users_status",
        ),
        sa.CheckConstraint(
            "char_length(username_norm) BETWEEN 1 AND 64",
            name="ck_users_username_norm_length",
        ),
        sa.CheckConstraint(
            "char_length(display_name) BETWEEN 1 AND 128",
            name="ck_users_display_name_length",
        ),
        # The column cannot hold a plaintext password: only a PHC-encoded
        # Argon2id hash matches. Nothing else may be stored.
        sa.CheckConstraint(
            r"password_hash ~ '^\$argon2id\$'",
            name="ck_users_password_hash_argon2id",
        ),
        sa.CheckConstraint(
            "(status = 'deleted') = (deleted_at IS NOT NULL)",
            name="ck_users_deleted_at_matches_status",
        ),
        schema=SCHEMA,
    )

    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("session_hash", sa.Text(), nullable=False),
        sa.Column("csrf_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_sessions"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            [f"{SCHEMA}.users.id"],
            name="fk_sessions_user_id_users",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("session_hash", name="uq_sessions_session_hash"),
        # The core.sessions invariant: "不保存 Cookie 明文". A session token is
        # URL-safe base64; 64 lowercase hex characters is not a shape it can
        # take, so a plaintext cookie value fails the write.
        sa.CheckConstraint(
            f"session_hash ~ '{TOKEN_HASH_PATTERN}'",
            name="ck_sessions_session_hash_sha256",
        ),
        sa.CheckConstraint(
            f"csrf_hash ~ '{TOKEN_HASH_PATTERN}'",
            name="ck_sessions_csrf_hash_sha256",
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_sessions_expires_after_created"),
        sa.CheckConstraint(
            "last_seen_at >= created_at",
            name="ck_sessions_last_seen_after_created",
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="ck_sessions_revoked_after_created",
        ),
        schema=SCHEMA,
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"], schema=SCHEMA)
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"], schema=SCHEMA)


def downgrade() -> None:
    """Return the core Schema to the empty initial state."""
    op.drop_index("ix_sessions_expires_at", table_name="sessions", schema=SCHEMA)
    op.drop_index("ix_sessions_user_id", table_name="sessions", schema=SCHEMA)
    op.drop_table("sessions", schema=SCHEMA)
    op.drop_table("users", schema=SCHEMA)

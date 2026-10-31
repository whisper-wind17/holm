"""Create core.bootstrap_tokens and core.system_initialization.

Revision ID: 20260901_0007
Revises: 20260829_0006
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260901_0007"
down_revision: str | None = "20260829_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "core"
GUARD_FUNCTION = "reject_system_initialization_mutation"
GUARD_TRIGGER = "trg_system_initialization_immutable"


def upgrade() -> None:
    """Add the one-time initialization Token table and the singleton fact row."""
    op.create_table(
        "bootstrap_tokens",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_bootstrap_tokens"),
        sa.UniqueConstraint("token_hash", name="uq_bootstrap_tokens_token_hash"),
        sa.CheckConstraint(
            "token_hash ~ '^[0-9a-f]{64}$'",
            name="ck_bootstrap_tokens_token_hash_sha256",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_bootstrap_tokens_expires_after_created",
        ),
        sa.CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= created_at",
            name="ck_bootstrap_tokens_consumed_after_created",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "system_initialization",
        sa.Column("id", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column("system_initialized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("admin_user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_system_initialization"),
        sa.ForeignKeyConstraint(
            ["admin_user_id"],
            [f"{SCHEMA}.users.id"],
            name="fk_system_initialization_admin_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("id = 1", name="ck_system_initialization_singleton_row"),
        schema=SCHEMA,
    )
    # The primary key already forbids a second initialization. This trigger adds
    # the other half: once written, the recorded instant cannot be rewritten or
    # removed, so "永久关闭" is a database property rather than a convention
    # about which repository methods happen to exist.
    op.execute(
        sa.text(
            f"""
            CREATE FUNCTION {SCHEMA}.{GUARD_FUNCTION}() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION
                    'core.system_initialization is append-once and immutable'
                    USING ERRCODE = 'restrict_violation';
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            CREATE TRIGGER {GUARD_TRIGGER}
            BEFORE UPDATE OR DELETE ON {SCHEMA}.system_initialization
            FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.{GUARD_FUNCTION}()
            """
        )
    )


def downgrade() -> None:
    """Remove only what this revision introduced, trigger function included."""
    op.execute(sa.text(f"DROP TRIGGER {GUARD_TRIGGER} ON {SCHEMA}.system_initialization"))
    op.drop_table("system_initialization", schema=SCHEMA)
    op.execute(sa.text(f"DROP FUNCTION {SCHEMA}.{GUARD_FUNCTION}()"))
    op.drop_table("bootstrap_tokens", schema=SCHEMA)

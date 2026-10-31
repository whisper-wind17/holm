"""Create core.audit_events.

Revision ID: 20260829_0004
Revises: 20260828_0003
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260829_0004"
down_revision: str | None = "20260828_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "core"


def upgrade() -> None:
    """Add the long-retained database audit ledger."""
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("object_type", sa.Text(), nullable=False),
        sa.Column("object_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "actor_type IN ('anonymous', 'user', 'system')",
            name="ck_audit_events_actor_type",
        ),
        sa.CheckConstraint(
            "action IN ('auth.login', 'auth.logout', 'account.password_change', "
            "'account.change', 'permission.change', 'permission.authorization_check', "
            "'api_key.change', 'model_source.credential_change', 'download.change', "
            "'model_artifact.delete', 'model_instance.lifecycle', 'ai_service.change', "
            "'system_config.change')",
            name="ck_audit_events_action",
        ),
        sa.CheckConstraint(
            "object_type IN ('user', 'session', 'permission', 'api_key', 'model_source', "
            "'download_task', 'model_artifact', 'model_instance', 'ai_service', "
            "'system_config')",
            name="ck_audit_events_object_type",
        ),
        sa.CheckConstraint(
            "result IN ('succeeded', 'failed', 'denied')",
            name="ck_audit_events_result",
        ),
        sa.CheckConstraint(
            "(actor_type = 'user') = (actor_id IS NOT NULL)",
            name="ck_audit_events_user_actor_has_id",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(details) = 'object'",
            name="ck_audit_events_details_object",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_audit_events_request_id",
        "audit_events",
        ["request_id"],
        unique=False,
        schema=SCHEMA,
    )
    op.create_index(
        "ix_audit_events_created_at",
        "audit_events",
        ["created_at"],
        unique=False,
        schema=SCHEMA,
    )
    op.create_index(
        "ix_audit_events_actor_id_created_at",
        "audit_events",
        ["actor_id", "created_at"],
        unique=False,
        schema=SCHEMA,
    )
    op.create_index(
        "ix_audit_events_object_type_object_id_created_at",
        "audit_events",
        ["object_type", "object_id", "created_at"],
        unique=False,
        schema=SCHEMA,
    )


def downgrade() -> None:
    """Remove only the audit ledger introduced by this revision."""
    op.drop_table("audit_events", schema=SCHEMA)

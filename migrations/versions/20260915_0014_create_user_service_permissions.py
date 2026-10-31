"""Create the user-to-AI-Service authorization relation.

Revision ID: 20260915_0014
Revises: 20260915_0013
Create Date: 2026-09-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260915_0014"
down_revision: str | None = "20260915_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "core"


def upgrade() -> None:
    """Add one shared permission relation for both management entry points."""

    op.create_table(
        "user_service_permissions",
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("service_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.Uuid(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "user_id",
            "service_id",
            name="pk_user_service_permissions",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            [f"{SCHEMA}.users.id"],
            name="fk_user_service_permissions_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            [f"{SCHEMA}.ai_services.id"],
            name="fk_user_service_permissions_service_id_ai_services",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            [f"{SCHEMA}.users.id"],
            name="fk_user_service_permissions_created_by_users",
            ondelete="RESTRICT",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_user_service_permissions_service_id",
        "user_service_permissions",
        ["service_id"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    """Remove only the prototype user-service permission relation."""

    op.drop_table("user_service_permissions", schema=SCHEMA)

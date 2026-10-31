"""Independent AI Service display names.

Revision ID: 20260930_0035
Revises: 20260930_0034
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260930_0035"
down_revision: str | None = "20260930_0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ai_services", sa.Column("name", sa.Text(), nullable=True), schema="core")
    # The complete UUID keeps even identical truncated prefixes collision-free.
    op.execute(
        "UPDATE core.ai_services SET name = left(btrim(public_model_name), 7) "
        "|| '-' || replace(id::text, '-', '')"
    )
    op.alter_column("ai_services", "name", nullable=False, schema="core")
    op.create_unique_constraint("uq_ai_services_name", "ai_services", ["name"], schema="core")
    op.create_check_constraint(
        op.f("ck_ai_services_display_name"),
        "ai_services",
        "char_length(name) BETWEEN 2 AND 40 AND name = btrim(name)",
        schema="core",
    )


def downgrade() -> None:
    op.drop_column("ai_services", "name", schema="core")

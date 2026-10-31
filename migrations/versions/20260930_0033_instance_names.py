"""Separate administrator instance names from Runtime identifiers.

Revision ID: 20260930_0033
Revises: 20260929_0032
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260930_0033"
down_revision: str | None = "20260929_0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("model_instances", sa.Column("name", sa.Text(), nullable=True), schema="core")
    # Full UUID suffix is unique even when legacy model names and UUID prefixes match.
    op.execute(
        """
        UPDATE core.model_instances AS i
        SET name = left(btrim(m.display_name), 31) || '-' || replace(i.id::text, '-', '')
        FROM core.model_artifacts AS a JOIN core.models AS m ON m.id = a.model_id
        WHERE i.artifact_id = a.id
        """
    )
    op.alter_column("model_instances", "name", nullable=False, schema="core")
    op.create_unique_constraint(
        "uq_model_instances_name", "model_instances", ["name"], schema="core"
    )
    op.create_check_constraint(
        op.f("ck_model_instances_name"),
        "model_instances",
        "char_length(name) BETWEEN 2 AND 64 AND name = btrim(name)",
        schema="core",
    )


def downgrade() -> None:
    op.drop_column("model_instances", "name", schema="core")

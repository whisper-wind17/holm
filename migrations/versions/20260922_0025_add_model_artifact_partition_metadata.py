"""add model artifact partition metadata

Revision ID: 20260922_0025
Revises: 20260921_0024
Create Date: 2026-09-22 03:42:45.991984
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260922_0025"
down_revision: str | None = "20260921_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "model_artifacts",
        sa.Column("attention_heads", sa.Integer(), nullable=True),
        schema="core",
    )
    op.add_column(
        "model_artifacts",
        sa.Column("kv_heads", sa.Integer(), nullable=True),
        schema="core",
    )
    op.create_check_constraint(
        "ck_model_artifacts_attention_heads_positive",
        "model_artifacts",
        "attention_heads IS NULL OR attention_heads > 0",
        schema="core",
    )
    op.create_check_constraint(
        "ck_model_artifacts_kv_heads_positive",
        "model_artifacts",
        "kv_heads IS NULL OR kv_heads > 0",
        schema="core",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_model_artifacts_kv_heads_positive",
        "model_artifacts",
        schema="core",
        type_="check",
    )
    op.drop_constraint(
        "ck_model_artifacts_attention_heads_positive",
        "model_artifacts",
        schema="core",
        type_="check",
    )
    op.drop_column("model_artifacts", "kv_heads", schema="core")
    op.drop_column("model_artifacts", "attention_heads", schema="core")

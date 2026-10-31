"""Create encrypted Model Source credentials.

Revision ID: 20260902_0009
Revises: 20260902_0008
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260902_0009"
down_revision: str | None = "20260902_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "core"
MODEL_SOURCE_TYPES = ("Hugging Face", "ModelScope")


def _source_type_clause() -> str:
    rendered = ", ".join(f"'{value}'" for value in MODEL_SOURCE_TYPES)
    return f"type IN ({rendered})"


def upgrade() -> None:
    """Create only the baseline-defined subset of ``core.model_sources``."""

    op.create_table(
        "model_sources",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("credential_key_id", sa.Text(), nullable=True),
        sa.Column("credential_nonce", sa.LargeBinary(), nullable=True),
        sa.Column("credential_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("credential_tag", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_model_sources"),
        sa.UniqueConstraint("type", name="uq_model_sources_type"),
        sa.CheckConstraint(_source_type_clause(), name="ck_model_sources_type"),
        sa.CheckConstraint(
            "(credential_key_id IS NULL AND credential_nonce IS NULL "
            "AND credential_ciphertext IS NULL AND credential_tag IS NULL) OR "
            "(credential_key_id IS NOT NULL AND credential_nonce IS NOT NULL "
            "AND credential_ciphertext IS NOT NULL AND credential_tag IS NOT NULL)",
            name="ck_model_sources_credential_envelope_complete",
        ),
        sa.CheckConstraint(
            "credential_nonce IS NULL OR octet_length(credential_nonce) = 12",
            name="ck_model_sources_credential_nonce_length",
        ),
        sa.CheckConstraint(
            "credential_ciphertext IS NULL OR octet_length(credential_ciphertext) > 0",
            name="ck_model_sources_credential_ciphertext_nonempty",
        ),
        sa.CheckConstraint(
            "credential_tag IS NULL OR octet_length(credential_tag) = 16",
            name="ck_model_sources_credential_tag_length",
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    """Remove the partial Model Source relation without touching model-catalog tables."""

    op.drop_table("model_sources", schema=SCHEMA)

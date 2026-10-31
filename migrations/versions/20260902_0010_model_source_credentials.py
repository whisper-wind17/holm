"""Complete the Model Source credential and Model origin contracts.

Revision ID: 20260902_0010
Revises: 20260902_0009
Create Date: 2026-09-02
"""

from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "20260902_0010"
down_revision: str | None = "20260902_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "core"
LEGACY_MODEL_SOURCE_TYPES = {
    "huggingface": "Hugging Face",
    "modelscope": "ModelScope",
    "local_path": "Local Path",
}
MODEL_SOURCE_TYPES = ("Hugging Face", "ModelScope")
MODEL_ORIGIN_TYPES = ("Hugging Face", "ModelScope", "Local Path")
MODEL_SOURCE_VALIDATION_STATUSES = (
    "accessible",
    "invalid_credential",
    "permission_denied",
    "not_found",
    "rate_limited",
)


def _values_clause(column: str, values: Sequence[str]) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({rendered})"


def _backfill_model_source_references() -> None:
    """Create only source rows required by pre-existing credential-backed Models."""

    connection = op.get_bind()
    for source_type in MODEL_SOURCE_TYPES:
        parameters = {"source_type": source_type}
        source_id = connection.execute(
            sa.text("SELECT id FROM core.model_sources WHERE type = :source_type"),
            parameters,
        ).scalar_one_or_none()
        has_models = connection.execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM core.models WHERE source_type = :source_type)"),
            parameters,
        ).scalar_one()
        if source_id is None and has_models:
            source_id = uuid4()
            connection.execute(
                sa.text(
                    "INSERT INTO core.model_sources ("
                    "id, type, credential_key_id, credential_nonce, "
                    "credential_ciphertext, credential_tag, created_at, updated_at"
                    ") VALUES ("
                    ":source_id, :source_type, NULL, NULL, NULL, NULL, "
                    "clock_timestamp(), clock_timestamp())"
                ),
                {"source_id": source_id, **parameters},
            )
        if source_id is not None:
            connection.execute(
                sa.text(
                    "UPDATE core.models SET source_id = :source_id "
                    "WHERE source_type = :source_type AND source_id IS NULL"
                ),
                {"source_id": source_id, **parameters},
            )


def _normalize_legacy_model_source_types() -> None:
    """Translate the exact values accepted by the pre-0010 model fixture."""

    connection = op.get_bind()
    for legacy_value, canonical_value in LEGACY_MODEL_SOURCE_TYPES.items():
        connection.execute(
            sa.text(
                "UPDATE core.models SET source_type = :canonical_value "
                "WHERE source_type = :legacy_value"
            ),
            {
                "canonical_value": canonical_value,
                "legacy_value": legacy_value,
            },
        )


def upgrade() -> None:
    """Add validation facts and database-enforced Model source identity."""

    op.add_column(
        "model_sources",
        sa.Column("verification_status", sa.Text(), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "model_sources",
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "model_sources",
        sa.Column("verification_reason_code", sa.Text(), nullable=True),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_model_sources_verification_status",
        "model_sources",
        "verification_status IS NULL OR "
        + _values_clause("verification_status", MODEL_SOURCE_VALIDATION_STATUSES),
        schema=SCHEMA,
    )
    op.create_unique_constraint(
        "uq_model_sources_id_type",
        "model_sources",
        ["id", "type"],
        schema=SCHEMA,
    )

    op.add_column(
        "models",
        sa.Column("source_id", sa.Uuid(as_uuid=True), nullable=True),
        schema=SCHEMA,
    )
    _normalize_legacy_model_source_types()
    _backfill_model_source_references()
    op.create_check_constraint(
        "ck_models_source_type",
        "models",
        _values_clause("source_type", MODEL_ORIGIN_TYPES),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_models_source_scope",
        "models",
        "(source_type = 'Local Path' AND source_id IS NULL) OR "
        "(source_type IN ('Hugging Face', 'ModelScope') AND source_id IS NOT NULL)",
        schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_models_source_identity_model_sources",
        "models",
        "model_sources",
        ["source_id", "source_type"],
        ["id", "type"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    """Remove only the constraints and columns introduced by this revision."""

    op.drop_constraint(
        "fk_models_source_identity_model_sources",
        "models",
        schema=SCHEMA,
        type_="foreignkey",
    )
    op.drop_constraint("ck_models_source_scope", "models", schema=SCHEMA, type_="check")
    op.drop_constraint("ck_models_source_type", "models", schema=SCHEMA, type_="check")
    op.drop_column("models", "source_id", schema=SCHEMA)

    op.drop_constraint(
        "uq_model_sources_id_type",
        "model_sources",
        schema=SCHEMA,
        type_="unique",
    )
    op.drop_constraint(
        "ck_model_sources_verification_status",
        "model_sources",
        schema=SCHEMA,
        type_="check",
    )
    op.drop_column("model_sources", "verification_reason_code", schema=SCHEMA)
    op.drop_column("model_sources", "verified_at", schema=SCHEMA)
    op.drop_column("model_sources", "verification_status", schema=SCHEMA)

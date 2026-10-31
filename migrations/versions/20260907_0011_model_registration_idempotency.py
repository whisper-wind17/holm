"""Add Model registration metadata and HTTP idempotency constraints.

Revision ID: 20260907_0011
Revises: 20260902_0010
Create Date: 2026-09-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260907_0011"
down_revision: str | None = "20260902_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "core"
SHA256_SQL_PATTERN = "^[0-9a-f]{64}$"
METADATA_STATUSES = ("CONFIRMED", "UNCONFIRMED")


def _values_clause(column: str, values: Sequence[str]) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    """Create the reusable request record and make unknown metadata representable."""

    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("key_hash", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("response_object_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_idempotency_records"),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            [f"{SCHEMA}.users.id"],
            name="fk_idempotency_records_actor_id_users",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "actor_id",
            "operation",
            "key_hash",
            name="uq_idempotency_scope",
        ),
        sa.CheckConstraint(
            "char_length(operation) > 0",
            name="ck_idempotency_records_operation_nonempty",
        ),
        sa.CheckConstraint(
            f"key_hash ~ '{SHA256_SQL_PATTERN}'",
            name="ck_idempotency_records_key_hash_sha256",
        ),
        sa.CheckConstraint(
            f"request_hash ~ '{SHA256_SQL_PATTERN}'",
            name="ck_idempotency_records_request_hash_sha256",
        ),
        schema=SCHEMA,
    )

    op.execute(
        "UPDATE core.models SET metadata_status = 'CONFIRMED' WHERE metadata_status = 'Resolved'"
    )
    op.create_check_constraint(
        "ck_models_metadata_status",
        "models",
        _values_clause("metadata_status", METADATA_STATUSES),
        schema=SCHEMA,
    )
    op.drop_constraint("ck_models_modalities_array", "models", schema=SCHEMA, type_="check")
    op.drop_constraint(
        "ck_models_supported_runtimes_array",
        "models",
        schema=SCHEMA,
        type_="check",
    )
    op.alter_column("models", "modalities", nullable=True, schema=SCHEMA)
    op.alter_column("models", "supported_runtimes", nullable=True, schema=SCHEMA)
    op.create_check_constraint(
        "ck_models_modalities_array",
        "models",
        "modalities IS NULL OR jsonb_typeof(modalities) = 'array'",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_models_supported_runtimes_array",
        "models",
        "supported_runtimes IS NULL OR jsonb_typeof(supported_runtimes) = 'array'",
        schema=SCHEMA,
    )


def downgrade() -> None:
    """Restore the pre-registration relation shapes."""

    op.drop_constraint(
        "ck_models_supported_runtimes_array",
        "models",
        schema=SCHEMA,
        type_="check",
    )
    op.drop_constraint("ck_models_modalities_array", "models", schema=SCHEMA, type_="check")
    op.execute("UPDATE core.models SET modalities = '[]'::jsonb WHERE modalities IS NULL")
    op.execute(
        "UPDATE core.models SET supported_runtimes = '[]'::jsonb WHERE supported_runtimes IS NULL"
    )
    op.alter_column("models", "supported_runtimes", nullable=False, schema=SCHEMA)
    op.alter_column("models", "modalities", nullable=False, schema=SCHEMA)
    op.create_check_constraint(
        "ck_models_supported_runtimes_array",
        "models",
        "jsonb_typeof(supported_runtimes) = 'array'",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_models_modalities_array",
        "models",
        "jsonb_typeof(modalities) = 'array'",
        schema=SCHEMA,
    )
    op.drop_constraint("ck_models_metadata_status", "models", schema=SCHEMA, type_="check")
    op.execute(
        "UPDATE core.models SET metadata_status = 'Resolved' "
        "WHERE metadata_status IN ('CONFIRMED', 'UNCONFIRMED')"
    )
    op.drop_table("idempotency_records", schema=SCHEMA)

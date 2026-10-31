"""Move AI Service Primary Backends into an independent history table.

Revision ID: 20260917_0017
Revises: 20260916_0016
Create Date: 2026-09-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260917_0017"
down_revision: str | None = "20260916_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "core"


def upgrade() -> None:
    op.create_table(
        "ai_service_backends",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("service_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("instance_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("instance_name_snapshot", sa.Text(), nullable=False),
        sa.Column("runtime_snapshot", sa.Text(), nullable=False),
        sa.Column("backend_key", sa.Text(), nullable=False),
        sa.Column("weight", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("health_gate_passed", sa.Boolean(), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_ai_service_backends"),
        sa.ForeignKeyConstraint(
            ["service_id"],
            [f"{SCHEMA}.ai_services.id"],
            name="fk_ai_service_backends_service_id_ai_services",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            [f"{SCHEMA}.model_instances.id"],
            name="fk_ai_service_backends_instance_id_model_instances",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "retired_at IS NOT NULL OR instance_id IS NOT NULL",
            name="ck_active_backend_has_instance",
        ),
        sa.CheckConstraint(
            "backend_key = 'primary'",
            name="ck_ai_service_backends_backend_key_primary",
        ),
        sa.CheckConstraint(
            "retired_at IS NOT NULL OR (weight = 100 AND enabled)",
            name="ck_ai_service_backends_active_primary_shape",
        ),
        sa.CheckConstraint(
            "retired_at IS NULL OR retired_at >= joined_at",
            name="ck_ai_service_backends_retired_after_joined",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_active_service_backend_key",
        "ai_service_backends",
        ["service_id", "backend_key"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("retired_at IS NULL"),
    )
    op.create_index(
        "uq_active_service_backend_instance",
        "ai_service_backends",
        ["service_id", "instance_id"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("retired_at IS NULL"),
    )
    op.create_index(
        "ix_ai_service_backends_instance_id",
        "ai_service_backends",
        ["instance_id"],
        schema=SCHEMA,
    )

    op.execute(
        sa.text(
            """
            INSERT INTO core.ai_service_backends (
                id, service_id, instance_id, instance_name_snapshot, runtime_snapshot,
                backend_key, weight, enabled, health_gate_passed, joined_at, retired_at
            )
            SELECT
                gen_random_uuid(), service.id, instance.id, instance.internal_model_name,
                instance.runtime_type, 'primary', 100, true,
                (instance.status = 'Running'), service.created_at, NULL
            FROM core.ai_services AS service
            JOIN core.model_instances AS instance
              ON instance.id = service.primary_instance_id
            WHERE service.deleted_at IS NULL
            """
        )
    )
    op.drop_index("ix_ai_services_primary_instance_id", table_name="ai_services", schema=SCHEMA)
    op.drop_constraint(
        "fk_ai_services_primary_instance_id_model_instances",
        "ai_services",
        schema=SCHEMA,
        type_="foreignkey",
    )
    op.drop_column("ai_services", "primary_instance_id", schema=SCHEMA)


def downgrade() -> None:
    op.add_column(
        "ai_services",
        sa.Column("primary_instance_id", sa.Uuid(as_uuid=True), nullable=True),
        schema=SCHEMA,
    )
    op.execute(
        sa.text(
            """
            UPDATE core.ai_services AS service
            SET primary_instance_id = backend.instance_id
            FROM core.ai_service_backends AS backend
            WHERE backend.service_id = service.id
              AND backend.backend_key = 'primary'
              AND backend.retired_at IS NULL
            """
        )
    )
    op.alter_column(
        "ai_services",
        "primary_instance_id",
        existing_type=sa.Uuid(as_uuid=True),
        nullable=False,
        schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_ai_services_primary_instance_id_model_instances",
        "ai_services",
        "model_instances",
        ["primary_instance_id"],
        ["id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_ai_services_primary_instance_id",
        "ai_services",
        ["primary_instance_id"],
        schema=SCHEMA,
    )
    op.drop_table("ai_service_backends", schema=SCHEMA)

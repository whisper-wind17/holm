"""Record the metering provenance on both Request Record projections.

Revision ID: 20260918_0020
Revises: 20260917_0019
Create Date: 2026-09-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260918_0020"
down_revision: str | None = "20260917_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CORE_SCHEMA = "core"
USAGE_SCHEMA = "usage"
TABLE = "request_records"
METERING_SOURCES = (
    "runtime_metrics",
    "runtime_usage",
    "tokenizer_fallback",
    "unavailable",
)
METRICS_CAPABILITIES = ("full", "partial", "unsupported")


def _values(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    # Both columns stay NULL for every row written before this revision: the
    # metering source of a historical record is genuinely unknown and must not
    # be back-filled with a guess (Token origin must never be estimated).
    for schema in (CORE_SCHEMA, USAGE_SCHEMA):
        op.add_column(
            TABLE,
            sa.Column("metering_source", sa.Text(), nullable=True),
            schema=schema,
        )
        op.add_column(
            TABLE,
            sa.Column("metrics_capability", sa.Text(), nullable=True),
            schema=schema,
        )
        op.create_check_constraint(
            f"ck_{TABLE}_metering_source",
            TABLE,
            f"metering_source IS NULL OR metering_source IN ({_values(METERING_SOURCES)})",
            schema=schema,
        )
        op.create_check_constraint(
            f"ck_{TABLE}_metrics_capability",
            TABLE,
            "metrics_capability IS NULL OR "
            f"metrics_capability IN ({_values(METRICS_CAPABILITIES)})",
            schema=schema,
        )


def downgrade() -> None:
    for schema in (CORE_SCHEMA, USAGE_SCHEMA):
        op.drop_constraint(f"ck_{TABLE}_metrics_capability", TABLE, schema=schema)
        op.drop_constraint(f"ck_{TABLE}_metering_source", TABLE, schema=schema)
        op.drop_column(TABLE, "metrics_capability", schema=schema)
        op.drop_column(TABLE, "metering_source", schema=schema)

"""Create the three PostgreSQL Schema namespaces.

Revision ID: 20260828_0001
Revises: None
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from holm_persistence.schemas import SCHEMA_NAMES

revision: str = "20260828_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create only the frozen Schema namespaces; business tables come later."""
    for schema_name in SCHEMA_NAMES:
        op.execute(sa.schema.CreateSchema(schema_name))


def downgrade() -> None:
    """Return the database to the empty initial state."""
    for schema_name in reversed(SCHEMA_NAMES):
        op.execute(sa.schema.DropSchema(schema_name))

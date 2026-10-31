"""Release display names after logical AI Service deletion.

Revision ID: 20261009_0039
Revises: 20261009_0038
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261009_0039"
down_revision: str | None = "20261009_0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("uq_ai_services_name", "ai_services", schema="core", type_="unique")
    op.create_index(
        "uq_ai_services_name",
        "ai_services",
        ["name"],
        unique=True,
        schema="core",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_ai_services_name", table_name="ai_services", schema="core")
    op.create_unique_constraint("uq_ai_services_name", "ai_services", ["name"], schema="core")

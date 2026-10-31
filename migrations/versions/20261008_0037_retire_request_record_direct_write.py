"""Retire Gateway INSERT on the retained prototype Request Record table.

Revision ID: 20261008_0037
Revises: 20261008_0036
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261008_0037"
down_revision: str | None = "20261008_0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("REVOKE INSERT ON TABLE core.request_records FROM holm_gateway")


def downgrade() -> None:
    op.execute("GRANT INSERT ON TABLE core.request_records TO holm_gateway")

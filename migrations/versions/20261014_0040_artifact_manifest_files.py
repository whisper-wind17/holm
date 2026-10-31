"""Persist Manifest files and allow Control to delete terminal tasks."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from holm_persistence.role_grants import CONTROL
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "20261014_0040"
down_revision: str | None = "20261009_0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "model_artifacts",
        sa.Column("manifest_files", JSONB(), nullable=True),
        schema="core",
    )
    op.execute(f'GRANT DELETE ON TABLE core.download_tasks TO "{CONTROL}"')


def downgrade() -> None:
    op.execute(f'REVOKE DELETE ON TABLE core.download_tasks FROM "{CONTROL}"')
    op.drop_column("model_artifacts", "manifest_files", schema="core")

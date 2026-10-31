"""Retain stored objects and privileges while renaming identifiers."""

from collections.abc import Sequence

from alembic import op
from holm_persistence.identifier_rename import rename_legacy_identifiers

revision: str = "20261020_0044"
down_revision: str | None = "20261020_0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    rename_legacy_identifiers(op.get_bind())


def downgrade() -> None:
    # Historical revisions now create Holm objects too; renaming back would make
    # an existing installation disagree with a fresh database at the same revision.
    pass

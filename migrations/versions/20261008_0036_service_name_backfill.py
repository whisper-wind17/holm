"""Readable names for services still bearing the 0035 automatic name.

Revision ID: 20261008_0036
Revises: 20260930_0035
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261008_0036"
down_revision: str | None = "20260930_0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Count candidates across all rows, including manually named and deleted services.
    op.execute(
        """
        WITH candidates AS (
            SELECT id, btrim(public_model_name) AS candidate,
                   count(*) OVER (PARTITION BY btrim(public_model_name)) AS occurrences
            FROM core.ai_services
        )
        UPDATE core.ai_services AS service
        SET name = candidates.candidate
        FROM candidates
        WHERE service.id = candidates.id
          AND service.name = left(btrim(service.public_model_name), 7)
                             || '-' || replace(service.id::text, '-', '')
          AND char_length(candidates.candidate) BETWEEN 2 AND 40
          AND candidates.occurrences = 1
          AND NOT EXISTS (
              SELECT 1 FROM core.ai_services AS occupied
              WHERE occupied.id <> service.id AND occupied.name = candidates.candidate
          )
        """
    )


def downgrade() -> None:
    # Data-only correction: previous names cannot be recovered reliably.
    pass

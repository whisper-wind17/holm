"""Readable instance display names, without changing routing identities.

Revision ID: 20261009_0038
Revises: 20261008_0037
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261009_0038"
down_revision: str | None = "20261008_0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    rows = (
        connection.execute(
            sa.text(
                """
            SELECT i.id, i.name, m.display_name
            FROM core.model_instances i
            JOIN core.model_artifacts a ON a.id = i.artifact_id
            JOIN core.models m ON m.id = a.model_id
            WHERE left(i.name, char_length(left(btrim(m.display_name), 31)) + 1)
                    = left(btrim(m.display_name), 31) || '-'
              AND substring(i.name FROM char_length(left(btrim(m.display_name), 31)) + 2)
                    ~ '^[0-9a-f]{32}$'
            ORDER BY i.created_at, i.id
            """
            )
        )
        .mappings()
        .all()
    )
    occupied = set(connection.execute(sa.text("SELECT name FROM core.model_instances")).scalars())
    for row in rows:
        # Freeze the display-name rule here: future domain changes must not alter this migration.
        base = row["display_name"].rsplit("/", 1)[-1].strip()
        if len(base) < 2:
            base = "模型实例"
        for sequence in range(1, len(occupied) + 2):
            suffix = "" if sequence == 1 else f"-{sequence}"
            candidate = base[: 64 - len(suffix)].rstrip() + suffix
            if candidate not in occupied:
                break
        connection.execute(
            sa.text("UPDATE core.model_instances SET name=:name WHERE id=:id"),
            {"name": candidate, "id": row["id"]},
        )
        occupied.remove(row["name"])
        occupied.add(candidate)


def downgrade() -> None:
    # Display-only correction: the original automatic names cannot be recovered.
    pass

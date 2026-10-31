"""Allow Gateway to resolve model reasoning policy by architecture."""

from collections.abc import Sequence

from alembic import op
from holm_persistence.role_grants import GATEWAY

revision: str = "20261016_0041"
down_revision: str | None = "20261014_0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(f'GRANT SELECT (id, architecture) ON TABLE core.models TO "{GATEWAY}"')
    op.execute(f'GRANT SELECT (model_id) ON TABLE core.model_artifacts TO "{GATEWAY}"')


def downgrade() -> None:
    op.execute(f'REVOKE SELECT (model_id) ON TABLE core.model_artifacts FROM "{GATEWAY}"')
    op.execute(f'REVOKE SELECT (id, architecture) ON TABLE core.models FROM "{GATEWAY}"')

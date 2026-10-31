"""Allow only deletion owners to detach Artifact references and delete rows.

Revision ID: 20260923_0028
Revises: 20260923_0027
"""

from collections.abc import Sequence

from alembic import op
from holm_persistence.role_grants import CONTROL, DOWNLOAD

revision: str = "20260923_0028"
down_revision: str | None = "20260923_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(f'GRANT DELETE ON TABLE core.model_artifacts TO "{CONTROL}", "{DOWNLOAD}"')
    op.execute(f'GRANT INSERT ON TABLE core.jobs TO "{DOWNLOAD}"')
    op.execute(f'GRANT SELECT ON TABLE core.model_instances TO "{DOWNLOAD}"')
    op.execute(f'GRANT INSERT ON TABLE core.audit_events TO "{DOWNLOAD}"')


def downgrade() -> None:
    op.execute(f'REVOKE INSERT ON TABLE core.audit_events FROM "{DOWNLOAD}"')
    op.execute(f'REVOKE SELECT ON TABLE core.model_instances FROM "{DOWNLOAD}"')
    op.execute(f'REVOKE DELETE ON TABLE core.model_artifacts FROM "{CONTROL}", "{DOWNLOAD}"')
    op.execute(f'REVOKE INSERT ON TABLE core.jobs FROM "{DOWNLOAD}"')

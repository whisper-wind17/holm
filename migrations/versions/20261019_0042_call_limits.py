"""Nullable call limits; existing grants remain unlimited."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from holm_persistence.role_grants import CONTROL, GATEWAY

revision: str = "20261019_0042"
down_revision: str | None = "20261016_0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user_service_permissions",
        sa.Column("max_concurrent_requests", sa.Integer(), nullable=True),
        schema="core",
    )
    op.add_column(
        "user_service_permissions",
        sa.Column("max_requests_per_minute", sa.Integer(), nullable=True),
        schema="core",
    )
    op.create_check_constraint(
        "ck_permission_concurrent",
        "user_service_permissions",
        "max_concurrent_requests BETWEEN 1 AND 64",
        schema="core",
    )
    op.create_check_constraint(
        "ck_permission_per_minute",
        "user_service_permissions",
        "max_requests_per_minute BETWEEN 1 AND 600",
        schema="core",
    )

    op.execute(
        f'GRANT SELECT (max_concurrent_requests, max_requests_per_minute) ON core.user_service_permissions TO "{GATEWAY}"'
    )
    op.execute(
        f'GRANT UPDATE (max_concurrent_requests, max_requests_per_minute) ON core.user_service_permissions TO "{CONTROL}"'
    )


def downgrade() -> None:
    op.drop_constraint("ck_permission_per_minute", "user_service_permissions", schema="core")
    op.drop_constraint("ck_permission_concurrent", "user_service_permissions", schema="core")
    op.drop_column("user_service_permissions", "max_requests_per_minute", schema="core")
    op.drop_column("user_service_permissions", "max_concurrent_requests", schema="core")

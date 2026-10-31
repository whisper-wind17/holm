"""Migrate API key display prefixes without changing credentials."""

from collections.abc import Sequence

from alembic import op

revision: str = "20261020_0045"
down_revision: str | None = "20261020_0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_api_keys_display_prefix", "api_keys", schema="core", type_="check")
    op.execute("UPDATE core.api_keys SET display_prefix = 'holm_sk_'")
    op.create_check_constraint(
        "ck_api_keys_display_prefix", "api_keys", "display_prefix = 'holm_sk_'", schema="core"
    )


def downgrade() -> None:
    op.drop_constraint("ck_api_keys_display_prefix", "api_keys", schema="core", type_="check")
    op.execute("UPDATE core.api_keys SET display_prefix = 'pwsk_'")
    op.create_check_constraint(
        "ck_api_keys_display_prefix", "api_keys", "display_prefix = 'pwsk_'", schema="core"
    )

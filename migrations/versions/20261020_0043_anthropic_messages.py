"""Extend the metadata-only request projection to Anthropic messages."""

from collections.abc import Sequence

from alembic import op

revision: str = "20261020_0043"
down_revision: str | None = "20261019_0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _checks(*, anthropic: bool) -> None:
    for field, old, new in (
        ("protocol", "openai", "anthropic"),
        ("operation", "chat.completions", "messages"),
    ):
        name = f"ck_request_records_{field}"
        op.drop_constraint(name, "request_records", schema="usage", type_="check")
        values = f"'{old}', '{new}'" if anthropic else f"'{old}'"
        op.create_check_constraint(
            name, "request_records", f"{field} IS NULL OR {field} IN ({values})", schema="usage"
        )


def upgrade() -> None:
    _checks(anthropic=True)


def downgrade() -> None:
    # Existing Anthropic rows make this fail atomically; never delete history.
    _checks(anthropic=False)

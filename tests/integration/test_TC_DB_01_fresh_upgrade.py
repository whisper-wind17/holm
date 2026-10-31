"""TC-DB-01: fresh PostgreSQL upgrade and idempotent repeat."""

from migration_test_support import (
    PostgresFixture,
    assert_head_structure,
    database_structure,
    upgrade_head,
)


def test_TC_DB_01_fresh_upgrade_is_complete_and_idempotent(
    ephemeral_postgres: PostgresFixture,
) -> None:
    engine = ephemeral_postgres.engine
    initial = database_structure(engine)
    assert initial.schemas == ("public",)
    assert initial.relations == ()
    assert initial.alembic_versions == ()

    upgrade_head(engine)
    first_upgrade = database_structure(engine)
    assert_head_structure(first_upgrade)

    upgrade_head(engine)
    assert database_structure(engine) == first_upgrade

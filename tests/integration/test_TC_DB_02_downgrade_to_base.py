"""TC-DB-02: downgrade PostgreSQL to an object-free base and upgrade again."""

from migration_test_support import (
    PostgresFixture,
    assert_head_structure,
    database_structure,
    downgrade_base,
    upgrade_head,
)


def test_TC_DB_02_downgrade_to_base_removes_objects_and_reupgrades(
    ephemeral_postgres: PostgresFixture,
) -> None:
    engine = ephemeral_postgres.engine
    initial = database_structure(engine)

    upgrade_head(engine)
    first_upgrade = database_structure(engine)
    assert_head_structure(first_upgrade)

    downgrade_base(engine)
    assert database_structure(engine) == initial

    upgrade_head(engine)
    assert database_structure(engine) == first_upgrade

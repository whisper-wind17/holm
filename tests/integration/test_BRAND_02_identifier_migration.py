"""FX-BRAND-02-OLD-DB: actual PostgreSQL objects renamed to the pre-brand names."""

from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from conftest import EphemeralPostgres, temporary_postgres
from holm_persistence.migrations import upgrade_to_head
from migration_test_support import ALEMBIC_CONFIG_PATH, head_revision
from sqlalchemy.engine import Connection

PREVIOUS = "20261020_0043"
REVISION = "20261020_0044"
LEGACY = "private" + "ws_"
ROLES = (
    "control",
    "gateway",
    "download_worker",
    "instance_controller",
    "ledger_writer",
    "maintenance_worker",
)
FUNCTIONS = (
    "update_api_key_last_used",
    "drop_expired_month_partitions",
    "ensure_month_partition",
    "schedule_artifact_checks",
)


def _upgrade(connection: Connection, revision: str) -> None:
    command.upgrade(Config(ALEMBIC_CONFIG_PATH, attributes={"connection": connection}), revision)


@pytest.fixture
def BRAND_02_database() -> Iterator[EphemeralPostgres]:
    # Each case owns a separate cluster because roles are cluster-wide objects.
    with temporary_postgres() as database:
        with database.engine.connect() as connection:
            _upgrade(connection, PREVIOUS)
        yield database


def _functions(connection: Connection) -> list[sa.Row[tuple[str, str]]]:
    return list(
        connection.execute(
            sa.text(
                "SELECT p.proname, pg_get_function_identity_arguments(p.oid) "
                "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'core' AND p.proname LIKE 'holm_%' ORDER BY p.oid"
            )
        ).all()
    )


def _old_objects(connection: Connection) -> None:
    for suffix in ROLES:
        connection.execute(sa.text(f'ALTER ROLE "holm_{suffix}" RENAME TO "{LEGACY}{suffix}"'))
    for name, signature in _functions(connection):
        suffix = name.removeprefix("holm_")
        connection.execute(
            sa.text(f'ALTER FUNCTION core."{name}"({signature}) RENAME TO "{LEGACY}{suffix}"')
        )


def test_BRAND_02_database_at_0040_upgrades_before_new_role_grants() -> None:
    # FX-BRAND-02-OLD-DB at the revision immediately before 0041's Gateway grants.
    with temporary_postgres() as database:
        with database.engine.connect() as connection:
            _upgrade(connection, "20261014_0040")
        with database.engine.begin() as connection:
            _old_objects(connection)
        with database.engine.connect() as connection:
            upgrade_to_head(connection, ALEMBIC_CONFIG_PATH)
            # Later migrations move head past 0044; this path only needs to reach head.
            assert (
                connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
                == head_revision()
            )
            assert (
                connection.scalar(
                    sa.text("SELECT count(*) FROM pg_roles WHERE rolname LIKE :prefix"),
                    {"prefix": LEGACY + "%"},
                )
                == 0
            )
            assert (
                connection.scalar(
                    sa.text(
                        "SELECT count(*) FROM pg_proc p "
                        "JOIN pg_namespace n ON n.oid = p.pronamespace "
                        "WHERE n.nspname = 'core' AND p.proname LIKE :prefix"
                    ),
                    {"prefix": LEGACY + "%"},
                )
                == 0
            )
            for column in ("id", "architecture"):
                assert (
                    connection.scalar(
                        sa.text(
                            "SELECT has_column_privilege("
                            "'holm_gateway', 'core.models', :column, 'SELECT')"
                        ),
                        {"column": column},
                    )
                    is True
                )
            assert (
                connection.scalar(
                    sa.text(
                        "SELECT has_function_privilege('holm_maintenance_worker', "
                        "'core.holm_schedule_artifact_checks()', 'EXECUTE')"
                    )
                )
                is True
            )
            assert {name.removeprefix("holm_") for name, _ in _functions(connection)} == set(
                FUNCTIONS
            )


def test_BRAND_02_existing_database_preserves_oids_grants_and_overloads(
    BRAND_02_database: EphemeralPostgres,
) -> None:
    engine = BRAND_02_database.engine
    with engine.begin() as connection:
        # A second signature proves pg_proc enumeration does not lose overloads.
        connection.execute(
            sa.text(
                "CREATE FUNCTION core.holm_schedule_artifact_checks(integer) RETURNS integer "
                "LANGUAGE SQL AS 'SELECT $1'"
            )
        )
        connection.execute(sa.text("CREATE ROLE brand02_member NOLOGIN"))
        connection.execute(sa.text("GRANT holm_gateway TO brand02_member"))
        role_oids = (
            connection.execute(
                sa.text("SELECT oid FROM pg_roles WHERE rolname LIKE 'holm_%' ORDER BY oid")
            )
            .scalars()
            .all()
        )
        function_state = connection.execute(
            sa.text(
                "SELECT p.oid, p.proowner, p.proacl, p.prosrc FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'core' AND p.proname LIKE 'holm_%' ORDER BY p.oid"
            )
        ).all()
        assert len(role_oids) == 6
        assert len(function_state) == 5
        _old_objects(connection)
    with engine.connect() as connection:
        _upgrade(connection, REVISION)
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == REVISION
        assert (
            connection.scalar(
                sa.text("SELECT count(*) FROM pg_roles WHERE rolname LIKE :prefix"),
                {"prefix": LEGACY + "%"},
            )
            == 0
        )
        assert (
            connection.scalar(
                sa.text(
                    "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE n.nspname = 'core' AND p.proname LIKE :prefix"
                ),
                {"prefix": LEGACY + "%"},
            )
            == 0
        )
        assert (
            connection.execute(
                sa.text("SELECT oid FROM pg_roles WHERE rolname LIKE 'holm_%' ORDER BY oid")
            )
            .scalars()
            .all()
            == role_oids
        )
        assert (
            connection.execute(
                sa.text(
                    "SELECT p.oid, p.proowner, p.proacl, p.prosrc FROM pg_proc p "
                    "JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE n.nspname = 'core' AND p.proname LIKE 'holm_%' ORDER BY p.oid"
                )
            ).all()
            == function_state
        )
        # The frozen M7-07 policy grants four columns, never the whole users table.
        assert (
            connection.scalar(
                sa.text("SELECT has_table_privilege('holm_gateway', 'core.users', 'SELECT')")
            )
            is False
        )
        for column in ("id", "status", "role", "deleted_at"):
            assert (
                connection.scalar(
                    sa.text(
                        "SELECT has_column_privilege("
                        "'holm_gateway', 'core.users', :column, 'SELECT')"
                    ),
                    {"column": column},
                )
                is True
            )
        assert (
            connection.scalar(
                sa.text(
                    "SELECT has_column_privilege("
                    "'holm_gateway', 'core.users', 'password_hash', 'SELECT')"
                )
            )
            is False
        )
        assert (
            connection.scalar(
                sa.text(
                    "SELECT has_function_privilege('holm_maintenance_worker', "
                    "'core.holm_schedule_artifact_checks()', 'EXECUTE')"
                )
            )
            is True
        )
        assert (
            connection.scalar(
                sa.text("SELECT pg_has_role('brand02_member', 'holm_gateway', 'MEMBER')")
            )
            is True
        )
        assert {name.removeprefix("holm_") for name, _ in _functions(connection)} == set(FUNCTIONS)


@pytest.mark.parametrize("kind", ["role", "function"])
def test_BRAND_02_both_names_fail_loudly(
    BRAND_02_database: EphemeralPostgres,
    kind: str,
) -> None:
    engine = BRAND_02_database.engine
    with engine.begin() as connection:
        _old_objects(connection)
        if kind == "role":
            connection.execute(sa.text("CREATE ROLE holm_control NOLOGIN"))
        else:
            connection.execute(
                sa.text(
                    "CREATE FUNCTION core.holm_schedule_artifact_checks(integer) RETURNS integer "
                    "LANGUAGE SQL AS 'SELECT $1'"
                )
            )
    with (
        engine.connect() as connection,
        pytest.raises(RuntimeError, match="Identifier rename failed: both identifiers exist"),
    ):
        _upgrade(connection, REVISION)
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == PREVIOUS
        # The failed migration rolled back its preceding role renames too.
        assert (
            connection.scalar(
                sa.text("SELECT count(*) FROM pg_roles WHERE rolname LIKE :prefix"),
                {"prefix": LEGACY + "%"},
            )
            == 6
        )

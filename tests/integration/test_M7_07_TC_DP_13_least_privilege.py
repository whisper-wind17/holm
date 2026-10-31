"""M7-07 / TC-DP-13: real PostgreSQL runtime grants and narrow owner functions."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from holm_persistence import METADATA
from holm_persistence.role_grants import (
    COLUMN_SELECTS,
    CONTROL,
    DOWNLOAD,
    GATEWAY,
    INSTANCE,
    LEDGER,
    RUNTIME_ROLES,
    TABLE_GRANTS,
    verify_table_grants,
)
from migration_test_support import upgrade_head
from sqlalchemy.exc import DBAPIError


@pytest.fixture(scope="module", autouse=True)
def _migrated_database(ephemeral_postgres: EphemeralPostgres) -> Iterator[None]:
    upgrade_head(ephemeral_postgres.engine)
    yield


def _as_role(connection: sa.Connection, role: str) -> None:
    assert role in RUNTIME_ROLES
    connection.execute(sa.text(f'SET LOCAL ROLE "{role}"'))
    assert connection.scalar(sa.text("SELECT current_user")) == role


def _assert_denied(engine: sa.Engine, role: str, statement: str) -> None:
    with pytest.raises(DBAPIError) as error, engine.begin() as connection:
        _as_role(connection, role)
        connection.execute(sa.text(statement))
    assert getattr(error.value.orig, "sqlstate", None) == "42501"


def test_M7_07_TC_DP_13_each_mapped_table_has_explicit_grant_decision(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    verify_table_grants(METADATA)
    extra = sa.MetaData()
    for table in METADATA.tables.values():
        table.to_metadata(extra)
    sa.Table("future_table", extra, sa.Column("id", sa.Integer), schema="core")
    with pytest.raises(ValueError, match="missing=.*core.future_table"):
        verify_table_grants(extra)
    surprise = sa.Table("future_table", METADATA, sa.Column("id", sa.Integer), schema="core")
    try:
        with pytest.raises(ValueError, match="missing=.*core.future_table"):
            upgrade_head(ephemeral_postgres.engine)
    finally:
        METADATA.remove(surprise)


def test_M7_07_TC_DP_13_runtime_groups_are_nologin(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    with ephemeral_postgres.engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                "SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole "
                "FROM pg_roles WHERE rolname = ANY(:roles)"
            ),
            {"roles": list(RUNTIME_ROLES)},
        ).all()
    assert {row.rolname for row in rows} == set(RUNTIME_ROLES)
    assert all(not any(row[1:]) for row in rows)


def test_M7_07_TC_DP_13_each_runtime_role_can_use_its_reviewed_operations(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    # Zero-row DML proves PostgreSQL's permission checks against real tables
    # without manufacturing invalid domain records or changing fixture data.
    for table_name, grants in TABLE_GRANTS.items():
        table = METADATA.tables[table_name]
        target = f'"{table.schema}"."{table.name}"'
        for role, operations in grants.items():
            with ephemeral_postgres.engine.begin() as connection:
                _as_role(connection, role)
                if "S" in operations:
                    selected = COLUMN_SELECTS.get((table_name, role))
                    first = selected[0] if selected else next(iter(table.columns)).name
                    connection.execute(sa.text(f'SELECT "{first}" FROM {target} LIMIT 0'))
                if "I" in operations:
                    column = next(
                        col.name
                        for col in table.columns
                        if col.identity is None and col.computed is None
                    )
                    connection.execute(
                        sa.text(f'INSERT INTO {target} ("{column}") SELECT NULL WHERE false')
                    )
                if "U" in operations:
                    column = next(
                        col.name
                        for col in table.columns
                        if col.identity is None and col.computed is None
                    )
                    connection.execute(
                        sa.text(f'UPDATE {target} SET "{column}" = "{column}" WHERE false')
                    )
                if "D" in operations:
                    connection.execute(sa.text(f"DELETE FROM {target} WHERE false"))


@pytest.mark.parametrize(
    ("role", "forbidden_write"),
    (
        (
            CONTROL,
            "INSERT INTO telemetry.gateway_service_metric_samples (service_id) "
            "SELECT NULL WHERE false",
        ),
        (GATEWAY, "UPDATE core.users SET status = status WHERE false"),
        (DOWNLOAD, "UPDATE core.api_keys SET last_used_at = last_used_at WHERE false"),
        (INSTANCE, "UPDATE core.api_keys SET last_used_at = last_used_at WHERE false"),
        (LEDGER, "UPDATE core.api_keys SET last_used_at = last_used_at WHERE false"),
    ),
)
def test_M7_07_TC_DP_13_unlisted_write_and_ddl_are_denied(
    ephemeral_postgres: EphemeralPostgres, role: str, forbidden_write: str
) -> None:
    _assert_denied(ephemeral_postgres.engine, role, forbidden_write)
    _assert_denied(
        ephemeral_postgres.engine, role, "CREATE TABLE core.m7_07_forbidden (id integer)"
    )
    _assert_denied(
        ephemeral_postgres.engine, role, "CREATE TEMP TABLE m7_07_forbidden (id integer)"
    )
    _assert_denied(
        ephemeral_postgres.engine, role, "CREATE TABLE public.m7_07_forbidden (id integer)"
    )


def test_M7_07_TC_DP_13_partition_functions_accept_only_managed_parent_and_date(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    for role, parent in (
        (LEDGER, "usage.request_records"),
        (CONTROL, "telemetry.instance_metrics_1m"),
    ):
        with ephemeral_postgres.engine.begin() as connection:
            _as_role(connection, role)
            name = connection.scalar(
                sa.text("SELECT core.holm_ensure_month_partition(:parent, :start)"),
                {"parent": parent, "start": datetime(2020, 1, 1, tzinfo=UTC).date()},
            )
            assert name == parent.split(".")[1] + "_202001"
            dropped = connection.scalar(
                sa.text("SELECT core.holm_drop_expired_month_partitions(:parent, :cutoff)"),
                {"parent": parent, "cutoff": datetime(2021, 1, 1, tzinfo=UTC)},
            )
            assert dropped == [name]
    for parent in ("core.users", "usage.request_records; DROP TABLE core.users"):
        with pytest.raises(DBAPIError) as error, ephemeral_postgres.engine.begin() as connection:
            _as_role(connection, LEDGER)
            connection.execute(
                sa.text("SELECT core.holm_ensure_month_partition(:parent, DATE '2020-01-01')"),
                {"parent": parent},
            )
        assert getattr(error.value.orig, "sqlstate", None) == "22023"
        with pytest.raises(DBAPIError) as error, ephemeral_postgres.engine.begin() as connection:
            _as_role(connection, LEDGER)
            connection.execute(
                sa.text("SELECT core.holm_drop_expired_month_partitions(:parent, :cutoff)"),
                {"parent": parent, "cutoff": datetime(2021, 1, 1, tzinfo=UTC)},
            )
        assert getattr(error.value.orig, "sqlstate", None) == "22023"
    with pytest.raises(DBAPIError) as error, ephemeral_postgres.engine.begin() as connection:
        _as_role(connection, LEDGER)
        connection.execute(
            sa.text(
                "SELECT core.holm_ensure_month_partition("
                "'usage.request_records', DATE '2020-01-02')"
            )
        )
    assert getattr(error.value.orig, "sqlstate", None) == "22023"
    _assert_denied(
        ephemeral_postgres.engine,
        GATEWAY,
        "SELECT core.holm_ensure_month_partition('usage.request_records', DATE '2020-01-01')",
    )


def test_FIX_11_retention_floor_for_privileged_partition_drop(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    with ephemeral_postgres.engine.begin() as connection:
        database_now = connection.scalar(sa.text("SELECT now()"))
        assert isinstance(database_now, datetime)
        current_month = database_now.date().replace(day=1)
        _as_role(connection, LEDGER)
        old_name = connection.scalar(
            sa.text("SELECT core.holm_ensure_month_partition(:parent, :month)"),
            {"parent": "usage.request_records", "month": datetime(2019, 1, 1, tzinfo=UTC).date()},
        )
        recent_name = connection.scalar(
            sa.text("SELECT core.holm_ensure_month_partition(:parent, :month)"),
            {"parent": "usage.request_records", "month": current_month},
        )
    with pytest.raises(DBAPIError) as error, ephemeral_postgres.engine.begin() as connection:
        _as_role(connection, LEDGER)
        connection.execute(
            sa.text("SELECT core.holm_drop_expired_month_partitions(:parent, :cutoff)"),
            {"parent": "usage.request_records", "cutoff": database_now - timedelta(days=29)},
        )
    assert getattr(error.value.orig, "sqlstate", None) == "22023"
    assert "cutoff exceeds minimum retention period" in str(error.value.orig)

    with ephemeral_postgres.engine.begin() as connection:
        _as_role(connection, LEDGER)
        dropped = connection.scalar(
            sa.text("SELECT core.holm_drop_expired_month_partitions(:parent, :cutoff)"),
            {"parent": "usage.request_records", "cutoff": database_now - timedelta(days=31)},
        )
        assert dropped == [old_name]
        assert (
            connection.scalar(
                sa.text("SELECT to_regclass(:name)"), {"name": f"usage.{recent_name}"}
            )
            is not None
        )


def test_FIX_11_sql_retention_floor_matches_python_config(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    from holm_common.config import MIN_RETENTION_DAYS

    with ephemeral_postgres.engine.connect() as connection:
        definition = connection.scalar(
            sa.text(
                "SELECT pg_get_functiondef("
                "'core.holm_drop_expired_month_partitions(text, timestamptz)'::regprocedure)"
            )
        )
    assert isinstance(definition, str)
    match = re.search(r"cutoff > now\(\) - (?:'|INTERVAL ')(\d+) days'", definition, re.IGNORECASE)
    assert match is not None
    assert int(match.group(1)) == MIN_RETENTION_DAYS


def test_M7_07_TC_DP_13_last_used_function_preserves_monotonic_timestamp(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    key_id = UUID("00000000-0000-7000-8000-000000007007")
    # The owner seeds the row; the runtime role only invokes the narrow function.
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO core.users "
                "(id, username_norm, display_name, role, status, password_hash, "
                "must_change_password, created_at, updated_at) "
                "VALUES (:id, 'm707user', 'M7-07', 'admin', 'active', "
                "'$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$c2FsdA', false, :at, :at)"
            ),
            {"id": key_id, "at": datetime(2020, 1, 1, tzinfo=UTC)},
        )
        connection.execute(
            sa.text(
                "INSERT INTO core.api_keys "
                "(id, user_id, name, lookup_id, display_prefix, last4, pepper_key_id, "
                "secret_hmac, status, created_at) "
                "VALUES (:id, :id, 'M7-07', 'AAAAAAAAAAAAAAAAAAAAAAAAAA', 'holm_sk_', "
                "'AAAA', 'test', decode(repeat('00', 32), 'hex'), 'Active', :at)"
            ),
            {"id": key_id, "at": datetime(2020, 1, 1, tzinfo=UTC)},
        )
    later = datetime(2020, 2, 1, tzinfo=UTC)
    earlier = datetime(2020, 1, 15, tzinfo=UTC)
    with ephemeral_postgres.engine.begin() as connection:
        _as_role(connection, LEDGER)
        for value in (later, earlier):
            assert (
                connection.scalar(
                    sa.text("SELECT core.holm_update_api_key_last_used(:id, :at)"),
                    {"id": key_id, "at": value},
                )
                is True
            )
    with ephemeral_postgres.engine.begin() as connection:
        assert (
            connection.scalar(
                sa.text("SELECT last_used_at FROM core.api_keys WHERE id = :id"), {"id": key_id}
            )
            == later
        )


def test_M7_07_TC_DP_13_gateway_ledger_and_control_primary_writes(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    moment = datetime(2020, 3, 1, tzinfo=UTC)
    request_id = UUID("00000000-0000-7000-8000-000000007008")
    with pytest.raises(DBAPIError) as error, ephemeral_postgres.engine.begin() as connection:
        _as_role(connection, GATEWAY)
        connection.execute(
            sa.text(
                "INSERT INTO core.request_records "
                "(request_id, received_at, completed_at, stream, status, http_status, duration_ms) "
                "VALUES (:id, :at, :at, false, 'completed', 200, 1)"
            ),
            {"id": request_id, "at": moment},
        )
    assert getattr(error.value.orig, "sqlstate", None) == "42501"
    assert TABLE_GRANTS["core.request_records"] == {}
    with ephemeral_postgres.engine.begin() as connection:
        _as_role(connection, LEDGER)
        connection.execute(
            sa.text("SELECT core.holm_ensure_month_partition('usage.request_records', :at)"),
            {"at": moment.date()},
        )
        connection.execute(
            sa.text(
                "INSERT INTO usage.request_id_registry "
                "(request_id, received_at, highest_event_seq) VALUES (:id, :at, 1)"
            ),
            {"id": request_id, "at": moment},
        )
        connection.execute(
            sa.text(
                "INSERT INTO usage.request_records "
                "(received_at, request_id, event_seq, record_state, updated_at) "
                "VALUES (:at, :id, 1, 'received', :at)"
            ),
            {"id": request_id, "at": moment},
        )
    with ephemeral_postgres.engine.begin() as connection:
        _as_role(connection, CONTROL)
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        assert (
            connection.scalar(
                sa.text("SELECT count(*) FROM usage.request_records WHERE request_id = :id"),
                {"id": request_id},
            )
            == 1
        )
        connection.execute(
            sa.text(
                "SELECT core.holm_ensure_month_partition('telemetry.instance_metrics_1m', :at)"
            ),
            {"at": moment.date()},
        )
        connection.execute(
            sa.text(
                "INSERT INTO telemetry.instance_metrics_1m "
                "(bucket_at, instance_id, quality, telemetry_status) "
                "VALUES (:at, :id, 'full', 'available')"
            ),
            {"id": request_id, "at": moment},
        )
    # Domain Event uses GENERATED AS IDENTITY. A real INSERT proves the group
    # grants need no direct sequence privilege and still support each appender.
    for role, offset in ((CONTROL, 1), (DOWNLOAD, 2), (INSTANCE, 3)):
        with ephemeral_postgres.engine.begin() as connection:
            _as_role(connection, role)
            event_id = connection.scalar(
                sa.text(
                    "INSERT INTO core.domain_events "
                    "(aggregate_id, event, version, payload, occurred_at) "
                    "VALUES (:id, 'M7-07', 1, '{}'::jsonb, :at) RETURNING id"
                ),
                {
                    "id": UUID(f"00000000-0000-7000-8000-{offset:012d}"),
                    "at": moment,
                },
            )
            assert isinstance(event_id, int) and event_id > 0


def test_CLEAN_02_TC_DP_13_retained_core_table_has_exact_empty_runtime_grants(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    assert TABLE_GRANTS["core.request_records"] == {}
    with ephemeral_postgres.engine.connect() as connection:
        for role in RUNTIME_ROLES:
            privileges = {
                operation
                for operation in ("SELECT", "INSERT", "UPDATE", "DELETE")
                if connection.scalar(
                    sa.text(
                        "SELECT has_table_privilege(:role, 'core.request_records', :operation)"
                    ),
                    {"role": role, "operation": operation},
                )
            }
            assert privileges == set(), role
            assert not connection.scalar(
                sa.text("SELECT has_any_column_privilege(:role, 'core.request_records', 'INSERT')"),
                {"role": role},
            )

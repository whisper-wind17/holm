"""TC-DB-04: real PostgreSQL test transactions always roll back."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Final, Protocol

import pytest
from conftest import TEST_TRANSACTION_APPLICATION_NAME
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

ROLLED_BACK_TABLE: Final[str] = "tc_db_04_rolled_back_state"
EXCEPTION_TABLE: Final[str] = "tc_db_04_exception_state"
LOCK_CLASS_ID: Final[int] = 5_263_188
LOCK_OBJECT_ID: Final[int] = 1


class PostgresFixture(Protocol):
    @property
    def engine(self) -> Engine: ...


type TransactionFactory = Callable[[], AbstractContextManager[Connection]]


class _InjectedFailure(RuntimeError):
    pass


def _relation_exists(connection: Connection, relation_name: str) -> bool:
    return (
        connection.execute(
            text("SELECT to_regclass(:relation_name) IS NOT NULL"),
            {"relation_name": f"public.{relation_name}"},
        ).scalar_one()
        is True
    )


def test_TC_DB_04_01_writes_exist_only_inside_the_test_transaction(
    database_transaction: Connection,
) -> None:
    database_transaction.exec_driver_sql(
        f"CREATE TABLE public.{ROLLED_BACK_TABLE} (value integer NOT NULL)"
    )
    database_transaction.exec_driver_sql(
        f"INSERT INTO public.{ROLLED_BACK_TABLE} (value) VALUES (1)"
    )
    assert (
        database_transaction.exec_driver_sql(
            f"SELECT count(*) FROM public.{ROLLED_BACK_TABLE}"
        ).scalar_one()
        == 1
    )


def test_TC_DB_04_02_next_test_cannot_observe_the_previous_test_write(
    database_transaction: Connection,
) -> None:
    assert not _relation_exists(database_transaction, ROLLED_BACK_TABLE)


def test_TC_DB_04_03_exception_cleanup_releases_transaction_and_lock(
    ephemeral_postgres: PostgresFixture,
    database_transaction_factory: TransactionFactory,
) -> None:
    with pytest.raises(_InjectedFailure), database_transaction_factory() as connection:
        connection.exec_driver_sql(
            f"CREATE TABLE public.{EXCEPTION_TABLE} (value integer NOT NULL)"
        )
        connection.execute(
            text("SELECT pg_advisory_xact_lock(:class_id, :object_id)"),
            {"class_id": LOCK_CLASS_ID, "object_id": LOCK_OBJECT_ID},
        ).one()
        raise _InjectedFailure

    with ephemeral_postgres.engine.connect() as inspection:
        assert not _relation_exists(inspection, EXCEPTION_TABLE)
        lock_count = inspection.execute(
            text(
                """
                SELECT count(*)
                FROM pg_locks
                WHERE locktype = 'advisory'
                  AND classid = :class_id
                  AND objid = :object_id
                """
            ),
            {"class_id": LOCK_CLASS_ID, "object_id": LOCK_OBJECT_ID},
        ).scalar_one()
        transaction_session_count = inspection.execute(
            text(
                """
                SELECT count(*)
                FROM pg_stat_activity
                WHERE application_name = :application_name
                  AND state IN ('active', 'idle in transaction', 'idle in transaction (aborted)')
                """
            ),
            {"application_name": TEST_TRANSACTION_APPLICATION_NAME},
        ).scalar_one()
        assert lock_count == 0
        assert transaction_session_count == 0

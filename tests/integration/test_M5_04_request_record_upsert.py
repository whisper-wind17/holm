"""M5-04 real-PostgreSQL Request Record replay and Migration checks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from conftest import EphemeralPostgres
from holm_domain import ErrorCode, RequestFinishReason
from holm_ledger import (
    AbortedEvent,
    CompletedEvent,
    EventType,
    FailedEvent,
    InProgressEvent,
    LedgerEvent,
    ReceivedEvent,
    RejectedEvent,
    RequestOperation,
    RequestProtocol,
    UnknownEvent,
)
from holm_persistence import (
    RequestRecordLedgerRepository,
    RequestRecordUpsertOutcome,
    create_database_engine,
    create_session_factory,
    ensure_request_record_partitions,
)
from migration_test_support import ALEMBIC_CONFIG_PATH, head_revision, upgrade_head
from sqlalchemy.engine import URL

JANUARY: Final = datetime(2026, 1, 31, 23, 59, tzinfo=UTC)
FEBRUARY: Final = datetime(2026, 2, 1, 0, 1, tzinfo=UTC)
USER_ID: Final = UUID("75000000-0000-4000-8000-000000000001")
KEY_ID: Final = UUID("75000000-0000-4000-8000-000000000002")
SERVICE_ID: Final = UUID("75000000-0000-4000-8000-000000000003")
INSTANCE_ID: Final = UUID("75000000-0000-4000-8000-000000000004")


def _uuid(value: int) -> UUID:
    return UUID(f"76000000-0000-4000-8000-{value:012d}")


def _async_url(fixture: EphemeralPostgres) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username="ubuntu",
        host=str(fixture.socket_directory),
        port=fixture.port,
        database="postgres",
    )


@pytest.fixture(scope="module", autouse=True)
def _migrated_database(ephemeral_postgres: EphemeralPostgres) -> Iterator[None]:
    upgrade_head(ephemeral_postgres.engine)
    yield


@pytest.fixture(autouse=True)
def _empty_usage_tables(
    ephemeral_postgres: EphemeralPostgres,
    _migrated_database: None,
) -> Iterator[None]:
    del _migrated_database
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(sa.text("TRUNCATE usage.request_records, usage.request_id_registry"))
    yield
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(sa.text("TRUNCATE usage.request_records, usage.request_id_registry"))


def _received(request_id: UUID, received_at: datetime = JANUARY) -> ReceivedEvent:
    return ReceivedEvent(
        request_id=request_id,
        event_seq=1,
        created_at=received_at,
        received_at=received_at,
        protocol=RequestProtocol.OPENAI,
        operation=RequestOperation.CHAT_COMPLETIONS,
        endpoint="/v1/chat/completions",
        stream=True,
        input_bytes=128,
    )


def _in_progress(
    request_id: UUID,
    received_at: datetime = JANUARY,
    *,
    event_seq: int = 2,
) -> InProgressEvent:
    return InProgressEvent(
        request_id=request_id,
        event_seq=event_seq,
        created_at=received_at + timedelta(milliseconds=1),
        received_at=received_at,
        user_id=USER_ID,
        api_key_id=KEY_ID,
        service_id=SERVICE_ID,
        instance_id=INSTANCE_ID,
        requested_max_tokens=64,
    )


def _completed(request_id: UUID, received_at: datetime = JANUARY) -> CompletedEvent:
    return CompletedEvent(
        request_id=request_id,
        event_seq=3,
        created_at=received_at + timedelta(milliseconds=2),
        received_at=received_at,
        input_tokens=3,
        output_tokens=5,
        ttft_ms=1,
        duration_ms=2,
        finish_reason=RequestFinishReason.STOP,
        http_status=200,
    )


def _mixed_events() -> tuple[LedgerEvent, ...]:
    completed_id = _uuid(1)
    rejected_id = _uuid(2)
    failed_id = _uuid(3)
    aborted_id = _uuid(4)
    unknown_id = _uuid(5)
    return (
        _received(completed_id),
        _in_progress(completed_id),
        _completed(completed_id),
        _received(rejected_id),
        RejectedEvent(
            request_id=rejected_id,
            event_seq=2,
            created_at=JANUARY + timedelta(milliseconds=1),
            received_at=JANUARY,
            error_code=ErrorCode.INVALID_API_KEY,
            http_status=401,
        ),
        _received(failed_id),
        _in_progress(failed_id),
        FailedEvent(
            request_id=failed_id,
            event_seq=3,
            created_at=JANUARY + timedelta(milliseconds=2),
            received_at=JANUARY,
            input_tokens=2,
            output_tokens=1,
            duration_ms=3,
            error_code=ErrorCode.GATEWAY_OVERLOADED,
            http_status=503,
        ),
        _received(aborted_id),
        _in_progress(aborted_id),
        AbortedEvent(
            request_id=aborted_id,
            event_seq=3,
            created_at=JANUARY + timedelta(milliseconds=2),
            received_at=JANUARY,
            input_tokens=1,
            duration_ms=3,
            client_disconnected=True,
        ),
        _received(unknown_id, FEBRUARY),
        _in_progress(unknown_id, FEBRUARY),
        UnknownEvent(
            request_id=unknown_id,
            event_seq=3,
            created_at=FEBRUARY + timedelta(minutes=6),
            received_at=FEBRUARY,
            last_confirmed_event_type=EventType.IN_PROGRESS,
            last_confirmed_event_seq=2,
            input_tokens=1,
        ),
    )


def _snapshot(fixture: EphemeralPostgres) -> tuple[tuple[object, ...], tuple[object, ...]]:
    with fixture.engine.connect() as connection:
        registry = tuple(
            tuple(row)
            for row in connection.execute(
                sa.text(
                    "SELECT request_id, received_at, highest_event_seq "
                    "FROM usage.request_id_registry ORDER BY request_id"
                )
            ).tuples()
        )
        records = tuple(
            tuple(row)
            for row in connection.execute(
                sa.text("SELECT * FROM usage.request_records ORDER BY received_at, request_id")
            ).tuples()
        )
    return registry, records


async def _upsert(
    fixture: EphemeralPostgres,
    events: tuple[LedgerEvent, ...],
    *,
    commit: bool = True,
) -> tuple[RequestRecordUpsertOutcome, ...]:
    engine = create_database_engine(_async_url(fixture), connect_timeout_seconds=5)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            repository = RequestRecordLedgerRepository(session, engine)
            results = await repository.upsert_batch(events)
            if commit:
                await session.commit()
            else:
                await session.rollback()
        return tuple(result.outcome for result in results)
    finally:
        await engine.dispose()


def test_M5_04_TC_LG_01_mixed_replay_and_reshuffled_batches_are_idempotent(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    events = _mixed_events()
    first = asyncio.run(_upsert(ephemeral_postgres, events))
    assert first == (RequestRecordUpsertOutcome.APPLIED,) * len(events)
    expected = _snapshot(ephemeral_postgres)
    assert len(expected[0]) == 5
    assert len(expected[1]) == 5

    second = asyncio.run(_upsert(ephemeral_postgres, events))
    assert second == (RequestRecordUpsertOutcome.IGNORED,) * len(events)
    assert _snapshot(ephemeral_postgres) == expected

    reshuffled = tuple(reversed(events))
    split = len(reshuffled) // 2
    asyncio.run(_upsert(ephemeral_postgres, reshuffled[:split]))
    asyncio.run(_upsert(ephemeral_postgres, reshuffled[split:]))
    assert _snapshot(ephemeral_postgres) == expected
    assert "M5_04_BODY_SENTINEL" not in repr(expected)


def test_M5_04_event_specific_updates_preserve_received_and_identity_fields(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    request_id = _uuid(11)
    asyncio.run(
        _upsert(
            ephemeral_postgres,
            (_received(request_id), _in_progress(request_id), _completed(request_id)),
        )
    )
    with ephemeral_postgres.engine.connect() as connection:
        row = (
            connection.execute(
                sa.text(
                    "SELECT record_state, protocol, operation, endpoint, stream, input_bytes, "
                    "user_id, api_key_id, service_id, instance_id, requested_max_tokens, "
                    "input_tokens, output_tokens, ttft_ms, duration_ms, finish_reason "
                    "FROM usage.request_records WHERE request_id = :request_id"
                ),
                {"request_id": request_id},
            )
            .mappings()
            .one()
        )
    assert dict(row) == {
        "record_state": "completed",
        "protocol": "openai",
        "operation": "chat.completions",
        "endpoint": "/v1/chat/completions",
        "stream": True,
        "input_bytes": 128,
        "user_id": USER_ID,
        "api_key_id": KEY_ID,
        "service_id": SERVICE_ID,
        "instance_id": INSTANCE_ID,
        "requested_max_tokens": 64,
        "input_tokens": 3,
        "output_tokens": 5,
        "ttft_ms": 1,
        "duration_ms": 2,
        "finish_reason": "stop",
    }


def test_M5_04_TC_LG_06_stale_terminal_replay_is_ignored(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    request_id = _uuid(12)
    asyncio.run(
        _upsert(
            ephemeral_postgres,
            (_received(request_id), _in_progress(request_id), _completed(request_id)),
        )
    )
    before = _snapshot(ephemeral_postgres)
    outcome = asyncio.run(_upsert(ephemeral_postgres, (_in_progress(request_id, event_seq=3),)))
    assert outcome == (RequestRecordUpsertOutcome.IGNORED,)
    assert _snapshot(ephemeral_postgres) == before


def test_M5_04_TC_LG_07_received_at_conflict_is_logged_and_never_crosses_partition(
    ephemeral_postgres: EphemeralPostgres,
    caplog: pytest.LogCaptureFixture,
) -> None:
    request_id = _uuid(13)
    caplog.set_level(logging.CRITICAL, logger="holm.persistence.request_ledger")
    outcomes = asyncio.run(
        _upsert(
            ephemeral_postgres,
            (_received(request_id, JANUARY), _received(request_id, FEBRUARY)),
        )
    )
    assert outcomes == (
        RequestRecordUpsertOutcome.APPLIED,
        RequestRecordUpsertOutcome.CONFLICT,
    )
    with ephemeral_postgres.engine.connect() as connection:
        registry = connection.execute(
            sa.text(
                "SELECT received_at, highest_event_seq FROM usage.request_id_registry "
                "WHERE request_id = :request_id"
            ),
            {"request_id": request_id},
        ).one()
        records = tuple(
            tuple(row)
            for row in connection.execute(
                sa.text(
                    "SELECT received_at, tableoid::regclass::text FROM usage.request_records "
                    "WHERE request_id = :request_id"
                ),
                {"request_id": request_id},
            ).tuples()
        )
    assert registry == (JANUARY, 1)
    assert records == ((JANUARY, "usage.request_records_202601"),)
    assert len(caplog.records) == 1
    record = caplog.records[0]
    log_fields = vars(record)
    assert record.levelno == logging.CRITICAL
    assert log_fields["request_id"] == str(request_id)
    assert log_fields["event_seq"] == 1
    assert log_fields["event_type"] == EventType.RECEIVED.value
    assert "M5_04_BODY_SENTINEL" not in record.getMessage()


def test_M5_04_month_partitions_are_created_in_independent_repeatable_transaction(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> tuple[tuple[str, ...], tuple[str, ...]]:
        engine = create_database_engine(_async_url(ephemeral_postgres), connect_timeout_seconds=5)
        try:
            first = await ensure_request_record_partitions(engine, (JANUARY, FEBRUARY))
            second = await ensure_request_record_partitions(engine, (FEBRUARY, JANUARY))
            return first, second
        finally:
            await engine.dispose()

    first, second = asyncio.run(scenario())
    assert first == second == ("request_records_202601", "request_records_202602")
    with ephemeral_postgres.engine.connect() as connection:
        partitions = tuple(
            connection.execute(
                sa.text(
                    "SELECT child.relname FROM pg_inherits "
                    "JOIN pg_class AS parent ON parent.oid = inhparent "
                    "JOIN pg_class AS child ON child.oid = inhrelid "
                    "JOIN pg_namespace AS namespace ON namespace.oid = child.relnamespace "
                    "WHERE namespace.nspname = 'usage' "
                    "AND parent.relname = 'request_records' ORDER BY child.relname"
                )
            ).scalars()
        )
    assert partitions == ("request_records_202601", "request_records_202602")


def test_M5_04_repository_never_commits_the_caller_session(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    outcome = asyncio.run(_upsert(ephemeral_postgres, (_received(_uuid(14)),), commit=False))
    assert outcome == (RequestRecordUpsertOutcome.APPLIED,)
    assert _snapshot(ephemeral_postgres) == ((), ())


def test_M5_04_migration_parent_checks_no_foreign_keys_and_one_step_cycle(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    with ephemeral_postgres.engine.connect() as connection:
        row = connection.execute(
            sa.text(
                "SELECT relation.relkind, pg_get_partkeydef(relation.oid) "
                "FROM pg_class AS relation "
                "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
                "WHERE namespace.nspname = 'usage' AND relation.relname = 'request_records'"
            )
        ).one()
        constraint_types = tuple(
            connection.execute(
                sa.text(
                    "SELECT contype FROM pg_constraint AS constraint_record "
                    "JOIN pg_class AS relation ON relation.oid = constraint_record.conrelid "
                    "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
                    "WHERE namespace.nspname = 'usage' "
                    "AND relation.relname IN ('request_records', 'request_id_registry') "
                    "ORDER BY contype"
                )
            ).scalars()
        )
    assert row == ("p", "RANGE (received_at)")
    assert "f" not in constraint_types
    assert "c" in constraint_types
    assert "u" in constraint_types

    with ephemeral_postgres.engine.begin() as connection:
        config = Config(ALEMBIC_CONFIG_PATH, attributes={"connection": connection})
        command.downgrade(config, "20260917_0017")
        assert (
            connection.execute(sa.text("SELECT to_regclass('usage.request_records')")).scalar()
            is None
        )
        assert (
            connection.execute(sa.text("SELECT to_regclass('usage.request_id_registry')")).scalar()
            is None
        )
        assert connection.execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'usage')")
        ).scalar_one()
        command.upgrade(config, "head")
        assert (
            connection.execute(sa.text("SELECT to_regclass('usage.request_records')")).scalar()
            is not None
        )
        assert (
            connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
            == head_revision()
        )

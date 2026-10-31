"""M5-08 idempotent Usage buckets and bucketed summary queries."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from holm_control_api import usage_service as usage_service_module
from holm_control_api.usage_service import UsageQueryService
from holm_domain import (
    MeteringSource,
    MetricsCapability,
    RequestRecordState,
    UserRole,
)
from holm_ledger import EventType
from holm_persistence import (
    LEDGER_REQUEST_RECORDS,
    USAGE_15M,
    UsageFilters,
    UsageRepository,
    UsageTotals,
    create_database_engine,
    create_session_factory,
)
from migration_test_support import upgrade_head
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError

NOW: Final[datetime] = datetime(2026, 9, 18, 12, 7, tzinfo=UTC)
USER_ID: Final[UUID] = UUID("58000000-0000-4000-8000-000000000011")
KEY_ID: Final[UUID] = UUID("58000000-0000-4000-8000-000000000012")
SERVICE_ID: Final[UUID] = UUID("58000000-0000-4000-8000-000000000013")


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
    statement = sa.text(
        "TRUNCATE usage.usage_15m, usage.request_records, usage.request_id_registry CASCADE"
    )
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(statement)
        connection.execute(
            sa.text(
                "CREATE TABLE IF NOT EXISTS usage.request_records_202609 "
                "PARTITION OF usage.request_records "
                "FOR VALUES FROM ('2026-09-01T00:00:00+00') "
                "TO ('2026-10-01T00:00:00+00')"
            )
        )
    yield
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(statement)


def _record(
    value: int,
    *,
    at: datetime,
    state: RequestRecordState,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    capability: MetricsCapability | None = None,
    metering: MeteringSource | None = None,
    last_confirmed: EventType | None = None,
    dimensions: bool = True,
) -> dict[str, object | None]:
    terminal_at = None
    if state not in (RequestRecordState.RECEIVED, RequestRecordState.IN_PROGRESS):
        terminal_at = at + timedelta(seconds=1)
    return {
        "received_at": at,
        "request_id": UUID(f"58000000-0000-4000-8001-{value:012d}"),
        "event_seq": 3,
        "record_state": state.value,
        "user_id": USER_ID if dimensions else None,
        "api_key_id": KEY_ID if dimensions else None,
        "service_id": SERVICE_ID if dimensions else None,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "metrics_capability": None if capability is None else capability.value,
        "metering_source": None if metering is None else metering.value,
        "last_confirmed_event_type": (None if last_confirmed is None else last_confirmed.value),
        "terminal_at": terminal_at,
        "updated_at": terminal_at or at,
    }


def _insert_records(
    fixture: EphemeralPostgres,
    records: list[dict[str, object | None]],
) -> None:
    with fixture.engine.begin() as connection:
        connection.execute(sa.insert(LEDGER_REQUEST_RECORDS), records)


def _bucket_rows(fixture: EphemeralPostgres) -> list[tuple[object, ...]]:
    with fixture.engine.connect() as connection:
        return list(
            connection.execute(sa.select(USAGE_15M).order_by(USAGE_15M.c.bucket_start)).tuples()
        )


def test_M5_08_migration_creates_only_sparse_metadata_metrics(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    with ephemeral_postgres.engine.connect() as connection:
        columns = {
            str(row.column_name): str(row.is_nullable)
            for row in connection.execute(
                sa.text(
                    "SELECT column_name, is_nullable FROM information_schema.columns "
                    "WHERE table_schema = 'usage' AND table_name = 'usage_15m'"
                )
            )
        }
    assert columns == {
        "bucket_start": "NO",
        "user_id": "NO",
        "api_key_id": "NO",
        "service_id": "NO",
        "requests": "NO",
        "input_tokens": "YES",
        "output_tokens": "YES",
        "error_requests": "NO",
        "aborted_requests": "NO",
        "partial_token_requests": "NO",
        "updated_at": "NO",
    }
    assert "total_tokens" not in columns
    assert not {"messages", "prompt", "response", "body"}.intersection(columns)


def test_M5_08_migration_rejects_misaligned_or_negative_buckets(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    base_values = {
        "bucket_start": NOW.replace(minute=0),
        "user_id": USER_ID,
        "api_key_id": KEY_ID,
        "service_id": SERVICE_ID,
        "requests": 1,
        "input_tokens": None,
        "output_tokens": None,
        "error_requests": 0,
        "aborted_requests": 0,
        "partial_token_requests": 0,
        "updated_at": NOW,
    }
    invalid_values = (
        {**base_values, "bucket_start": NOW.replace(minute=1)},
        {**base_values, "requests": -1},
    )
    for values in invalid_values:
        with ephemeral_postgres.engine.connect() as connection:
            transaction = connection.begin()
            with pytest.raises(IntegrityError):
                connection.execute(sa.insert(USAGE_15M).values(**values))
            transaction.rollback()


def test_M5_08_TC_LG_10_recompute_is_exact_and_idempotent(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    previous = NOW.replace(minute=45, second=0, microsecond=0) - timedelta(hours=1)
    current = NOW.replace(minute=0, second=0, microsecond=0)
    records = [
        _record(
            1, at=previous, state=RequestRecordState.COMPLETED, input_tokens=10, output_tokens=5
        ),
        _record(
            2,
            at=previous + timedelta(minutes=1),
            state=RequestRecordState.FAILED,
            output_tokens=2,
            capability=MetricsCapability.PARTIAL,
        ),
        _record(
            3,
            at=previous + timedelta(minutes=2),
            state=RequestRecordState.ABORTED,
            input_tokens=3,
            capability=MetricsCapability.PARTIAL,
        ),
        _record(
            4,
            at=previous + timedelta(minutes=3),
            state=RequestRecordState.UNKNOWN,
            input_tokens=4,
            output_tokens=1,
            last_confirmed=EventType.IN_PROGRESS,
        ),
        _record(
            5,
            at=previous + timedelta(minutes=4),
            state=RequestRecordState.UNKNOWN,
            input_tokens=99,
            output_tokens=99,
            last_confirmed=EventType.RECEIVED,
        ),
        _record(
            6,
            at=previous + timedelta(minutes=5),
            state=RequestRecordState.REJECTED,
            input_tokens=99,
            output_tokens=99,
        ),
        _record(
            7,
            at=previous + timedelta(minutes=6),
            state=RequestRecordState.IN_PROGRESS,
        ),
        _record(
            8,
            at=previous + timedelta(minutes=7),
            state=RequestRecordState.COMPLETED,
            input_tokens=99,
            output_tokens=99,
            dimensions=False,
        ),
        _record(
            9,
            at=current + timedelta(minutes=1),
            state=RequestRecordState.COMPLETED,
            input_tokens=1,
            output_tokens=1,
        ),
    ]
    _insert_records(ephemeral_postgres, records)

    async def recompute() -> int:
        engine = create_database_engine(_async_url(ephemeral_postgres))
        try:
            factory = create_session_factory(engine)
            async with factory() as session, session.begin():
                return await UsageRepository(session).recompute_recent_buckets(now=NOW)
        finally:
            await engine.dispose()

    assert asyncio.run(recompute()) == 2
    first = _bucket_rows(ephemeral_postgres)
    assert len(first) == 2
    previous_row = first[0]
    assert previous_row[4:10] == (5, 17, 8, 1, 1, 2)

    assert asyncio.run(recompute()) == 2
    assert _bucket_rows(ephemeral_postgres) == first

    _insert_records(
        ephemeral_postgres,
        [
            _record(
                10,
                at=current + timedelta(minutes=2),
                state=RequestRecordState.COMPLETED,
                input_tokens=2,
                output_tokens=3,
            )
        ],
    )
    assert asyncio.run(recompute()) == 2
    changed = _bucket_rows(ephemeral_postgres)
    assert changed[0] == first[0]
    assert changed[1][4:7] == (2, 3, 4)


def test_M5_08_empty_database_aggregation_is_sparse(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> tuple[int, int]:
        engine = create_database_engine(_async_url(ephemeral_postgres))
        try:
            factory = create_session_factory(engine)
            async with factory() as session, session.begin():
                processed = await UsageRepository(session).recompute_recent_buckets(now=NOW)
            async with factory() as session:
                count = int(await session.scalar(sa.select(sa.func.count()).select_from(USAGE_15M)))
            return processed, count
        finally:
            await engine.dispose()

    assert asyncio.run(scenario()) == (2, 0)


def test_M5_08_bucketed_window_adds_right_partial_bucket_without_history_fallback(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    bucket_start = datetime(2026, 9, 18, 11, 45, tzinfo=UTC)
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(
            sa.insert(USAGE_15M).values(
                bucket_start=bucket_start,
                user_id=USER_ID,
                api_key_id=KEY_ID,
                service_id=SERVICE_ID,
                requests=1,
                input_tokens=2,
                output_tokens=None,
                error_requests=0,
                aborted_requests=0,
                partial_token_requests=0,
                updated_at=NOW,
            )
        )
    _insert_records(
        ephemeral_postgres,
        [
            _record(
                20,
                at=datetime(2026, 9, 18, 12, 3, tzinfo=UTC),
                state=RequestRecordState.COMPLETED,
                input_tokens=None,
                output_tokens=4,
                capability=MetricsCapability.PARTIAL,
            ),
            _record(
                21,
                at=datetime(2026, 9, 18, 11, 20, tzinfo=UTC),
                state=RequestRecordState.COMPLETED,
                input_tokens=99,
                output_tokens=99,
            ),
        ],
    )

    async def scenario() -> tuple[UsageTotals, UsageTotals]:
        engine = create_database_engine(_async_url(ephemeral_postgres))
        try:
            factory = create_session_factory(engine)
            async with factory() as session:
                repository = UsageRepository(session)
                combined = await repository.summarize_bucketed_window(
                    UsageFilters(
                        from_time=bucket_start,
                        to_time=datetime(2026, 9, 18, 12, 7, tzinfo=UTC),
                    ),
                    now=datetime(2026, 9, 18, 12, 40, tzinfo=UTC),
                )
                missing = await repository.summarize_bucketed_window(
                    UsageFilters(
                        from_time=datetime(2026, 9, 18, 11, 15, tzinfo=UTC),
                        to_time=datetime(2026, 9, 18, 11, 30, tzinfo=UTC),
                    ),
                    now=datetime(2026, 9, 18, 12, 40, tzinfo=UTC),
                )
                return combined, missing
        finally:
            await engine.dispose()

    combined, missing = asyncio.run(scenario())
    assert (
        combined.requests,
        combined.input_tokens,
        combined.output_tokens,
        combined.total_tokens,
        combined.partial_token_requests,
    ) == (2, 2, 4, 6, 1)
    assert missing.requests == 0
    assert missing.input_tokens is None
    assert missing.output_tokens is None


def test_M5_08_timezone_queries_change_boundaries_without_mutating_buckets(
    ephemeral_postgres: EphemeralPostgres,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 9, 18, 0, 7, tzinfo=UTC)
    _insert_records(
        ephemeral_postgres,
        [
            _record(
                30,
                at=datetime(2026, 9, 17, 23, 50, tzinfo=UTC),
                state=RequestRecordState.COMPLETED,
                input_tokens=1,
                output_tokens=1,
            ),
            _record(
                31,
                at=datetime(2026, 9, 18, 0, 3, tzinfo=UTC),
                state=RequestRecordState.COMPLETED,
                input_tokens=2,
                output_tokens=2,
            ),
        ],
    )

    async def fixed_database_now(session: object) -> datetime:
        del session
        return now

    monkeypatch.setattr(usage_service_module, "database_utc_now", fixed_database_now)

    async def scenario() -> tuple[int, int]:
        engine = create_database_engine(_async_url(ephemeral_postgres))
        try:
            factory = create_session_factory(engine)
            async with factory() as session, session.begin():
                await UsageRepository(session).recompute_recent_buckets(now=now)
            before = _bucket_rows(ephemeral_postgres)
            service = UsageQueryService(session_factory=factory)
            utc = await service.summary(
                time_zone="UTC",
                actor_id=USER_ID,
                actor_role=UserRole.ADMIN,
            )
            shanghai = await service.summary(
                time_zone="Asia/Shanghai",
                actor_id=USER_ID,
                actor_role=UserRole.ADMIN,
            )
            assert _bucket_rows(ephemeral_postgres) == before
            return utc.today.requests, shanghai.today.requests
        finally:
            await engine.dispose()

    assert asyncio.run(scenario()) == (1, 2)


def test_M5_08_unavailable_metering_counts_requests_without_zero_filled_tokens(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    """NULL Tokens never become zeroes in a bucket or a total."""

    current = NOW.replace(minute=0, second=0, microsecond=0)
    _insert_records(
        ephemeral_postgres,
        [
            _record(
                40,
                at=current + timedelta(minutes=1),
                state=RequestRecordState.COMPLETED,
                input_tokens=None,
                output_tokens=None,
                metering=MeteringSource.UNAVAILABLE,
            ),
            _record(
                41,
                at=current + timedelta(minutes=2),
                state=RequestRecordState.COMPLETED,
                input_tokens=None,
                output_tokens=None,
                metering=MeteringSource.UNAVAILABLE,
            ),
            _record(
                42,
                at=current + timedelta(minutes=3),
                state=RequestRecordState.COMPLETED,
                input_tokens=10,
                output_tokens=4,
                metering=MeteringSource.RUNTIME_USAGE,
            ),
        ],
    )

    async def scenario() -> UsageTotals:
        engine = create_database_engine(_async_url(ephemeral_postgres))
        try:
            factory = create_session_factory(engine)
            async with factory() as session, session.begin():
                await UsageRepository(session).recompute_recent_buckets(now=NOW)
            async with factory() as session:
                return await UsageRepository(session).summarize_bucketed_window(
                    UsageFilters(from_time=current, to_time=NOW),
                    now=NOW + timedelta(minutes=30),
                )
        finally:
            await engine.dispose()

    totals = asyncio.run(scenario())
    rows = _bucket_rows(ephemeral_postgres)
    assert len(rows) == 1
    # requests, input_tokens, output_tokens - three requests, one confirmed Token pair.
    assert rows[0][4:7] == (3, 10, 4)
    assert (totals.requests, totals.input_tokens, totals.output_tokens) == (3, 10, 4)

    async def only_unavailable() -> UsageTotals:
        engine = create_database_engine(_async_url(ephemeral_postgres))
        try:
            factory = create_session_factory(engine)
            async with factory() as session:
                return await UsageRepository(session).summarize_bucketed_window(
                    UsageFilters(
                        from_time=current,
                        to_time=current + timedelta(minutes=3),
                    ),
                    now=NOW + timedelta(minutes=30),
                )
        finally:
            await engine.dispose()

    # The right-hand partial bucket is aggregated live: two Unavailable requests only.
    partial = asyncio.run(only_unavailable())
    assert partial.requests == 2
    assert partial.input_tokens is None
    assert partial.output_tokens is None
    assert partial.total_tokens is None


def test_M5_08_open_bucket_is_live_before_any_bucket_row_exists(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    """a request in the current 15-minute bucket is visible immediately."""

    day_start = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    _insert_records(
        ephemeral_postgres,
        [
            _record(
                50,
                at=NOW - timedelta(minutes=1),
                state=RequestRecordState.COMPLETED,
                input_tokens=7,
                output_tokens=3,
            )
        ],
    )

    async def scenario() -> UsageTotals:
        engine = create_database_engine(_async_url(ephemeral_postgres))
        try:
            factory = create_session_factory(engine)
            async with factory() as session:
                return await UsageRepository(session).summarize_bucketed_window(
                    UsageFilters(from_time=day_start, to_time=day_start + timedelta(days=1)),
                    now=NOW,
                )
        finally:
            await engine.dispose()

    totals = asyncio.run(scenario())
    assert _bucket_rows(ephemeral_postgres) == []
    assert (totals.requests, totals.input_tokens, totals.output_tokens) == (1, 7, 3)

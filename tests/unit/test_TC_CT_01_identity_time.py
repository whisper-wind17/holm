"""TC-CT-01 UUIDv7 and UTC contract tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from holm_common.ids import UUID7Generator, uuid7_unix_ms
from holm_common.time import (
    UTCDateTimeError,
    as_utc,
    format_rfc3339_utc,
    parse_rfc3339_utc,
    require_utc,
    utc_now,
)


def test_TC_CT_01_uuid7_is_valid_unique_and_monotonic_across_clock_rollback() -> None:
    timestamp = 1_787_800_000_123
    clock_values = iter((timestamp, timestamp, timestamp - 10, timestamp + 1))
    generator = UUID7Generator(
        clock_milliseconds=lambda: next(clock_values),
        random_bits=lambda _: 0,
    )

    values = [generator.new() for _ in range(4)]

    assert len(set(values)) == len(values)
    assert values == sorted(values, key=lambda value: value.int)
    assert all(value.version == 7 for value in values)
    assert all(value.variant == uuid.RFC_4122 for value in values)
    assert [uuid7_unix_ms(value) for value in values] == [
        timestamp,
        timestamp,
        timestamp,
        timestamp + 1,
    ]


def test_TC_CT_01_utc_normalization_requirement_and_rfc3339_round_trip() -> None:
    local = datetime(2026, 8, 27, 10, 30, 45, 123456, tzinfo=timezone(timedelta(hours=8)))
    expected = datetime(2026, 8, 27, 2, 30, 45, 123456, tzinfo=UTC)

    assert as_utc(local) == expected
    assert require_utc(expected) == expected
    rendered = format_rfc3339_utc(local)
    assert rendered == "2026-08-27T02:30:45.123456Z"
    assert parse_rfc3339_utc(rendered) == expected
    assert parse_rfc3339_utc("2026-08-27T02:30:45.123456+00:00") == expected
    assert utc_now().utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "operation,value",
    [
        (as_utc, datetime(2026, 8, 27, 2, 30, 45)),
        (require_utc, datetime(2026, 8, 27, 10, 30, 45, tzinfo=timezone(timedelta(hours=8)))),
        (parse_rfc3339_utc, "2026-08-27T10:30:45+08:00"),
        (parse_rfc3339_utc, "2026-08-27 02:30:45Z"),
        (parse_rfc3339_utc, "not-a-time"),
    ],
)
def test_TC_CT_01_rejects_non_utc_or_naive_time(operation: object, value: datetime | str) -> None:
    assert callable(operation)
    with pytest.raises(UTCDateTimeError):
        operation(value)  # type: ignore[operator]

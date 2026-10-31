"""M5-08 UTC bucket and ledger identity contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from holm_ledger import InProgressEvent
from holm_ledger_writer.__main__ import _parse_args
from holm_ledger_writer.writer import LedgerWriter
from holm_persistence import usage_bucket_start
from pydantic import ValidationError


def test_M5_08_bucket_floor_is_utc_and_fifteen_minute_aligned() -> None:
    value = datetime.fromisoformat("2026-09-18T14:29:59.999999+08:00")
    assert usage_bucket_start(value) == datetime(2026, 9, 18, 6, 15, tzinfo=UTC)


def test_M5_08_bucket_floor_rejects_naive_time() -> None:
    with pytest.raises(ValueError):
        usage_bucket_start(datetime(2026, 9, 18, 6, 15))


def test_M5_08_ledger_writer_cli_defaults_usage_aggregation_to_thirty_seconds() -> None:
    args = _parse_args(
        [
            "--database-url-file",
            "/run/secrets/database-url",
            "--request-ledger-path",
            "/var/lib/holm/request-ledger.sqlite3",
        ]
    )
    assert args.database_url_file == Path("/run/secrets/database-url")
    assert args.usage_aggregate_seconds == 30.0


def test_M5_08_ledger_writer_rejects_non_positive_usage_interval() -> None:
    with pytest.raises(ValueError, match="Usage aggregate interval"):
        LedgerWriter(
            ledger=cast(Any, object()),
            session_factory=cast(Any, object()),
            engine=cast(Any, object()),
            usage_aggregate_seconds=0,
        )


def test_M5_08_in_progress_requires_all_three_usage_dimensions() -> None:
    common = {
        "request_id": UUID("58000000-0000-4000-8000-000000000001"),
        "event_seq": 2,
        "created_at": datetime(2026, 9, 18, 6, 0, 1, tzinfo=UTC),
        "received_at": datetime(2026, 9, 18, 6, 0, tzinfo=UTC),
        "instance_id": UUID("58000000-0000-4000-8000-000000000005"),
        "requested_max_tokens": 16,
    }
    with pytest.raises(ValidationError):
        InProgressEvent.model_validate(common)

    event = InProgressEvent.model_validate(
        {
            **common,
            "user_id": UUID("58000000-0000-4000-8000-000000000002"),
            "api_key_id": UUID("58000000-0000-4000-8000-000000000003"),
            "service_id": UUID("58000000-0000-4000-8000-000000000004"),
        }
    )
    assert event.user_id is not None
    assert event.api_key_id is not None
    assert event.service_id is not None

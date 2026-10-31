"""TC-CT-04 framework-independent Domain Event tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest
from holm_domain.events import DomainEvent

AGGREGATE_ID = UUID("018f47f2-8c00-7000-8000-000000000001")
OCCURRED_AT = datetime(2026, 8, 27, 2, 30, 45, 123456, tzinfo=UTC)


def test_TC_CT_04_domain_event_copies_and_freezes_json_payload() -> None:
    source: dict[str, object] = {
        "stage": "loading_weights",
        "progress": 0.72,
        "parts": [1, {"ready": True}],
    }
    event = DomainEvent(
        id=12,
        event="instance.progress",
        aggregate_id=AGGREGATE_ID,
        version=4,
        occurred_at=OCCURRED_AT,
        payload=source,
    )
    source["stage"] = "mutated"
    parts = source["parts"]
    assert isinstance(parts, list)
    parts.append("mutated")

    assert event.payload_json() == {
        "stage": "loading_weights",
        "progress": 0.72,
        "parts": [1, {"ready": True}],
    }
    with pytest.raises(TypeError):
        event.payload["stage"] = "forbidden"  # type: ignore[index]


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": 0},
        {"id": True},
        {"version": 0},
        {"version": True},
        {"event": ""},
        {"aggregate_id": "not-a-uuid"},
        {"occurred_at": datetime(2026, 8, 27, 2, 30, 45)},
        {"occurred_at": datetime(2026, 8, 27, 10, 30, 45, tzinfo=timezone(timedelta(hours=8)))},
        {"payload": {"invalid": object()}},
        {"payload": {"invalid": float("nan")}},
    ],
)
def test_TC_CT_04_domain_event_rejects_invalid_boundaries(
    overrides: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "id": 12,
        "event": "instance.progress",
        "aggregate_id": AGGREGATE_ID,
        "version": 4,
        "occurred_at": OCCURRED_AT,
        "payload": {},
    }
    values.update(overrides)

    with pytest.raises(ValueError):
        DomainEvent(**values)  # type: ignore[arg-type]

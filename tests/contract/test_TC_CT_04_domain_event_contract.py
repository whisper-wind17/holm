"""TC-CT-04 serialized Domain Event contract tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from holm_contracts.events import DomainEventEnvelope
from holm_domain.events import DomainEvent


def test_TC_CT_04_domain_event_envelope_has_fixed_fields_and_utc_text() -> None:
    event = DomainEvent(
        id=42,
        event="instance.progress",
        aggregate_id=UUID("018f47f2-8c00-7000-8000-000000000003"),
        version=12,
        occurred_at=datetime(2026, 8, 27, 2, 30, 45, 123456, tzinfo=UTC),
        payload={"stage": "loading_weights", "progress": 0.72},
    )

    serialized = DomainEventEnvelope.from_domain(event).model_dump(mode="json")

    assert serialized == {
        "id": 42,
        "event": "instance.progress",
        "aggregate_id": "018f47f2-8c00-7000-8000-000000000003",
        "version": 12,
        "occurred_at": "2026-08-27T02:30:45.123456Z",
        "payload": {"stage": "loading_weights", "progress": 0.72},
    }

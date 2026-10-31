"""Structured, body-free lifecycle events for Model Instances."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, Literal

import sqlalchemy as sa
from holm_domain.model_instances import ModelInstanceEventType, ModelInstanceStatus
from sqlalchemy.dialects.postgresql import JSONB

from holm_persistence.accounts import METADATA, _values_clause
from holm_persistence.schemas import CORE_SCHEMA

MODEL_INSTANCE_EVENTS_TABLE_NAME: Final[str] = "model_instance_events"
MODEL_INSTANCE_EVENT_ACTORS: Final[tuple[str, ...]] = ("admin", "controller", "system")

type ModelInstanceEventActor = Literal["admin", "controller", "system"]


class UnsafeModelInstanceEventDetail(ValueError):
    """An event detail was not produced by the empty default-deny allowlist."""


def safe_model_instance_event_detail(detail: Mapping[str, object]) -> dict[str, object]:
    """Copy the current empty allowlist, rejecting rather than redacting body fields."""

    if detail:
        raise UnsafeModelInstanceEventDetail("model instance event detail is not allowlisted")
    return {}


MODEL_INSTANCE_EVENTS: Final[sa.Table] = sa.Table(
    MODEL_INSTANCE_EVENTS_TABLE_NAME,
    METADATA,
    sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
    # Cancellation deletes the instance configuration.  Keeping this historical
    # identifier without a foreign key preserves the cancellation event itself.
    sa.Column("instance_id", sa.Uuid(as_uuid=True), nullable=False),
    sa.Column("event_type", sa.Text(), nullable=False),
    sa.Column("from_status", sa.Text(), nullable=True),
    sa.Column("to_status", sa.Text(), nullable=True),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("actor", sa.Text(), nullable=False),
    sa.Column(
        "detail",
        JSONB,
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    ),
    sa.CheckConstraint(_values_clause("event_type", ModelInstanceEventType), name="event_type"),
    sa.CheckConstraint(
        f"actor IN ({', '.join(repr(value) for value in MODEL_INSTANCE_EVENT_ACTORS)})",
        name="actor",
    ),
    sa.CheckConstraint(
        f"from_status IS NULL OR {_values_clause('from_status', ModelInstanceStatus)}",
        name="from_status",
    ),
    sa.CheckConstraint(
        f"to_status IS NULL OR {_values_clause('to_status', ModelInstanceStatus)}",
        name="to_status",
    ),
    sa.CheckConstraint("detail = '{}'::jsonb", name="detail_allowlist"),
    schema=CORE_SCHEMA,
)

sa.Index(
    "ix_model_instance_events_instance_occurred",
    MODEL_INSTANCE_EVENTS.c.instance_id,
    MODEL_INSTANCE_EVENTS.c.occurred_at.desc(),
    MODEL_INSTANCE_EVENTS.c.id.desc(),
)


__all__ = [
    "MODEL_INSTANCE_EVENTS",
    "MODEL_INSTANCE_EVENTS_TABLE_NAME",
    "MODEL_INSTANCE_EVENT_ACTORS",
    "ModelInstanceEventActor",
    "UnsafeModelInstanceEventDetail",
    "safe_model_instance_event_detail",
]

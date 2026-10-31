"""SQLAlchemy definition for ordered, replayable ``core.domain_events``."""

from __future__ import annotations

from typing import Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from holm_persistence.accounts import METADATA
from holm_persistence.schemas import CORE_SCHEMA

DOMAIN_EVENTS_TABLE_NAME: Final[str] = "domain_events"
DOMAIN_EVENT_NOTIFY_CHANNEL: Final[str] = "holm_domain_events"
DOMAIN_EVENT_LOCK_CLASS_ID: Final[int] = 1_347_895_109
DOMAIN_EVENT_LOCK_OBJECT_ID: Final[int] = 1_163_282_004

_DOMAIN_EVENTS: Final[sa.Table] = sa.Table(
    DOMAIN_EVENTS_TABLE_NAME,
    METADATA,
    sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
    sa.Column(
        "xact_id",
        sa.BigInteger(),
        nullable=False,
        server_default=sa.text("(pg_current_xact_id()::text::bigint)"),
    ),
    sa.Column("aggregate_id", sa.Uuid(as_uuid=True), nullable=False),
    sa.Column("event", sa.Text(), nullable=False),
    sa.Column("version", sa.Integer(), nullable=False),
    sa.Column("payload", JSONB(), nullable=False),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("aggregate_id", "version"),
    sa.CheckConstraint("char_length(event) BETWEEN 1 AND 128", name="event_length"),
    sa.CheckConstraint("version > 0", name="version_positive"),
    sa.CheckConstraint("xact_id > 0", name="xact_id_positive"),
    sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_object"),
    sa.Index("ix_domain_events_occurred_at", "occurred_at"),
    schema=CORE_SCHEMA,
)

__all__ = [
    "DOMAIN_EVENTS_TABLE_NAME",
    "DOMAIN_EVENT_LOCK_CLASS_ID",
    "DOMAIN_EVENT_LOCK_OBJECT_ID",
    "DOMAIN_EVENT_NOTIFY_CHANNEL",
]

"""SQLAlchemy definition for the long-retained ``core.audit_events`` table."""

from __future__ import annotations

from typing import Final

import sqlalchemy as sa
from holm_domain import AuditAction, AuditActorType, AuditObjectType, AuditResult
from sqlalchemy.dialects.postgresql import JSONB

from holm_persistence.accounts import METADATA, _values_clause
from holm_persistence.schemas import CORE_SCHEMA

AUDIT_EVENTS_TABLE_NAME: Final[str] = "audit_events"

AUDIT_EVENTS: Final[sa.Table] = sa.Table(
    AUDIT_EVENTS_TABLE_NAME,
    METADATA,
    sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
    sa.Column("actor_type", sa.Text(), nullable=False),
    sa.Column("actor_id", sa.Uuid(as_uuid=True), nullable=True),
    sa.Column("action", sa.Text(), nullable=False),
    sa.Column("object_type", sa.Text(), nullable=False),
    sa.Column("object_id", sa.Uuid(as_uuid=True), nullable=True),
    sa.Column("result", sa.Text(), nullable=False),
    sa.Column("request_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
    sa.Column("details", JSONB(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
    sa.CheckConstraint(_values_clause("actor_type", AuditActorType), name="actor_type"),
    sa.CheckConstraint(_values_clause("action", AuditAction), name="action"),
    sa.CheckConstraint(_values_clause("object_type", AuditObjectType), name="object_type"),
    sa.CheckConstraint(_values_clause("result", AuditResult), name="result"),
    sa.CheckConstraint(
        "(actor_type = 'user') = (actor_id IS NOT NULL)",
        name="user_actor_has_id",
    ),
    sa.CheckConstraint("jsonb_typeof(details) = 'object'", name="details_object"),
    sa.Index("ix_audit_events_actor_id_created_at", "actor_id", "created_at"),
    sa.Index(
        "ix_audit_events_object_type_object_id_created_at",
        "object_type",
        "object_id",
        "created_at",
    ),
    schema=CORE_SCHEMA,
)


__all__ = ["AUDIT_EVENTS", "AUDIT_EVENTS_TABLE_NAME"]

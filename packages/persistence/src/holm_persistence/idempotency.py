"""Reusable HTTP idempotency records."""

from __future__ import annotations

from typing import Final

import sqlalchemy as sa

from holm_persistence.accounts import METADATA, TOKEN_HASH_SQL_PATTERN
from holm_persistence.schemas import CORE_SCHEMA

IDEMPOTENCY_RECORDS_TABLE_NAME: Final[str] = "idempotency_records"

IDEMPOTENCY_RECORDS: Final[sa.Table] = sa.Table(
    IDEMPOTENCY_RECORDS_TABLE_NAME,
    METADATA,
    sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
    sa.Column(
        "actor_id",
        sa.Uuid(as_uuid=True),
        sa.ForeignKey(f"{CORE_SCHEMA}.users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("operation", sa.Text(), nullable=False),
    sa.Column("key_hash", sa.Text(), nullable=False),
    sa.Column("request_hash", sa.Text(), nullable=False),
    sa.Column("response_object_id", sa.Uuid(as_uuid=True), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint(
        "actor_id",
        "operation",
        "key_hash",
        name="uq_idempotency_scope",
    ),
    sa.CheckConstraint("char_length(operation) > 0", name="operation_nonempty"),
    sa.CheckConstraint(
        f"key_hash ~ '{TOKEN_HASH_SQL_PATTERN}'",
        name="key_hash_sha256",
    ),
    sa.CheckConstraint(
        f"request_hash ~ '{TOKEN_HASH_SQL_PATTERN}'",
        name="request_hash_sha256",
    ),
    schema=CORE_SCHEMA,
)

__all__ = ["IDEMPOTENCY_RECORDS", "IDEMPOTENCY_RECORDS_TABLE_NAME"]

"""Materialized UTC Usage buckets."""

from __future__ import annotations

from typing import Final

import sqlalchemy as sa

from holm_persistence.accounts import METADATA
from holm_persistence.schemas import USAGE_SCHEMA

USAGE_15M_TABLE_NAME: Final[str] = "usage_15m"
USAGE_BUCKET_SECONDS: Final[int] = 15 * 60

USAGE_15M: Final[sa.Table] = sa.Table(
    USAGE_15M_TABLE_NAME,
    METADATA,
    sa.Column("bucket_start", sa.DateTime(timezone=True), primary_key=True),
    sa.Column("user_id", sa.Uuid(as_uuid=True), primary_key=True),
    sa.Column("api_key_id", sa.Uuid(as_uuid=True), primary_key=True),
    sa.Column("service_id", sa.Uuid(as_uuid=True), primary_key=True),
    sa.Column("requests", sa.BigInteger(), nullable=False),
    sa.Column("input_tokens", sa.BigInteger(), nullable=True),
    sa.Column("output_tokens", sa.BigInteger(), nullable=True),
    sa.Column("error_requests", sa.BigInteger(), nullable=False),
    sa.Column("aborted_requests", sa.BigInteger(), nullable=False),
    sa.Column("partial_token_requests", sa.BigInteger(), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint(
        "bucket_start = date_bin(INTERVAL '15 minutes', bucket_start, "
        "TIMESTAMPTZ '1970-01-01 00:00:00+00')",
        name="bucket_start_aligned",
    ),
    sa.CheckConstraint("requests >= 0", name="requests"),
    sa.CheckConstraint("input_tokens IS NULL OR input_tokens >= 0", name="input_tokens"),
    sa.CheckConstraint("output_tokens IS NULL OR output_tokens >= 0", name="output_tokens"),
    sa.CheckConstraint("error_requests >= 0", name="error_requests"),
    sa.CheckConstraint("aborted_requests >= 0", name="aborted_requests"),
    sa.CheckConstraint(
        "partial_token_requests >= 0",
        name="partial_token_requests",
    ),
    schema=USAGE_SCHEMA,
)


__all__ = ["USAGE_15M", "USAGE_15M_TABLE_NAME", "USAGE_BUCKET_SECONDS"]

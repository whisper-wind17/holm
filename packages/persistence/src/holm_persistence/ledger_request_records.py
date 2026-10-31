"""Partitioned, metadata-only Request Record projection tables."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Final

import sqlalchemy as sa
from holm_domain import (
    GATEWAY_ERROR_CODES,
    MeteringSource,
    MetricsCapability,
    RequestFinishReason,
    RequestRecordState,
)
from holm_ledger import EventType, RequestOperation, RequestProtocol

from holm_persistence.accounts import METADATA
from holm_persistence.schemas import USAGE_SCHEMA

REQUEST_ID_REGISTRY_TABLE_NAME: Final[str] = "request_id_registry"
LEDGER_REQUEST_RECORDS_TABLE_NAME: Final[str] = "request_records"
REQUEST_EVENT_QUARANTINE_TABLE_NAME: Final[str] = "request_event_quarantine"


def _values(values: Iterable[StrEnum]) -> str:
    return ", ".join(f"'{item.value}'" for item in sorted(values, key=lambda item: item.value))


REQUEST_ID_REGISTRY: Final[sa.Table] = sa.Table(
    REQUEST_ID_REGISTRY_TABLE_NAME,
    METADATA,
    sa.Column("request_id", sa.Uuid(as_uuid=True), primary_key=True),
    sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("highest_event_seq", sa.BigInteger(), nullable=False),
    sa.CheckConstraint("highest_event_seq > 0", name="highest_event_seq"),
    schema=USAGE_SCHEMA,
)

LEDGER_REQUEST_RECORDS: Final[sa.Table] = sa.Table(
    LEDGER_REQUEST_RECORDS_TABLE_NAME,
    METADATA,
    sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("request_id", sa.Uuid(as_uuid=True), nullable=False),
    sa.Column("event_seq", sa.BigInteger(), nullable=False),
    sa.Column("record_state", sa.Text(), nullable=False),
    sa.Column("protocol", sa.Text(), nullable=True),
    sa.Column("operation", sa.Text(), nullable=True),
    sa.Column("endpoint", sa.Text(), nullable=True),
    sa.Column("stream", sa.Boolean(), nullable=True),
    sa.Column("input_bytes", sa.BigInteger(), nullable=True),
    sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=True),
    sa.Column("api_key_id", sa.Uuid(as_uuid=True), nullable=True),
    sa.Column("service_id", sa.Uuid(as_uuid=True), nullable=True),
    sa.Column("instance_id", sa.Uuid(as_uuid=True), nullable=True),
    sa.Column("requested_max_tokens", sa.BigInteger(), nullable=True),
    sa.Column("input_tokens", sa.BigInteger(), nullable=True),
    sa.Column("output_tokens", sa.BigInteger(), nullable=True),
    sa.Column("ttft_ms", sa.BigInteger(), nullable=True),
    sa.Column("duration_ms", sa.BigInteger(), nullable=True),
    sa.Column("finish_reason", sa.Text(), nullable=True),
    sa.Column("http_status", sa.Integer(), nullable=True),
    sa.Column("error_code", sa.Text(), nullable=True),
    sa.Column("client_disconnected", sa.Boolean(), nullable=True),
    sa.Column("last_confirmed_event_type", sa.Text(), nullable=True),
    sa.Column("last_confirmed_event_seq", sa.BigInteger(), nullable=True),
    sa.Column("metering_source", sa.Text(), nullable=True),
    sa.Column("metrics_capability", sa.Text(), nullable=True),
    sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("received_at", "request_id"),
    sa.CheckConstraint("event_seq > 0", name="event_seq"),
    sa.CheckConstraint(
        f"record_state IN ({_values(RequestRecordState)})",
        name="record_state",
    ),
    sa.CheckConstraint(
        f"protocol IS NULL OR protocol IN ({_values(RequestProtocol)})",
        name="protocol",
    ),
    sa.CheckConstraint(
        f"operation IS NULL OR operation IN ({_values(RequestOperation)})",
        name="operation",
    ),
    sa.CheckConstraint("input_bytes IS NULL OR input_bytes >= 0", name="input_bytes"),
    sa.CheckConstraint(
        "requested_max_tokens IS NULL OR requested_max_tokens >= 0",
        name="requested_max_tokens",
    ),
    sa.CheckConstraint("input_tokens IS NULL OR input_tokens >= 0", name="input_tokens"),
    sa.CheckConstraint("output_tokens IS NULL OR output_tokens >= 0", name="output_tokens"),
    sa.CheckConstraint("ttft_ms IS NULL OR ttft_ms >= 0", name="ttft_ms"),
    sa.CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="duration_ms"),
    sa.CheckConstraint(
        f"finish_reason IS NULL OR finish_reason IN ({_values(RequestFinishReason)})",
        name="finish_reason",
    ),
    sa.CheckConstraint(
        "http_status IS NULL OR http_status BETWEEN 100 AND 599",
        name="http_status",
    ),
    sa.CheckConstraint(
        f"error_code IS NULL OR error_code IN ({_values(GATEWAY_ERROR_CODES)})",
        name="error_code",
    ),
    sa.CheckConstraint(
        "client_disconnected IS NULL OR client_disconnected",
        name="client_disconnected",
    ),
    sa.CheckConstraint(
        f"last_confirmed_event_type IS NULL OR last_confirmed_event_type IN ({_values(EventType)})",
        name="last_confirmed_event_type",
    ),
    sa.CheckConstraint(
        "last_confirmed_event_seq IS NULL OR last_confirmed_event_seq > 0",
        name="last_confirmed_event_seq",
    ),
    sa.CheckConstraint(
        f"metering_source IS NULL OR metering_source IN ({_values(MeteringSource)})",
        name="metering_source",
    ),
    sa.CheckConstraint(
        f"metrics_capability IS NULL OR metrics_capability IN ({_values(MetricsCapability)})",
        name="metrics_capability",
    ),
    sa.Index(
        "ix_usage_request_records_received_at_desc",
        sa.desc(sa.column("received_at")),
    ),
    sa.Index(
        "ix_usage_request_records_user_id_received_at",
        "user_id",
        sa.desc(sa.column("received_at")),
    ),
    sa.Index(
        "ix_usage_request_records_service_id_received_at",
        "service_id",
        sa.desc(sa.column("received_at")),
    ),
    schema=USAGE_SCHEMA,
    postgresql_partition_by="RANGE (received_at)",
)

REQUEST_EVENT_QUARANTINE: Final[sa.Table] = sa.Table(
    REQUEST_EVENT_QUARANTINE_TABLE_NAME,
    METADATA,
    sa.Column("request_id", sa.Uuid(as_uuid=True), primary_key=True),
    sa.Column("event_seq", sa.BigInteger(), primary_key=True),
    sa.Column("event_type", sa.Text(), nullable=False),
    sa.Column("event_received_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("registered_received_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("event_seq > 0", name="event_seq"),
    sa.CheckConstraint(
        f"event_type IN ({_values(EventType)})",
        name="event_type",
    ),
    schema=USAGE_SCHEMA,
)


__all__ = [
    "LEDGER_REQUEST_RECORDS",
    "LEDGER_REQUEST_RECORDS_TABLE_NAME",
    "REQUEST_EVENT_QUARANTINE",
    "REQUEST_EVENT_QUARANTINE_TABLE_NAME",
    "REQUEST_ID_REGISTRY",
    "REQUEST_ID_REGISTRY_TABLE_NAME",
]

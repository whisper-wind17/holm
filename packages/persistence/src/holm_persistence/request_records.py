"""Metadata-only Request Record table."""

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
    RequestRecordStatus,
)

from holm_persistence.accounts import METADATA, USERS_TABLE_NAME
from holm_persistence.ai_services import (
    AI_SERVICES_TABLE_NAME,
    API_KEYS_TABLE_NAME,
)
from holm_persistence.model_instances import MODEL_INSTANCES_TABLE_NAME
from holm_persistence.schemas import CORE_SCHEMA

REQUEST_RECORDS_TABLE_NAME: Final[str] = "request_records"


def _values(values: Iterable[StrEnum]) -> str:
    return ", ".join(f"'{item.value}'" for item in sorted(values, key=lambda item: item.value))


REQUEST_RECORDS: Final[sa.Table] = sa.Table(
    REQUEST_RECORDS_TABLE_NAME,
    METADATA,
    sa.Column("request_id", sa.Uuid(as_uuid=True), primary_key=True),
    sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column(
        "user_id",
        sa.Uuid(as_uuid=True),
        sa.ForeignKey(f"{CORE_SCHEMA}.{USERS_TABLE_NAME}.id", ondelete="RESTRICT"),
        nullable=True,
    ),
    sa.Column(
        "api_key_id",
        sa.Uuid(as_uuid=True),
        sa.ForeignKey(f"{CORE_SCHEMA}.{API_KEYS_TABLE_NAME}.id", ondelete="RESTRICT"),
        nullable=True,
    ),
    sa.Column(
        "service_id",
        sa.Uuid(as_uuid=True),
        sa.ForeignKey(f"{CORE_SCHEMA}.{AI_SERVICES_TABLE_NAME}.id", ondelete="SET NULL"),
        nullable=True,
    ),
    sa.Column(
        "instance_id",
        sa.Uuid(as_uuid=True),
        sa.ForeignKey(
            f"{CORE_SCHEMA}.{MODEL_INSTANCES_TABLE_NAME}.id",
            ondelete="SET NULL",
        ),
        nullable=True,
    ),
    sa.Column("stream", sa.Boolean(), nullable=False),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("http_status", sa.Integer(), nullable=False),
    sa.Column("error_code", sa.Text(), nullable=True),
    sa.Column("input_tokens", sa.BigInteger(), nullable=True),
    sa.Column("output_tokens", sa.BigInteger(), nullable=True),
    sa.Column("ttft_ms", sa.BigInteger(), nullable=True),
    sa.Column("duration_ms", sa.BigInteger(), nullable=False),
    sa.Column("finish_reason", sa.Text(), nullable=True),
    sa.Column("metering_source", sa.Text(), nullable=True),
    sa.Column("metrics_capability", sa.Text(), nullable=True),
    sa.CheckConstraint("completed_at >= received_at", name="completed_after_received"),
    sa.CheckConstraint(
        f"status IN ({_values(tuple(RequestRecordStatus))})",
        name="status",
    ),
    sa.CheckConstraint("http_status BETWEEN 100 AND 599", name="http_status"),
    sa.CheckConstraint(
        f"error_code IS NULL OR error_code IN ({_values(GATEWAY_ERROR_CODES)})",
        name="error_code",
    ),
    sa.CheckConstraint("input_tokens IS NULL OR input_tokens >= 0", name="input_tokens"),
    sa.CheckConstraint("output_tokens IS NULL OR output_tokens >= 0", name="output_tokens"),
    sa.CheckConstraint("ttft_ms IS NULL OR ttft_ms >= 0", name="ttft_ms"),
    sa.CheckConstraint("duration_ms >= 0", name="duration_ms"),
    sa.CheckConstraint(
        f"finish_reason IS NULL OR finish_reason IN ({_values(tuple(RequestFinishReason))})",
        name="finish_reason",
    ),
    sa.CheckConstraint(
        f"metering_source IS NULL OR metering_source IN ({_values(tuple(MeteringSource))})",
        name="metering_source",
    ),
    sa.CheckConstraint(
        "metrics_capability IS NULL OR metrics_capability IN "
        f"({_values(tuple(MetricsCapability))})",
        name="metrics_capability",
    ),
    sa.Index(
        "ix_request_records_received_at_desc",
        sa.desc(sa.column("received_at")),
    ),
    sa.Index(
        "ix_request_records_service_id_received_at",
        "service_id",
        sa.desc(sa.column("received_at")),
    ),
    sa.Index(
        "ix_request_records_user_id_received_at",
        "user_id",
        sa.desc(sa.column("received_at")),
    ),
    schema=CORE_SCHEMA,
)


__all__ = ["REQUEST_RECORDS", "REQUEST_RECORDS_TABLE_NAME"]

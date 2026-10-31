"""Model Source table with encrypted credential columns only."""

from __future__ import annotations

from typing import Final

import sqlalchemy as sa
from holm_domain import ModelSourceType, ModelSourceValidationStatus

from holm_persistence.accounts import METADATA, _values_clause
from holm_persistence.schemas import CORE_SCHEMA

MODEL_SOURCES_TABLE_NAME: Final[str] = "model_sources"

MODEL_SOURCES: Final[sa.Table] = sa.Table(
    MODEL_SOURCES_TABLE_NAME,
    METADATA,
    sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
    sa.Column("type", sa.Text(), nullable=False),
    sa.Column("credential_key_id", sa.Text(), nullable=True),
    sa.Column("credential_nonce", sa.LargeBinary(), nullable=True),
    sa.Column("credential_ciphertext", sa.LargeBinary(), nullable=True),
    sa.Column("credential_tag", sa.LargeBinary(), nullable=True),
    sa.Column("verification_status", sa.Text(), nullable=True),
    sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("verification_reason_code", sa.Text(), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("type"),
    sa.UniqueConstraint("id", "type"),
    sa.CheckConstraint(_values_clause("type", ModelSourceType), name="type"),
    sa.CheckConstraint(
        "verification_status IS NULL OR "
        + _values_clause("verification_status", ModelSourceValidationStatus),
        name="verification_status",
    ),
    sa.CheckConstraint(
        "(credential_key_id IS NULL AND credential_nonce IS NULL "
        "AND credential_ciphertext IS NULL AND credential_tag IS NULL) OR "
        "(credential_key_id IS NOT NULL AND credential_nonce IS NOT NULL "
        "AND credential_ciphertext IS NOT NULL AND credential_tag IS NOT NULL)",
        name="credential_envelope_complete",
    ),
    sa.CheckConstraint(
        "credential_nonce IS NULL OR octet_length(credential_nonce) = 12",
        name="credential_nonce_length",
    ),
    sa.CheckConstraint(
        "credential_ciphertext IS NULL OR octet_length(credential_ciphertext) > 0",
        name="credential_ciphertext_nonempty",
    ),
    sa.CheckConstraint(
        "credential_tag IS NULL OR octet_length(credential_tag) = 16",
        name="credential_tag_length",
    ),
    schema=CORE_SCHEMA,
)

__all__ = ["MODEL_SOURCES", "MODEL_SOURCES_TABLE_NAME"]

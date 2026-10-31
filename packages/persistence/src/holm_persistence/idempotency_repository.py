"""Database-convergent, reusable HTTP idempotency claims."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from holm_common import as_utc, new_uuid7
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from holm_persistence.idempotency import IDEMPOTENCY_RECORDS

IDEMPOTENCY_HMAC_MINIMUM_BYTES = 32


@dataclass(frozen=True, slots=True)
class IdempotencyDigests:
    """Only irreversible request identities; plaintexts are never retained."""

    key_hash: str = field(repr=False)
    request_hash: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    """One committed or transaction-local idempotency decision."""

    id: UUID
    actor_id: UUID
    operation: str
    response_object_id: UUID
    created_at: datetime
    key_hash: str = field(repr=False)
    request_hash: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class IdempotencyClaim:
    """Whether this transaction owns the operation or observed its owner."""

    record: IdempotencyRecord
    claimed: bool


def idempotency_digests(
    *,
    hmac_secret: bytes,
    idempotency_key: str,
    normalized_request: bytes,
) -> IdempotencyDigests:
    """Derive the two fixed-size values permitted to reach persistence."""

    if not isinstance(hmac_secret, bytes) or len(hmac_secret) < IDEMPOTENCY_HMAC_MINIMUM_BYTES:
        raise ValueError("idempotency HMAC secret is too short")
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise ValueError("Idempotency-Key must not be empty")
    if not isinstance(normalized_request, bytes) or not normalized_request:
        raise ValueError("normalized idempotency request must not be empty")
    return IdempotencyDigests(
        key_hash=hmac.new(
            hmac_secret,
            idempotency_key.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest(),
        request_hash=hashlib.sha256(normalized_request).hexdigest(),
    )


def _record(mapping: sa.RowMapping) -> IdempotencyRecord:
    return IdempotencyRecord(
        id=mapping["id"],
        actor_id=mapping["actor_id"],
        operation=str(mapping["operation"]),
        key_hash=str(mapping["key_hash"]),
        request_hash=str(mapping["request_hash"]),
        response_object_id=mapping["response_object_id"],
        created_at=as_utc(mapping["created_at"]),
    )


class IdempotencyRepository:
    """Claim a request with one unique-index-mediated write, never a pre-read."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(
        self,
        *,
        actor_id: UUID,
        operation: str,
        digests: IdempotencyDigests,
        response_object_id: UUID,
        now: datetime,
        expires_before: datetime,
    ) -> IdempotencyClaim:
        """Atomically insert, replace an expired record, or observe the winner."""

        insert = postgresql_insert(IDEMPOTENCY_RECORDS).values(
            id=new_uuid7(),
            actor_id=actor_id,
            operation=operation,
            key_hash=digests.key_hash,
            request_hash=digests.request_hash,
            response_object_id=response_object_id,
            created_at=as_utc(now),
        )
        statement = insert.on_conflict_do_update(
            constraint="uq_idempotency_scope",
            set_={
                "request_hash": insert.excluded.request_hash,
                "response_object_id": insert.excluded.response_object_id,
                "created_at": insert.excluded.created_at,
            },
            where=IDEMPOTENCY_RECORDS.c.created_at <= as_utc(expires_before),
        ).returning(*IDEMPOTENCY_RECORDS.c)
        mapping = (await self._session.execute(statement)).mappings().one_or_none()
        if mapping is not None:
            return IdempotencyClaim(record=_record(mapping), claimed=True)

        observed = (
            (
                await self._session.execute(
                    sa.select(IDEMPOTENCY_RECORDS).where(
                        IDEMPOTENCY_RECORDS.c.actor_id == actor_id,
                        IDEMPOTENCY_RECORDS.c.operation == operation,
                        IDEMPOTENCY_RECORDS.c.key_hash == digests.key_hash,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if observed is None:
            raise RuntimeError("idempotency conflict winner disappeared")
        return IdempotencyClaim(record=_record(observed), claimed=False)


__all__ = [
    "IDEMPOTENCY_HMAC_MINIMUM_BYTES",
    "IdempotencyClaim",
    "IdempotencyDigests",
    "IdempotencyRecord",
    "IdempotencyRepository",
    "idempotency_digests",
]

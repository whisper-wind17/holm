"""Repository for encrypted Model Source credentials and rotation fences."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from holm_common import as_utc, new_uuid7
from holm_domain import ModelSourceType, ModelSourceValidationStatus
from holm_security import CredentialEnvelope
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from holm_persistence.model_sources import MODEL_SOURCES


@dataclass(frozen=True, slots=True)
class ModelSourceRecord:
    """One source row; encrypted bytes are excluded from repr by the envelope type."""

    id: UUID
    source_type: ModelSourceType
    credential: CredentialEnvelope | None
    verification_status: ModelSourceValidationStatus | None
    verified_at: datetime | None
    verification_reason_code: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class KeyReferenceScan:
    """A non-ambiguous database scan result used by the Key-disable gate."""

    referenced_rows: int
    scanned_rows: int


def _record(mapping: sa.RowMapping) -> ModelSourceRecord:
    key_id = mapping["credential_key_id"]
    credential = None
    if key_id is not None:
        credential = CredentialEnvelope(
            key_id=key_id,
            nonce=bytes(mapping["credential_nonce"]),
            ciphertext=bytes(mapping["credential_ciphertext"]),
            tag=bytes(mapping["credential_tag"]),
        )
    return ModelSourceRecord(
        id=mapping["id"],
        source_type=ModelSourceType(mapping["type"]),
        credential=credential,
        verification_status=(
            None
            if mapping["verification_status"] is None
            else ModelSourceValidationStatus(mapping["verification_status"])
        ),
        verified_at=(None if mapping["verified_at"] is None else as_utc(mapping["verified_at"])),
        verification_reason_code=mapping["verification_reason_code"],
        created_at=as_utc(mapping["created_at"]),
        updated_at=as_utc(mapping["updated_at"]),
    )


class ModelSourceRepository:
    """Read and atomically replace credential envelopes inside a caller UoW."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        source_type: ModelSourceType,
        now: datetime,
        source_id: UUID | None = None,
    ) -> ModelSourceRecord:
        statement = (
            sa.insert(MODEL_SOURCES)
            .values(
                id=new_uuid7() if source_id is None else source_id,
                type=ModelSourceType(source_type).value,
                credential_key_id=None,
                credential_nonce=None,
                credential_ciphertext=None,
                credential_tag=None,
                verification_status=None,
                verified_at=None,
                verification_reason_code=None,
                created_at=as_utc(now),
                updated_at=as_utc(now),
            )
            .returning(*MODEL_SOURCES.c)
        )
        return _record((await self._session.execute(statement)).mappings().one())

    async def ensure(
        self,
        *,
        source_type: ModelSourceType,
        now: datetime,
    ) -> ModelSourceRecord:
        """Create one official source row if absent, without racing another writer."""

        await self._session.execute(
            postgresql_insert(MODEL_SOURCES)
            .values(
                id=new_uuid7(),
                type=ModelSourceType(source_type).value,
                credential_key_id=None,
                credential_nonce=None,
                credential_ciphertext=None,
                credential_tag=None,
                verification_status=None,
                verified_at=None,
                verification_reason_code=None,
                created_at=as_utc(now),
                updated_at=as_utc(now),
            )
            .on_conflict_do_nothing(index_elements=[MODEL_SOURCES.c.type])
        )
        record = await self.get_by_type(source_type)
        if record is None:
            raise RuntimeError("model source ensure did not produce a row")
        return record

    async def get(self, source_id: UUID) -> ModelSourceRecord | None:
        statement = sa.select(MODEL_SOURCES).where(MODEL_SOURCES.c.id == source_id)
        mapping = (await self._session.execute(statement)).mappings().one_or_none()
        return None if mapping is None else _record(mapping)

    async def get_by_type(
        self,
        source_type: ModelSourceType,
    ) -> ModelSourceRecord | None:
        statement = sa.select(MODEL_SOURCES).where(
            MODEL_SOURCES.c.type == ModelSourceType(source_type).value
        )
        mapping = (await self._session.execute(statement)).mappings().one_or_none()
        return None if mapping is None else _record(mapping)

    async def list_all(self) -> tuple[ModelSourceRecord, ...]:
        statement = sa.select(MODEL_SOURCES).order_by(MODEL_SOURCES.c.type)
        return tuple(_record(row) for row in (await self._session.execute(statement)).mappings())

    async def set_credential(
        self,
        source_id: UUID,
        *,
        credential: CredentialEnvelope,
        now: datetime,
    ) -> ModelSourceRecord | None:
        statement = (
            sa.update(MODEL_SOURCES)
            .where(MODEL_SOURCES.c.id == source_id)
            .values(
                credential_key_id=credential.key_id,
                credential_nonce=credential.nonce,
                credential_ciphertext=credential.ciphertext,
                credential_tag=credential.tag,
                updated_at=as_utc(now),
            )
            .returning(*MODEL_SOURCES.c)
        )
        mapping = (await self._session.execute(statement)).mappings().one_or_none()
        return None if mapping is None else _record(mapping)

    async def list_configured(self) -> tuple[ModelSourceRecord, ...]:
        statement = (
            sa.select(MODEL_SOURCES)
            .where(MODEL_SOURCES.c.credential_key_id.is_not(None))
            .order_by(MODEL_SOURCES.c.id)
        )
        return tuple(_record(row) for row in (await self._session.execute(statement)).mappings())

    async def list_needing_key(
        self,
        key_id: str,
        *,
        limit: int,
    ) -> tuple[ModelSourceRecord, ...]:
        if limit < 1:
            raise ValueError("rotation batch limit must be positive")
        statement = (
            sa.select(MODEL_SOURCES)
            .where(
                MODEL_SOURCES.c.credential_key_id.is_not(None),
                MODEL_SOURCES.c.credential_key_id != key_id,
            )
            .order_by(MODEL_SOURCES.c.id)
            .limit(limit)
        )
        return tuple(_record(row) for row in (await self._session.execute(statement)).mappings())

    async def compare_and_swap_credential(
        self,
        source_id: UUID,
        *,
        expected_key_id: str,
        credential: CredentialEnvelope,
        now: datetime,
    ) -> bool:
        """Replace all four fields in one conditional statement or change nothing."""

        statement = (
            sa.update(MODEL_SOURCES)
            .where(
                MODEL_SOURCES.c.id == source_id,
                MODEL_SOURCES.c.credential_key_id == expected_key_id,
            )
            .values(
                credential_key_id=credential.key_id,
                credential_nonce=credential.nonce,
                credential_ciphertext=credential.ciphertext,
                credential_tag=credential.tag,
                updated_at=as_utc(now),
            )
            .returning(MODEL_SOURCES.c.id)
        )
        return (await self._session.execute(statement)).scalar_one_or_none() is not None

    async def compare_and_swap_full_credential(
        self,
        source_id: UUID,
        *,
        expected_key_id: str | None,
        credential: CredentialEnvelope | None,
        now: datetime,
        expected_credential: CredentialEnvelope | None = None,
        match_full_envelope: bool = False,
        verification_status: ModelSourceValidationStatus | None = None,
        verification_reason_code: str | None = None,
        update_validation: bool = False,
    ) -> bool:
        """Write the envelope and validation facts in one conditional statement or not at all."""

        conditions: list[sa.ColumnElement[bool]] = [MODEL_SOURCES.c.id == source_id]
        if expected_key_id is None:
            conditions.append(MODEL_SOURCES.c.credential_key_id.is_(None))
        else:
            conditions.append(MODEL_SOURCES.c.credential_key_id == expected_key_id)
        if match_full_envelope:
            if expected_credential is None:
                conditions.extend(
                    (
                        MODEL_SOURCES.c.credential_nonce.is_(None),
                        MODEL_SOURCES.c.credential_ciphertext.is_(None),
                        MODEL_SOURCES.c.credential_tag.is_(None),
                    )
                )
            else:
                conditions.extend(
                    (
                        MODEL_SOURCES.c.credential_nonce == expected_credential.nonce,
                        MODEL_SOURCES.c.credential_ciphertext == expected_credential.ciphertext,
                        MODEL_SOURCES.c.credential_tag == expected_credential.tag,
                    )
                )
        values: dict[str, object] = {
            "credential_key_id": None if credential is None else credential.key_id,
            "credential_nonce": None if credential is None else credential.nonce,
            "credential_ciphertext": None if credential is None else credential.ciphertext,
            "credential_tag": None if credential is None else credential.tag,
            "updated_at": as_utc(now),
        }
        if update_validation:
            values.update(
                verification_status=(
                    None
                    if verification_status is None
                    else ModelSourceValidationStatus(verification_status).value
                ),
                verified_at=(None if verification_reason_code is None else as_utc(now)),
                verification_reason_code=verification_reason_code,
            )
        statement = (
            sa.update(MODEL_SOURCES)
            .where(*conditions)
            .values(**values)
            .returning(MODEL_SOURCES.c.id)
        )
        return (await self._session.execute(statement)).scalar_one_or_none() is not None

    async def clear_credential(
        self,
        source_id: UUID,
        *,
        expected_credential: CredentialEnvelope,
        now: datetime,
    ) -> bool:
        """Clear the complete envelope through the same full-value CAS path."""

        return await self.compare_and_swap_full_credential(
            source_id,
            expected_key_id=expected_credential.key_id,
            expected_credential=expected_credential,
            match_full_envelope=True,
            credential=None,
            now=now,
            update_validation=True,
            verification_status=None,
            verification_reason_code=None,
        )

    async def key_reference_scan(self, key_id: str) -> KeyReferenceScan:
        statement = sa.select(
            sa.func.count().filter(MODEL_SOURCES.c.credential_key_id == key_id),
            sa.func.count().filter(MODEL_SOURCES.c.credential_key_id.is_not(None)),
        ).select_from(MODEL_SOURCES)
        referenced, scanned = (await self._session.execute(statement)).one()
        return KeyReferenceScan(referenced_rows=int(referenced), scanned_rows=int(scanned))


__all__ = ["KeyReferenceScan", "ModelSourceRecord", "ModelSourceRepository"]

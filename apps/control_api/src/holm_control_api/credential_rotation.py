"""Startup validation and resumable Master Key credential re-encryption."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final, Protocol
from uuid import UUID

from holm_common import utc_now
from holm_domain import CredentialType
from holm_persistence import AsyncSessionFactory, ModelSourceRepository
from holm_security import KeyringError, MasterKeyring

DEFAULT_ROTATION_BATCH_SIZE: Final[int] = 64
DEFAULT_MAX_ROTATION_BATCHES: Final[int] = 1024
LOGGER = logging.getLogger(__name__)


class CredentialStartupCheck(Protocol):
    """The application lifespan invokes this check before declaring startup complete."""

    async def check(self) -> None: ...


class RotationObserver(Protocol):
    """Optional non-secret progress sink used for process control and telemetry."""

    async def row_decrypted(self, source_id: UUID, row_number: int) -> None: ...


class _NullRotationObserver:
    async def row_decrypted(self, source_id: UUID, row_number: int) -> None:
        return None


class CredentialStartupError(RuntimeError):
    """Sanitized startup refusal for unavailable or invalid credential material."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"credential startup rejected: {code}")


@dataclass(frozen=True, slots=True)
class RotationResult:
    """Key IDs and counts are safe to record; no secret material is retained."""

    writable_key_id: str
    scanned_rows: int
    reencrypted_rows: int
    concurrent_updates: int
    reencrypted_source_ids: tuple[UUID, ...]


class DatabaseCredentialStartupCheck:
    """Validate that every configured envelope has a usable Key before startup."""

    __slots__ = ("_keyring", "_session_factory")

    def __init__(
        self,
        session_factory: AsyncSessionFactory,
        keyring: MasterKeyring | None,
    ) -> None:
        self._session_factory = session_factory
        self._keyring = keyring

    async def check(self) -> None:
        async with self._session_factory() as session:
            records = await ModelSourceRepository(session).list_configured()
        if not records:
            return
        if self._keyring is None:
            raise CredentialStartupError("keyring_missing")
        for record in records:
            credential = record.credential
            if credential is None:
                raise CredentialStartupError("credential_envelope_incomplete")
            if not self._keyring.can_decrypt(credential.key_id):
                raise CredentialStartupError("key_unavailable")


class CredentialRotationService:
    """Re-encrypt old envelopes in atomic, conditional batches and resume from rows."""

    __slots__ = ("_keyring", "_observer", "_session_factory")

    def __init__(
        self,
        session_factory: AsyncSessionFactory,
        keyring: MasterKeyring,
        observer: RotationObserver | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._keyring = keyring
        self._observer = _NullRotationObserver() if observer is None else observer

    async def rotate(
        self,
        *,
        batch_size: int = DEFAULT_ROTATION_BATCH_SIZE,
        max_batches: int = DEFAULT_MAX_ROTATION_BATCHES,
    ) -> RotationResult:
        if batch_size < 1 or max_batches < 1:
            raise ValueError("rotation bounds must be positive")
        writable_key_id = self._keyring.writable_key_id
        scanned_rows = 0
        reencrypted_rows = 0
        concurrent_updates = 0
        reencrypted_source_ids: list[UUID] = []
        for _batch_number in range(max_batches):
            async with self._session_factory() as session, session.begin():
                repository = ModelSourceRepository(session)
                records = await repository.list_needing_key(
                    writable_key_id,
                    limit=batch_size,
                )
                if not records:
                    result = RotationResult(
                        writable_key_id=writable_key_id,
                        scanned_rows=scanned_rows,
                        reencrypted_rows=reencrypted_rows,
                        concurrent_updates=concurrent_updates,
                        reencrypted_source_ids=tuple(reencrypted_source_ids),
                    )
                    LOGGER.info(
                        "credential rotation complete",
                        extra={
                            "key_id": writable_key_id,
                            "scanned_rows": scanned_rows,
                            "reencrypted_rows": reencrypted_rows,
                            "concurrent_updates": concurrent_updates,
                        },
                    )
                    return result
                for record in records:
                    scanned_rows += 1
                    old = record.credential
                    if old is None:
                        raise KeyringError("credential_envelope_incomplete")
                    plaintext = self._keyring.decrypt(
                        old,
                        source_id=record.id,
                        credential_type=CredentialType.ACCESS_TOKEN.value,
                    )
                    await self._observer.row_decrypted(record.id, scanned_rows)
                    replacement = self._keyring.encrypt(
                        plaintext,
                        source_id=record.id,
                        credential_type=CredentialType.ACCESS_TOKEN.value,
                    )
                    updated = await repository.compare_and_swap_credential(
                        record.id,
                        expected_key_id=old.key_id,
                        credential=replacement,
                        now=utc_now(),
                    )
                    if updated:
                        reencrypted_rows += 1
                        reencrypted_source_ids.append(record.id)
                    else:
                        concurrent_updates += 1
        raise KeyringError("rotation_batch_limit")

    async def require_key_can_be_disabled(self, key_id: str) -> None:
        """Use the database scan as the only authority for the disable gate."""

        async with self._session_factory() as session:
            scan = await ModelSourceRepository(session).key_reference_scan(key_id)
        self._keyring.require_key_can_be_disabled(
            key_id,
            referenced_rows=scan.referenced_rows,
            scanned_rows=scan.scanned_rows,
        )


__all__ = [
    "CredentialRotationService",
    "CredentialStartupCheck",
    "CredentialStartupError",
    "DatabaseCredentialStartupCheck",
    "RotationResult",
    "RotationObserver",
]

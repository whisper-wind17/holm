"""Authenticated Master Key rotation operation and audit boundary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from holm_domain import (
    AuditAction,
    AuditActorType,
    AuditObjectType,
    AuditResult,
)
from holm_persistence import (
    AsyncSessionFactory,
    AuditEventDraft,
    AuditWriter,
    ModelSourceRepository,
)
from holm_security import (
    KeyringError,
    MasterKeyring,
    MasterKeyRotationPhase,
    MasterKeyState,
    build_audit_details,
)
from sqlalchemy.exc import SQLAlchemyError

from holm_control_api.authentication import AuthenticationFailure, AuthenticationService
from holm_control_api.credential_rotation import CredentialRotationService


class MasterKeyRotationStatus(StrEnum):
    """Outward state that keeps no-op distinct from a completed rotation."""

    NOT_REQUIRED = "not_required"
    ROTATION = "rotation"


class MasterKeyRotationUnavailable(RuntimeError):
    """A sanitized, stable failure to derive or advance rotation state."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Master Key rotation unavailable: {code}")


@dataclass(frozen=True, slots=True)
class MasterKeyRotationSnapshot:
    """Derived state containing only safe Key identifiers and row counts."""

    status: MasterKeyRotationStatus
    phase: MasterKeyRotationPhase | None
    writable_key_id: str | None
    old_key_id: str | None
    referenced_rows: int
    scanned_rows: int


@dataclass(frozen=True, slots=True)
class MasterKeyRotationAdvanceResult:
    """One idempotent advance result without per-row or secret data."""

    state: MasterKeyRotationSnapshot
    reencrypted_rows: int


class MasterKeyRotationOperationService:
    """Derive, authenticate, advance, and audit Master Key rotation."""

    __slots__ = (
        "_audit_writer",
        "_authentication",
        "_keyring",
        "_rotation",
        "_session_factory",
    )

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory,
        authentication: AuthenticationService,
        keyring: MasterKeyring | None,
    ) -> None:
        self._session_factory = session_factory
        self._authentication = authentication
        self._keyring = keyring
        self._rotation = (
            None if keyring is None else CredentialRotationService(session_factory, keyring)
        )
        self._audit_writer = AuditWriter(session_factory)

    async def inspect(self) -> MasterKeyRotationSnapshot:
        """Derive state from immutable Key metadata and a fresh database scan."""

        try:
            async with self._session_factory() as session:
                repository = ModelSourceRepository(session)
                configured = await repository.list_configured()
                if not configured:
                    return self._not_required(scanned_rows=0)
                if self._keyring is None:
                    raise MasterKeyRotationUnavailable("master_keyring_unavailable")

                writable_key_id = self._keyring.writable_key_id
                key_states = self._keyring.key_states
                old_keys = tuple(
                    (key_id, state)
                    for key_id, state in key_states.items()
                    if key_id != writable_key_id
                )
                if not old_keys:
                    configured_key_ids = {
                        record.credential.key_id
                        for record in configured
                        if record.credential is not None
                    }
                    if configured_key_ids == {writable_key_id}:
                        return self._not_required(scanned_rows=len(configured))
                    raise MasterKeyRotationUnavailable("rotation_key_unavailable")
                if len(old_keys) != 1:
                    raise MasterKeyRotationUnavailable("rotation_key_ambiguous")

                old_key_id, old_key_state = old_keys[0]
                scan = await repository.key_reference_scan(old_key_id)
        except MasterKeyRotationUnavailable:
            raise
        except SQLAlchemyError:
            raise MasterKeyRotationUnavailable("reference_scan_unavailable") from None

        if scan.scanned_rows <= 0:
            raise MasterKeyRotationUnavailable("reference_scan_empty")
        try:
            if (
                old_key_state is MasterKeyState.DECRYPT_ONLY
                and scan.referenced_rows == scan.scanned_rows
            ):
                phase = self._keyring.begin_rotation(old_key_id)
            else:
                phase = self._keyring.rotation_phase_from_references(
                    old_key_id,
                    referenced_rows=scan.referenced_rows,
                    scanned_rows=scan.scanned_rows,
                )
        except KeyringError:
            raise MasterKeyRotationUnavailable("rotation_state_unavailable") from None
        return MasterKeyRotationSnapshot(
            status=MasterKeyRotationStatus.ROTATION,
            phase=phase,
            writable_key_id=self._keyring.writable_key_id,
            old_key_id=old_key_id,
            referenced_rows=scan.referenced_rows,
            scanned_rows=scan.scanned_rows,
        )

    async def advance(
        self,
        *,
        actor_id: UUID,
        session_token: str | None,
        csrf_token: str | None,
        current_password: str | None,
        request_id: UUID,
    ) -> MasterKeyRotationAdvanceResult:
        """Reauthenticate, resume persisted progress, and emit exactly one audit row."""

        try:
            authenticated = await self._authentication.reauthenticate_high_risk_operation(
                session_token=session_token,
                csrf_token=csrf_token,
                password=current_password,
            )
        except AuthenticationFailure:
            await self._write_audit(
                actor_id=actor_id,
                request_id=request_id,
                result=AuditResult.DENIED,
                state=None,
                reencrypted_rows=0,
            )
            raise
        if authenticated.user.id != actor_id:
            raise RuntimeError("high-risk reauthentication identity changed")

        state: MasterKeyRotationSnapshot | None = None
        reencrypted_rows = 0
        try:
            state = await self.inspect()
            if state.status is MasterKeyRotationStatus.ROTATION:
                if self._rotation is None:
                    raise MasterKeyRotationUnavailable("master_keyring_unavailable")
                result = await self._rotation.rotate()
                reencrypted_rows = result.reencrypted_rows
                state = await self.inspect()
        except (KeyringError, MasterKeyRotationUnavailable):
            await self._write_audit(
                actor_id=actor_id,
                request_id=request_id,
                result=AuditResult.FAILED,
                state=state,
                reencrypted_rows=reencrypted_rows,
            )
            raise MasterKeyRotationUnavailable("rotation_advance_unavailable") from None

        await self._write_audit(
            actor_id=actor_id,
            request_id=request_id,
            result=AuditResult.SUCCEEDED,
            state=state,
            reencrypted_rows=reencrypted_rows,
        )
        return MasterKeyRotationAdvanceResult(
            state=state,
            reencrypted_rows=reencrypted_rows,
        )

    def _not_required(self, *, scanned_rows: int) -> MasterKeyRotationSnapshot:
        return MasterKeyRotationSnapshot(
            status=MasterKeyRotationStatus.NOT_REQUIRED,
            phase=None,
            writable_key_id=(None if self._keyring is None else self._keyring.writable_key_id),
            old_key_id=None,
            referenced_rows=0,
            scanned_rows=scanned_rows,
        )

    async def _write_audit(
        self,
        *,
        actor_id: UUID,
        request_id: UUID,
        result: AuditResult,
        state: MasterKeyRotationSnapshot | None,
        reencrypted_rows: int,
    ) -> None:
        await self._audit_writer.write(
            AuditEventDraft(
                actor_type=AuditActorType.USER,
                actor_id=actor_id,
                action=AuditAction.MODEL_SOURCE_CREDENTIAL_CHANGE,
                object_type=AuditObjectType.MODEL_SOURCE,
                object_id=None,
                result=result,
                request_id=request_id,
                details=build_audit_details(
                    AuditAction.MODEL_SOURCE_CREDENTIAL_CHANGE.value,
                    {
                        "writable_key_id": (None if state is None else state.writable_key_id),
                        "old_key_id": None if state is None else state.old_key_id,
                        "referenced_rows": (0 if state is None else state.referenced_rows),
                        "scanned_rows": 0 if state is None else state.scanned_rows,
                        "reencrypted_rows": reencrypted_rows,
                    },
                ),
            )
        )


__all__ = [
    "MasterKeyRotationAdvanceResult",
    "MasterKeyRotationOperationService",
    "MasterKeyRotationSnapshot",
    "MasterKeyRotationStatus",
    "MasterKeyRotationUnavailable",
]

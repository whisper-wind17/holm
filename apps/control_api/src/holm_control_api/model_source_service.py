"""Model Source credential presentation, replacement, validation, and audit."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from holm_domain import (
    AuditAction,
    AuditActorType,
    AuditObjectType,
    AuditResult,
    CredentialType,
    ModelSourceConfigurationState,
    ModelSourceType,
    ModelSourceValidationAction,
    ModelSourceValidationReason,
    ModelSourceValidationStatus,
)
from holm_persistence import (
    AsyncSessionFactory,
    AuditEventDraft,
    AuditWriter,
    ModelSourceRecord,
    ModelSourceRepository,
    database_utc_now,
)
from holm_security import (
    CredentialDecryptionError,
    MasterKeyring,
    build_audit_details,
)

from holm_control_api.authentication import AuthenticationFailure, AuthenticationService
from holm_control_api.model_source_validation import (
    ModelSourceValidationResult,
    ModelSourceValidatorRegistry,
)

_CREDENTIAL_TYPE = CredentialType.ACCESS_TOKEN.value


class CredentialChangeOperation(StrEnum):
    """Stable audit classifications for the three idempotent write operations."""

    WRITE = "write"
    REPLACE = "replace"
    CLEAR = "clear"


class ModelSourceCredentialConflict(RuntimeError):
    """Another writer changed the envelope after this request read it."""

    def __init__(self) -> None:
        super().__init__("model source credential update conflicted")


class ModelSourceCredentialNotSaved(RuntimeError):
    """A sanitized validation conclusion that must not alter the stored credential."""

    def __init__(self, *, reason: ModelSourceValidationReason, rejected: bool) -> None:
        super().__init__("model source credential not saved")
        self.reason = reason
        self.rejected = rejected


@dataclass(frozen=True, slots=True)
class ModelSourcePresentation:
    """Safe outward projection with no database ID or cryptographic fields."""

    source_type: ModelSourceType
    configuration_state: ModelSourceConfigurationState
    credential_last4: str | None
    verification_status: ModelSourceValidationStatus | None
    verified_at: datetime | None
    verification_reason_code: ModelSourceValidationReason | None
    next_action: ModelSourceValidationAction | None


_ACTIONS_BY_REASON = {
    ModelSourceValidationReason.CREDENTIAL_VALID: None,
    ModelSourceValidationReason.CREDENTIAL_INVALID: (ModelSourceValidationAction.UPDATE_CREDENTIAL),
    ModelSourceValidationReason.SOURCE_PERMISSION_DENIED: (
        ModelSourceValidationAction.CHECK_SOURCE_ACCESS
    ),
    ModelSourceValidationReason.SOURCE_NOT_FOUND: (
        ModelSourceValidationAction.CHECK_SOURCE_REFERENCE
    ),
    ModelSourceValidationReason.SOURCE_RATE_LIMITED: ModelSourceValidationAction.RETRY_LATER,
    ModelSourceValidationReason.VALIDATOR_UNAVAILABLE: (
        ModelSourceValidationAction.INSTALL_VALIDATOR
    ),
    ModelSourceValidationReason.VALIDATION_TIMEOUT: ModelSourceValidationAction.RETRY_LATER,
    ModelSourceValidationReason.VALIDATION_FAILED: ModelSourceValidationAction.RETRY_LATER,
}


class ModelSourceCredentialService:
    """Own the high-risk credential operation without retaining plaintext."""

    __slots__ = (
        "_audit_writer",
        "_authentication",
        "_keyring",
        "_session_factory",
        "_validators",
    )

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory,
        authentication: AuthenticationService,
        keyring: MasterKeyring,
        validators: ModelSourceValidatorRegistry | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._authentication = authentication
        self._keyring = keyring
        self._validators = ModelSourceValidatorRegistry() if validators is None else validators
        self._audit_writer = AuditWriter(session_factory)

    async def read_all(self) -> tuple[ModelSourcePresentation, ...]:
        """Return both official credential sources, including absent rows."""

        async with self._session_factory() as session:
            records = {
                record.source_type: record
                for record in await ModelSourceRepository(session).list_all()
            }
        return tuple(
            self._present(source_type, records.get(source_type)) for source_type in ModelSourceType
        )

    async def update(
        self,
        *,
        actor_id: UUID,
        session_token: str | None,
        csrf_token: str | None,
        current_password: str | None,
        source_type: ModelSourceType,
        credential: str | None,
        clear: bool,
        request_id: UUID,
        confirm_unverified: bool = False,
    ) -> ModelSourcePresentation:
        """Authenticate, validate once, and conditionally replace all envelope fields."""

        source_type = ModelSourceType(source_type)
        existing_id = await self._source_id(source_type)
        attempted_operation = (
            CredentialChangeOperation.CLEAR if clear else CredentialChangeOperation.REPLACE
        )
        try:
            authenticated = await self._authentication.reauthenticate_high_risk_operation(
                session_token=session_token,
                csrf_token=csrf_token,
                password=current_password,
            )
        except AuthenticationFailure as failure:
            await self._audit_writer.write(
                self._audit_event(
                    actor_id=actor_id,
                    object_id=existing_id,
                    source_type=source_type,
                    operation=attempted_operation,
                    result=AuditResult.DENIED,
                    request_id=request_id,
                    reason_code=failure.code,
                    validation_status=None,
                )
            )
            raise
        if authenticated.user.id != actor_id:
            raise RuntimeError("high-risk reauthentication identity changed")

        now: datetime
        async with self._session_factory() as session:
            repository = ModelSourceRepository(session)
            now = await database_utc_now(session)
            current = await repository.ensure(source_type=source_type, now=now)
            operation = (
                CredentialChangeOperation.CLEAR
                if clear
                else (
                    CredentialChangeOperation.WRITE
                    if current.credential is None
                    else CredentialChangeOperation.REPLACE
                )
            )

            validation: ModelSourceValidationResult | None = None
            replacement = current.credential
            if clear:
                if current.credential is not None:
                    changed = await repository.clear_credential(
                        current.id,
                        expected_credential=current.credential,
                        now=now,
                    )
                else:
                    changed = True
            else:
                if credential is None:
                    raise ValueError("credential is required unless clear is true")
                plaintext = credential.encode("utf-8")
                validation = await self._validators.validate(
                    source_type,
                    credential=plaintext,
                )
                rejected = validation.status in {
                    ModelSourceValidationStatus.INVALID_CREDENTIAL,
                    ModelSourceValidationStatus.PERMISSION_DENIED,
                    ModelSourceValidationStatus.NOT_FOUND,
                }
                if rejected or (
                    validation.status is not ModelSourceValidationStatus.ACCESSIBLE
                    and not confirm_unverified
                ):
                    # Roll back ensure() too: rejection must preserve even an absent row.
                    await session.rollback()
                    await self._audit_writer.write(
                        self._audit_event(
                            actor_id=actor_id,
                            object_id=existing_id,
                            source_type=source_type,
                            operation=operation,
                            result=AuditResult.FAILED,
                            request_id=request_id,
                            reason_code=validation.reason_code.value,
                            validation_status=validation.status,
                        )
                    )
                    raise ModelSourceCredentialNotSaved(
                        reason=validation.reason_code, rejected=rejected
                    )
                if validation.status is not ModelSourceValidationStatus.ACCESSIBLE:
                    validation = ModelSourceValidationResult(
                        status=None,
                        reason_code=validation.reason_code,
                        next_action=validation.next_action,
                    )
                if not self._matches(current, plaintext):
                    replacement = self._keyring.encrypt(
                        plaintext,
                        source_id=current.id,
                        credential_type=_CREDENTIAL_TYPE,
                    )
                changed = await repository.compare_and_swap_full_credential(
                    current.id,
                    expected_key_id=(
                        None if current.credential is None else current.credential.key_id
                    ),
                    expected_credential=current.credential,
                    match_full_envelope=True,
                    credential=replacement,
                    now=now,
                    update_validation=True,
                    verification_status=validation.status,
                    verification_reason_code=validation.reason_code.value,
                )

            if not changed:
                await session.rollback()
                await self._audit_writer.write(
                    self._audit_event(
                        actor_id=actor_id,
                        object_id=current.id,
                        source_type=source_type,
                        operation=operation,
                        result=AuditResult.FAILED,
                        request_id=request_id,
                        reason_code="credential_conflict",
                        validation_status=None,
                    )
                )
                raise ModelSourceCredentialConflict()

            updated = await repository.get(current.id)
            if updated is None:
                raise RuntimeError("updated model source disappeared")
            audit_result = (
                AuditResult.SUCCEEDED
                if clear
                or (
                    validation is not None
                    and validation.status is ModelSourceValidationStatus.ACCESSIBLE
                )
                else AuditResult.FAILED
            )
            reason_code = (
                "credential_cleared"
                if clear
                else (
                    "validation_result_missing"
                    if validation is None
                    else validation.reason_code.value
                )
            )
            await self._audit_writer.add(
                session,
                self._audit_event(
                    actor_id=actor_id,
                    object_id=current.id,
                    source_type=source_type,
                    operation=operation,
                    result=audit_result,
                    request_id=request_id,
                    reason_code=reason_code,
                    validation_status=(None if validation is None else validation.status),
                ),
            )
            await session.commit()
        return self._present(source_type, updated)

    async def _source_id(self, source_type: ModelSourceType) -> UUID | None:
        async with self._session_factory() as session:
            record = await ModelSourceRepository(session).get_by_type(source_type)
            return None if record is None else record.id

    def _matches(self, record: ModelSourceRecord, plaintext: bytes) -> bool:
        if record.credential is None or not self._keyring.can_decrypt(record.credential.key_id):
            return False
        try:
            current = self._keyring.decrypt(
                record.credential,
                source_id=record.id,
                credential_type=_CREDENTIAL_TYPE,
            )
        except CredentialDecryptionError:
            return False
        return hmac.compare_digest(current, plaintext)

    def _present(
        self,
        source_type: ModelSourceType,
        record: ModelSourceRecord | None,
    ) -> ModelSourcePresentation:
        if record is None or record.credential is None:
            configuration_state = ModelSourceConfigurationState.UNCONFIGURED
            last4 = None
        elif not self._keyring.can_decrypt(record.credential.key_id):
            configuration_state = ModelSourceConfigurationState.KEY_UNAVAILABLE
            last4 = None
        else:
            try:
                plaintext = self._keyring.decrypt(
                    record.credential,
                    source_id=record.id,
                    credential_type=_CREDENTIAL_TYPE,
                ).decode("utf-8")
            except (CredentialDecryptionError, UnicodeDecodeError):
                configuration_state = ModelSourceConfigurationState.KEY_UNAVAILABLE
                last4 = None
            else:
                configuration_state = ModelSourceConfigurationState.CONFIGURED
                last4 = plaintext[-4:] if len(plaintext) >= 8 else None

        persisted_reason = None if record is None else record.verification_reason_code
        reason = None if persisted_reason is None else ModelSourceValidationReason(persisted_reason)
        return ModelSourcePresentation(
            source_type=source_type,
            configuration_state=configuration_state,
            credential_last4=last4,
            verification_status=None if record is None else record.verification_status,
            verified_at=None if record is None else record.verified_at,
            verification_reason_code=reason,
            next_action=None if reason is None else _ACTIONS_BY_REASON[reason],
        )

    @staticmethod
    def _audit_event(
        *,
        actor_id: UUID,
        object_id: UUID | None,
        source_type: ModelSourceType,
        operation: CredentialChangeOperation,
        result: AuditResult,
        request_id: UUID,
        reason_code: str,
        validation_status: ModelSourceValidationStatus | None,
    ) -> AuditEventDraft:
        return AuditEventDraft(
            actor_type=AuditActorType.USER,
            actor_id=actor_id,
            action=AuditAction.MODEL_SOURCE_CREDENTIAL_CHANGE,
            object_type=AuditObjectType.MODEL_SOURCE,
            object_id=object_id,
            result=result,
            request_id=request_id,
            details=build_audit_details(
                AuditAction.MODEL_SOURCE_CREDENTIAL_CHANGE.value,
                {
                    "operation": operation.value,
                    "reason_code": reason_code,
                    "source_type": source_type.value,
                    "validation_status": (
                        None if validation_status is None else validation_status.value
                    ),
                },
            ),
        )


__all__ = [
    "CredentialChangeOperation",
    "ModelSourceCredentialConflict",
    "ModelSourceCredentialService",
    "ModelSourcePresentation",
]

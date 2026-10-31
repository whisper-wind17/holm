"""Personal API Key creation, safe listing, revocation, and audit."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from holm_common import as_utc
from holm_domain import (
    ApiKeyStatus,
    AuditAction,
    AuditActorType,
    AuditObjectType,
    AuditResult,
    DomainEventType,
    UserRole,
)
from holm_persistence import (
    AiServiceRepository,
    ApiKeyCreate,
    ApiKeyRecord,
    ApiKeyRepository,
    AsyncSessionFactory,
    AuditEventDraft,
    AuditWriter,
    DuplicateApiKeyLookupError,
    UserManagementRepository,
    append_route_invalidation_event,
    database_utc_now,
)
from holm_persistence.ai_service_repository import PublicAiServiceRecord
from holm_security import ApiKeyPepperKeyring, IssuedApiKey, build_audit_details, issue_api_key

MAX_API_KEY_ISSUANCE_ATTEMPTS = 3


class ApiKeyNotFound(LookupError):
    """The requested API Key is absent or belongs to another user."""


class ApiKeyActiveDeletion(ValueError):
    """An effective Active key must be revoked before hiding its display record."""


class ApiKeyExpirationInvalid(ValueError):
    """The requested expiration is not later than PostgreSQL's current time."""


class ApiKeyScopeServiceNotFound(LookupError):
    """At least one requested service scope does not exist."""


class ApiKeyScopeNotAuthorized(PermissionError):
    """A user requested a service outside their current grants."""


@dataclass(frozen=True, slots=True)
class CreatePersonalApiKey:
    name: str
    expires_at: datetime | None
    service_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class CreatedPersonalApiKey:
    record: ApiKeyRecord
    plaintext: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ListedPersonalApiKey:
    record: ApiKeyRecord
    effective_services: tuple[PublicAiServiceRecord, ...]


class ApiKeyManagementService:
    """Ensure plaintext exists only between issuance and the create response."""

    __slots__ = ("_audit_writer", "_keyring", "_session_factory")

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory,
        keyring: ApiKeyPepperKeyring,
    ) -> None:
        self._session_factory = session_factory
        self._keyring = keyring
        self._audit_writer = AuditWriter(session_factory)

    @property
    def session_factory(self) -> AsyncSessionFactory:
        """Share the already configured factory with adjacent account routes."""

        return self._session_factory

    async def list(self, user_id: UUID) -> tuple[ApiKeyRecord, ...]:
        async with self._session_factory() as session:
            return await ApiKeyRepository(session).list_owned(user_id, exclude_deleted=True)

    async def list_with_effective_services(
        self, user_id: UUID, *, user_role: UserRole
    ) -> tuple[ListedPersonalApiKey, ...]:
        async with self._session_factory() as session:
            keys = await ApiKeyRepository(session).list_owned(user_id, exclude_deleted=True)
            services = await AiServiceRepository(session).enabled_public_services(
                user_id=user_id, user_role=user_role
            )
            return tuple(
                ListedPersonalApiKey(
                    record=key,
                    effective_services=tuple(
                        service
                        for service in services
                        if not key.service_ids or service.id in key.service_ids
                    ),
                )
                for key in keys
            )

    async def database_now(self) -> datetime:
        async with self._session_factory() as session:
            return await database_utc_now(session)

    async def create(
        self,
        value: CreatePersonalApiKey,
        *,
        user_id: UUID,
        user_role: UserRole,
        request_id: UUID,
    ) -> CreatedPersonalApiKey:
        service_ids = tuple(sorted(set(value.service_ids)))
        async with self._session_factory() as session, session.begin():
            now = await database_utc_now(session)
            expires_at = None if value.expires_at is None else as_utc(value.expires_at)
            if expires_at is not None and expires_at <= now:
                raise ApiKeyExpirationInvalid()
            existing = await AiServiceRepository(session).existing_service_ids(service_ids)
            if existing != frozenset(service_ids):
                raise ApiKeyScopeServiceNotFound()
            if service_ids:
                authorized = await UserManagementRepository(session).authorized_service_ids(user_id)
                if not frozenset(service_ids).issubset(authorized):
                    raise ApiKeyScopeNotAuthorized()

            issued: IssuedApiKey | None = None
            record: ApiKeyRecord | None = None
            repository = ApiKeyRepository(session)
            for _ in range(MAX_API_KEY_ISSUANCE_ATTEMPTS):
                candidate = issue_api_key(self._keyring)
                try:
                    async with session.begin_nested():
                        created = await repository.create(
                            ApiKeyCreate(
                                user_id=user_id,
                                name=value.name,
                                lookup_id=candidate.lookup_id,
                                display_prefix=candidate.display_prefix,
                                last4=candidate.last4,
                                pepper_key_id=candidate.pepper_key_id,
                                secret_hmac=candidate.secret_hmac,
                                expires_at=expires_at,
                                service_ids=service_ids,
                            ),
                            now=now,
                        )
                except DuplicateApiKeyLookupError:
                    continue
                issued = candidate
                record = created
                break
            if issued is None or record is None:
                raise RuntimeError("API Key issuance collision limit reached")
            await self._audit_writer.add(
                session,
                self._audit_event(
                    actor_id=user_id,
                    object_id=record.id,
                    request_id=request_id,
                    operation="create",
                ),
            )
            await append_route_invalidation_event(
                session,
                event=DomainEventType.API_KEY_CHANGED,
                aggregate_id=record.id,
            )
            return CreatedPersonalApiKey(record=record, plaintext=issued.plaintext)

    async def rename(
        self, key_id: UUID, *, name: str, user_id: UUID, request_id: UUID
    ) -> ApiKeyRecord:
        async with self._session_factory() as session, session.begin():
            repository = ApiKeyRepository(session)
            current = await repository.get_owned(key_id, user_id=user_id, for_update=True)
            if current is None or current.deleted_at is not None:
                raise ApiKeyNotFound()
            record = await repository.rename(key_id, name=name)
            await self._audit_writer.add(
                session,
                self._audit_event(
                    actor_id=user_id, object_id=key_id, request_id=request_id, operation="rename"
                ),
            )
            return record

    async def delete(self, key_id: UUID, *, user_id: UUID, request_id: UUID) -> ApiKeyRecord:
        async with self._session_factory() as session, session.begin():
            repository = ApiKeyRepository(session)
            current = await repository.get_owned(key_id, user_id=user_id, for_update=True)
            if current is None:
                raise ApiKeyNotFound()
            now = await database_utc_now(session)
            if current.effective_status_at(now) is ApiKeyStatus.ACTIVE:
                raise ApiKeyActiveDeletion()
            record = (
                current
                if current.deleted_at is not None
                else await repository.hide(key_id, now=now)
            )
            await self._audit_writer.add(
                session,
                self._audit_event(
                    actor_id=user_id, object_id=key_id, request_id=request_id, operation="delete"
                ),
            )
            return record

    async def revoke(
        self,
        key_id: UUID,
        *,
        user_id: UUID,
        request_id: UUID,
    ) -> ApiKeyRecord:
        async with self._session_factory() as session, session.begin():
            repository = ApiKeyRepository(session)
            current = await repository.get_owned(key_id, user_id=user_id, for_update=True)
            if current is None:
                raise ApiKeyNotFound()
            record = current
            if current.revoked_at is None:
                record = await repository.revoke(
                    key_id,
                    now=await database_utc_now(session),
                )
                await append_route_invalidation_event(
                    session,
                    event=DomainEventType.API_KEY_CHANGED,
                    aggregate_id=record.id,
                )
            await self._audit_writer.add(
                session,
                self._audit_event(
                    actor_id=user_id,
                    object_id=record.id,
                    request_id=request_id,
                    operation="revoke",
                ),
            )
            return record

    @staticmethod
    def _audit_event(
        *, actor_id: UUID, object_id: UUID, request_id: UUID, operation: str
    ) -> AuditEventDraft:
        return AuditEventDraft(
            actor_type=AuditActorType.USER,
            actor_id=actor_id,
            action=AuditAction.API_KEY_CHANGE,
            object_type=AuditObjectType.API_KEY,
            object_id=object_id,
            result=AuditResult.SUCCEEDED,
            request_id=request_id,
            details=build_audit_details(AuditAction.API_KEY_CHANGE.value, {"operation": operation}),
        )


__all__ = [
    "ApiKeyActiveDeletion",
    "ApiKeyExpirationInvalid",
    "ApiKeyManagementService",
    "ApiKeyNotFound",
    "ApiKeyScopeServiceNotFound",
    "ApiKeyScopeNotAuthorized",
    "CreatePersonalApiKey",
    "CreatedPersonalApiKey",
]

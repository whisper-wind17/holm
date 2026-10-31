"""Transactional persistence for AI Services, API Keys, and validation snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from holm_common import as_utc, new_uuid7
from holm_domain import ApiKeyStatus, ModelInstanceStatus, UserRole, UserStatus
from holm_security import ApiKeyValidationSnapshot
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from holm_persistence.accounts import USERS
from holm_persistence.ai_services import (
    AI_SERVICE_BACKENDS,
    AI_SERVICES,
    API_KEY_SERVICE_SCOPES,
    API_KEYS,
    USER_SERVICE_PERMISSIONS,
)
from holm_persistence.database import AsyncSessionFactory
from holm_persistence.model_instances import MODEL_INSTANCES

ACTIVE_SERVICE_NAME_INDEX = "uq_ai_services_active_public_model_name"
API_KEY_LOOKUP_CONSTRAINT = "uq_api_keys_lookup_id"


class DuplicateAiServiceNameError(RuntimeError):
    """The public model name is already used by a non-deleted AI Service."""


class DuplicateAiServiceDisplayNameError(RuntimeError):
    """The display name is already used by an AI Service."""


class DuplicateApiKeyLookupError(RuntimeError):
    """A generated Lookup ID collided and issuance must draw another."""


@dataclass(frozen=True, slots=True)
class AiServiceCreate:
    public_model_name: str
    description: str
    enabled: bool
    primary_instance_id: UUID
    name: str | None = None


@dataclass(frozen=True, slots=True)
class AiServiceRecord:
    id: UUID
    name: str
    public_model_name: str
    description: str
    enabled: bool
    primary_instance_id: UUID
    route_version: int
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


@dataclass(frozen=True, slots=True)
class PublicAiServiceRecord:
    """Safe service identity; independent of backend readiness."""

    id: UUID
    name: str
    public_model_name: str


@dataclass(frozen=True, slots=True)
class AiServiceBackendTarget:
    id: UUID
    instance_name: str
    runtime_name: str
    status: ModelInstanceStatus


@dataclass(frozen=True, slots=True)
class ApiKeyCreate:
    user_id: UUID
    name: str
    lookup_id: str
    display_prefix: str
    last4: str
    pepper_key_id: str
    secret_hmac: bytes = field(repr=False)
    expires_at: datetime | None
    service_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class ApiKeyRecord:
    id: UUID
    user_id: UUID
    name: str
    lookup_id: str
    display_prefix: str
    last4: str
    pepper_key_id: str
    status: ApiKeyStatus
    expires_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime
    revoked_at: datetime | None
    service_ids: tuple[UUID, ...]
    secret_hmac: bytes = field(repr=False)
    deleted_at: datetime | None = None

    def effective_status_at(self, now: datetime) -> ApiKeyStatus:
        if (
            self.status is ApiKeyStatus.ACTIVE
            and self.expires_at is not None
            and as_utc(now) >= self.expires_at
        ):
            return ApiKeyStatus.EXPIRED
        return self.status


def _ai_service_record(
    mapping: sa.RowMapping,
    *,
    primary_instance_id: UUID | None = None,
) -> AiServiceRecord:
    deleted_at = mapping["deleted_at"]
    return AiServiceRecord(
        id=mapping["id"],
        name=str(mapping["name"]),
        public_model_name=str(mapping["public_model_name"]),
        description=str(mapping["description"]),
        enabled=bool(mapping["enabled"]),
        primary_instance_id=(
            mapping["primary_instance_id"] if primary_instance_id is None else primary_instance_id
        ),
        route_version=int(mapping["route_version"]),
        created_at=as_utc(mapping["created_at"]),
        updated_at=as_utc(mapping["updated_at"]),
        deleted_at=None if deleted_at is None else as_utc(deleted_at),
    )


def _api_key_record(mapping: sa.RowMapping, service_ids: tuple[UUID, ...]) -> ApiKeyRecord:
    expires_at = mapping["expires_at"]
    last_used_at = mapping["last_used_at"]
    revoked_at = mapping["revoked_at"]
    return ApiKeyRecord(
        id=mapping["id"],
        user_id=mapping["user_id"],
        name=str(mapping["name"]),
        lookup_id=str(mapping["lookup_id"]),
        display_prefix=str(mapping["display_prefix"]),
        last4=str(mapping["last4"]),
        pepper_key_id=str(mapping["pepper_key_id"]),
        secret_hmac=bytes(mapping["secret_hmac"]),
        status=ApiKeyStatus(mapping["status"]),
        expires_at=None if expires_at is None else as_utc(expires_at),
        last_used_at=None if last_used_at is None else as_utc(last_used_at),
        created_at=as_utc(mapping["created_at"]),
        revoked_at=None if revoked_at is None else as_utc(revoked_at),
        service_ids=service_ids,
        deleted_at=None if mapping["deleted_at"] is None else as_utc(mapping["deleted_at"]),
    )


def _constraint_name(error: IntegrityError) -> str | None:
    diagnostic = getattr(getattr(error, "orig", None), "diag", None)
    return getattr(diagnostic, "constraint_name", None)


class AiServiceRepository:
    """Keep service binding changes and Route Version in one transaction."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def instance_for_share(self, instance_id: UUID) -> AiServiceBackendTarget | None:
        statement = (
            sa.select(
                MODEL_INSTANCES.c.id,
                MODEL_INSTANCES.c.name,
                MODEL_INSTANCES.c.runtime_type,
                MODEL_INSTANCES.c.status,
            )
            .where(MODEL_INSTANCES.c.id == instance_id)
            .with_for_update(read=True)
        )
        mapping = (await self._session.execute(statement)).mappings().one_or_none()
        if mapping is None:
            return None
        return AiServiceBackendTarget(
            id=mapping["id"],
            instance_name=str(mapping["name"]),
            runtime_name=str(mapping["runtime_type"]),
            status=ModelInstanceStatus(mapping["status"]),
        )

    async def _insert_primary_backend(
        self,
        *,
        service_id: UUID,
        target: AiServiceBackendTarget,
        now: datetime,
    ) -> None:
        await self._session.execute(
            sa.insert(AI_SERVICE_BACKENDS).values(
                id=new_uuid7(),
                service_id=service_id,
                instance_id=target.id,
                instance_name_snapshot=target.instance_name,
                runtime_snapshot=target.runtime_name,
                backend_key="primary",
                weight=100,
                enabled=True,
                health_gate_passed=target.status is ModelInstanceStatus.RUNNING,
                joined_at=as_utc(now),
                retired_at=None,
            )
        )

    async def create(self, value: AiServiceCreate, *, now: datetime) -> AiServiceRecord:
        target = await self.instance_for_share(value.primary_instance_id)
        if target is None:
            raise LookupError("AI Service backend target does not exist")
        service_id = new_uuid7()
        fallback_name = value.public_model_name.strip()[:7] + "-" + service_id.hex
        name = value.name
        if name is None:
            candidate = value.public_model_name.strip()
            occupied = await self._session.scalar(
                sa.select(AI_SERVICES.c.id)
                .where(AI_SERVICES.c.name == candidate, AI_SERVICES.c.deleted_at.is_(None))
                .limit(1)
            )
            name = candidate if 2 <= len(candidate) <= 40 and occupied is None else fallback_name
        statement = (
            sa.insert(AI_SERVICES)
            .values(
                id=service_id,
                name=name,
                public_model_name=value.public_model_name,
                description=value.description,
                enabled=value.enabled,
                route_version=1,
                created_at=as_utc(now),
                updated_at=as_utc(now),
                deleted_at=None,
            )
            .returning(*AI_SERVICES.c)
        )
        try:
            try:
                async with self._session.begin_nested():
                    mapping = (await self._session.execute(statement)).mappings().one()
            except IntegrityError as error:
                if (
                    value.name is not None
                    or name == fallback_name
                    or _constraint_name(error) != "uq_ai_services_name"
                ):
                    raise
                # Roll back only the collided insert; allow exactly one fallback attempt.
                mapping = (
                    (await self._session.execute(statement.values(name=fallback_name)))
                    .mappings()
                    .one()
                )
        except IntegrityError as error:
            if _constraint_name(error) == "uq_ai_services_name":
                raise DuplicateAiServiceDisplayNameError() from None
            if _constraint_name(error) == ACTIVE_SERVICE_NAME_INDEX:
                raise DuplicateAiServiceNameError() from None
            raise
        await self._insert_primary_backend(service_id=service_id, target=target, now=now)
        return _ai_service_record(mapping, primary_instance_id=target.id)

    @staticmethod
    def _active_primary_join() -> sa.Join:
        return AI_SERVICES.join(
            AI_SERVICE_BACKENDS,
            sa.and_(
                AI_SERVICE_BACKENDS.c.service_id == AI_SERVICES.c.id,
                AI_SERVICE_BACKENDS.c.backend_key == "primary",
                AI_SERVICE_BACKENDS.c.retired_at.is_(None),
            ),
        )

    @staticmethod
    def _service_columns() -> tuple[sa.ColumnElement[object], ...]:
        return (*AI_SERVICES.c, AI_SERVICE_BACKENDS.c.instance_id.label("primary_instance_id"))

    async def get(self, service_id: UUID, *, for_update: bool = False) -> AiServiceRecord | None:
        statement = (
            sa.select(*self._service_columns())
            .select_from(self._active_primary_join())
            .where(
                AI_SERVICES.c.id == service_id,
                AI_SERVICES.c.deleted_at.is_(None),
            )
        )
        if for_update:
            statement = statement.with_for_update(of=AI_SERVICES)
        mapping = (await self._session.execute(statement)).mappings().one_or_none()
        return None if mapping is None else _ai_service_record(mapping)

    async def list(self) -> tuple[AiServiceRecord, ...]:
        statement = (
            sa.select(*self._service_columns())
            .select_from(self._active_primary_join())
            .where(AI_SERVICES.c.deleted_at.is_(None))
            .order_by(AI_SERVICES.c.id)
        )
        return tuple(
            _ai_service_record(mapping)
            for mapping in (await self._session.execute(statement)).mappings()
        )

    async def enabled_public_services(
        self, *, user_id: UUID, user_role: UserRole
    ) -> tuple[PublicAiServiceRecord, ...]:
        """Read the permission range without joining transient backend state."""
        statement = sa.select(
            AI_SERVICES.c.id, AI_SERVICES.c.name, AI_SERVICES.c.public_model_name
        ).where(AI_SERVICES.c.deleted_at.is_(None), AI_SERVICES.c.enabled.is_(True))
        statement = statement.where(
            sa.exists().where(
                USER_SERVICE_PERMISSIONS.c.user_id == user_id,
                USER_SERVICE_PERMISSIONS.c.service_id == AI_SERVICES.c.id,
            )
        )
        return tuple(
            PublicAiServiceRecord(id=row.id, name=row.name, public_model_name=row.public_model_name)
            for row in await self._session.execute(
                statement.order_by(AI_SERVICES.c.public_model_name, AI_SERVICES.c.id)
            )
        )

    async def update(
        self,
        service_id: UUID,
        *,
        description: str,
        enabled: bool,
        primary_instance_id: UUID,
        route_version: int,
        now: datetime,
    ) -> AiServiceRecord:
        statement = (
            sa.update(AI_SERVICES)
            .where(AI_SERVICES.c.id == service_id, AI_SERVICES.c.deleted_at.is_(None))
            .values(
                description=description,
                enabled=enabled,
                route_version=route_version,
                updated_at=as_utc(now),
            )
            .returning(*AI_SERVICES.c)
        )
        mapping = (await self._session.execute(statement)).mappings().one()
        return _ai_service_record(mapping, primary_instance_id=primary_instance_id)

    async def rename(
        self,
        current: AiServiceRecord,
        *,
        public_model_name: str,
        now: datetime,
    ) -> AiServiceRecord:
        # Legacy names may contain uppercase letters. Preserve them, but prevent a
        # new lowercase name from shadowing their case-insensitive identity.
        conflict = await self._session.scalar(
            sa.select(AI_SERVICES.c.id)
            .where(
                AI_SERVICES.c.id != current.id,
                AI_SERVICES.c.deleted_at.is_(None),
                sa.func.lower(AI_SERVICES.c.public_model_name) == public_model_name,
            )
            .limit(1)
        )
        if conflict is not None:
            raise DuplicateAiServiceNameError()
        statement = (
            sa.update(AI_SERVICES)
            .where(AI_SERVICES.c.id == current.id, AI_SERVICES.c.deleted_at.is_(None))
            .values(
                public_model_name=public_model_name,
                route_version=current.route_version + 1,
                updated_at=as_utc(now),
            )
            .returning(*AI_SERVICES.c)
        )
        try:
            mapping = (await self._session.execute(statement)).mappings().one()
        except IntegrityError as error:
            if _constraint_name(error) == ACTIVE_SERVICE_NAME_INDEX:
                raise DuplicateAiServiceNameError() from None
            raise
        return _ai_service_record(mapping, primary_instance_id=current.primary_instance_id)

    async def rename_display_name(
        self, current: AiServiceRecord, *, name: str, now: datetime
    ) -> AiServiceRecord:
        try:
            mapping = (
                (
                    await self._session.execute(
                        sa.update(AI_SERVICES)
                        .where(AI_SERVICES.c.id == current.id)
                        .values(
                            name=name,
                            route_version=current.route_version + 1,
                            updated_at=as_utc(now),
                        )
                        .returning(*AI_SERVICES.c)
                    )
                )
                .mappings()
                .one()
            )
        except IntegrityError as error:
            if _constraint_name(error) == "uq_ai_services_name":
                raise DuplicateAiServiceDisplayNameError() from None
            raise
        return _ai_service_record(mapping, primary_instance_id=current.primary_instance_id)

    async def rebind_primary(
        self,
        service_id: UUID,
        *,
        target: AiServiceBackendTarget,
        now: datetime,
    ) -> None:
        retired_ids = tuple(
            (
                await self._session.execute(
                    sa.update(AI_SERVICE_BACKENDS)
                    .where(
                        AI_SERVICE_BACKENDS.c.service_id == service_id,
                        AI_SERVICE_BACKENDS.c.backend_key == "primary",
                        AI_SERVICE_BACKENDS.c.retired_at.is_(None),
                    )
                    .values(retired_at=as_utc(now))
                    .returning(AI_SERVICE_BACKENDS.c.id)
                )
            ).scalars()
        )
        if len(retired_ids) != 1:
            raise RuntimeError("AI Service must have exactly one active Primary Backend")
        await self._insert_primary_backend(service_id=service_id, target=target, now=now)

    async def soft_delete_bound_to_instance(
        self, instance_id: UUID, *, now: datetime
    ) -> tuple[UUID, ...]:
        service_ids = tuple(
            (
                await self._session.execute(
                    sa.select(AI_SERVICES.c.id)
                    .where(
                        AI_SERVICES.c.deleted_at.is_(None),
                        AI_SERVICES.c.id.in_(
                            sa.select(AI_SERVICE_BACKENDS.c.service_id).where(
                                AI_SERVICE_BACKENDS.c.instance_id == instance_id,
                                AI_SERVICE_BACKENDS.c.retired_at.is_(None),
                            )
                        ),
                    )
                    .order_by(AI_SERVICES.c.id)
                    .with_for_update()
                )
            ).scalars()
        )
        return await self.soft_delete(service_ids, now=now)

    async def soft_delete(
        self, service_ids: tuple[UUID, ...], *, now: datetime
    ) -> tuple[UUID, ...]:
        """Logically delete services under row locks; keep scopes and permissions intact."""
        if not service_ids:
            return ()
        service_ids = tuple(
            (
                await self._session.execute(
                    sa.select(AI_SERVICES.c.id)
                    .where(AI_SERVICES.c.id.in_(service_ids), AI_SERVICES.c.deleted_at.is_(None))
                    .order_by(AI_SERVICES.c.id)
                    .with_for_update()
                )
            ).scalars()
        )
        if not service_ids:
            return ()
        await self._session.execute(
            sa.update(AI_SERVICES)
            .where(AI_SERVICES.c.id.in_(service_ids))
            .values(
                deleted_at=as_utc(now),
                updated_at=as_utc(now),
                route_version=AI_SERVICES.c.route_version + 1,
            )
        )
        await self._session.execute(
            sa.update(AI_SERVICE_BACKENDS)
            .where(
                AI_SERVICE_BACKENDS.c.service_id.in_(service_ids),
                AI_SERVICE_BACKENDS.c.retired_at.is_(None),
            )
            .values(retired_at=as_utc(now))
        )
        # Scope rows must survive: removing the last row would make a key unrestricted.
        # Permission rows and the service identity also retain historical attribution.
        return service_ids

    async def existing_service_ids(self, service_ids: tuple[UUID, ...]) -> frozenset[UUID]:
        if not service_ids:
            return frozenset()
        values = (
            await self._session.execute(
                sa.select(AI_SERVICES.c.id).where(
                    AI_SERVICES.c.id.in_(service_ids),
                    AI_SERVICES.c.deleted_at.is_(None),
                )
            )
        ).scalars()
        return frozenset(values)


class ApiKeyRepository:
    """Persist digests and safe display fields; plaintext is not accepted."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, value: ApiKeyCreate, *, now: datetime) -> ApiKeyRecord:
        key_id = new_uuid7()
        statement = (
            sa.insert(API_KEYS)
            .values(
                id=key_id,
                user_id=value.user_id,
                name=value.name,
                lookup_id=value.lookup_id,
                display_prefix=value.display_prefix,
                last4=value.last4,
                pepper_key_id=value.pepper_key_id,
                secret_hmac=value.secret_hmac,
                status=ApiKeyStatus.ACTIVE.value,
                expires_at=None if value.expires_at is None else as_utc(value.expires_at),
                last_used_at=None,
                created_at=as_utc(now),
                revoked_at=None,
            )
            .returning(*API_KEYS.c)
        )
        try:
            mapping = (await self._session.execute(statement)).mappings().one()
        except IntegrityError as error:
            if _constraint_name(error) == API_KEY_LOOKUP_CONSTRAINT:
                raise DuplicateApiKeyLookupError() from None
            raise
        if value.service_ids:
            await self._session.execute(
                sa.insert(API_KEY_SERVICE_SCOPES),
                [{"key_id": key_id, "service_id": service_id} for service_id in value.service_ids],
            )
        return _api_key_record(mapping, value.service_ids)

    async def _service_ids(self, key_id: UUID) -> tuple[UUID, ...]:
        values = (
            await self._session.execute(
                sa.select(API_KEY_SERVICE_SCOPES.c.service_id)
                .where(API_KEY_SERVICE_SCOPES.c.key_id == key_id)
                .order_by(API_KEY_SERVICE_SCOPES.c.service_id)
            )
        ).scalars()
        return tuple(values)

    async def get_owned(
        self, key_id: UUID, *, user_id: UUID, for_update: bool = False
    ) -> ApiKeyRecord | None:
        statement = sa.select(API_KEYS).where(
            API_KEYS.c.id == key_id,
            API_KEYS.c.user_id == user_id,
        )
        if for_update:
            statement = statement.with_for_update()
        mapping = (await self._session.execute(statement)).mappings().one_or_none()
        if mapping is None:
            return None
        return _api_key_record(mapping, await self._service_ids(key_id))

    async def list_owned(
        self, user_id: UUID, *, exclude_deleted: bool = False
    ) -> tuple[ApiKeyRecord, ...]:
        # Security operations need every historical key, including hidden expired keys.
        statement = sa.select(API_KEYS).where(API_KEYS.c.user_id == user_id)
        if exclude_deleted:
            statement = statement.where(API_KEYS.c.deleted_at.is_(None))
        mappings = tuple(
            (await self._session.execute(statement.order_by(API_KEYS.c.id))).mappings()
        )
        if not mappings:
            return ()
        scopes: dict[UUID, list[UUID]] = {mapping["id"]: [] for mapping in mappings}
        for key_id, service_id in await self._session.execute(
            sa.select(API_KEY_SERVICE_SCOPES.c.key_id, API_KEY_SERVICE_SCOPES.c.service_id)
            .where(API_KEY_SERVICE_SCOPES.c.key_id.in_(scopes))
            .order_by(API_KEY_SERVICE_SCOPES.c.service_id)
        ):
            scopes[key_id].append(service_id)
        return tuple(_api_key_record(mapping, tuple(scopes[mapping["id"]])) for mapping in mappings)

    async def rename(self, key_id: UUID, *, name: str) -> ApiKeyRecord:
        mapping = (
            (
                await self._session.execute(
                    sa.update(API_KEYS)
                    .where(API_KEYS.c.id == key_id)
                    .values(name=name)
                    .returning(*API_KEYS.c)
                )
            )
            .mappings()
            .one()
        )
        return _api_key_record(mapping, await self._service_ids(key_id))

    async def hide(self, key_id: UUID, *, now: datetime) -> ApiKeyRecord:
        mapping = (
            (
                await self._session.execute(
                    sa.update(API_KEYS)
                    .where(API_KEYS.c.id == key_id)
                    .values(deleted_at=as_utc(now))
                    .returning(*API_KEYS.c)
                )
            )
            .mappings()
            .one()
        )
        return _api_key_record(mapping, await self._service_ids(key_id))

    async def revoke(self, key_id: UUID, *, now: datetime) -> ApiKeyRecord:
        statement = (
            sa.update(API_KEYS)
            .where(API_KEYS.c.id == key_id)
            .values(
                status=ApiKeyStatus.REVOKED.value,
                revoked_at=as_utc(now),
            )
            .returning(*API_KEYS.c)
        )
        mapping = (await self._session.execute(statement)).mappings().one()
        return _api_key_record(mapping, await self._service_ids(key_id))

    async def revoke_all_for_user(self, user_id: UUID, *, now: datetime) -> int:
        statement = (
            sa.update(API_KEYS)
            .where(
                API_KEYS.c.user_id == user_id,
                API_KEYS.c.status != ApiKeyStatus.REVOKED.value,
            )
            .values(status=ApiKeyStatus.REVOKED.value, revoked_at=as_utc(now))
            .returning(API_KEYS.c.id)
        )
        return len((await self._session.execute(statement)).all())


class PostgresApiKeyValidationLookup:
    """Load all Key-validation facts using PostgreSQL's own clock."""

    __slots__ = ("_session_factory",)

    def __init__(self, session_factory: AsyncSessionFactory) -> None:
        self._session_factory = session_factory

    async def __call__(self, lookup_id: str) -> ApiKeyValidationSnapshot | None:
        async with self._session_factory() as session:
            scoped_service_ids = (
                sa.select(sa.func.array_agg(API_KEY_SERVICE_SCOPES.c.service_id))
                .where(API_KEY_SERVICE_SCOPES.c.key_id == API_KEYS.c.id)
                .correlate(API_KEYS)
                .scalar_subquery()
            )
            service_limits = (
                sa.select(
                    sa.func.jsonb_object_agg(
                        sa.cast(USER_SERVICE_PERMISSIONS.c.service_id, sa.Text),
                        sa.func.jsonb_build_array(
                            USER_SERVICE_PERMISSIONS.c.max_concurrent_requests,
                            USER_SERVICE_PERMISSIONS.c.max_requests_per_minute,
                        ),
                    )
                )
                .where(USER_SERVICE_PERMISSIONS.c.user_id == USERS.c.id)
                .correlate(USERS)
                .scalar_subquery()
            )
            authorized_service_ids = (
                sa.select(sa.func.array_agg(USER_SERVICE_PERMISSIONS.c.service_id))
                .where(USER_SERVICE_PERMISSIONS.c.user_id == USERS.c.id)
                .correlate(USERS)
                .scalar_subquery()
            )
            row = (
                (
                    await session.execute(
                        sa.select(
                            API_KEYS.c.id,
                            API_KEYS.c.user_id,
                            API_KEYS.c.pepper_key_id,
                            API_KEYS.c.secret_hmac,
                            API_KEYS.c.status,
                            API_KEYS.c.expires_at,
                            USERS.c.status.label("user_status"),
                            USERS.c.role.label("user_role"),
                            scoped_service_ids.label("scoped_service_ids"),
                            authorized_service_ids.label("authorized_service_ids"),
                            service_limits.label("service_limits"),
                            sa.func.clock_timestamp().label("database_now"),
                        )
                        .select_from(API_KEYS.join(USERS, API_KEYS.c.user_id == USERS.c.id))
                        .where(API_KEYS.c.lookup_id == lookup_id)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
        status = ApiKeyStatus(row["status"])
        expires_at = row["expires_at"]
        return ApiKeyValidationSnapshot(
            user_id=row["user_id"],
            api_key_id=row["id"],
            pepper_key_id=str(row["pepper_key_id"]),
            secret_hmac=bytes(row["secret_hmac"]),
            revoked=status is ApiKeyStatus.REVOKED,
            expired_status=status is ApiKeyStatus.EXPIRED,
            expires_at=None if expires_at is None else as_utc(expires_at),
            user_active=UserStatus(row["user_status"]) is UserStatus.ACTIVE,
            user_is_admin=UserRole(row["user_role"]) is UserRole.ADMIN,
            scoped_service_ids=frozenset(row["scoped_service_ids"] or ()),
            authorized_service_ids=frozenset(row["authorized_service_ids"] or ()),
            service_limits={
                UUID(key): (value[0], value[1])
                for key, value in (row["service_limits"] or {}).items()
            },
            database_now=as_utc(row["database_now"]),
        )


__all__ = [
    "ACTIVE_SERVICE_NAME_INDEX",
    "API_KEY_LOOKUP_CONSTRAINT",
    "AiServiceCreate",
    "AiServiceRecord",
    "AiServiceRepository",
    "ApiKeyCreate",
    "ApiKeyRecord",
    "ApiKeyRepository",
    "DuplicateAiServiceNameError",
    "DuplicateApiKeyLookupError",
    "PostgresApiKeyValidationLookup",
    "PublicAiServiceRecord",
]

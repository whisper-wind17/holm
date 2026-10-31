"""Administrator account lifecycle and shared AI Service authorization."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from holm_common.usernames import normalize_account_name, normalize_display_name
from holm_domain import (
    AuditAction,
    AuditActorType,
    AuditObjectType,
    AuditResult,
    DomainEventType,
    UserRole,
    UserStatus,
)
from holm_persistence import (
    AiServiceRecord,
    AiServiceRepository,
    ApiKeyRecord,
    ApiKeyRepository,
    AsyncSessionFactory,
    AuditEventDraft,
    AuditRepository,
    AuditWriter,
    SessionRepository,
    UserManagementRepository,
    UserOverviewRecord,
    UserRepository,
    append_route_invalidation_event,
    database_utc_now,
)
from holm_security import (
    PasswordHasher,
    TemporaryPassword,
    build_audit_details,
    issue_temporary_password,
)

from holm_control_api.api_key_service import ApiKeyNotFound

USER_ACTIVITY_ACTIONS = (
    AuditAction.LOGIN.value,
    AuditAction.PASSWORD_CHANGE.value,
    AuditAction.ACCOUNT_CHANGE.value,
    AuditAction.PERMISSION_CHANGE.value,
    AuditAction.API_KEY_CHANGE.value,
)


def _activity_cursor_decode(cursor: str) -> tuple[datetime, UUID]:
    try:
        if len(cursor) > 1024:
            raise ValueError
        value = json.loads(
            base64.b64decode(
                cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
            ).decode("utf-8")
        )
        if not isinstance(value, dict) or set(value) != {"v", "k", "t", "i"}:
            raise ValueError
        if value["v"] != 1 or value["k"] != "user_activity":
            raise ValueError
        if not isinstance(value["t"], str) or not isinstance(value["i"], str):
            raise ValueError
        moment = datetime.fromisoformat(value["t"])
        if moment.tzinfo is None:
            raise ValueError
        return moment, UUID(value["i"])
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise UserInputInvalid() from None


def _activity_cursor_encode(moment: datetime, row_id: UUID) -> str:
    payload = json.dumps(
        {"v": 1, "k": "user_activity", "t": moment.isoformat(), "i": str(row_id)},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


class UserNotFound(LookupError):
    """The addressed account is absent or logically deleted."""


class UserSelfProtection(ValueError):
    """An administrator attempted to disable or delete their own account."""


class LastActiveAdministrator(ValueError):
    """The operation would leave the system without an active administrator."""


class PermissionTargetInvalid(ValueError):
    """A service or account target is absent or ineligible."""


class ServicePermissionNotFound(LookupError):
    """The AI Service addressed by a permission read does not exist."""


class UserInputInvalid(ValueError):
    """Normalized account input violates the public account contract."""


@dataclass(frozen=True, slots=True)
class CreatedUser:
    overview: UserOverviewRecord
    temporary_password: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class UserDetail:
    overview: UserOverviewRecord
    authorized_services: tuple[AiServiceRecord, ...]
    api_keys: tuple[ApiKeyRecord, ...]
    database_now: datetime
    limits: Mapping[UUID, tuple[int | None, int | None]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RevokedUserApiKey:
    record: ApiKeyRecord
    database_now: datetime


@dataclass(frozen=True, slots=True)
class PersonalAiService:
    id: UUID
    name: str
    public_model_name: str
    description: str
    available: bool
    enabled: bool
    last_used_at: datetime | None
    last_succeeded_at: datetime | None


@dataclass(frozen=True, slots=True)
class PersonalProfile:
    username: str
    display_name: str
    role: UserRole
    current_login_at: datetime
    previous_login_at: datetime | None
    created_at: datetime
    authorized_service_count: int


def _normalized_display_name(username: str, display_name: str) -> str:
    name = username if not display_name.strip() else normalize_display_name(display_name)
    if len(name) > 80:
        raise UserInputInvalid()
    return name


class UserManagementService:
    """Run account changes and their sanitized audit row in one transaction."""

    __slots__ = (
        "_audit_writer",
        "_password_hasher",
        "_password_slots",
        "_session_factory",
    )

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory,
        password_hasher: PasswordHasher | None = None,
        max_concurrent_password_operations: int = 4,
    ) -> None:
        if max_concurrent_password_operations < 1:
            raise ValueError("max_concurrent_password_operations must be positive")
        self._session_factory = session_factory
        self._password_hasher = PasswordHasher() if password_hasher is None else password_hasher
        self._password_slots = asyncio.BoundedSemaphore(max_concurrent_password_operations)
        self._audit_writer = AuditWriter(session_factory)

    async def _issue_temporary_password(self) -> TemporaryPassword:
        async with self._password_slots:
            return await asyncio.to_thread(issue_temporary_password, self._password_hasher)

    async def list_users(
        self,
        *,
        search: str | None,
        role: UserRole | None,
        status: UserStatus | None,
        include_deleted: bool,
    ) -> tuple[UserOverviewRecord, ...]:
        async with self._session_factory() as session:
            return await UserManagementRepository(session).list_users(
                search=search,
                role=role,
                status=status,
                include_deleted=include_deleted,
            )

    async def create_user(
        self,
        *,
        username: str,
        display_name: str,
        role: UserRole,
        actor_id: UUID,
        request_id: UUID,
    ) -> CreatedUser:
        username_norm = normalize_account_name(username)
        normalized_display_name = _normalized_display_name(username_norm, display_name)
        temporary = await self._issue_temporary_password()
        async with self._session_factory() as session, session.begin():
            now = await database_utc_now(session)
            user = await UserRepository(session).create(
                username=username_norm,
                display_name=normalized_display_name,
                role=role,
                password_hash=temporary.password_hash,
                now=now,
                must_change_password=True,
            )
            await self._audit_writer.add(
                session,
                self._audit(
                    actor_id=actor_id,
                    action=AuditAction.ACCOUNT_CHANGE,
                    operation="create",
                    object_id=user.id,
                    request_id=request_id,
                    changed_fields=("username", "display_name", "role", "status"),
                ),
            )
            overview = await UserManagementRepository(session).get_overview(user.id)
            if overview is None:
                raise RuntimeError("created user disappeared inside transaction")
            return CreatedUser(overview=overview, temporary_password=temporary.password)

    async def get_user(self, user_id: UUID) -> UserDetail:
        async with self._session_factory() as session:
            repository = UserManagementRepository(session)
            overview = await repository.get_overview(user_id)
            if overview is None:
                raise UserNotFound()
            services = await repository.authorized_services(user_id)
            return UserDetail(
                overview=overview,
                authorized_services=services,
                limits=await repository.permission_limits(user_id=user_id),
                api_keys=await ApiKeyRepository(session).list_owned(user_id),
                database_now=await database_utc_now(session),
            )

    async def get_user_activity(
        self,
        user_id: UUID,
        *,
        actions: tuple[str, ...] = (),
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, object]:
        if not 1 <= limit <= 100 or any(action not in USER_ACTIVITY_ACTIONS for action in actions):
            raise UserInputInvalid()
        before = _activity_cursor_decode(cursor) if cursor is not None else None
        async with self._session_factory() as session:
            if await UserManagementRepository(session).get_overview(user_id) is None:
                raise UserNotFound()
            rows = await AuditRepository(session).user_activity(
                user_id,
                actions=actions or USER_ACTIVITY_ACTIONS,
                before=before,
                limit=limit + 1,
            )
        items: list[dict[str, object]] = []
        for row in rows[:limit]:
            details = row["details"]
            operation = details.get("operation")
            if row["action"] == "account.password_change":
                operation = "change" if row["actor_id"] == row["object_id"] else "reset"
            elif row["action"] == "auth.login":
                operation = None
            before_limits = dict(json.loads(value) for value in details.get("limits_before", []))
            after_limits = dict(json.loads(value) for value in details.get("limits_after", []))
            limit_changed_service_ids = sorted(
                key
                for key in before_limits.keys() & after_limits.keys()
                if before_limits[key] is not None
                and after_limits[key] is not None
                and before_limits[key] != after_limits[key]
            )
            is_user = row["actor_type"] == "user"
            items.append(
                {
                    "id": str(row["id"]),
                    "created_at": row["created_at"].isoformat(),
                    "action": row["action"],
                    "operation": operation,
                    "result": row["result"],
                    "reason_code": details.get("reason_code"),
                    "actor": {
                        "type": row["actor_type"],
                        "id": str(row["actor_id"]) if is_user else None,
                        "username": row["actor_username"] if is_user else None,
                        "display_name": row["actor_display_name"] if is_user else None,
                    },
                    "api_key": {
                        "id": str(row["key_id"]),
                        "name": row["key_name"],
                        "display_prefix": row["display_prefix"],
                        "last4": row["last4"],
                    }
                    if row["key_id"] is not None
                    else None,
                    "added_service_ids": details.get("added_service_ids", []),
                    "removed_service_ids": details.get("removed_service_ids", []),
                    "limit_changed_service_ids": limit_changed_service_ids,
                }
            )
        return {
            "items": items,
            "next_cursor": _activity_cursor_encode(
                rows[limit - 1]["created_at"], rows[limit - 1]["id"]
            )
            if len(rows) > limit
            else None,
        }

    async def disable_user(
        self, user_id: UUID, *, actor_id: UUID, request_id: UUID
    ) -> UserOverviewRecord:
        return await self._set_status(
            user_id,
            status=UserStatus.DISABLED,
            actor_id=actor_id,
            request_id=request_id,
        )

    async def enable_user(
        self, user_id: UUID, *, actor_id: UUID, request_id: UUID
    ) -> UserOverviewRecord:
        async with self._session_factory() as session, session.begin():
            repository = UserManagementRepository(session)
            current = await repository.get_overview(user_id, for_update=True)
            if current is None or current.user.status is UserStatus.DELETED:
                raise UserNotFound()
            now = await database_utc_now(session)
            await UserRepository(session).set_status(user_id, UserStatus.ACTIVE, now=now)
            await self._audit_writer.add(
                session,
                self._audit(
                    actor_id=actor_id,
                    action=AuditAction.ACCOUNT_CHANGE,
                    operation="enable",
                    object_id=user_id,
                    request_id=request_id,
                    changed_fields=("status",),
                ),
            )
            updated = await repository.get_overview(user_id)
            if updated is None:
                raise RuntimeError("enabled user disappeared inside transaction")
            return updated

    async def _set_status(
        self,
        user_id: UUID,
        *,
        status: UserStatus,
        actor_id: UUID,
        request_id: UUID,
    ) -> UserOverviewRecord:
        async with self._session_factory() as session, session.begin():
            repository = UserManagementRepository(session)
            active_administrators = await repository.lock_active_administrators()
            current = await repository.get_overview(user_id, for_update=True)
            if current is None or current.user.status is UserStatus.DELETED:
                raise UserNotFound()
            if user_id == actor_id:
                raise UserSelfProtection()
            if (
                current.user.role is UserRole.ADMIN
                and current.user.status is UserStatus.ACTIVE
                and len(active_administrators) <= 1
            ):
                raise LastActiveAdministrator()
            now = await database_utc_now(session)
            await UserRepository(session).set_status(user_id, status, now=now)
            await SessionRepository(session).revoke_all_for_user(user_id, now=now)
            await self._audit_writer.add(
                session,
                self._audit(
                    actor_id=actor_id,
                    action=AuditAction.ACCOUNT_CHANGE,
                    operation="disable",
                    object_id=user_id,
                    request_id=request_id,
                    changed_fields=("status",),
                ),
            )
            updated = await repository.get_overview(user_id)
            if updated is None:
                raise RuntimeError("disabled user disappeared inside transaction")
            return updated

    async def reset_password(
        self, user_id: UUID, *, actor_id: UUID, request_id: UUID
    ) -> CreatedUser:
        temporary = await self._issue_temporary_password()
        async with self._session_factory() as session, session.begin():
            repository = UserManagementRepository(session)
            current = await repository.get_overview(user_id, for_update=True)
            if current is None or current.user.status is UserStatus.DELETED:
                raise UserNotFound()
            now = await database_utc_now(session)
            await UserRepository(session).set_password(
                user_id,
                password_hash=temporary.password_hash,
                now=now,
                must_change_password=True,
            )
            await SessionRepository(session).revoke_all_for_user(user_id, now=now)
            await self._audit_writer.add(
                session,
                self._audit(
                    actor_id=actor_id,
                    action=AuditAction.PASSWORD_CHANGE,
                    object_id=user_id,
                    request_id=request_id,
                    changed_fields=("password_hash", "must_change_password"),
                ),
            )
            updated = await repository.get_overview(user_id)
            if updated is None:
                raise RuntimeError("reset user disappeared inside transaction")
            return CreatedUser(overview=updated, temporary_password=temporary.password)

    async def delete_user(
        self, user_id: UUID, *, actor_id: UUID, request_id: UUID
    ) -> UserOverviewRecord:
        async with self._session_factory() as session, session.begin():
            repository = UserManagementRepository(session)
            active_administrators = await repository.lock_active_administrators()
            current = await repository.get_overview(user_id, for_update=True)
            if current is None or current.user.status is UserStatus.DELETED:
                raise UserNotFound()
            if user_id == actor_id:
                raise UserSelfProtection()
            if (
                current.user.role is UserRole.ADMIN
                and current.user.status is UserStatus.ACTIVE
                and len(active_administrators) <= 1
            ):
                raise LastActiveAdministrator()
            now = await database_utc_now(session)
            keys_to_revoke = tuple(
                key
                for key in await ApiKeyRepository(session).list_owned(user_id)
                if key.revoked_at is None
            )
            await UserRepository(session).soft_delete(user_id, now=now)
            await SessionRepository(session).revoke_all_for_user(user_id, now=now)
            await ApiKeyRepository(session).revoke_all_for_user(user_id, now=now)
            await repository.delete_permissions_for_user(user_id)
            await self._audit_writer.add(
                session,
                self._audit(
                    actor_id=actor_id,
                    action=AuditAction.ACCOUNT_CHANGE,
                    operation="delete",
                    object_id=user_id,
                    request_id=request_id,
                    changed_fields=("status", "deleted_at", "api_keys", "service_permissions"),
                ),
            )
            for key in keys_to_revoke:
                await append_route_invalidation_event(
                    session,
                    event=DomainEventType.API_KEY_CHANGED,
                    aggregate_id=key.id,
                )
            updated = await repository.get_overview(user_id)
            if updated is None:
                raise RuntimeError("deleted user disappeared inside transaction")
            return updated

    async def revoke_user_api_key(
        self, user_id: UUID, key_id: UUID, *, actor_id: UUID, request_id: UUID
    ) -> RevokedUserApiKey:
        """Administrator revoke; mirrors the owner path and works for disabled users."""
        async with self._session_factory() as session, session.begin():
            current = await UserManagementRepository(session).get_overview(user_id)
            if current is None or current.user.status is UserStatus.DELETED:
                raise UserNotFound()
            repository = ApiKeyRepository(session)
            record = await repository.get_owned(key_id, user_id=user_id, for_update=True)
            if record is None:
                raise ApiKeyNotFound()
            now = await database_utc_now(session)
            if record.revoked_at is None:
                record = await repository.revoke(key_id, now=now)
                await append_route_invalidation_event(
                    session,
                    event=DomainEventType.API_KEY_CHANGED,
                    aggregate_id=record.id,
                )
            await self._audit_writer.add(
                session,
                AuditEventDraft(
                    actor_type=AuditActorType.USER,
                    actor_id=actor_id,
                    action=AuditAction.API_KEY_CHANGE,
                    object_type=AuditObjectType.API_KEY,
                    object_id=record.id,
                    result=AuditResult.SUCCEEDED,
                    request_id=request_id,
                    details=build_audit_details(
                        AuditAction.API_KEY_CHANGE.value, {"operation": "revoke"}
                    ),
                ),
            )
            return RevokedUserApiKey(record=record, database_now=now)

    async def replace_user_permissions(
        self,
        user_id: UUID,
        service_ids: tuple[UUID, ...],
        *,
        actor_id: UUID,
        request_id: UUID,
        limits: Mapping[UUID, Mapping[str, int | None]] | None = None,
    ) -> tuple[AiServiceRecord, ...]:
        normalized = tuple(sorted(set(service_ids)))
        if limits is not None and not limits.keys() <= set(normalized):
            raise PermissionTargetInvalid()
        async with self._session_factory() as session, session.begin():
            repository = UserManagementRepository(session)
            target = await repository.get_overview(user_id, for_update=True)
            if target is None or target.user.status is UserStatus.DELETED:
                raise PermissionTargetInvalid()
            if await AiServiceRepository(session).existing_service_ids(normalized) != frozenset(
                normalized
            ):
                raise PermissionTargetInvalid()
            previous = set(await repository.authorized_service_ids(user_id))
            added = sorted(str(value) for value in set(normalized) - previous)
            removed = sorted(str(value) for value in previous - set(normalized))
            before_limits = await repository.permission_limits(user_id=user_id)
            now = await database_utc_now(session)
            await repository.replace_for_user(
                user_id, normalized, actor_id=actor_id, now=now, limits=limits
            )
            after_limits = await repository.permission_limits(user_id=user_id)
            limits_before, limits_after = self._limit_values(before_limits, after_limits)
            # Keep the existing activity batches and operation, including limit changes.
            for offset in range(0, max(len(added), len(removed), len(limits_before), 1), 32):
                await self._audit_writer.add(
                    session,
                    self._audit(
                        actor_id=actor_id,
                        action=AuditAction.PERMISSION_CHANGE,
                        object_id=user_id,
                        request_id=request_id,
                        changed_fields=("service_ids",) + (("limits",) if limits_before else ()),
                        operation="replace",
                        limits_before=limits_before[offset : offset + 32],
                        limits_after=limits_after[offset : offset + 32],
                        added_service_ids=tuple(added[offset : offset + 32]),
                        removed_service_ids=tuple(removed[offset : offset + 32]),
                    ),
                )
            await append_route_invalidation_event(
                session,
                event=DomainEventType.USER_ACCESS_CHANGED,
                aggregate_id=user_id,
            )
            return await repository.authorized_services(user_id)

    async def replace_service_permissions(
        self,
        service_id: UUID,
        user_ids: tuple[UUID, ...],
        *,
        actor_id: UUID,
        request_id: UUID,
        limits: Mapping[UUID, Mapping[str, int | None]] | None = None,
    ) -> tuple[UUID, ...]:
        normalized = tuple(sorted(set(user_ids)))
        if limits is not None and not limits.keys() <= set(normalized):
            raise PermissionTargetInvalid()
        async with self._session_factory() as session, session.begin():
            service = await AiServiceRepository(session).get(service_id, for_update=True)
            repository = UserManagementRepository(session)
            if service is None or await repository.grantable_user_ids(normalized) != frozenset(
                normalized
            ):
                raise PermissionTargetInvalid()
            previous = set(await repository.permission_user_ids(service_id))
            before_limits = await repository.permission_limits(service_id=service_id)
            now = await database_utc_now(session)
            await repository.replace_for_service(
                service_id,
                normalized,
                actor_id=actor_id,
                now=now,
                limits=limits,
            )
            after_limits = await repository.permission_limits(service_id=service_id)
            limits_before, limits_after = self._limit_values(before_limits, after_limits)
            for offset in range(0, max(len(limits_before), 1), 32):
                await self._audit_writer.add(
                    session,
                    self._audit(
                        actor_id=actor_id,
                        action=AuditAction.PERMISSION_CHANGE,
                        object_id=service_id,
                        request_id=request_id,
                        changed_fields=("user_ids",) + (("limits",) if limits_before else ()),
                        object_type=AuditObjectType.AI_SERVICE,
                        limits_before=limits_before[offset : offset + 32],
                        limits_after=limits_after[offset : offset + 32],
                    ),
                )
            changed_limits = {
                key
                for key in before_limits.keys() & after_limits.keys()
                if before_limits[key] != after_limits[key]
            }
            for changed_user_id in sorted(
                previous.symmetric_difference(normalized) | changed_limits
            ):
                added = changed_user_id not in previous
                removed = changed_user_id not in normalized
                user_before, user_after = self._limit_values(
                    {service_id: before_limits[changed_user_id]}
                    if changed_user_id in before_limits
                    else {},
                    {service_id: after_limits[changed_user_id]}
                    if changed_user_id in after_limits
                    else {},
                )
                await self._audit_writer.add(
                    session,
                    self._audit(
                        actor_id=actor_id,
                        action=AuditAction.PERMISSION_CHANGE,
                        object_id=changed_user_id,
                        request_id=request_id,
                        changed_fields=(("service_ids",) if added or removed else ()) + ("limits",),
                        operation="replace_by_service",
                        limits_before=user_before,
                        limits_after=user_after,
                        added_service_ids=(str(service_id),) if added else (),
                        removed_service_ids=(str(service_id),) if removed else (),
                    ),
                )
            await append_route_invalidation_event(
                session,
                event=DomainEventType.USER_ACCESS_CHANGED,
                aggregate_id=service_id,
            )
            return await repository.permission_user_ids(service_id)

    @staticmethod
    def _limit_values(
        before: Mapping[UUID, tuple[int | None, int | None]],
        after: Mapping[UUID, tuple[int | None, int | None]],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        # Only grant IDs and validated numeric limits enter the audit allowlist.
        changed = sorted(
            key for key in before.keys() | after.keys() if before.get(key) != after.get(key)
        )
        return (
            tuple(
                json.dumps([str(key), before.get(key)], separators=(",", ":")) for key in changed
            ),
            tuple(json.dumps([str(key), after.get(key)], separators=(",", ":")) for key in changed),
        )

    async def service_permission_limits(
        self, service_id: UUID
    ) -> dict[UUID, tuple[int | None, int | None]]:
        async with self._session_factory() as session:
            if await AiServiceRepository(session).get(service_id) is None:
                raise ServicePermissionNotFound()
            return await UserManagementRepository(session).permission_limits(service_id=service_id)

    async def list_personal_services(
        self, *, user_id: UUID, role: UserRole
    ) -> tuple[PersonalAiService, ...]:
        async with self._session_factory() as session:
            records = await UserManagementRepository(session).list_personal_services(
                user_id=user_id,
                role=role,
            )
        return tuple(
            PersonalAiService(
                id=record.id,
                name=record.name,
                public_model_name=record.public_model_name,
                description=record.description,
                available=record.available,
                enabled=record.enabled,
                last_used_at=record.last_used_at,
                last_succeeded_at=record.last_succeeded_at,
            )
            for record in records
        )

    async def personal_profile(
        self,
        *,
        user_id: UUID,
        current_login_at: datetime,
        display_name: str | None = None,
        request_id: UUID | None = None,
    ) -> PersonalProfile:
        async with self._session_factory() as session, session.begin():
            repository = UserManagementRepository(session)
            overview = await repository.get_overview(user_id, for_update=display_name is not None)
            if overview is None or overview.user.deleted_at is not None:
                raise UserNotFound()
            name = overview.user.display_name
            if display_name is not None:
                name = _normalized_display_name(overview.user.username_norm, display_name)
                await repository.update_display_name(user_id, name)
                assert request_id is not None
                await self._audit_writer.add(
                    session,
                    self._audit(
                        actor_id=user_id,
                        action=AuditAction.ACCOUNT_CHANGE,
                        object_id=user_id,
                        request_id=request_id,
                        changed_fields=("display_name",),
                    ),
                )
            return PersonalProfile(
                username=overview.user.username_norm,
                display_name=name,
                role=overview.user.role,
                current_login_at=current_login_at,
                previous_login_at=await repository.previous_login_at(user_id, current_login_at),
                created_at=overview.user.created_at,
                authorized_service_count=overview.authorized_service_count,
            )

    @staticmethod
    def _audit(
        *,
        actor_id: UUID,
        action: AuditAction,
        object_id: UUID,
        request_id: UUID,
        changed_fields: tuple[str, ...],
        object_type: AuditObjectType = AuditObjectType.USER,
        operation: str | None = None,
        added_service_ids: tuple[str, ...] = (),
        removed_service_ids: tuple[str, ...] = (),
        limits_before: tuple[str, ...] = (),
        limits_after: tuple[str, ...] = (),
    ) -> AuditEventDraft:
        return AuditEventDraft(
            actor_type=AuditActorType.USER,
            actor_id=actor_id,
            action=action,
            object_type=object_type,
            object_id=object_id,
            result=AuditResult.SUCCEEDED,
            request_id=request_id,
            details=build_audit_details(
                action.value,
                {
                    "changed_fields": list(changed_fields),
                    **(
                        {"limits_before": list(limits_before), "limits_after": list(limits_after)}
                        if limits_before
                        else {}
                    ),
                    **({"operation": operation} if operation is not None else {}),
                    **(
                        {
                            "added_service_ids": list(added_service_ids),
                            "removed_service_ids": list(removed_service_ids),
                        }
                        if operation in {"replace", "replace_by_service"}
                        else {}
                    ),
                },
            ),
        )


__all__ = [
    "CreatedUser",
    "LastActiveAdministrator",
    "PermissionTargetInvalid",
    "ServicePermissionNotFound",
    "UserDetail",
    "UserInputInvalid",
    "UserManagementService",
    "UserNotFound",
    "UserSelfProtection",
]

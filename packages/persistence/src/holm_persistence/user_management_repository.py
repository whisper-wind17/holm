"""Repository queries for account administration and AI Service grants."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from holm_common import as_utc
from holm_domain import (
    AiServiceRuntimeStatus,
    ApiKeyStatus,
    AuditAction,
    AuditResult,
    ModelInstanceStatus,
    RequestRecordState,
    UserRole,
    UserStatus,
    derive_ai_service_runtime_status,
)
from holm_domain.call_limits import DEFAULT_MAX_CONCURRENT_REQUESTS
from sqlalchemy.ext.asyncio import AsyncSession

from holm_persistence.account_repository import UserRecord, _user_record
from holm_persistence.accounts import SESSIONS, USERS
from holm_persistence.ai_service_repository import AiServiceRecord, _ai_service_record
from holm_persistence.ai_services import (
    AI_SERVICE_BACKENDS,
    AI_SERVICES,
    API_KEYS,
    USER_SERVICE_PERMISSIONS,
)
from holm_persistence.audit_events import AUDIT_EVENTS
from holm_persistence.ledger_request_records import LEDGER_REQUEST_RECORDS
from holm_persistence.model_instances import MODEL_INSTANCES


@dataclass(frozen=True, slots=True)
class UserOverviewRecord:
    user: UserRecord
    authorized_service_count: int
    api_key_count: int
    last_login_at: datetime | None


@dataclass(frozen=True, slots=True)
class PersonalAiServiceRecord:
    id: UUID
    name: str
    public_model_name: str
    description: str
    available: bool
    enabled: bool
    last_used_at: datetime | None
    last_succeeded_at: datetime | None


def _overview(mapping: sa.RowMapping) -> UserOverviewRecord:
    last_login_at = mapping["last_login_at"]
    return UserOverviewRecord(
        user=_user_record(mapping),
        authorized_service_count=int(mapping["authorized_service_count"]),
        api_key_count=int(mapping["api_key_count"]),
        last_login_at=None if last_login_at is None else as_utc(last_login_at),
    )


class UserManagementRepository:
    """Keep account lifecycle protection and grant replacement transactional."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _overview_columns() -> tuple[sa.ColumnElement[object], ...]:
        permission_count = (
            sa.select(sa.func.count())
            .select_from(
                USER_SERVICE_PERMISSIONS.join(
                    AI_SERVICES,
                    USER_SERVICE_PERMISSIONS.c.service_id == AI_SERVICES.c.id,
                )
            )
            .where(
                USER_SERVICE_PERMISSIONS.c.user_id == USERS.c.id,
                AI_SERVICES.c.deleted_at.is_(None),
            )
            .correlate(USERS)
            .scalar_subquery()
        )
        key_count = (
            sa.select(sa.func.count())
            .select_from(API_KEYS)
            .where(
                API_KEYS.c.user_id == USERS.c.id,
                # Only keys that can still authenticate.
                API_KEYS.c.status == ApiKeyStatus.ACTIVE.value,
                API_KEYS.c.revoked_at.is_(None),
                sa.or_(API_KEYS.c.expires_at.is_(None), API_KEYS.c.expires_at > sa.func.now()),
            )
            .correlate(USERS)
            .scalar_subquery()
        )
        last_login = (
            sa.select(sa.func.max(SESSIONS.c.last_seen_at))
            .where(SESSIONS.c.user_id == USERS.c.id)
            .correlate(USERS)
            .scalar_subquery()
        )
        return (
            *USERS.c,
            permission_count.label("authorized_service_count"),
            key_count.label("api_key_count"),
            last_login.label("last_login_at"),
        )

    async def list_users(
        self,
        *,
        search: str | None,
        role: UserRole | None,
        status: UserStatus | None,
        include_deleted: bool,
    ) -> tuple[UserOverviewRecord, ...]:
        predicates: list[sa.ColumnElement[bool]] = []
        if not include_deleted:
            predicates.append(USERS.c.deleted_at.is_(None))
        if search:
            pattern = f"%{search}%"
            predicates.append(
                sa.or_(
                    USERS.c.username_norm.ilike(pattern),
                    USERS.c.display_name.ilike(pattern),
                )
            )
        if role is not None:
            predicates.append(USERS.c.role == role.value)
        if status is not None:
            predicates.append(USERS.c.status == status.value)
        statement = (
            sa.select(*self._overview_columns())
            .where(*predicates)
            .order_by(
                sa.case((USERS.c.status == UserStatus.ACTIVE.value, 0), else_=1),
                USERS.c.username_norm,
            )
        )
        return tuple(
            _overview(mapping)
            for mapping in (await self._session.execute(statement)).mappings().all()
        )

    async def get_overview(
        self, user_id: UUID, *, for_update: bool = False
    ) -> UserOverviewRecord | None:
        statement = sa.select(*self._overview_columns()).where(USERS.c.id == user_id)
        if for_update:
            statement = statement.with_for_update()
        mapping = (await self._session.execute(statement)).mappings().one_or_none()
        return None if mapping is None else _overview(mapping)

    async def lock_active_administrators(self) -> tuple[UUID, ...]:
        statement = (
            sa.select(USERS.c.id)
            .where(
                USERS.c.role == UserRole.ADMIN.value,
                USERS.c.status == UserStatus.ACTIVE.value,
                USERS.c.deleted_at.is_(None),
            )
            .order_by(USERS.c.id)
            .with_for_update()
        )
        return tuple((await self._session.execute(statement)).scalars())

    async def authorized_service_ids(self, user_id: UUID) -> frozenset[UUID]:
        values = (
            await self._session.execute(
                sa.select(USER_SERVICE_PERMISSIONS.c.service_id).where(
                    USER_SERVICE_PERMISSIONS.c.user_id == user_id
                )
            )
        ).scalars()
        return frozenset(values)

    async def authorized_services(self, user_id: UUID) -> tuple[AiServiceRecord, ...]:
        statement = (
            sa.select(
                *AI_SERVICES.c,
                AI_SERVICE_BACKENDS.c.instance_id.label("primary_instance_id"),
            )
            .select_from(
                USER_SERVICE_PERMISSIONS.join(
                    AI_SERVICES,
                    USER_SERVICE_PERMISSIONS.c.service_id == AI_SERVICES.c.id,
                ).join(
                    AI_SERVICE_BACKENDS,
                    sa.and_(
                        AI_SERVICE_BACKENDS.c.service_id == AI_SERVICES.c.id,
                        AI_SERVICE_BACKENDS.c.backend_key == "primary",
                        AI_SERVICE_BACKENDS.c.retired_at.is_(None),
                    ),
                )
            )
            .where(
                USER_SERVICE_PERMISSIONS.c.user_id == user_id,
                AI_SERVICES.c.deleted_at.is_(None),
            )
            .order_by(AI_SERVICES.c.public_model_name)
        )
        return tuple(
            _ai_service_record(mapping)
            for mapping in (await self._session.execute(statement)).mappings().all()
        )

    async def grantable_user_ids(self, user_ids: tuple[UUID, ...]) -> frozenset[UUID]:
        if not user_ids:
            return frozenset()
        values = (
            await self._session.execute(
                sa.select(USERS.c.id).where(
                    USERS.c.id.in_(user_ids),
                    USERS.c.deleted_at.is_(None),
                )
            )
        ).scalars()
        return frozenset(values)

    async def permission_user_ids(self, service_id: UUID) -> tuple[UUID, ...]:
        values = (
            await self._session.execute(
                sa.select(USER_SERVICE_PERMISSIONS.c.user_id)
                .where(USER_SERVICE_PERMISSIONS.c.service_id == service_id)
                .order_by(USER_SERVICE_PERMISSIONS.c.user_id)
            )
        ).scalars()
        return tuple(values)

    async def permission_limits(
        self,
        *,
        user_id: UUID | None = None,
        service_id: UUID | None = None,
    ) -> dict[UUID, tuple[int | None, int | None]]:
        table = USER_SERVICE_PERMISSIONS
        owner = table.c.user_id if user_id is not None else table.c.service_id
        peer = table.c.service_id if user_id is not None else table.c.user_id
        rows = await self._session.execute(
            sa.select(peer, table.c.max_concurrent_requests, table.c.max_requests_per_minute)
            .where(owner == (user_id if user_id is not None else service_id))
            .order_by(peer)
        )
        return {row[0]: (row[1], row[2]) for row in rows}

    async def replace_for_user(
        self,
        user_id: UUID,
        service_ids: tuple[UUID, ...],
        *,
        actor_id: UUID,
        now: datetime,
        limits: Mapping[UUID, Mapping[str, int | None]] | None = None,
    ) -> None:
        await self._replace_permissions(
            "user_id",
            user_id,
            "service_id",
            service_ids,
            actor_id=actor_id,
            now=now,
            limits=limits or {},
        )

    async def replace_for_service(
        self,
        service_id: UUID,
        user_ids: tuple[UUID, ...],
        *,
        actor_id: UUID,
        now: datetime,
        limits: Mapping[UUID, Mapping[str, int | None]] | None = None,
    ) -> None:
        await self._replace_permissions(
            "service_id",
            service_id,
            "user_id",
            user_ids,
            actor_id=actor_id,
            now=now,
            limits=limits or {},
        )

    async def _replace_permissions(
        self,
        owner_column: str,
        owner_id: UUID,
        peer_column: str,
        peers: tuple[UUID, ...],
        *,
        actor_id: UUID,
        now: datetime,
        limits: Mapping[UUID, Mapping[str, int | None]],
    ) -> None:
        table = USER_SERVICE_PERMISSIONS
        owner, peer = table.c[owner_column], table.c[peer_column]
        existing = set(
            (await self._session.execute(sa.select(peer).where(owner == owner_id))).scalars()
        )
        desired = set(peers)
        removed = existing - desired
        if removed:
            await self._session.execute(
                sa.delete(table).where(owner == owner_id, peer.in_(removed))
            )
        for peer_id in peers:
            values = dict(limits.get(peer_id, {}))
            if peer_id in existing:
                if values:
                    await self._session.execute(
                        sa.update(table).where(owner == owner_id, peer == peer_id).values(**values)
                    )
            else:
                await self._session.execute(
                    sa.insert(table).values(
                        {
                            owner_column: owner_id,
                            peer_column: peer_id,
                            "created_at": as_utc(now),
                            "created_by": actor_id,
                            "max_concurrent_requests": DEFAULT_MAX_CONCURRENT_REQUESTS,
                            "max_requests_per_minute": None,
                            **values,
                        }
                    )
                )

    async def delete_permissions_for_user(self, user_id: UUID) -> int:
        statement = (
            sa.delete(USER_SERVICE_PERMISSIONS)
            .where(USER_SERVICE_PERMISSIONS.c.user_id == user_id)
            .returning(USER_SERVICE_PERMISSIONS.c.service_id)
        )
        return len((await self._session.execute(statement)).all())

    async def list_personal_services(
        self,
        *,
        user_id: UUID,
        role: UserRole,
    ) -> tuple[PersonalAiServiceRecord, ...]:
        # Restrict by user before aggregation, using the existing user/time index.
        activity = (
            sa.select(
                LEDGER_REQUEST_RECORDS.c.service_id,
                sa.func.max(LEDGER_REQUEST_RECORDS.c.received_at).label("last_used_at"),
                sa.func.max(
                    sa.func.coalesce(
                        LEDGER_REQUEST_RECORDS.c.terminal_at,
                        LEDGER_REQUEST_RECORDS.c.received_at,
                    )
                )
                .filter(LEDGER_REQUEST_RECORDS.c.record_state == RequestRecordState.COMPLETED.value)
                .label("last_succeeded_at"),
            )
            .where(LEDGER_REQUEST_RECORDS.c.user_id == user_id)
            .group_by(LEDGER_REQUEST_RECORDS.c.service_id)
            .subquery()
        )
        statement = (
            sa.select(
                AI_SERVICES.c.id,
                AI_SERVICES.c.enabled,
                activity.c.last_used_at,
                activity.c.last_succeeded_at,
                AI_SERVICES.c.name,
                AI_SERVICES.c.public_model_name,
                AI_SERVICES.c.description,
                MODEL_INSTANCES.c.status,
            )
            .select_from(
                AI_SERVICES.join(
                    AI_SERVICE_BACKENDS,
                    sa.and_(
                        AI_SERVICE_BACKENDS.c.service_id == AI_SERVICES.c.id,
                        AI_SERVICE_BACKENDS.c.backend_key == "primary",
                        AI_SERVICE_BACKENDS.c.retired_at.is_(None),
                    ),
                ).join(
                    MODEL_INSTANCES,
                    AI_SERVICE_BACKENDS.c.instance_id == MODEL_INSTANCES.c.id,
                )
            )
            .where(
                AI_SERVICES.c.deleted_at.is_(None),
            )
            .outerjoin(activity, activity.c.service_id == AI_SERVICES.c.id)
        )
        statement = statement.join(
            USER_SERVICE_PERMISSIONS,
            sa.and_(
                USER_SERVICE_PERMISSIONS.c.service_id == AI_SERVICES.c.id,
                USER_SERVICE_PERMISSIONS.c.user_id == user_id,
            ),
        )
        rows = (await self._session.execute(statement)).mappings().all()
        records = tuple(
            PersonalAiServiceRecord(
                id=row["id"],
                name=str(row["name"]),
                public_model_name=str(row["public_model_name"]),
                description=str(row["description"]),
                enabled=bool(row["enabled"]),
                available=derive_ai_service_runtime_status(
                    enabled=bool(row["enabled"]),
                    primary_instance_status=ModelInstanceStatus(str(row["status"])),
                )
                is AiServiceRuntimeStatus.AVAILABLE,
                last_used_at=None if row["last_used_at"] is None else as_utc(row["last_used_at"]),
                last_succeeded_at=None
                if row["last_succeeded_at"] is None
                else as_utc(row["last_succeeded_at"]),
            )
            for row in rows
        )

        return tuple(
            sorted(
                records,
                key=lambda record: (
                    record.last_used_at is None,
                    -record.last_used_at.timestamp() if record.last_used_at else 0,
                    not record.available,
                    record.name,
                    record.id,
                ),
            )
        )

    async def previous_login_at(self, user_id: UUID, current_login_at: datetime) -> datetime | None:
        value = await self._session.scalar(
            sa.select(sa.func.max(AUDIT_EVENTS.c.created_at)).where(
                AUDIT_EVENTS.c.actor_id == user_id,
                AUDIT_EVENTS.c.action == AuditAction.LOGIN.value,
                AUDIT_EVENTS.c.result == AuditResult.SUCCEEDED.value,
                AUDIT_EVENTS.c.created_at < current_login_at,
            )
        )
        return None if value is None else as_utc(value)

    async def update_display_name(self, user_id: UUID, display_name: str) -> None:
        await self._session.execute(
            sa.update(USERS)
            .where(USERS.c.id == user_id)
            .values(display_name=display_name, updated_at=sa.func.now())
        )


__all__ = [
    "PersonalAiServiceRecord",
    "UserManagementRepository",
    "UserOverviewRecord",
]

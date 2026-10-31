"""Versioned in-memory authentication and routing facts for the Gateway."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Protocol
from uuid import UUID

import sqlalchemy as sa
from holm_common import as_utc
from holm_domain import ApiKeyStatus, UserRole, UserStatus
from holm_persistence import (
    AI_SERVICE_BACKENDS,
    AI_SERVICES,
    API_KEY_SERVICE_SCOPES,
    API_KEYS,
    MODEL_ARTIFACTS,
    MODEL_INSTANCES,
    MODELS,
    USER_SERVICE_PERMISSIONS,
    USERS,
    AsyncSessionFactory,
    DomainEventRepository,
    NotificationListener,
)
from holm_security import ApiKeyValidationSnapshot

from holm_gateway.routing import GatewayRoute

_LOGGER = logging.getLogger("holm.gateway.route_snapshot")


@dataclass(frozen=True, slots=True)
class SnapshotApiKey:
    id: UUID
    user_id: UUID
    pepper_key_id: str
    secret_hmac: bytes = field(repr=False)
    status: ApiKeyStatus
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class SnapshotUser:
    status: UserStatus
    is_admin: bool
    authorized_service_ids: frozenset[UUID]
    service_limits: Mapping[UUID, tuple[int | None, int | None]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RouteSnapshot:
    """A deeply immutable set of facts published with one reference swap."""

    version: int
    loaded_at: float
    api_keys_by_lookup: Mapping[str, SnapshotApiKey]
    users_by_id: Mapping[UUID, SnapshotUser]
    key_scopes_by_key_id: Mapping[UUID, frozenset[UUID]]
    routes_by_model: Mapping[str, GatewayRoute]


class RouteSnapshotSource(Protocol):
    async def load(self, *, loaded_at: float) -> RouteSnapshot: ...

    async def current_event_sequence(self) -> int: ...


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


class RouteSnapshotUnavailable(RuntimeError):
    """The initial snapshot is absent or the last database proof is too old."""


class PostgresRouteSnapshotSource:
    """Build a complete snapshot behind the Domain Event ordering fence."""

    __slots__ = ("_session_factory",)

    def __init__(self, session_factory: AsyncSessionFactory) -> None:
        self._session_factory = session_factory

    async def load(self, *, loaded_at: float) -> RouteSnapshot:
        async with self._session_factory() as session:
            # Acquire the event read fence before state reads. A business writer
            # either commits both state and event before this point or receives a
            # later sequence after this transaction releases the fence.
            version = await DomainEventRepository(session).stable_frontier()
            users = (
                (
                    await session.execute(
                        sa.select(USERS.c.id, USERS.c.status, USERS.c.role).where(
                            USERS.c.deleted_at.is_(None)
                        )
                    )
                )
                .mappings()
                .all()
            )
            permission_rows = (
                (
                    await session.execute(
                        sa.select(
                            USER_SERVICE_PERMISSIONS.c.user_id,
                            USER_SERVICE_PERMISSIONS.c.service_id,
                            USER_SERVICE_PERMISSIONS.c.max_concurrent_requests,
                            USER_SERVICE_PERMISSIONS.c.max_requests_per_minute,
                        )
                    )
                )
                .tuples()
                .all()
            )
            key_rows = (
                (
                    await session.execute(
                        sa.select(
                            API_KEYS.c.id,
                            API_KEYS.c.user_id,
                            API_KEYS.c.lookup_id,
                            API_KEYS.c.pepper_key_id,
                            API_KEYS.c.secret_hmac,
                            API_KEYS.c.status,
                            API_KEYS.c.expires_at,
                        )
                    )
                )
                .mappings()
                .all()
            )
            scope_rows = (
                (
                    await session.execute(
                        sa.select(
                            API_KEY_SERVICE_SCOPES.c.key_id,
                            API_KEY_SERVICE_SCOPES.c.service_id,
                        )
                    )
                )
                .tuples()
                .all()
            )
            route_rows = (await session.execute(self._route_statement())).mappings().all()

        permissions: dict[UUID, set[UUID]] = {}
        limits: dict[UUID, dict[UUID, tuple[int | None, int | None]]] = {}
        for user_id, service_id, concurrent, per_minute in permission_rows:
            permissions.setdefault(user_id, set()).add(service_id)
            limits.setdefault(user_id, {})[service_id] = (concurrent, per_minute)
        scopes: dict[UUID, set[UUID]] = {}
        for key_id, service_id in scope_rows:
            scopes.setdefault(key_id, set()).add(service_id)

        user_map = {
            row["id"]: SnapshotUser(
                status=UserStatus(row["status"]),
                is_admin=UserRole(row["role"]) is UserRole.ADMIN,
                authorized_service_ids=frozenset(permissions.get(row["id"], ())),
                service_limits=MappingProxyType(limits.get(row["id"], {})),
            )
            for row in users
        }
        key_map = {
            str(row["lookup_id"]): SnapshotApiKey(
                id=row["id"],
                user_id=row["user_id"],
                pepper_key_id=str(row["pepper_key_id"]),
                secret_hmac=bytes(row["secret_hmac"]),
                status=ApiKeyStatus(row["status"]),
                expires_at=(None if row["expires_at"] is None else as_utc(row["expires_at"])),
            )
            for row in key_rows
        }
        scope_map = {row["id"]: frozenset(scopes.get(row["id"], ())) for row in key_rows}
        route_map = {
            str(row["public_model_name"]): GatewayRoute(
                service_id=row["id"],
                instance_id=row["instance_id"],
                public_model_name=str(row["public_model_name"]),
                enabled=bool(row["enabled"]),
                instance_status=None if row["status"] is None else str(row["status"]),
                runtime_name="" if row["runtime_type"] is None else str(row["runtime_type"]),
                internal_endpoint=(
                    None if row["internal_endpoint"] is None else str(row["internal_endpoint"])
                ),
                internal_model_name=(
                    "" if row["internal_model_name"] is None else str(row["internal_model_name"])
                ),
                route_version=int(row["route_version"]),
                tokenizer_bundle_path=_optional_text(row["tokenizer_bundle_path"]),
                tokenizer_bundle_hash=_optional_text(row["tokenizer_bundle_hash"]),
                artifact_storage_type=_optional_text(row["storage_type"]),
                model_architecture=_optional_text(row["architecture"]),
            )
            for row in route_rows
        }
        return RouteSnapshot(
            version=version,
            loaded_at=loaded_at,
            api_keys_by_lookup=MappingProxyType(key_map),
            users_by_id=MappingProxyType(user_map),
            key_scopes_by_key_id=MappingProxyType(scope_map),
            routes_by_model=MappingProxyType(route_map),
        )

    async def current_event_sequence(self) -> int:
        async with self._session_factory() as session:
            return await DomainEventRepository(session).stable_frontier()

    @staticmethod
    def _route_statement() -> sa.Select[tuple[object, ...]]:
        active_backend = sa.and_(
            AI_SERVICE_BACKENDS.c.service_id == AI_SERVICES.c.id,
            AI_SERVICE_BACKENDS.c.backend_key == "primary",
            AI_SERVICE_BACKENDS.c.enabled.is_(True),
            AI_SERVICE_BACKENDS.c.retired_at.is_(None),
        )
        return (
            sa.select(
                AI_SERVICES.c.id,
                AI_SERVICES.c.public_model_name,
                AI_SERVICES.c.enabled,
                AI_SERVICES.c.route_version,
                AI_SERVICE_BACKENDS.c.instance_id,
                MODEL_INSTANCES.c.status,
                MODEL_INSTANCES.c.runtime_type,
                MODEL_INSTANCES.c.internal_endpoint,
                MODEL_INSTANCES.c.internal_model_name,
                MODEL_ARTIFACTS.c.storage_type,
                MODEL_ARTIFACTS.c.tokenizer_bundle_path,
                MODEL_ARTIFACTS.c.tokenizer_bundle_hash,
                MODELS.c.architecture,
            )
            .select_from(
                AI_SERVICES.outerjoin(AI_SERVICE_BACKENDS, active_backend)
                .outerjoin(
                    MODEL_INSTANCES,
                    MODEL_INSTANCES.c.id == AI_SERVICE_BACKENDS.c.instance_id,
                )
                .outerjoin(
                    MODEL_ARTIFACTS,
                    MODEL_ARTIFACTS.c.id == MODEL_INSTANCES.c.artifact_id,
                )
                .outerjoin(MODELS, MODELS.c.id == MODEL_ARTIFACTS.c.model_id)
            )
            .where(AI_SERVICES.c.deleted_at.is_(None))
            .order_by(AI_SERVICES.c.public_model_name)
        )


class RouteSnapshotManager:
    """Own startup loading, freshness proof, LISTEN wakeups, and polling recovery."""

    __slots__ = (
        "_clock",
        "_database_reachable",
        "_last_confirmed_at",
        "_listener",
        "_listener_task",
        "_lock",
        "_max_age_seconds",
        "_poll_seconds",
        "_poll_task",
        "_snapshot",
        "_snapshot_observer",
        "_snapshot_observer_tasks",
        "_source",
        "_stop",
        "_wake",
    )

    def __init__(
        self,
        source: RouteSnapshotSource,
        *,
        listener: NotificationListener | None,
        poll_seconds: float = 1.0,
        max_age_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        snapshot_observer: Callable[[RouteSnapshot], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        self._source = source
        self._listener = listener
        self._poll_seconds = poll_seconds
        self._max_age_seconds = max_age_seconds
        self._clock = clock
        self._snapshot_observer = snapshot_observer
        self._snapshot_observer_tasks: set[asyncio.Task[None]] = set()
        self._snapshot: RouteSnapshot | None = None
        self._last_confirmed_at: float | None = None
        self._database_reachable = False
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._poll_task: asyncio.Task[None] | None = None
        self._listener_task: asyncio.Task[None] | None = None

    @property
    def snapshot(self) -> RouteSnapshot:
        snapshot = self._snapshot
        if snapshot is None:
            raise RouteSnapshotUnavailable()
        return snapshot

    @property
    def last_confirmed_at(self) -> float | None:
        return self._last_confirmed_at

    def ensure_available(self) -> None:
        confirmed = self._last_confirmed_at
        if (
            self._snapshot is None
            or confirmed is None
            or self._clock() - confirmed > self._max_age_seconds
        ):
            raise RouteSnapshotUnavailable()

    def call_limits(self, user_id: UUID, service_id: UUID) -> tuple[int | None, int | None]:
        user = self.snapshot.users_by_id.get(user_id)
        return (None, None) if user is None else user.service_limits.get(service_id, (None, None))

    async def find(self, public_model_name: str) -> GatewayRoute | None:
        return self.snapshot.routes_by_model.get(public_model_name)

    async def list_enabled(self) -> tuple[GatewayRoute, ...]:
        return tuple(route for route in self.snapshot.routes_by_model.values() if route.enabled)

    async def refresh(self) -> RouteSnapshot:
        async with self._lock:
            now = self._clock()
            snapshot = await self._source.load(loaded_at=now)
            self._snapshot = snapshot
            self._last_confirmed_at = self._clock()
            self._database_reachable = True
        self._schedule_snapshot_observer(snapshot)
        return snapshot

    def _schedule_snapshot_observer(self, snapshot: RouteSnapshot) -> None:
        observer = self._snapshot_observer
        if observer is None:
            return
        task = asyncio.create_task(observer(snapshot))
        self._snapshot_observer_tasks.add(task)
        task.add_done_callback(self._snapshot_observer_done)

    def _snapshot_observer_done(self, task: asyncio.Task[None]) -> None:
        self._snapshot_observer_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:  # noqa: BLE001 - routing remains authoritative on observer failure
            _LOGGER.warning(
                "route snapshot observer unavailable",
                extra={"component": "gateway", "error_code": "snapshot_observer_unavailable"},
            )

    async def wait_for_snapshot_observers(self) -> None:
        """Wait for already scheduled observers; intended for startup and deterministic tests."""

        while self._snapshot_observer_tasks:
            await asyncio.gather(*tuple(self._snapshot_observer_tasks), return_exceptions=True)

    async def start(self) -> None:
        if self._poll_task is not None:
            raise RuntimeError("Route Snapshot manager is already running")
        self._stop.clear()
        delay = 0.1
        while not self._stop.is_set():
            try:
                await self.refresh()
                break
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - startup retry never renders database details
                self._database_reachable = False
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                delay = min(delay * 2, 5.0)
        if self._stop.is_set():
            return
        if self._listener is not None:
            self._listener_task = asyncio.create_task(self._listener_loop())
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        tasks = tuple(task for task in (self._listener_task, self._poll_task) if task is not None)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.wait_for_snapshot_observers()
        self._listener_task = None
        self._poll_task = None

    async def check_once(self, *, force_refresh: bool = False) -> None:
        """Run one compensation check; public for deterministic fault tests."""

        try:
            sequence = await self._source.current_event_sequence()
            snapshot = self._snapshot
            recovered = not self._database_reachable
            self._database_reachable = True
            if force_refresh or recovered or snapshot is None or sequence > snapshot.version:
                await self.refresh()
            else:
                self._last_confirmed_at = self._clock()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - preserve the old snapshot and a sanitized state only
            self._database_reachable = False

    async def _poll_loop(self) -> None:
        while not self._stop.is_set():
            notified = False
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_seconds)
                notified = True
            except TimeoutError:
                pass
            self._wake.clear()
            if not self._stop.is_set():
                await self.check_once(force_refresh=notified)

    async def _listener_loop(self) -> None:
        assert self._listener is not None
        delay = 0.1
        while not self._stop.is_set():
            try:
                await self._listener.listen(self._wake, self._stop)
                delay = 0.1
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - do not expose connection or DSN details
                _LOGGER.warning(
                    "route snapshot listener unavailable",
                    extra={"component": "gateway", "error_code": "listener_unavailable"},
                )
            if not self._stop.is_set():
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                delay = min(delay * 2, 5.0)


class SnapshotApiKeyValidationLookup:
    """Adapt immutable snapshot facts to the sole API Key validation primitive."""

    __slots__ = ("_manager", "_utc_now")

    def __init__(
        self,
        manager: RouteSnapshotManager,
        *,
        utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._manager = manager
        self._utc_now = utc_now

    async def __call__(self, lookup_id: str) -> ApiKeyValidationSnapshot | None:
        snapshot = self._manager.snapshot
        key = snapshot.api_keys_by_lookup.get(lookup_id)
        if key is None:
            return None
        user = snapshot.users_by_id.get(key.user_id)
        if user is None:
            return None
        return ApiKeyValidationSnapshot(
            user_id=key.user_id,
            api_key_id=key.id,
            pepper_key_id=key.pepper_key_id,
            secret_hmac=key.secret_hmac,
            revoked=key.status is ApiKeyStatus.REVOKED,
            expired_status=key.status is ApiKeyStatus.EXPIRED,
            expires_at=key.expires_at,
            user_active=user.status is UserStatus.ACTIVE,
            scoped_service_ids=snapshot.key_scopes_by_key_id.get(key.id, frozenset()),
            database_now=as_utc(self._utc_now()),
            user_is_admin=user.is_admin,
            authorized_service_ids=user.authorized_service_ids,
            service_limits=user.service_limits,
        )


__all__ = [
    "PostgresRouteSnapshotSource",
    "RouteSnapshot",
    "RouteSnapshotManager",
    "RouteSnapshotSource",
    "RouteSnapshotUnavailable",
    "SnapshotApiKey",
    "SnapshotApiKeyValidationLookup",
    "SnapshotUser",
]

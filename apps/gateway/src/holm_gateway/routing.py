"""Short-lived PostgreSQL routing view for public AI Service names."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

import sqlalchemy as sa
from holm_domain import ModelInstanceStatus
from holm_persistence import (
    AI_SERVICE_BACKENDS,
    AI_SERVICES,
    MODEL_ARTIFACTS,
    MODEL_INSTANCES,
    MODELS,
    AsyncSessionFactory,
)
from holm_protocols import ServiceRoute


@dataclass(frozen=True, slots=True)
class GatewayRoute:
    service_id: UUID
    instance_id: UUID | None
    public_model_name: str
    enabled: bool
    instance_status: str | None
    runtime_name: str
    internal_endpoint: str | None = field(repr=False)
    internal_model_name: str = field(repr=False)
    route_version: int = 1
    # metering facts for the backing Artifact; absent for a route with no
    # running instance, and deliberately empty for an External Artifact.
    tokenizer_bundle_path: str | None = field(default=None, repr=False)
    tokenizer_bundle_hash: str | None = field(default=None, repr=False)
    artifact_storage_type: str | None = field(default=None, repr=False)
    model_architecture: str | None = field(default=None, repr=False)


def _text(value: object) -> str | None:
    return None if value is None else str(value)


class RouteSource(Protocol):
    async def list_routes(self) -> tuple[GatewayRoute, ...]:
        """Return every non-deleted AI Service and its Primary Backend facts."""
        ...


class GatewayRouteDirectory(Protocol):
    async def find(self, public_model_name: str) -> GatewayRoute | None: ...

    async def list_enabled(self) -> tuple[GatewayRoute, ...]: ...


class PostgresRouteSource:
    """Read only the columns needed to route an inference request."""

    __slots__ = ("_session_factory",)

    def __init__(self, session_factory: AsyncSessionFactory) -> None:
        self._session_factory = session_factory

    async def list_routes(self) -> tuple[GatewayRoute, ...]:
        statement = (
            sa.select(
                AI_SERVICES.c.id,
                MODEL_INSTANCES.c.id.label("instance_id"),
                AI_SERVICES.c.public_model_name,
                AI_SERVICES.c.enabled,
                AI_SERVICES.c.route_version,
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
                AI_SERVICES.join(
                    AI_SERVICE_BACKENDS,
                    sa.and_(
                        AI_SERVICE_BACKENDS.c.service_id == AI_SERVICES.c.id,
                        AI_SERVICE_BACKENDS.c.backend_key == "primary",
                        AI_SERVICE_BACKENDS.c.retired_at.is_(None),
                    ),
                )
                .join(
                    MODEL_INSTANCES,
                    AI_SERVICE_BACKENDS.c.instance_id == MODEL_INSTANCES.c.id,
                )
                .join(
                    MODEL_ARTIFACTS,
                    MODEL_INSTANCES.c.artifact_id == MODEL_ARTIFACTS.c.id,
                )
                .outerjoin(MODELS, MODELS.c.id == MODEL_ARTIFACTS.c.model_id)
            )
            .where(AI_SERVICES.c.deleted_at.is_(None))
            .order_by(AI_SERVICES.c.public_model_name)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).mappings().all()
        return tuple(
            GatewayRoute(
                service_id=row["id"],
                instance_id=row["instance_id"],
                public_model_name=str(row["public_model_name"]),
                enabled=bool(row["enabled"]),
                instance_status=str(row["status"]),
                runtime_name=str(row["runtime_type"]),
                internal_endpoint=(
                    None if row["internal_endpoint"] is None else str(row["internal_endpoint"])
                ),
                internal_model_name=str(row["internal_model_name"]),
                route_version=int(row["route_version"]),
                tokenizer_bundle_path=_text(row["tokenizer_bundle_path"]),
                tokenizer_bundle_hash=_text(row["tokenizer_bundle_hash"]),
                artifact_storage_type=_text(row["storage_type"]),
                model_architecture=_text(row["architecture"]),
            )
            for row in rows
        )


class CachedRouteDirectory:
    """Refresh all routes at most once per short TTL, never once per SSE event."""

    __slots__ = ("_clock", "_expires_at", "_lock", "_routes", "_source", "_ttl")

    def __init__(
        self,
        source: RouteSource,
        *,
        ttl_seconds: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 0 < ttl_seconds <= 2.0:
            raise ValueError("route TTL must be in (0, 2] seconds")
        self._source = source
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = asyncio.Lock()
        self._expires_at = 0.0
        self._routes: tuple[GatewayRoute, ...] = ()

    async def _snapshot(self) -> tuple[GatewayRoute, ...]:
        now = self._clock()
        if now < self._expires_at:
            return self._routes
        async with self._lock:
            now = self._clock()
            if now >= self._expires_at:
                routes = await self._source.list_routes()
                self._routes = routes
                self._expires_at = self._clock() + self._ttl
            return self._routes

    async def find(self, public_model_name: str) -> GatewayRoute | None:
        return next(
            (
                route
                for route in await self._snapshot()
                if route.public_model_name == public_model_name
            ),
            None,
        )

    async def list_enabled(self) -> tuple[GatewayRoute, ...]:
        return tuple(route for route in await self._snapshot() if route.enabled)


class InferenceRouteDirectory:
    """Present Gateway routing facts through the protocol-independent core port."""

    __slots__ = ("_route_observer", "_routes")

    def __init__(
        self,
        routes: GatewayRouteDirectory,
        route_observer: Callable[[GatewayRoute], None] | None = None,
    ) -> None:
        self._routes = routes
        self._route_observer = route_observer

    async def find(self, public_model_name: str) -> ServiceRoute | None:
        route = await self._routes.find(public_model_name)
        return None if route is None else self._adapt(route)

    async def list_enabled(self) -> tuple[ServiceRoute, ...]:
        return tuple(self._adapt(route) for route in await self._routes.list_enabled())

    def _adapt(self, route: GatewayRoute) -> ServiceRoute:
        if self._route_observer is not None:
            self._route_observer(route)
        return ServiceRoute(
            service_id=route.service_id,
            instance_id=route.instance_id,
            public_model_name=route.public_model_name,
            enabled=route.enabled,
            ready=(
                route.instance_status == ModelInstanceStatus.RUNNING.value
                and route.internal_endpoint is not None
            ),
            endpoint=route.internal_endpoint,
            runtime_name=route.runtime_name,
            runtime_model_name=route.internal_model_name,
            route_version=route.route_version,
            tokenizer_bundle_path=route.tokenizer_bundle_path,
            tokenizer_bundle_hash=route.tokenizer_bundle_hash,
            artifact_storage_type=route.artifact_storage_type,
            model_architecture=route.model_architecture,
        )


__all__ = [
    "CachedRouteDirectory",
    "GatewayRoute",
    "GatewayRouteDirectory",
    "InferenceRouteDirectory",
    "PostgresRouteSource",
    "RouteSource",
]

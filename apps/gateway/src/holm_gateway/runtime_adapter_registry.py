"""Route Runtime requests through a closed, fail-loud Adapter registry."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Iterable, Mapping
from types import MappingProxyType
from typing import Protocol
from uuid import UUID

import httpx
from holm_domain import ErrorCode, ModelInstanceStatus
from holm_protocols import (
    CanonicalEvent,
    CanonicalRequest,
    InferenceFailure,
    PlatformError,
    RuntimeAdapter,
)

from holm_gateway.routing import GatewayRoute
from holm_gateway.sglang_adapter import SglangRuntimeAdapter
from holm_gateway.vllm_adapter import VllmRuntimeAdapter

_LOGGER = logging.getLogger("holm.gateway.runtime_adapter")


class ManagedRuntimeAdapter(RuntimeAdapter, Protocol):
    async def health(self, endpoint: str) -> bool: ...

    async def sync_routes(self, routes: Iterable[GatewayRoute]) -> None: ...

    async def aclose(self) -> None: ...


def _unavailable() -> InferenceFailure:
    return InferenceFailure(PlatformError(ErrorCode.BACKEND_UNAVAILABLE))


class RuntimeAdapterRegistry:
    """Select an Adapter from route data without a Runtime-name fallback."""

    __slots__ = ("_adapters", "_runtime_by_endpoint")

    def __init__(self, adapters: Mapping[str, ManagedRuntimeAdapter]) -> None:
        if not adapters:
            raise ValueError("Runtime Adapter registry must not be empty")
        self._adapters = MappingProxyType(dict(adapters))
        self._runtime_by_endpoint: dict[str, str] = {}

    def bind_route(self, route: GatewayRoute) -> None:
        """Bind one usable route before the inference core dispatches it."""

        if (
            not route.enabled
            or route.instance_status != ModelInstanceStatus.RUNNING.value
            or route.internal_endpoint is None
        ):
            return
        if route.runtime_name not in self._adapters:
            _LOGGER.error("runtime adapter unavailable")
            raise _unavailable()
        self._runtime_by_endpoint[route.internal_endpoint] = route.runtime_name

    async def sync_routes(self, routes: Iterable[GatewayRoute]) -> None:
        """Replace endpoint bindings and synchronize each Adapter's client pool."""

        route_values = tuple(routes)
        usable = tuple(
            route
            for route in route_values
            if route.enabled
            and route.instance_status == ModelInstanceStatus.RUNNING.value
            and route.internal_endpoint is not None
        )
        bindings: dict[str, str] = {}
        for route in usable:
            if route.runtime_name not in self._adapters:
                _LOGGER.error("runtime adapter unavailable")
                raise _unavailable()
            assert route.internal_endpoint is not None
            bindings[route.internal_endpoint] = route.runtime_name
        self._runtime_by_endpoint = bindings
        for runtime_name, adapter in self._adapters.items():
            await adapter.sync_routes(
                route for route in route_values if route.runtime_name == runtime_name
            )

    async def health(self, runtime_name: str, endpoint: str) -> bool:
        adapter = self._adapters.get(runtime_name)
        if adapter is None:
            _LOGGER.error("runtime adapter unavailable")
            raise _unavailable()
        return await adapter.health(endpoint)

    async def stream(
        self,
        endpoint: str,
        request_id: UUID,
        canonical_request: CanonicalRequest,
    ) -> AsyncGenerator[CanonicalEvent, None]:
        runtime_name = self._runtime_by_endpoint.get(endpoint)
        adapter = None if runtime_name is None else self._adapters.get(runtime_name)
        if adapter is None:
            _LOGGER.error("runtime adapter unavailable")
            raise _unavailable()
        async for event in adapter.stream(endpoint, request_id, canonical_request):
            yield event

    async def aclose(self) -> None:
        for adapter in self._adapters.values():
            await adapter.aclose()


def create_runtime_adapter_registry(
    client: httpx.AsyncClient | None = None,
    *,
    max_response_bytes: int,
    max_sse_event_bytes: int,
    pool_max_connections: int,
    pool_timeout_seconds: float,
    connect_timeout_seconds: float | None,
) -> RuntimeAdapterRegistry:
    """Build the immutable Runtime-name registry once at startup."""

    return RuntimeAdapterRegistry(
        {
            "vllm": VllmRuntimeAdapter(
                client,
                max_response_bytes=max_response_bytes,
                max_sse_event_bytes=max_sse_event_bytes,
                pool_max_connections=pool_max_connections,
                pool_timeout_seconds=pool_timeout_seconds,
                connect_timeout_seconds=connect_timeout_seconds,
            ),
            "sglang": SglangRuntimeAdapter(
                client,
                max_response_bytes=max_response_bytes,
                max_sse_event_bytes=max_sse_event_bytes,
                pool_max_connections=pool_max_connections,
                pool_timeout_seconds=pool_timeout_seconds,
                connect_timeout_seconds=connect_timeout_seconds,
            ),
        }
    )


__all__ = [
    "ManagedRuntimeAdapter",
    "RuntimeAdapterRegistry",
    "create_runtime_adapter_registry",
]

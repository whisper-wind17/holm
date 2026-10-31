"""Ports used by Protocol Adapters, the inference core, and Runtime Adapters."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from holm_protocols.models import (
    CanonicalEvent,
    CanonicalRequest,
    PlatformError,
)


@dataclass(frozen=True, slots=True)
class ProtocolRoute:
    method: str
    path: str
    operation: str


@dataclass(frozen=True, slots=True)
class MappedError:
    status_code: int
    body: bytes
    media_type: str
    headers: tuple[tuple[str, str], ...] = ()


class ProtocolAdapter(Protocol):
    """The six independent responsibilities."""

    def routes(self) -> tuple[ProtocolRoute, ...]: ...

    def extract_credential(self, headers: Mapping[str, str]) -> str | None: ...

    def parse_request(self, body: bytes, *, runtime_name: str) -> CanonicalRequest: ...

    def encode_stream_event(self, event: CanonicalEvent) -> bytes | None: ...

    def encode_non_stream(self, events: tuple[CanonicalEvent, ...]) -> bytes: ...

    def map_error(self, error: PlatformError, request_id: UUID) -> MappedError: ...


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    user_id: UUID
    api_key_id: UUID
    scoped_service_ids: frozenset[UUID]
    user_is_admin: bool
    authorized_service_ids: frozenset[UUID]
    service_limits: Mapping[UUID, tuple[int | None, int | None]] = field(default_factory=dict)

    def allows_service(self, service_id: UUID) -> bool:
        key_allows = not self.scoped_service_ids or service_id in self.scoped_service_ids
        user_allows = service_id in self.authorized_service_ids
        return key_allows and user_allows


class AuthenticationPort(Protocol):
    async def authenticate(self, credential: str | None) -> AuthenticatedPrincipal: ...


@dataclass(frozen=True, slots=True)
class ServiceRoute:
    service_id: UUID
    instance_id: UUID | None
    public_model_name: str
    enabled: bool
    ready: bool
    endpoint: str | None
    runtime_name: str
    runtime_model_name: str
    route_version: int = 1
    # Facts the Gateway needs to meter this route from its Artifact's
    # published Tokenizer Bundle without a PostgreSQL query on the hot path.
    tokenizer_bundle_path: str | None = None
    tokenizer_bundle_hash: str | None = None
    artifact_storage_type: str | None = None
    model_architecture: str | None = None


class RouteDirectory(Protocol):
    async def find(self, public_model_name: str) -> ServiceRoute | None: ...

    async def list_enabled(self) -> tuple[ServiceRoute, ...]: ...


class RequestLifecycle(Protocol):
    def set_stream(self, stream: bool) -> None: ...

    def begin_received(self, input_bytes: int) -> None: ...

    async def raise_if_received_unavailable(self) -> None: ...

    def set_authenticated(self, principal: AuthenticatedPrincipal) -> None: ...

    def set_route(self, route: ServiceRoute) -> None: ...

    async def mark_in_progress(self, requested_max_tokens: int | None) -> None: ...

    def mark_forwarded(self) -> None: ...

    def begin_metering(self, request: CanonicalRequest) -> None: ...

    def observe_event(self, event: CanonicalEvent) -> None: ...

    async def mark_error(self, error: PlatformError) -> None: ...

    async def mark_aborted(self) -> None: ...

    async def mark_completed(self) -> None: ...


class RuntimeAdapter(Protocol):
    def stream(
        self,
        endpoint: str,
        request_id: UUID,
        canonical_request: CanonicalRequest,
    ) -> AsyncGenerator[CanonicalEvent, None]: ...

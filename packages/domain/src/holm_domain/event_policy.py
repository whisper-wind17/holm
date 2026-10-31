"""Default-deny Domain Event registration, payload construction, and visibility."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import InitVar, dataclass
from types import MappingProxyType
from typing import Final
from uuid import UUID

from holm_domain.authorization import (
    AuthorizationSubject,
    Capability,
    PermissionScope,
    permission_scope,
)
from holm_domain.events import FrozenJsonValue, JsonValue, freeze_json_object, thaw_json_object

MAX_EVENT_NAME_LENGTH: Final[int] = 128
FORBIDDEN_PAYLOAD_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "request",
        "request_body",
        "response",
        "response_body",
        "messages",
        "prompt",
        "tool_arguments",
        "code",
        "generated_result",
    }
)
_PAYLOAD_BUILDER_TOKEN: Final[object] = object()
_DRAFT_BUILDER_TOKEN: Final[object] = object()
_SAFE_EVENT_NAME: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_GLOBAL_EVENT_SCOPES: Final[frozenset[PermissionScope]] = frozenset(
    {
        PermissionScope.ALL,
        PermissionScope.ALLOWED,
        PermissionScope.CALL_METADATA,
    }
)


class EventRegistrationError(ValueError):
    """An event type is absent, duplicated, or cannot be proven safe."""


class AllowlistedEventPayload(Mapping[str, FrozenJsonValue]):
    """Branded immutable payload obtainable only from ``EventTypeRegistry``."""

    __slots__ = ("_event", "_values")

    def __init__(
        self,
        event: str,
        values: Mapping[str, object],
        *,
        _builder_token: object,
    ) -> None:
        if _builder_token is not _PAYLOAD_BUILDER_TOKEN:
            raise TypeError("event payloads must be constructed by EventTypeRegistry")
        self._event = event
        self._values = freeze_json_object(values)

    @property
    def event(self) -> str:
        return self._event

    def __getitem__(self, key: str) -> FrozenJsonValue:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def json(self) -> dict[str, JsonValue]:
        return thaw_json_object(self._values)


@dataclass(frozen=True, slots=True)
class EventTypeDeclaration:
    """One event's capability, payload allowlist, and object-scope requirement."""

    event: str
    required_capability: Capability
    payload_fields: frozenset[str]
    requires_object_authorization: bool = False

    def __post_init__(self) -> None:
        if (
            len(self.event) > MAX_EVENT_NAME_LENGTH
            or _SAFE_EVENT_NAME.fullmatch(self.event) is None
        ):
            raise ValueError("event name must be a safe token between 1 and 128 characters")
        if not isinstance(self.required_capability, Capability):
            raise TypeError("required_capability must be a Capability")
        if any(not field or field != field.strip() for field in self.payload_fields):
            raise ValueError("payload field names must be non-empty and trimmed")
        forbidden = sorted(self.payload_fields.intersection(FORBIDDEN_PAYLOAD_FIELDS))
        if forbidden:
            raise ValueError(
                "Domain Event payload fields contain body data: " + ", ".join(forbidden)
            )


@dataclass(frozen=True, slots=True)
class DomainEventDraft:
    """A transaction-bound event whose payload has passed the registry builder."""

    event: str
    aggregate_id: UUID
    version: int
    payload: AllowlistedEventPayload
    _builder_token: InitVar[object]

    def __post_init__(self, _builder_token: object) -> None:
        if _builder_token is not _DRAFT_BUILDER_TOKEN:
            raise TypeError("Domain Event drafts must be constructed by EventTypeRegistry")
        if not isinstance(self.aggregate_id, UUID):
            raise TypeError("aggregate_id must be a UUID")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version <= 0:
            raise ValueError("version must be a positive integer")
        if not isinstance(self.payload, AllowlistedEventPayload):
            raise TypeError("payload must come from EventTypeRegistry.build_payload")
        if self.payload.event != self.event:
            raise ValueError("payload was built for a different event type")


class EventTypeRegistry:
    """Immutable non-empty registry used by both writers and SSE visibility."""

    __slots__ = ("_declarations",)

    def __init__(self, declarations: Iterable[EventTypeDeclaration]) -> None:
        declared: dict[str, EventTypeDeclaration] = {}
        for declaration in declarations:
            if not isinstance(declaration, EventTypeDeclaration):
                raise TypeError("event declarations must be EventTypeDeclaration values")
            if declaration.event in declared:
                raise EventRegistrationError(f"duplicate event type: {declaration.event}")
            declared[declaration.event] = declaration
        if not declared:
            raise EventRegistrationError("event registry must not be empty")
        self._declarations = MappingProxyType(declared)

    @property
    def event_types(self) -> frozenset[str]:
        return frozenset(self._declarations)

    def declaration_for(self, event: str) -> EventTypeDeclaration:
        try:
            return self._declarations[event]
        except KeyError:
            raise EventRegistrationError(f"unregistered event type: {event}") from None

    def build_payload(
        self,
        event: str,
        candidates: Mapping[str, object],
    ) -> AllowlistedEventPayload:
        """Copy only declared fields, starting from an empty object."""
        declaration = self.declaration_for(event)
        selected = {
            field: candidates[field] for field in declaration.payload_fields if field in candidates
        }
        return AllowlistedEventPayload(
            event,
            selected,
            _builder_token=_PAYLOAD_BUILDER_TOKEN,
        )

    def build_draft(
        self,
        event: str,
        *,
        aggregate_id: UUID,
        version: int,
        candidates: Mapping[str, object],
    ) -> DomainEventDraft:
        """Construct the only draft shape accepted by the persistence writer."""
        payload = self.build_payload(event, candidates)
        return DomainEventDraft(
            event=event,
            aggregate_id=aggregate_id,
            version=version,
            payload=payload,
            _builder_token=_DRAFT_BUILDER_TOKEN,
        )

    def can_deliver(self, event: str, subject: AuthorizationSubject) -> bool:
        """Fail closed for unknown and object-scoped events."""
        declaration = self._declarations.get(event)
        if declaration is None or declaration.requires_object_authorization:
            return False
        return (
            permission_scope(subject.role, declaration.required_capability) in _GLOBAL_EVENT_SCOPES
        )


__all__ = [
    "AllowlistedEventPayload",
    "DomainEventDraft",
    "EventRegistrationError",
    "EventTypeDeclaration",
    "EventTypeRegistry",
    "FORBIDDEN_PAYLOAD_FIELDS",
    "MAX_EVENT_NAME_LENGTH",
]

"""Registered business Domain Events shipped by the production applications."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Final

from holm_domain.authorization import Capability
from holm_domain.event_policy import EventTypeDeclaration, EventTypeRegistry


class DomainEventType(StrEnum):
    """The complete production Domain Event vocabulary."""

    INSTANCE_CLEANUP_REQUESTED = "instance.cleanup_requested"
    INSTANCE_CLEANUP_COMPLETED = "instance.cleanup_completed"
    INSTANCE_CLEANUP_FAILED = "instance.cleanup_failed"
    INSTANCE_VERIFICATION_REQUESTED = "instance.verification_requested"
    INSTANCE_VERIFICATION_QUEUED = "instance.verification_queued"
    INSTANCE_VERIFICATION_COMPLETED = "instance.verification_completed"
    INSTANCE_LAUNCH_AUTHORIZED = "instance.launch_authorized"
    DOWNLOAD_RESOLUTION_STARTED = "download.resolution_started"
    INSTANCE_RUNTIME_ORPHAN_ISOLATED = "instance.runtime_orphan_isolated"
    API_KEY_CHANGED = "api_key.changed"
    USER_ACCESS_CHANGED = "user.access_changed"
    AI_SERVICE_CHANGED = "ai_service.changed"
    INSTANCE_ROUTE_CHANGED = "instance.route_changed"


PRODUCTION_EVENT_DECLARATIONS: Final[tuple[EventTypeDeclaration, ...]] = (
    EventTypeDeclaration(
        event=DomainEventType.INSTANCE_CLEANUP_REQUESTED.value,
        required_capability=Capability.MANAGE_MODEL_INSTANCES,
        payload_fields=frozenset(),
    ),
    EventTypeDeclaration(
        event=DomainEventType.INSTANCE_CLEANUP_COMPLETED.value,
        required_capability=Capability.MANAGE_MODEL_INSTANCES,
        payload_fields=frozenset(),
    ),
    EventTypeDeclaration(
        event=DomainEventType.INSTANCE_CLEANUP_FAILED.value,
        required_capability=Capability.MANAGE_MODEL_INSTANCES,
        payload_fields=frozenset({"reason"}),
    ),
    EventTypeDeclaration(
        event=DomainEventType.INSTANCE_VERIFICATION_REQUESTED.value,
        required_capability=Capability.MANAGE_MODEL_INSTANCES,
        payload_fields=frozenset(
            {"request_id", "artifact_id", "config_version", "status", "attempt"}
        ),
    ),
    EventTypeDeclaration(
        event=DomainEventType.INSTANCE_VERIFICATION_QUEUED.value,
        required_capability=Capability.MANAGE_MODEL_INSTANCES,
        payload_fields=frozenset({"job_id"}),
    ),
    EventTypeDeclaration(
        event=DomainEventType.INSTANCE_VERIFICATION_COMPLETED.value,
        required_capability=Capability.MANAGE_MODEL_INSTANCES,
        payload_fields=frozenset({"result"}),
    ),
    EventTypeDeclaration(
        event=DomainEventType.INSTANCE_LAUNCH_AUTHORIZED.value,
        required_capability=Capability.MANAGE_MODEL_INSTANCES,
        payload_fields=frozenset(),
    ),
    EventTypeDeclaration(
        event=DomainEventType.DOWNLOAD_RESOLUTION_STARTED.value,
        required_capability=Capability.MANAGE_LOCAL_MODELS,
        payload_fields=frozenset({"status"}),
    ),
    EventTypeDeclaration(
        event=DomainEventType.INSTANCE_RUNTIME_ORPHAN_ISOLATED.value,
        required_capability=Capability.MANAGE_MODEL_INSTANCES,
        payload_fields=frozenset({"reason"}),
    ),
    EventTypeDeclaration(
        event=DomainEventType.API_KEY_CHANGED.value,
        required_capability=Capability.MANAGE_USERS,
        payload_fields=frozenset(),
    ),
    EventTypeDeclaration(
        event=DomainEventType.USER_ACCESS_CHANGED.value,
        required_capability=Capability.MANAGE_USERS,
        payload_fields=frozenset(),
    ),
    EventTypeDeclaration(
        event=DomainEventType.AI_SERVICE_CHANGED.value,
        required_capability=Capability.MANAGE_AI_SERVICES,
        payload_fields=frozenset(),
    ),
    EventTypeDeclaration(
        event=DomainEventType.INSTANCE_ROUTE_CHANGED.value,
        required_capability=Capability.MANAGE_MODEL_INSTANCES,
        payload_fields=frozenset(),
    ),
)

PRODUCTION_EVENT_REGISTRY: Final[EventTypeRegistry] = EventTypeRegistry(
    PRODUCTION_EVENT_DECLARATIONS
)


def assert_production_event_registry_complete(
    event_types: Iterable[DomainEventType] = DomainEventType,
    registry: EventTypeRegistry = PRODUCTION_EVENT_REGISTRY,
) -> None:
    """Fail loudly when the declared vocabulary and registered policy diverge."""

    expected = frozenset(event_type.value for event_type in event_types)
    actual = registry.event_types
    if actual != expected:
        raise AssertionError(
            "production Domain Event registry drift: "
            f"missing={sorted(expected - actual)}, unexpected={sorted(actual - expected)}"
        )


assert_production_event_registry_complete()


__all__ = [
    "DomainEventType",
    "PRODUCTION_EVENT_DECLARATIONS",
    "PRODUCTION_EVENT_REGISTRY",
    "assert_production_event_registry_complete",
]

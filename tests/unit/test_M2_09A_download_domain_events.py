"""M2-09A production Domain Event registration and payload guard."""

from __future__ import annotations

from enum import StrEnum

import pytest
from holm_domain import (
    PRODUCTION_EVENT_REGISTRY,
    DomainEventType,
    assert_production_event_registry_complete,
)


def test_M2_09A_pending_12_production_event_vocabulary_is_registered_exactly() -> None:
    expected = frozenset(
        {
            "download.resolution_started",
            "instance.runtime_orphan_isolated",
            "api_key.changed",
            "user.access_changed",
            "ai_service.changed",
            "instance.route_changed",
            "instance.verification_requested",
            "instance.verification_queued",
            "instance.verification_completed",
            "instance.launch_authorized",
            "instance.cleanup_requested",
            "instance.cleanup_completed",
            "instance.cleanup_failed",
        }
    )
    assert frozenset(event.value for event in DomainEventType) == expected
    assert PRODUCTION_EVENT_REGISTRY.event_types == expected
    assert_production_event_registry_complete()

    payload = PRODUCTION_EVENT_REGISTRY.build_payload(
        DomainEventType.DOWNLOAD_RESOLUTION_STARTED.value,
        {
            "status": "Resolving",
            "source_key": "M2-09A-SOURCE-SENTINEL",
            "repository_id": "M2-09A-REPOSITORY-SENTINEL",
            "credential": "M2-09A-CREDENTIAL-SENTINEL",
        },
    )
    assert payload.json() == {"status": "Resolving"}


def test_M2_09A_pending_12_positive_control_unregistered_production_type_fails() -> None:
    class MutatedDomainEventType(StrEnum):
        DOWNLOAD_RESOLUTION_STARTED = "download.resolution_started"
        UNREGISTERED = "download.unregistered_mutant"

    with pytest.raises(AssertionError, match="download.unregistered_mutant"):
        assert_production_event_registry_complete(MutatedDomainEventType)  # type: ignore[arg-type]

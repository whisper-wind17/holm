"""M1-05 default-deny event policy and bounded process fan-out tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest
from holm_control_api import EventFanout
from holm_domain import (
    AuthorizationSubject,
    Capability,
    DomainEvent,
    DomainEventDraft,
    EventRegistrationError,
    EventTypeDeclaration,
    EventTypeRegistry,
    UserRole,
)

ADMIN_EVENT = "test.system.changed"
SHARED_EVENT = "test.usage.changed"
OBJECT_EVENT = "test.service.permission.changed"
UNKNOWN_EVENT = "test.unknown"
AGGREGATE_ID = UUID("018f47f2-8c00-7000-8000-000000000501")
ADMIN_ID = UUID("018f47f2-8c00-7000-8000-000000000502")
USER_ID = UUID("018f47f2-8c00-7000-8000-000000000503")


def _registry() -> EventTypeRegistry:
    return EventTypeRegistry(
        (
            EventTypeDeclaration(
                ADMIN_EVENT,
                Capability.VIEW_SYSTEM,
                frozenset({"status"}),
            ),
            EventTypeDeclaration(
                SHARED_EVENT,
                Capability.VIEW_USAGE,
                frozenset({"count"}),
            ),
            EventTypeDeclaration(
                OBJECT_EVENT,
                Capability.VIEW_AI_SERVICES,
                frozenset({"service_id"}),
                requires_object_authorization=True,
            ),
        )
    )


def _subject(role: UserRole) -> AuthorizationSubject:
    return AuthorizationSubject(
        user_id=ADMIN_ID if role is UserRole.ADMIN else USER_ID,
        role=role,
    )


def _event(event_id: int, name: str, payload: dict[str, object]) -> DomainEvent:
    return DomainEvent(
        id=event_id,
        event=name,
        aggregate_id=AGGREGATE_ID,
        version=event_id,
        occurred_at=datetime(2026, 8, 29, 1, 2, 3, tzinfo=UTC),
        payload=payload,
    )


def test_M1_05_payload_allowlist_starts_empty_and_brands_the_draft() -> None:
    registry = _registry()
    candidates = {
        "status": "ready",
        "prompt": "M1-05-BODY-SENTINEL",
        "messages": [{"content": "M1-05-BODY-SENTINEL"}],
        "tool_arguments": {"secret": "M1-05-BODY-SENTINEL"},
    }
    draft = registry.build_draft(
        ADMIN_EVENT,
        aggregate_id=AGGREGATE_ID,
        version=1,
        candidates=candidates,
    )

    candidates["status"] = "mutated"
    assert draft.payload.json() == {"status": "ready"}
    with pytest.raises(TypeError, match="constructed by EventTypeRegistry"):
        DomainEventDraft(
            event=ADMIN_EVENT,
            aggregate_id=AGGREGATE_ID,
            version=1,
            payload=draft.payload,
            _builder_token=object(),
        )
    with pytest.raises(TypeError, match="constructed by EventTypeRegistry"):
        type(draft.payload)(ADMIN_EVENT, {"prompt": "bypass"}, _builder_token=object())
    with pytest.raises(ValueError, match="body data"):
        EventTypeDeclaration(
            "test.invalid.body",
            Capability.VIEW_SYSTEM,
            frozenset({"prompt"}),
        )
    with pytest.raises(EventRegistrationError, match="unregistered"):
        registry.build_payload(UNKNOWN_EVENT, {})
    with pytest.raises(ValueError, match="safe token"):
        EventTypeDeclaration(
            "test.safe\nid: 999",
            Capability.VIEW_SYSTEM,
            frozenset(),
        )


def test_M1_05_registry_is_nonempty_and_draft_builder_rejects_unknown_producers() -> None:
    registry = _registry()
    registry.build_draft(
        ADMIN_EVENT,
        aggregate_id=AGGREGATE_ID,
        version=1,
        candidates={"status": "ready"},
    )

    with pytest.raises(EventRegistrationError, match="must not be empty"):
        EventTypeRegistry(())
    with pytest.raises(EventRegistrationError, match=UNKNOWN_EVENT):
        registry.build_draft(
            UNKNOWN_EVENT,
            aggregate_id=AGGREGATE_ID,
            version=1,
            candidates={},
        )


def test_M1_05_visibility_filters_before_queueing_and_object_scope_fails_closed() -> None:
    async def exercise() -> None:
        fanout = EventFanout(_registry(), queue_size=2)
        admin = fanout.subscribe(_subject(UserRole.ADMIN))
        user = fanout.subscribe(_subject(UserRole.USER))

        await fanout.publish(_event(1, ADMIN_EVENT, {"status": "ready"}))
        assert (await admin.receive()).id == 1  # type: ignore[union-attr]
        assert user.queue_length == 0

        await fanout.publish(_event(2, SHARED_EVENT, {"count": 2}))
        assert (await admin.receive()).id == 2  # type: ignore[union-attr]
        # VIEW_USAGE is ALL for Admin but OWN for User. Until object-level
        # filtering arrives in M4-02, the User side must fail closed.
        assert user.queue_length == 0

        await fanout.publish(_event(3, OBJECT_EVENT, {"service_id": str(AGGREGATE_ID)}))
        assert admin.queue_length == user.queue_length == 0

    asyncio.run(exercise())


def test_M1_05_slow_subscriber_is_bounded_and_overflow_requires_resync() -> None:
    async def exercise() -> None:
        fanout = EventFanout(_registry(), queue_size=2)
        subscriber = fanout.subscribe(_subject(UserRole.ADMIN))
        for event_id in range(1, 101):
            await fanout.publish(_event(event_id, SHARED_EVENT, {"count": event_id}))

        assert subscriber.overflowed is True
        assert subscriber.queue_length <= subscriber.queue_capacity == 2
        assert fanout.subscriber_count == 0
        signal = await subscriber.receive()
        assert not isinstance(signal, DomainEvent)

    asyncio.run(exercise())


def test_M1_05_unknown_persisted_event_is_rejected_loudly_without_queue_leak() -> None:
    async def exercise() -> None:
        failures: list[str] = []
        fanout = EventFanout(
            _registry(),
            registration_failure=failures.append,
        )
        subscriber = fanout.subscribe(_subject(UserRole.ADMIN))
        await fanout.publish(_event(9, UNKNOWN_EVENT, {"body": "M1-05-BODY-SENTINEL"}))
        assert failures == [UNKNOWN_EVENT]
        assert subscriber.queue_length == 0

    asyncio.run(exercise())


def test_M1_05_many_connections_share_one_process_fanout() -> None:
    async def exercise() -> None:
        fanout = EventFanout(_registry(), queue_size=2)
        subscribers = [fanout.subscribe(_subject(UserRole.ADMIN)) for _ in range(512)]
        assert fanout.subscriber_count == 512

        await fanout.publish(_event(1, SHARED_EVENT, {"count": 1}))
        assert all(subscriber.queue_length == 1 for subscriber in subscribers)
        for subscriber in subscribers:
            fanout.unsubscribe(subscriber)
        assert fanout.subscriber_count == 0

    asyncio.run(exercise())

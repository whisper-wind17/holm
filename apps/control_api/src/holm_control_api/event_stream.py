"""Process-local SSE fan-out, bounded subscribers, replay, and frame encoding."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from typing import Final

from holm_contracts import DomainEventEnvelope
from holm_domain import (
    AuthorizationSubject,
    DomainEvent,
    ErrorCode,
    EventRegistrationError,
    EventTypeRegistry,
)
from holm_persistence import (
    DEFAULT_EVENT_PAGE_SIZE,
    DEFAULT_EVENT_POLL_SECONDS,
    AsyncSessionFactory,
    DomainEventReader,
    DomainEventRepository,
    NotificationListener,
    ReplayAnchorStatus,
)

DEFAULT_SUBSCRIBER_QUEUE_SIZE: Final[int] = 64
DEFAULT_KEEPALIVE_SECONDS: Final[float] = 15.0
MAX_LAST_EVENT_ID_LENGTH: Final[int] = 19
MAX_EVENT_ID: Final[int] = (1 << 63) - 1
_KEEPALIVE_FRAME: Final[bytes] = b": keepalive\n\n"
_LOGGER: Final[logging.Logger] = logging.getLogger("holm.event_stream")


@dataclass(frozen=True, slots=True)
class _ResyncRequired:
    pass


_RESYNC_REQUIRED: Final[_ResyncRequired] = _ResyncRequired()
type SubscriberItem = DomainEvent | _ResyncRequired


class EventSubscriber:
    """One bounded connection queue; overflow permanently closes continuity."""

    __slots__ = ("_closed", "_overflowed", "_queue", "subject")

    def __init__(self, subject: AuthorizationSubject, *, queue_size: int) -> None:
        self.subject = subject
        self._queue: asyncio.Queue[SubscriberItem] = asyncio.Queue(maxsize=queue_size)
        self._overflowed = False
        self._closed = False

    @property
    def overflowed(self) -> bool:
        return self._overflowed

    @property
    def queue_length(self) -> int:
        return self._queue.qsize()

    @property
    def queue_capacity(self) -> int:
        return self._queue.maxsize

    def offer(self, event: DomainEvent) -> None:
        if self._closed or self._overflowed:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._overflowed = True
            while True:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            self._queue.put_nowait(_RESYNC_REQUIRED)

    async def receive(self) -> SubscriberItem:
        return await self._queue.get()

    def receive_nowait(self) -> SubscriberItem:
        return self._queue.get_nowait()

    def close(self) -> None:
        self._closed = True


class EventFanout:
    """Filter before enqueueing so denied events leak neither frames nor pressure."""

    __slots__ = ("_queue_size", "_registry", "_registration_failure", "_subscribers")

    def __init__(
        self,
        registry: EventTypeRegistry,
        *,
        queue_size: int = DEFAULT_SUBSCRIBER_QUEUE_SIZE,
        registration_failure: Callable[[str], None] | None = None,
    ) -> None:
        if queue_size < 1:
            raise ValueError("subscriber queue size must be positive")
        self._registry = registry
        self._queue_size = queue_size
        self._registration_failure = (
            self._log_registration_failure if registration_failure is None else registration_failure
        )
        self._subscribers: set[EventSubscriber] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self, subject: AuthorizationSubject) -> EventSubscriber:
        subscriber = EventSubscriber(subject, queue_size=self._queue_size)
        self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: EventSubscriber) -> None:
        subscriber.close()
        self._subscribers.discard(subscriber)

    async def publish(self, event: DomainEvent) -> None:
        try:
            self._registry.declaration_for(event.event)
        except EventRegistrationError:
            self._registration_failure(event.event)
            return
        for subscriber in tuple(self._subscribers):
            if self._registry.can_deliver(event.event, subscriber.subject):
                subscriber.offer(event)
                if subscriber.overflowed:
                    self._subscribers.discard(subscriber)

    def can_deliver(self, event: DomainEvent, subject: AuthorizationSubject) -> bool:
        return self._registry.can_deliver(event.event, subject)

    @staticmethod
    def _log_registration_failure(event: str) -> None:
        _LOGGER.error(
            "unregistered Domain Event rejected",
            extra={"component": "control-api", "error_code": "unregistered_domain_event"},
        )


class EventStreamService:
    """Own the sole process reader and create short-lived replay sessions."""

    __slots__ = (
        "_fanout",
        "_keepalive_seconds",
        "_page_size",
        "_reader",
        "_session_factory",
    )

    def __init__(
        self,
        session_factory: AsyncSessionFactory,
        registry: EventTypeRegistry,
        *,
        listener: NotificationListener | None,
        poll_seconds: float | None = DEFAULT_EVENT_POLL_SECONDS,
        page_size: int = DEFAULT_EVENT_PAGE_SIZE,
        subscriber_queue_size: int = DEFAULT_SUBSCRIBER_QUEUE_SIZE,
        keepalive_seconds: float = DEFAULT_KEEPALIVE_SECONDS,
    ) -> None:
        if keepalive_seconds <= 0:
            raise ValueError("keepalive_seconds must be positive")
        self._session_factory = session_factory
        self._fanout = EventFanout(registry, queue_size=subscriber_queue_size)
        self._page_size = page_size
        self._keepalive_seconds = keepalive_seconds
        self._reader = DomainEventReader(
            session_factory,
            self._fanout.publish,
            listener=listener,
            poll_seconds=poll_seconds,
            page_size=page_size,
        )

    @property
    def fanout(self) -> EventFanout:
        return self._fanout

    @property
    def reader(self) -> DomainEventReader:
        return self._reader

    async def start(self) -> None:
        await self._reader.start()

    async def stop(self) -> None:
        await self._reader.stop()

    async def stream(
        self,
        subject: AuthorizationSubject,
        last_event_id: str | None,
    ) -> AsyncGenerator[bytes, None]:
        anchor = _parse_last_event_id(last_event_id)
        if last_event_id is not None and anchor is None:
            yield _resync_frame()
            return

        subscriber = self._fanout.subscribe(subject)
        try:
            resync_required = False
            async with self._session_factory() as session:
                repository = DomainEventRepository(session)
                if anchor is None:
                    cursor = await repository.stable_frontier()
                    frontier = cursor
                else:
                    validation = await repository.replay_anchor(anchor)
                    if validation.status is not ReplayAnchorStatus.VALID:
                        cursor = frontier = 0
                        resync_required = True
                    else:
                        cursor = anchor
                        frontier = await repository.stable_frontier()
                        resync_required = cursor > frontier
            if resync_required:
                yield _resync_frame()
                return

            while cursor < frontier:
                if subscriber.overflowed:
                    yield _resync_frame()
                    return
                # Fetch a bounded page and release its connection before
                # yielding any bytes. ASGI backpressure can therefore pause a
                # replaying client without pinning a database pool slot or the
                # advisory read fence.
                async with self._session_factory() as session:
                    page = await DomainEventRepository(session).stable_page(
                        after_id=cursor,
                        through_id=frontier,
                        limit=self._page_size,
                    )
                if not page:
                    break
                for event in page:
                    cursor = event.id
                    if self._fanout.can_deliver(event, subject):
                        yield _event_frame(event)

            # Events published during replay are buffered before the replay
            # query starts. Drop only those at/below its proven frontier.
            while True:
                try:
                    item = subscriber.receive_nowait()
                except asyncio.QueueEmpty:
                    break
                if isinstance(item, _ResyncRequired):
                    yield _resync_frame()
                    return
                if item.id > frontier:
                    yield _event_frame(item)

            while True:
                try:
                    item = await asyncio.wait_for(
                        subscriber.receive(),
                        timeout=self._keepalive_seconds,
                    )
                except TimeoutError:
                    yield _KEEPALIVE_FRAME
                    continue
                if isinstance(item, _ResyncRequired):
                    yield _resync_frame()
                    return
                yield _event_frame(item)
        finally:
            self._fanout.unsubscribe(subscriber)


def _parse_last_event_id(value: str | None) -> int | None:
    if value is None:
        return None
    if not value or len(value) > MAX_LAST_EVENT_ID_LENGTH or not value.isascii():
        return None
    if not value.isdigit() or value.startswith("0"):
        return None
    parsed = int(value)
    return parsed if 0 < parsed <= MAX_EVENT_ID else None


def _event_frame(event: DomainEvent) -> bytes:
    envelope = DomainEventEnvelope.from_domain(event)
    data = envelope.model_dump_json()
    return f"id: {event.id}\nevent: {event.event}\ndata: {data}\n\n".encode()


def _resync_frame() -> bytes:
    data = json.dumps(
        {"code": ErrorCode.RESYNC_REQUIRED.value},
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return f"event: {ErrorCode.RESYNC_REQUIRED.value}\ndata: {data}\n\n".encode()


__all__ = [
    "DEFAULT_KEEPALIVE_SECONDS",
    "DEFAULT_SUBSCRIBER_QUEUE_SIZE",
    "EventFanout",
    "EventStreamService",
    "EventSubscriber",
]

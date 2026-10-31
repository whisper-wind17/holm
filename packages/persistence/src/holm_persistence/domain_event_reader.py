"""One process-local Domain Event reader with NOTIFY hints and polling compensation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from enum import StrEnum
from typing import Final, Protocol

import psycopg
from holm_domain import DomainEvent
from sqlalchemy.engine import URL

from holm_persistence.database import DEFAULT_CONNECT_TIMEOUT_SECONDS, AsyncSessionFactory
from holm_persistence.domain_event_repository import (
    DEFAULT_EVENT_PAGE_SIZE,
    DomainEventRepository,
)
from holm_persistence.domain_events import DOMAIN_EVENT_NOTIFY_CHANNEL

DEFAULT_EVENT_POLL_SECONDS: Final[float] = 1.0
DEFAULT_LISTENER_RETRY_INITIAL_SECONDS: Final[float] = 0.1
DEFAULT_LISTENER_RETRY_MAX_SECONDS: Final[float] = 5.0
DEFAULT_LISTENER_READY_TIMEOUT_SECONDS: Final[float] = 10.0
_LOGGER: Final[logging.Logger] = logging.getLogger("holm.domain_event_reader")

type EventSink = Callable[[DomainEvent], Awaitable[None]]
type FailureSink = Callable[["DomainEventReaderFailure"], None]


class DomainEventReaderFailure(StrEnum):
    LISTENER_UNAVAILABLE = "listener_unavailable"
    INVALID_NOTIFICATION = "invalid_notification"
    POLLING_UNAVAILABLE = "polling_unavailable"


class NotificationListener(Protocol):
    async def listen(self, wake: asyncio.Event, stop: asyncio.Event) -> None: ...

    async def wait_ready(self) -> None: ...


class PsycopgNotificationListener:
    """Dedicated async psycopg LISTEN connection; payload must remain empty."""

    __slots__ = (
        "_connect_timeout_seconds",
        "_database",
        "_host",
        "_password",
        "_port",
        "_user",
        "ready",
    )

    def __init__(
        self,
        url: URL,
        *,
        connect_timeout_seconds: int = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    ) -> None:
        if not url.drivername.startswith("postgresql"):
            raise ValueError("notification listener requires PostgreSQL")
        if url.database is None:
            raise ValueError("notification listener requires a database name")
        if connect_timeout_seconds < 1:
            raise ValueError("connect_timeout_seconds must be at least one second")
        self._database = url.database
        self._host = url.host
        self._port = url.port
        self._user = url.username
        self._password = url.password
        self._connect_timeout_seconds = connect_timeout_seconds
        self.ready = asyncio.Event()

    async def listen(self, wake: asyncio.Event, stop: asyncio.Event) -> None:
        connection = await psycopg.AsyncConnection.connect(
            dbname=self._database,
            host=self._host,
            port=self._port,
            user=self._user,
            password=self._password,
            autocommit=True,
            connect_timeout=self._connect_timeout_seconds,
        )
        try:
            async with asyncio.timeout(self._connect_timeout_seconds):
                await connection.execute(f"LISTEN {DOMAIN_EVENT_NOTIFY_CHANNEL}")
            self.ready.set()
            # A reconnect may have missed notifications while the socket was
            # down. Wake the database reader immediately; NOTIFY never carries
            # the data and reconnect catch-up is therefore lossless.
            wake.set()
            while not stop.is_set():
                async for notification in connection.notifies(timeout=1.0, stop_after=1):
                    if notification.payload:
                        raise ValueError("Domain Event notification payload must be empty")
                    wake.set()
        finally:
            self.ready.clear()
            await connection.close()

    async def wait_ready(self) -> None:
        await self.ready.wait()


def _default_failure_sink(failure: DomainEventReaderFailure) -> None:
    _LOGGER.warning(
        "domain event reader degraded",
        extra={"component": "control-api", "error_code": failure.value},
    )


class DomainEventReader:
    """Share one ordered database cursor across every SSE connection in a process."""

    __slots__ = (
        "_cursor",
        "_failure_sink",
        "_listener",
        "_listener_retry_initial_seconds",
        "_listener_retry_max_seconds",
        "_listener_ready_timeout_seconds",
        "_listener_task",
        "_page_size",
        "_poll_seconds",
        "_poll_task",
        "_session_factory",
        "_sink",
        "_stop",
        "_wake",
    )

    def __init__(
        self,
        session_factory: AsyncSessionFactory,
        sink: EventSink,
        *,
        listener: NotificationListener | None,
        poll_seconds: float | None = DEFAULT_EVENT_POLL_SECONDS,
        page_size: int = DEFAULT_EVENT_PAGE_SIZE,
        listener_retry_initial_seconds: float = DEFAULT_LISTENER_RETRY_INITIAL_SECONDS,
        listener_retry_max_seconds: float = DEFAULT_LISTENER_RETRY_MAX_SECONDS,
        listener_ready_timeout_seconds: float = DEFAULT_LISTENER_READY_TIMEOUT_SECONDS,
        failure_sink: FailureSink = _default_failure_sink,
    ) -> None:
        if listener is None and poll_seconds is None:
            raise ValueError("Domain Event reader requires LISTEN or polling")
        if poll_seconds is not None and poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        if page_size < 1:
            raise ValueError("page_size must be positive")
        if (
            listener_retry_initial_seconds <= 0
            or listener_retry_max_seconds < listener_retry_initial_seconds
        ):
            raise ValueError("listener retry bounds are invalid")
        if listener_ready_timeout_seconds <= 0:
            raise ValueError("listener_ready_timeout_seconds must be positive")
        self._session_factory = session_factory
        self._sink = sink
        self._listener = listener
        self._poll_seconds = poll_seconds
        self._page_size = page_size
        self._listener_retry_initial_seconds = listener_retry_initial_seconds
        self._listener_retry_max_seconds = listener_retry_max_seconds
        self._listener_ready_timeout_seconds = listener_ready_timeout_seconds
        self._failure_sink = failure_sink
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._cursor = 0
        self._poll_task: asyncio.Task[None] | None = None
        self._listener_task: asyncio.Task[None] | None = None

    @property
    def cursor(self) -> int:
        return self._cursor

    async def start(self) -> None:
        if self._poll_task is not None:
            raise RuntimeError("Domain Event reader is already running")
        self._stop.clear()
        async with self._session_factory() as session:
            self._cursor = await DomainEventRepository(session).stable_frontier()
        if self._listener is not None:
            self._listener_task = asyncio.create_task(self._listener_loop())
            if self._poll_seconds is None:
                try:
                    await asyncio.wait_for(
                        self._listener.wait_ready(),
                        timeout=self._listener_ready_timeout_seconds,
                    )
                    # Catch events committed after the initial cursor read but
                    # before LISTEN became active. A queued notification then
                    # covers everything committed after this catch-up.
                    await self.read_once()
                except BaseException:
                    await self.stop()
                    raise
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        tasks = tuple(task for task in (self._listener_task, self._poll_task) if task is not None)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._listener_task = None
        self._poll_task = None

    async def read_once(self) -> None:
        """Read through one proven-stable frontier in bounded pages."""
        async with self._session_factory() as session:
            repository = DomainEventRepository(session)
            frontier = await repository.stable_frontier()
            while self._cursor < frontier:
                page = await repository.stable_page(
                    after_id=self._cursor,
                    through_id=frontier,
                    limit=self._page_size,
                )
                if not page:
                    break
                for event in page:
                    await self._sink(event)
                    self._cursor = event.id

    async def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self._poll_seconds is None:
                    await self._wake.wait()
                else:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(self._wake.wait(), timeout=self._poll_seconds)
                self._wake.clear()
                if not self._stop.is_set():
                    await self.read_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - report a stable class, then compensate again
                self._failure_sink(DomainEventReaderFailure.POLLING_UNAVAILABLE)

    async def _listener_loop(self) -> None:
        assert self._listener is not None
        retry_seconds = self._listener_retry_initial_seconds
        while not self._stop.is_set():
            try:
                await self._listener.listen(self._wake, self._stop)
                retry_seconds = self._listener_retry_initial_seconds
            except asyncio.CancelledError:
                raise
            except ValueError:
                self._failure_sink(DomainEventReaderFailure.INVALID_NOTIFICATION)
            except Exception:  # noqa: BLE001 - never expose a DSN or raw driver failure
                self._failure_sink(DomainEventReaderFailure.LISTENER_UNAVAILABLE)
            if not self._stop.is_set():
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=retry_seconds)
                retry_seconds = min(retry_seconds * 2, self._listener_retry_max_seconds)


__all__ = [
    "DEFAULT_EVENT_POLL_SECONDS",
    "DEFAULT_LISTENER_RETRY_INITIAL_SECONDS",
    "DEFAULT_LISTENER_RETRY_MAX_SECONDS",
    "DEFAULT_LISTENER_READY_TIMEOUT_SECONDS",
    "DomainEventReader",
    "DomainEventReaderFailure",
    "NotificationListener",
    "PsycopgNotificationListener",
]

"""M1-05 real-PostgreSQL ordering, notification, replay, and SSE tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol, cast
from uuid import UUID

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fixtures.asgi_client import (
    NO_CONFIGURED_CREDENTIAL_ROWS,
    AsgiResponse,
    AsgiTestClient,
)
from holm_contracts import DomainEventEnvelope, VersionPayload
from holm_control_api import (
    EVENTS_PATH,
    SESSION_COOKIE_NAME,
    AuthenticatedSession,
    AuthenticationFailure,
    AuthenticationService,
    DependencyCheck,
    EventStreamService,
    ReadinessReason,
    create_app,
)
from holm_domain import (
    AuthorizationSubject,
    Capability,
    DomainEvent,
    DomainEventDraft,
    ErrorCode,
    EventTypeDeclaration,
    EventTypeRegistry,
    UserRole,
    UserStatus,
)
from holm_persistence import (
    DEFAULT_EVENT_POLL_SECONDS,
    AsyncSessionFactory,
    AuditEventDraft,
    AuditEventRecord,
    DomainEventReader,
    DomainEventRepository,
    PsycopgNotificationListener,
    SessionRecord,
    UserRecord,
    create_database_engine,
    create_session_factory,
)
from migration_test_support import upgrade_head
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.pool import QueuePool

EVENT_NAME: Final[str] = "test.system.progress"
USER_EVENT_NAME: Final[str] = "test.usage.progress"
BODY_SENTINEL: Final[str] = "M1-05-CANONICAL-BODY-SENTINEL"
AGGREGATE_A: Final[UUID] = UUID("018f47f2-8c00-7000-8000-000000000601")
AGGREGATE_B: Final[UUID] = UUID("018f47f2-8c00-7000-8000-000000000602")
USER_ID: Final[UUID] = UUID("018f47f2-8c00-7000-8000-000000000603")


class PostgresFixture(Protocol):
    @property
    def engine(self) -> Engine: ...


@pytest.fixture(autouse=True)
def migrated_empty_event_table(ephemeral_postgres: PostgresFixture) -> Iterator[None]:
    upgrade_head(ephemeral_postgres.engine)
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(sa.text("TRUNCATE core.domain_events RESTART IDENTITY"))
    yield
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(sa.text("TRUNCATE core.domain_events RESTART IDENTITY"))


def _registry() -> EventTypeRegistry:
    return EventTypeRegistry(
        (
            EventTypeDeclaration(
                EVENT_NAME,
                Capability.VIEW_SYSTEM,
                frozenset({"stage", "progress"}),
            ),
            EventTypeDeclaration(
                USER_EVENT_NAME,
                Capability.VIEW_USAGE,
                frozenset({"progress"}),
            ),
        )
    )


def _draft(
    registry: EventTypeRegistry,
    *,
    aggregate_id: UUID,
    version: int,
    event: str = EVENT_NAME,
    extra_payload: dict[str, object] | None = None,
) -> DomainEventDraft:
    candidates: dict[str, object] = {
        "stage": f"stage-{version}",
        "progress": version / 10,
        "messages": [{"content": BODY_SENTINEL}],
        "prompt": BODY_SENTINEL,
        "tool_arguments": {"value": BODY_SENTINEL},
        "generated_result": BODY_SENTINEL,
    }
    if extra_payload:
        candidates.update(extra_payload)
    return registry.build_draft(
        event,
        aggregate_id=aggregate_id,
        version=version,
        candidates=candidates,
    )


def _async_database(ephemeral_postgres: PostgresFixture) -> tuple[AsyncEngine, AsyncSessionFactory]:
    engine = create_database_engine(ephemeral_postgres.engine.url)
    return engine, create_session_factory(engine)


async def _wait_for_pool_release(
    engine: AsyncEngine,
    *,
    attempts: int = 200,
) -> None:
    pool = cast(QueuePool, engine.sync_engine.pool)
    for _ in range(attempts):
        if pool.checkedout() == 0:
            return
        await asyncio.sleep(0.001)
    raise AssertionError("database pool still has a checked-out connection")


async def _append(
    factory: AsyncSessionFactory,
    draft: DomainEventDraft,
) -> DomainEvent:
    async with factory() as session, session.begin():
        return await DomainEventRepository(session).append(draft)


class _UnavailableListener:
    async def listen(self, wake: asyncio.Event, stop: asyncio.Event) -> None:
        raise OSError("listener deliberately unavailable")

    async def wait_ready(self) -> None:
        raise OSError("listener deliberately unavailable")


async def _wait_for_event(queue: asyncio.Queue[DomainEvent], timeout: float = 2.0) -> DomainEvent:
    return await asyncio.wait_for(queue.get(), timeout=timeout)


def test_M1_05_reverse_commit_order_waits_instead_of_skipping_low_id(
    ephemeral_postgres: PostgresFixture,
) -> None:
    async def exercise() -> None:
        engine, factory = _async_database(ephemeral_postgres)
        registry = _registry()
        session_a = factory()
        session_b = factory()
        transaction_a = await session_a.begin()
        transaction_b = await session_b.begin()
        try:
            # Strong counterexample: B gets the older xid, while A gets the
            # lower event ID. xid/xmin filtering alone would wrongly deem B's
            # high-ID row stable after B commits.
            xid_b = int(
                (
                    await session_b.execute(sa.text("SELECT pg_current_xact_id()::text::bigint"))
                ).scalar_one()
            )
            xid_a = int(
                (
                    await session_a.execute(sa.text("SELECT pg_current_xact_id()::text::bigint"))
                ).scalar_one()
            )
            assert xid_b < xid_a
            low = await DomainEventRepository(session_a).append(
                _draft(registry, aggregate_id=AGGREGATE_A, version=1)
            )
            high = await DomainEventRepository(session_b).append(
                _draft(registry, aggregate_id=AGGREGATE_B, version=1),
            )
            await transaction_b.commit()
            assert low.id < high.id

            # Positive control: the naive implementation sees only the high ID
            # and advances beyond the still-uncommitted low ID.
            async with factory() as naive_session:
                naive_first = tuple(
                    (
                        await naive_session.execute(
                            sa.text("SELECT id FROM core.domain_events WHERE id > 0 ORDER BY id")
                        )
                    ).scalars()
                )
            assert naive_first == (high.id,)
            naive_cursor = high.id

            async def protected_read() -> tuple[DomainEvent, ...]:
                async with factory() as protected_session:
                    protected = DomainEventRepository(protected_session)
                    frontier = await protected.stable_frontier()
                    return await protected.stable_page(after_id=0, through_id=frontier)

            protected_task = asyncio.create_task(protected_read())
            await asyncio.sleep(0.05)
            assert protected_task.done() is False
            await transaction_a.commit()
            page = await asyncio.wait_for(protected_task, timeout=2)
            assert [event.id for event in page] == [low.id, high.id]

            async with factory() as naive_session:
                naive_after = tuple(
                    (
                        await naive_session.execute(
                            sa.text(
                                "SELECT id FROM core.domain_events WHERE id > :cursor ORDER BY id"
                            ),
                            {"cursor": naive_cursor},
                        )
                    ).scalars()
                )
            assert naive_after == ()
            assert low.id not in naive_first + naive_after
        finally:
            if transaction_a.is_active:
                await transaction_a.rollback()
            if transaction_b.is_active:
                await transaction_b.rollback()
            await session_a.close()
            await session_b.close()
            await engine.dispose()

    asyncio.run(exercise())


def test_M1_05_polling_delivers_when_listener_is_completely_unavailable(
    ephemeral_postgres: PostgresFixture,
) -> None:
    async def exercise() -> None:
        engine, factory = _async_database(ephemeral_postgres)
        received: asyncio.Queue[DomainEvent] = asyncio.Queue()
        failures: list[object] = []
        reader = DomainEventReader(
            factory,
            received.put,
            listener=_UnavailableListener(),
            poll_seconds=0.05,
            listener_retry_initial_seconds=0.01,
            listener_retry_max_seconds=0.02,
            failure_sink=failures.append,
        )
        try:
            await reader.start()
            expected = await _append(
                factory,
                _draft(_registry(), aggregate_id=AGGREGATE_A, version=1),
            )
            assert (await _wait_for_event(received)).id == expected.id
            assert failures
            assert DEFAULT_EVENT_POLL_SECONDS == 1.0
        finally:
            await reader.stop()
            await engine.dispose()

    asyncio.run(exercise())


def test_M1_05_notify_delivers_with_polling_disabled_and_rollback_notifies_nothing(
    ephemeral_postgres: PostgresFixture,
) -> None:
    async def exercise() -> None:
        engine, factory = _async_database(ephemeral_postgres)
        received: asyncio.Queue[DomainEvent] = asyncio.Queue()
        listener = PsycopgNotificationListener(ephemeral_postgres.engine.url)
        reader = DomainEventReader(
            factory,
            received.put,
            listener=listener,
            poll_seconds=None,
        )
        try:
            await reader.start()

            async with factory() as session:
                transaction = await session.begin()
                await DomainEventRepository(session).append(
                    _draft(_registry(), aggregate_id=AGGREGATE_A, version=1)
                )
                await transaction.rollback()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(received.get(), timeout=0.15)

            expected = await _append(
                factory,
                _draft(_registry(), aggregate_id=AGGREGATE_A, version=1),
            )
            assert (await _wait_for_event(received)).id == expected.id
        finally:
            await reader.stop()
            await engine.dispose()

    asyncio.run(exercise())


async def _first_frame(
    service: EventStreamService,
    subject: AuthorizationSubject,
    last_event_id: str | None,
) -> bytes:
    stream = service.stream(subject, last_event_id)
    try:
        return await anext(stream)
    finally:
        await stream.aclose()


def test_M1_05_disconnect_replay_is_exact_and_resync_guards_are_in_stream(
    ephemeral_postgres: PostgresFixture,
) -> None:
    async def exercise() -> None:
        engine, factory = _async_database(ephemeral_postgres)
        registry = _registry()
        before_connect = await _append(
            factory,
            _draft(registry, aggregate_id=AGGREGATE_A, version=1),
        )
        service = EventStreamService(
            factory,
            registry,
            listener=None,
            poll_seconds=0.01,
            keepalive_seconds=1.0,
        )
        subject = AuthorizationSubject(user_id=USER_ID, role=UserRole.ADMIN)
        try:
            await service.start()

            # Establish a real first connection, receive N, then disconnect.
            connected = service.stream(subject, None)
            next_frame = asyncio.create_task(anext(connected))
            for _ in range(100):
                if service.fanout.subscriber_count == 1:
                    break
                await asyncio.sleep(0.005)
            assert service.fanout.subscriber_count == 1
            last_received = await _append(
                factory,
                _draft(registry, aggregate_id=AGGREGATE_A, version=2),
            )
            connected_frame = await asyncio.wait_for(next_frame, timeout=2)
            assert connected_frame.startswith(f"id: {last_received.id}\n".encode())
            await connected.aclose()
            assert service.fanout.subscriber_count == 0

            # These events are committed while no client is subscribed.
            disconnected_events = [
                await _append(
                    factory,
                    _draft(registry, aggregate_id=AGGREGATE_A, version=version),
                )
                for version in (3, 4)
            ]
            for _ in range(200):
                if service.reader.cursor >= disconnected_events[-1].id:
                    break
                await asyncio.sleep(0.005)
            assert service.reader.cursor >= disconnected_events[-1].id

            replay = service.stream(subject, str(last_received.id))
            first = await anext(replay)
            # The generator is paused at ASGI backpressure here; replay must
            # already have released its short-lived database session.
            await _wait_for_pool_release(engine)

            # Commit once more while replay is paused. It must cross the
            # replay/live handoff exactly once and after the replay frontier.
            during_replay = await _append(
                factory,
                _draft(registry, aggregate_id=AGGREGATE_A, version=5),
            )
            for _ in range(200):
                if service.reader.cursor >= during_replay.id:
                    break
                await asyncio.sleep(0.005)
            assert service.reader.cursor >= during_replay.id
            frames = [first, await anext(replay), await anext(replay)]
            await replay.aclose()

            frame_ids = [int(frame.split(b"\n", 1)[0].removeprefix(b"id: ")) for frame in frames]
            expected_ids = [event.id for event in disconnected_events] + [during_replay.id]
            assert frame_ids == expected_ids
            assert len(frame_ids) == len(set(frame_ids))
            assert frame_ids == sorted(frame_ids)
            envelopes = [
                DomainEventEnvelope.model_validate_json(
                    next(line[6:] for line in frame.splitlines() if line.startswith(b"data: "))
                )
                for frame in frames
            ]
            assert [envelope.id for envelope in envelopes] == expected_ids

            async with factory() as session, session.begin():
                await session.execute(
                    sa.text(
                        "UPDATE core.domain_events "
                        "SET occurred_at = clock_timestamp() - interval '25 hours' WHERE id = :id"
                    ),
                    {"id": before_connect.id},
                )

            for invalid in (
                "not-an-integer",
                "0",
                "9" * 20,
                str(before_connect.id),
                str(during_replay.id + 1000),
            ):
                frame = await _first_frame(service, subject, invalid)
                assert frame.startswith(b"event: resync_required\n")
                assert b"id:" not in frame
                assert json.loads(frame.split(b"data: ", 1)[1])["code"] == (
                    ErrorCode.RESYNC_REQUIRED.value
                )
                assert f"id: {during_replay.id}\n".encode() not in frame
        finally:
            await service.stop()
            await engine.dispose()

    asyncio.run(exercise())


def test_M2_09A_pending_22_pool_release_guard_rejects_a_pinned_session(
    ephemeral_postgres: PostgresFixture,
) -> None:
    async def exercise() -> None:
        engine, factory = _async_database(ephemeral_postgres)
        try:
            async with factory() as session:
                await session.execute(sa.text("SELECT 1"))
                with pytest.raises(
                    AssertionError,
                    match="still has a checked-out connection",
                ):
                    await _wait_for_pool_release(engine, attempts=2)
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_M1_05_payload_body_fields_never_reach_postgresql_and_version_is_unique(
    ephemeral_postgres: PostgresFixture,
) -> None:
    async def exercise() -> None:
        engine, factory = _async_database(ephemeral_postgres)
        registry = _registry()
        await _append(factory, _draft(registry, aggregate_id=AGGREGATE_A, version=1))
        async with factory() as session:
            payload_text = (
                await session.execute(sa.text("SELECT payload::text FROM core.domain_events"))
            ).scalar_one()
        assert BODY_SENTINEL not in payload_text
        assert all(
            forbidden not in payload_text
            for forbidden in ("messages", "prompt", "tool_arguments", "generated_result")
        )

        with pytest.raises(IntegrityError):
            await _append(factory, _draft(registry, aggregate_id=AGGREGATE_A, version=1))
        await engine.dispose()

    asyncio.run(exercise())


def test_M1_05_slow_stream_overflow_emits_resync_then_closes(
    ephemeral_postgres: PostgresFixture,
) -> None:
    async def exercise() -> None:
        engine, factory = _async_database(ephemeral_postgres)
        registry = _registry()
        service = EventStreamService(
            factory,
            registry,
            listener=None,
            poll_seconds=1.0,
            subscriber_queue_size=2,
        )
        subject = AuthorizationSubject(user_id=USER_ID, role=UserRole.ADMIN)
        stream = service.stream(subject, None)
        first_frame = asyncio.create_task(anext(stream))
        try:
            for _ in range(100):
                if service.fanout.subscriber_count == 1:
                    break
                await asyncio.sleep(0.005)
            assert service.fanout.subscriber_count == 1

            def in_memory_event(event_id: int) -> DomainEvent:
                return DomainEvent(
                    id=event_id,
                    event=EVENT_NAME,
                    aggregate_id=AGGREGATE_A,
                    version=event_id,
                    occurred_at=datetime(2026, 8, 29, 10, 0, tzinfo=UTC),
                    payload={"stage": "test", "progress": 0.1},
                )

            await service.fanout.publish(in_memory_event(1))
            assert (await asyncio.wait_for(first_frame, timeout=1)).startswith(b"id: 1\n")

            for event_id in range(2, 102):
                await service.fanout.publish(in_memory_event(event_id))
            assert service.fanout.subscriber_count == 0

            overflow = await asyncio.wait_for(anext(stream), timeout=1)
            assert overflow.startswith(b"event: resync_required\n")
            assert b"id:" not in overflow
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(anext(stream), timeout=1)
        finally:
            if not first_frame.done():
                first_frame.cancel()
            await stream.aclose()
            await engine.dispose()

    asyncio.run(exercise())


async def _readiness_ok() -> ReadinessReason:
    return ReadinessReason.OK


class _AuditWriter:
    async def add(self, session: AsyncSession, event: AuditEventDraft) -> AuditEventRecord:
        raise AssertionError("test authentication has no audit transaction")

    async def write(self, event: AuditEventDraft) -> AuditEventRecord:
        raise AssertionError("authorized SSE request must not write an audit event")


class _Authenticator:
    def __init__(self) -> None:
        now = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)
        user = UserRecord(
            id=USER_ID,
            username_norm="sse-admin",
            display_name="SSE Admin",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            password_hash="$argon2id$not-returned",
            must_change_password=False,
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )
        session = SessionRecord(
            id=UUID("018f47f2-8c00-7000-8000-000000000604"),
            user_id=USER_ID,
            session_hash="a" * 64,
            csrf_hash="b" * 64,
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(hours=8),
            revoked_at=None,
        )
        self.authenticated = AuthenticatedSession(user=user, session=session)
        self.audit_writer = _AuditWriter()

    async def authenticate_session(
        self,
        *,
        session_token: str | None,
        csrf_token: str | None = None,
        require_csrf: bool = False,
    ) -> AuthenticatedSession:
        if session_token is None:
            raise AuthenticationFailure(
                code="authentication_required",
                message="登录会话无效或已过期，请重新登录",
                status_code=401,
            )
        return self.authenticated


def _application(
    factory: AsyncSessionFactory,
    service: EventStreamService,
) -> FastAPI:
    return create_app(
        version_payload=VersionPayload(
            build_version="1.4.0+gm105000001",
            commit="5" * 40,
            schema_version=1,
            runtime_compat_hash="6" * 64,
        ),
        credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
        readiness_checks=[DependencyCheck(name="database", probe=_readiness_ok)],
        authentication=cast(AuthenticationService, _Authenticator()),
        event_stream=service,
    )


def _header(response: AsgiResponse, name: str) -> str:
    values = [value for key, value in response.headers if key.casefold() == name.casefold()]
    assert len(values) == 1
    return values[0]


def test_M1_05_sse_endpoint_is_authenticated_incremental_and_unbuffered(
    ephemeral_postgres: PostgresFixture,
) -> None:
    async def exercise() -> None:
        engine, factory = _async_database(ephemeral_postgres)
        registry = _registry()
        service = EventStreamService(
            factory,
            registry,
            listener=None,
            poll_seconds=0.05,
            keepalive_seconds=0.02,
        )
        app = _application(factory, service)
        client = AsgiTestClient(app)
        async with client.lifespan():
            unauthenticated = await client.get(EVENTS_PATH)
            assert unauthenticated.status == 401

            invalid_replay = await client.stream(
                "GET",
                EVENTS_PATH,
                headers={"Last-Event-ID": "invalid"},
                cookies={SESSION_COOKIE_NAME: "fixture-session"},
            )
            try:
                assert (await invalid_replay.read_chunk()).startswith(b"event: resync_required\n")
            finally:
                await invalid_replay.close()

            stream = await client.stream(
                "GET",
                EVENTS_PATH,
                cookies={SESSION_COOKIE_NAME: "fixture-session"},
            )
            try:
                assert stream.status == 200
                assert _header(cast(AsgiResponse, stream), "content-type").startswith(
                    "text/event-stream"
                )
                assert _header(cast(AsgiResponse, stream), "cache-control") == "no-store"
                assert _header(cast(AsgiResponse, stream), "x-accel-buffering") == "no"
                UUID(_header(cast(AsgiResponse, stream), "x-request-id"))

                keepalive = await stream.read_chunk()
                assert keepalive.startswith(b":")
                assert b"id:" not in keepalive

                expected = await _append(
                    factory,
                    _draft(registry, aggregate_id=AGGREGATE_A, version=1),
                )
                frame = b""
                for _ in range(10):
                    frame = await stream.read_chunk()
                    if frame.startswith(b"id: "):
                        break
                    assert frame.startswith(b":")
                    assert b"id:" not in frame
                assert frame.startswith(f"id: {expected.id}\nevent: {EVENT_NAME}\n".encode())
                assert (
                    DomainEventEnvelope.model_validate_json(
                        next(line[6:] for line in frame.splitlines() if line.startswith(b"data: "))
                    ).id
                    == expected.id
                )
            finally:
                await stream.close()
        await engine.dispose()

    asyncio.run(exercise())

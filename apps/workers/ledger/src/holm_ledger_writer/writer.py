"""Wakeable, commit-before-ack Request Ledger replay loop."""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal
from uuid import UUID

import sqlalchemy as sa
from holm_common import as_utc
from holm_domain import (
    LEDGER_DELAYED_AFTER_SECONDS,
    LEDGER_UNAVAILABLE_AFTER_SECONDS,
    Component,
    ComponentStatus,
    LedgerHealth,
    RequestRecordState,
    classify_backlog_age,
    encode_ledger_backlog_reason,
    worst_ledger_health,
)
from holm_domain.ledger_health import LedgerBacklogDetails
from holm_ledger import (
    BUSY_TIMEOUT_MS,
    MAX_BATCH_WINDOW_SECONDS,
    ConnectionRole,
    EventType,
    LedgerError,
    LedgerEvent,
    SequenceAction,
    SQLiteLedger,
    UnknownEvent,
    backlog_age_seconds,
    probe_ledger_disk,
)
from holm_ledger.store import PassiveCheckpointResult
from holm_persistence import (
    API_KEYS,
    LEDGER_REQUEST_RECORDS,
    REQUEST_EVENT_QUARANTINE,
    AsyncSessionFactory,
    ComponentHeartbeatRepository,
    RequestRecordLedgerRepository,
    RequestRecordUpsertOutcome,
    RequestRecordUpsertResult,
    UsageRepository,
    drop_expired_telemetry_partitions,
)
from holm_persistence.request_record_ledger_repository import (
    drop_expired_request_record_partitions,
)
from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import (
    DBAPIError,
    DisconnectionError,
    OperationalError,
    SQLAlchemyError,
)
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

_LOGGER: Final[logging.Logger] = logging.getLogger("holm.ledger_writer")
MAX_REPLAY_BATCH_SIZE: Final[int] = 500
# Gateway total timeout (3600 s) plus five minutes.
DEFAULT_UNKNOWN_AFTER_SECONDS: Final[float] = 3900.0
DEFAULT_UNKNOWN_SCAN_SECONDS: Final[float] = 300.0
DEFAULT_USAGE_AGGREGATE_SECONDS: Final[float] = 30.0
DEFAULT_BACKLOG_HEALTH_SECONDS: Final[float] = 5.0
DEFAULT_ACK_RETENTION_SECONDS: Final[float] = 48 * 60 * 60
DEFAULT_MAINTENANCE_SECONDS: Final[float] = 60.0
ACK_CLEANUP_BATCH_SIZE: Final[int] = 500
# A pending Gateway batch must either commit within its 2 ms group window and
# 100 ms lock budget, or enter storage protection.  Observing a quiet SQLite
# interval spanning both limits turns the in-process queue into a conservative
# cross-process signal without making the Gateway wait for maintenance.
GATEWAY_IDLE_CONFIRM_SECONDS: Final[float] = (
    MAX_BATCH_WINDOW_SECONDS + BUSY_TIMEOUT_MS[ConnectionRole.GATEWAY_SPOOL_WRITER] / 1_000
)
_BACKLOG_HEARTBEAT_STATUS: Final[dict[LedgerHealth, ComponentStatus]] = {
    LedgerHealth.AVAILABLE: ComponentStatus.HEALTHY,
    LedgerHealth.DELAYED: ComponentStatus.DEGRADED,
    LedgerHealth.UNAVAILABLE: ComponentStatus.DEGRADED,
}
MAX_UNKNOWN_SCAN_BATCH_SIZE: Final[int] = 500
_OPEN_RECORD_EVENT_TYPES: Final[dict[str, Literal[EventType.RECEIVED, EventType.IN_PROGRESS]]] = {
    RequestRecordState.RECEIVED.value: EventType.RECEIVED,
    RequestRecordState.IN_PROGRESS.value: EventType.IN_PROGRESS,
}
WAKE_CHECK_SECONDS: Final[float] = 0.1
DATABASE_RETRY_BACKOFF_SECONDS: Final[tuple[float, ...]] = (
    1.0,
    2.0,
    4.0,
    8.0,
    16.0,
    30.0,
)
_LAST_USED_EVENT_TYPES: Final[frozenset[EventType]] = frozenset(
    {
        EventType.IN_PROGRESS,
        EventType.REJECTED,
        EventType.COMPLETED,
        EventType.FAILED,
        EventType.ABORTED,
        EventType.UNKNOWN,
    }
)


@dataclass(frozen=True, slots=True)
class LedgerMaintenanceResult:
    yielded_to_gateway: bool
    deleted_acked_events: int
    dropped_partitions: tuple[str, ...]
    checkpoint: PassiveCheckpointResult | None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _minute(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(second=0, microsecond=0)


def _is_connection_failure(error: BaseException) -> bool:
    if isinstance(error, (DisconnectionError, OperationalError, SQLAlchemyTimeoutError)):
        return True
    if isinstance(error, DBAPIError) and error.connection_invalidated:
        return True
    cause = error.__cause__
    return cause is not None and cause is not error and _is_connection_failure(cause)


class LedgerWriter:
    """Replay one local Ledger into PostgreSQL without ever acknowledging early."""

    __slots__ = (
        "_backlog_health",
        "_backlog_details",
        "_backlog_health_seconds",
        "_build_version",
        "_clock",
        "_delayed_after_seconds",
        "_engine",
        "_gateway_idle_confirm_seconds",
        "_heartbeat_seconds",
        "_instance_id",
        "_last_used_minute",
        "_ledger",
        "_maintenance_failed",
        "_maintenance_seconds",
        "_poll_seconds",
        "_ack_retention_seconds",
        "_request_records_retention_days",
        "_session_factory",
        "_unavailable_after_seconds",
        "_unknown_after_seconds",
        "_unknown_scan_seconds",
        "_usage_aggregate_seconds",
    )

    def __init__(
        self,
        *,
        ledger: SQLiteLedger,
        session_factory: AsyncSessionFactory,
        engine: AsyncEngine,
        poll_seconds: float = 1.0,
        heartbeat_seconds: float = 10.0,
        instance_id: str = "ledger-writer",
        build_version: str = "0.0.0",
        unknown_after_seconds: float = DEFAULT_UNKNOWN_AFTER_SECONDS,
        unknown_scan_seconds: float = DEFAULT_UNKNOWN_SCAN_SECONDS,
        usage_aggregate_seconds: float = DEFAULT_USAGE_AGGREGATE_SECONDS,
        delayed_after_seconds: float = LEDGER_DELAYED_AFTER_SECONDS,
        unavailable_after_seconds: float = LEDGER_UNAVAILABLE_AFTER_SECONDS,
        backlog_health_seconds: float = DEFAULT_BACKLOG_HEALTH_SECONDS,
        ack_retention_seconds: float = DEFAULT_ACK_RETENTION_SECONDS,
        maintenance_seconds: float = DEFAULT_MAINTENANCE_SECONDS,
        request_records_retention_days: int = 30,
        gateway_idle_confirm_seconds: float = GATEWAY_IDLE_CONFIRM_SECONDS,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll interval must be positive")
        if heartbeat_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")
        if unknown_after_seconds <= 0:
            raise ValueError("unknown threshold must be positive")
        if unknown_scan_seconds <= 0:
            raise ValueError("unknown scan interval must be positive")
        if usage_aggregate_seconds <= 0:
            raise ValueError("Usage aggregate interval must be positive")
        if delayed_after_seconds <= 0 or unavailable_after_seconds <= 0:
            raise ValueError("backlog thresholds must be positive")
        if delayed_after_seconds > unavailable_after_seconds:
            raise ValueError("delayed threshold must not exceed the unavailable threshold")
        if backlog_health_seconds <= 0:
            raise ValueError("backlog health interval must be positive")
        if ack_retention_seconds <= 0:
            raise ValueError("Ack retention must be positive")
        if maintenance_seconds <= 0:
            raise ValueError("maintenance interval must be positive")
        if isinstance(request_records_retention_days, bool) or request_records_retention_days <= 0:
            raise ValueError("Request Record retention days must be positive")
        if gateway_idle_confirm_seconds <= 0:
            raise ValueError("Gateway idle confirmation interval must be positive")
        for value, name in ((instance_id, "instance_id"), (build_version, "build_version")):
            if not value or value != value.strip() or len(value) > 128:
                raise ValueError(f"{name} must be trimmed and between 1 and 128 characters")
        self._ledger = ledger
        self._session_factory = session_factory
        self._engine = engine
        self._poll_seconds = poll_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._instance_id = instance_id
        self._build_version = build_version
        self._last_used_minute: dict[UUID, datetime] = {}
        self._unknown_after_seconds = unknown_after_seconds
        self._unknown_scan_seconds = unknown_scan_seconds
        self._usage_aggregate_seconds = usage_aggregate_seconds
        self._delayed_after_seconds = delayed_after_seconds
        self._unavailable_after_seconds = unavailable_after_seconds
        self._backlog_health_seconds = backlog_health_seconds
        self._backlog_health = LedgerHealth.AVAILABLE
        self._backlog_details: LedgerBacklogDetails | None = None
        self._ack_retention_seconds = ack_retention_seconds
        self._maintenance_seconds = maintenance_seconds
        self._request_records_retention_days = request_records_retention_days
        self._gateway_idle_confirm_seconds = gateway_idle_confirm_seconds
        self._maintenance_failed = False
        self._clock = clock

    async def refresh_backlog_health(self) -> LedgerHealth:
        """Recompute the Ledger backlog verdict from local signals only.

        This runs inside the replay loop because the process owns exactly one
        SQLite connection and it must never be used from two worker threads at
        once.  A Ledger that cannot be read at all is reported as Unavailable
        rather than silently keeping the last good verdict.
        """

        try:
            self._backlog_details = await asyncio.to_thread(self._ledger.backlog_details)
            oldest = self._backlog_details.oldest_event_at
            disk = await asyncio.to_thread(probe_ledger_disk, self._ledger.path)
        except (LedgerError, OSError):
            _LOGGER.warning(
                "request Ledger backlog probe failed",
                extra={
                    "component": Component.LEDGER_WRITER.value,
                    "error_code": "backlog_probe_failed",
                },
            )
            self._backlog_details = None
            self._backlog_health = LedgerHealth.UNAVAILABLE
            return self._backlog_health
        age = backlog_age_seconds(oldest, as_utc(self._clock()))
        self._backlog_health = worst_ledger_health(
            classify_backlog_age(
                age,
                delayed_after=self._delayed_after_seconds,
                unavailable_after=self._unavailable_after_seconds,
            ),
            LedgerHealth.UNAVAILABLE if disk.critical else LedgerHealth.AVAILABLE,
        )
        return self._backlog_health

    async def aggregate_usage_once(self) -> int:
        """Recompute the current and previous UTC buckets in one transaction."""

        async with self._session_factory() as session, session.begin():
            return await UsageRepository(session).recompute_recent_buckets(
                now=as_utc(self._clock())
            )

    async def _gateway_has_pending_work(self) -> bool:
        """Conservatively observe the independent Gateway process through SQLite.

        Any unacknowledged row is treated as Gateway work even when it is old,
        and any external commit during the quiet window also causes a yield.
        This intentionally permits false positives in favor of Gateway writes.
        """

        before = await asyncio.to_thread(self._ledger.data_version)
        if await asyncio.to_thread(self._ledger.has_unacked_events):
            return True
        await asyncio.sleep(self._gateway_idle_confirm_seconds)
        if await asyncio.to_thread(self._ledger.has_unacked_events):
            return True
        after = await asyncio.to_thread(self._ledger.data_version)
        return after != before

    async def maintain_once(self) -> LedgerMaintenanceResult:
        """Yield to Gateway work, clean Ack history, retain partitions, then checkpoint."""

        if await self._gateway_has_pending_work():
            return LedgerMaintenanceResult(True, 0, (), None)

        cutoff = as_utc(self._clock()) - timedelta(seconds=self._ack_retention_seconds)
        deleted = 0
        while True:
            if deleted and await self._gateway_has_pending_work():
                return LedgerMaintenanceResult(True, deleted, (), None)
            batch = await asyncio.to_thread(
                self._ledger.delete_acked_before,
                cutoff,
                limit=ACK_CLEANUP_BATCH_SIZE,
            )
            deleted += batch
            if batch < ACK_CLEANUP_BATCH_SIZE:
                break

        try:
            request_partitions = await drop_expired_request_record_partitions(
                self._engine,
                retention_days=self._request_records_retention_days,
                now=as_utc(self._clock()),
            )
            telemetry_partitions = await drop_expired_telemetry_partitions(
                self._engine,
                now=as_utc(self._clock()),
            )
        except SQLAlchemyError:
            self._maintenance_failed = True
            _LOGGER.error(
                "partition retention failed",
                extra={
                    "component": Component.LEDGER_WRITER.value,
                    "error_code": "partition_retention_failed",
                },
            )
            raise
        dropped = (*request_partitions, *telemetry_partitions)
        if await self._gateway_has_pending_work():
            return LedgerMaintenanceResult(True, deleted, dropped, None)

        try:
            checkpoint = await asyncio.to_thread(self._ledger.checkpoint_passive)
        except LedgerError:
            self._maintenance_failed = True
            _LOGGER.warning(
                "request Ledger passive checkpoint failed",
                extra={
                    "component": Component.LEDGER_WRITER.value,
                    "error_code": "ledger_checkpoint_failed",
                },
            )
            return LedgerMaintenanceResult(False, deleted, dropped, None)

        if checkpoint.busy or not checkpoint.complete:
            self._maintenance_failed = True
            _LOGGER.warning(
                "request Ledger passive checkpoint did not complete",
                extra={
                    "component": Component.LEDGER_WRITER.value,
                    "error_code": (
                        "ledger_checkpoint_busy"
                        if checkpoint.busy
                        else "ledger_checkpoint_incomplete"
                    ),
                    "wal_frames": checkpoint.wal_frames,
                    "checkpointed_frames": checkpoint.checkpointed_frames,
                },
            )
        else:
            self._maintenance_failed = False
        if deleted or dropped:
            _LOGGER.info(
                "request Ledger maintenance completed",
                extra={
                    "component": Component.LEDGER_WRITER.value,
                    "deleted_acked_events": deleted,
                    "dropped_partition_count": len(dropped),
                },
            )
        return LedgerMaintenanceResult(False, deleted, dropped, checkpoint)

    async def converge_unknown_once(self) -> int:
        """Append Unknown for stale Received/InProgress records.

        Only the local Ledger is written; the normal replay loop projects the
        Unknown event into PostgreSQL. Returns the number of events written.
        """

        now = as_utc(self._clock())
        cutoff = now - timedelta(seconds=self._unknown_after_seconds)
        records = LEDGER_REQUEST_RECORDS.c
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    sa.select(
                        records.request_id,
                        records.received_at,
                        records.event_seq,
                        records.record_state,
                        records.user_id,
                        records.api_key_id,
                        records.service_id,
                        records.instance_id,
                        records.input_tokens,
                        records.output_tokens,
                    )
                    .where(
                        records.record_state.in_(tuple(_OPEN_RECORD_EVENT_TYPES)),
                        records.received_at < cutoff,
                    )
                    .order_by(records.received_at, records.request_id)
                    .limit(MAX_UNKNOWN_SCAN_BATCH_SIZE)
                )
            ).all()
        if not rows:
            return 0
        summaries = await asyncio.to_thread(
            self._ledger.summarize_requests,
            [row.request_id for row in rows],
        )

        events: list[UnknownEvent] = []
        for row in rows:
            request_id: UUID = row.request_id
            summary = summaries.get(request_id)
            if summary is None:
                _LOGGER.warning(
                    "stale request has no local Ledger events; skipping Unknown convergence",
                    extra={
                        "component": Component.LEDGER_WRITER.value,
                        "error_code": "unknown_convergence_events_missing",
                        "request_id": str(request_id),
                    },
                )
                continue
            if summary.unacked_count > 0:
                # Unreplayed events may still carry a terminal result, or this
                # writer's own Unknown awaiting replay; decide on a later scan.
                continue
            try:
                events.append(
                    UnknownEvent(
                        request_id=request_id,
                        event_seq=summary.max_event_seq + 1,
                        created_at=now,
                        received_at=as_utc(row.received_at),
                        last_confirmed_event_type=_OPEN_RECORD_EVENT_TYPES[row.record_state],
                        last_confirmed_event_seq=row.event_seq,
                        user_id=row.user_id,
                        api_key_id=row.api_key_id,
                        service_id=row.service_id,
                        instance_id=row.instance_id,
                        input_tokens=row.input_tokens,
                        output_tokens=row.output_tokens,
                    )
                )
            except ValidationError:
                _LOGGER.warning(
                    "stale request record is inconsistent with the local Ledger",
                    extra={
                        "component": Component.LEDGER_WRITER.value,
                        "error_code": "unknown_convergence_record_inconsistent",
                        "request_id": str(request_id),
                    },
                )
        if not events:
            return 0

        # One short write transaction; sequence rules re-check each request inside it,
        # so a terminal event the Gateway wrote after the scan wins and Unknown is dropped.
        decisions = await asyncio.to_thread(self._ledger.append_batch, events)
        written = 0
        for event, decision in zip(events, decisions, strict=True):
            if decision.action is SequenceAction.WRITE:
                written += 1
                _LOGGER.info(
                    "request converged to Unknown",
                    extra={
                        "component": Component.LEDGER_WRITER.value,
                        "request_id": str(event.request_id),
                        "event_seq": event.event_seq,
                        "last_confirmed_event_type": event.last_confirmed_event_type.value,
                    },
                )
            else:
                _LOGGER.info(
                    "Unknown convergence not applied",
                    extra={
                        "component": Component.LEDGER_WRITER.value,
                        "request_id": str(event.request_id),
                        "sequence_action": decision.action.value,
                        "sequence_reason": decision.reason,
                    },
                )
        return written

    async def replay_once(self) -> int:
        """Replay and acknowledge at most one 500-event batch."""

        stored = await asyncio.to_thread(
            self._ledger.list_unacked,
            limit=MAX_REPLAY_BATCH_SIZE,
        )
        if not stored:
            return 0
        events = tuple(item.event for item in stored)
        written_minutes = await self._commit_with_poison_recovery(events)
        self._last_used_minute.update(written_minutes)

        acknowledged = await asyncio.to_thread(
            self._ledger.mark_acked_batch,
            ((event.request_id, event.event_seq) for event in events),
            acked_at=datetime.now(UTC),
        )
        if acknowledged != len(events):
            _LOGGER.warning(
                "ledger acknowledgement count changed",
                extra={
                    "component": Component.LEDGER_WRITER.value,
                    "error_code": "ack_count_changed",
                    "expected_count": len(events),
                    "acknowledged_count": acknowledged,
                },
            )
        return len(events)

    async def _commit_with_poison_recovery(
        self, events: tuple[LedgerEvent, ...]
    ) -> dict[UUID, datetime]:
        try:
            return await self._commit_batch(events)
        except SQLAlchemyError as error:
            if _is_connection_failure(error):
                raise
            _LOGGER.critical(
                "ledger batch database error; isolating individual event",
                extra={
                    "component": Component.LEDGER_WRITER.value,
                    "error_code": "batch_event_invalid",
                    "event_count": len(events),
                },
            )
            return await self._commit_with_savepoints(events)

    async def _commit_batch(self, events: tuple[LedgerEvent, ...]) -> dict[UUID, datetime]:
        async with self._session_factory() as session, session.begin():
            results = await RequestRecordLedgerRepository(session, self._engine).upsert_batch(
                events
            )
            await self._quarantine_conflicts(session, events, results)
            written_minutes = await self._update_last_used(session, events, results)
        return written_minutes

    async def _commit_with_savepoints(
        self, events: tuple[LedgerEvent, ...]
    ) -> dict[UUID, datetime]:
        results: list[RequestRecordUpsertResult] = []
        poisoned: list[LedgerEvent] = []
        async with self._session_factory() as session, session.begin():
            repository = RequestRecordLedgerRepository(session, self._engine)
            for event in events:
                try:
                    async with session.begin_nested():
                        result = await repository.upsert_batch((event,))
                except SQLAlchemyError as error:
                    if _is_connection_failure(error):
                        raise
                    poisoned.append(event)
                    _LOGGER.critical(
                        "ledger event quarantined after database rejection",
                        extra={
                            "component": Component.LEDGER_WRITER.value,
                            "error_code": "event_database_rejected",
                            "request_id": str(event.request_id),
                            "event_seq": event.event_seq,
                            "event_type": event.event_type.value,
                        },
                    )
                else:
                    results.extend(result)
            await self._quarantine_conflicts(session, events, results)
            await self._insert_quarantine(
                session,
                ((event, event.received_at) for event in poisoned),
            )
            written_minutes = await self._update_last_used(session, events, results)
        return written_minutes

    async def _quarantine_conflicts(
        self,
        session: AsyncSession,
        events: tuple[LedgerEvent, ...],
        results: Iterable[RequestRecordUpsertResult],
    ) -> None:
        by_key = {(event.request_id, event.event_seq): event for event in events}
        await self._insert_quarantine(
            session,
            (
                (
                    by_key[(result.request_id, result.event_seq)],
                    result.registered_received_at
                    or by_key[(result.request_id, result.event_seq)].received_at,
                )
                for result in results
                if result.outcome is RequestRecordUpsertOutcome.CONFLICT
            ),
        )

    @staticmethod
    async def _insert_quarantine(
        session: AsyncSession,
        entries: Iterable[tuple[LedgerEvent, datetime]],
    ) -> None:
        values = [
            {
                "request_id": event.request_id,
                "event_seq": event.event_seq,
                "event_type": event.event_type.value,
                "event_received_at": event.received_at,
                "registered_received_at": registered_at,
                "quarantined_at": sa.func.clock_timestamp(),
            }
            for event, registered_at in entries
        ]
        if not values:
            return
        statement = postgresql_insert(REQUEST_EVENT_QUARANTINE).values(values)
        await session.execute(
            statement.on_conflict_do_nothing(
                index_elements=[
                    REQUEST_EVENT_QUARANTINE.c.request_id,
                    REQUEST_EVENT_QUARANTINE.c.event_seq,
                ]
            )
        )

    async def _update_last_used(
        self,
        session: AsyncSession,
        events: tuple[LedgerEvent, ...],
        results: Iterable[RequestRecordUpsertResult],
    ) -> dict[UUID, datetime]:
        event_by_key = {(event.request_id, event.event_seq): event for event in events}
        applied = [
            event_by_key[(result.request_id, result.event_seq)]
            for result in results
            if result.outcome is RequestRecordUpsertOutcome.APPLIED
            and result.event_type in _LAST_USED_EVENT_TYPES
        ]
        if not applied:
            return {}

        key_by_request: dict[UUID, UUID] = {}
        for event in events:
            key_id = getattr(event, "api_key_id", None)
            if key_id is not None:
                key_by_request[event.request_id] = key_id

        unresolved = {
            event.request_id for event in applied if event.request_id not in key_by_request
        }
        if unresolved:
            rows = (
                await session.execute(
                    sa.select(
                        LEDGER_REQUEST_RECORDS.c.request_id,
                        LEDGER_REQUEST_RECORDS.c.api_key_id,
                    ).where(LEDGER_REQUEST_RECORDS.c.request_id.in_(unresolved))
                )
            ).all()
            key_by_request.update(
                {request_id: key_id for request_id, key_id in rows if key_id is not None}
            )

        newest_by_key_minute: dict[tuple[UUID, datetime], datetime] = {}
        for event in applied:
            key_id = key_by_request.get(event.request_id)
            if key_id is None:
                continue
            minute = _minute(event.created_at)
            identity = (key_id, minute)
            newest_by_key_minute[identity] = max(
                event.created_at,
                newest_by_key_minute.get(identity, event.created_at),
            )

        candidates = [
            (key_id, minute, event_time)
            for (key_id, minute), event_time in newest_by_key_minute.items()
            if self._last_used_minute.get(key_id) != minute
        ]
        if not candidates:
            return {}
        key_ids = {key_id for key_id, _, _ in candidates}
        created_rows = (
            await session.execute(
                sa.select(API_KEYS.c.id, API_KEYS.c.created_at).where(API_KEYS.c.id.in_(key_ids))
            )
        ).all()
        created_by_key = {key_id: created_at for key_id, created_at in created_rows}

        written: dict[UUID, datetime] = {}
        for key_id, minute, event_time in sorted(candidates, key=lambda item: item[2]):
            created_at = created_by_key.get(key_id)
            if created_at is None:
                continue
            if event_time < created_at:
                _LOGGER.warning(
                    "API Key Last Used event predates key creation",
                    extra={
                        "component": Component.LEDGER_WRITER.value,
                        "error_code": "last_used_before_key_creation",
                        "api_key_id": str(key_id),
                    },
                )
                continue
            await session.execute(
                sa.text("SELECT core.holm_update_api_key_last_used(:key_id, :used_at)"),
                {"key_id": key_id, "used_at": event_time},
            )
            written[key_id] = minute
        return written

    async def run(self, stop: asyncio.Event) -> None:
        """Run replay and heartbeat loops until a cooperative stop is requested."""

        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self._replay_loop(stop))
            tasks.create_task(self._heartbeat_loop(stop))

    async def _replay_loop(self, stop: asyncio.Event) -> None:
        # Unknown convergence shares this loop so the single SQLite connection is
        # never used from two worker threads at once. The first scan runs at startup,
        # right after the first replay batch, so already-durable terminals win.
        loop = asyncio.get_running_loop()
        failure_count = 0
        data_version: int | None = None
        next_unknown_scan = loop.time()
        next_usage_aggregate = loop.time()
        next_backlog_health = loop.time()
        next_maintenance = loop.time()
        while not stop.is_set():
            converged = 0
            try:
                if loop.time() >= next_backlog_health:
                    # Refreshed before replay so a stalled PostgreSQL still
                    # leaves the administrator view a current backlog verdict.
                    await self.refresh_backlog_health()
                    next_backlog_health = loop.time() + self._backlog_health_seconds
                if data_version is None:
                    data_version = await asyncio.to_thread(self._ledger.data_version)
                processed = await self.replay_once()
                if (
                    processed < MAX_REPLAY_BATCH_SIZE
                    and not stop.is_set()
                    and loop.time() >= next_unknown_scan
                ):
                    converged = await self.converge_unknown_once()
                    next_unknown_scan = loop.time() + self._unknown_scan_seconds
                if not stop.is_set() and loop.time() >= next_usage_aggregate:
                    await self.aggregate_usage_once()
                    next_usage_aggregate = loop.time() + self._usage_aggregate_seconds
                if (
                    processed < MAX_REPLAY_BATCH_SIZE
                    and not stop.is_set()
                    and loop.time() >= next_maintenance
                ):
                    await self.maintain_once()
                    next_maintenance = loop.time() + self._maintenance_seconds
            except (LedgerError, SQLAlchemyError):
                delay = DATABASE_RETRY_BACKOFF_SECONDS[
                    min(failure_count, len(DATABASE_RETRY_BACKOFF_SECONDS) - 1)
                ]
                failure_count += 1
                _LOGGER.error(
                    "ledger replay unavailable; retrying",
                    extra={
                        "component": Component.LEDGER_WRITER.value,
                        "error_code": "replay_unavailable",
                        "retry_seconds": delay,
                    },
                )
                await self._wait_or_stop(stop, delay)
                continue
            failure_count = 0
            # Own-connection writes do not move PRAGMA data_version, so replay
            # appended Unknown events immediately instead of waiting for a change.
            if processed == MAX_REPLAY_BATCH_SIZE or converged > 0:
                continue
            assert data_version is not None
            data_version = await self._wait_for_change(stop, data_version)

    async def _wait_for_change(self, stop: asyncio.Event, data_version: int) -> int:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._poll_seconds
        while not stop.is_set():
            remaining = deadline - loop.time()
            if remaining <= 0:
                return data_version
            if await self._wait_or_stop(stop, min(WAKE_CHECK_SECONDS, remaining)):
                return data_version
            current = await asyncio.to_thread(self._ledger.data_version)
            if current != data_version:
                return current
        return data_version

    async def _heartbeat_loop(self, stop: asyncio.Event) -> None:
        await self._write_heartbeat(ComponentStatus.HEALTHY)
        while not stop.is_set():
            if await self._wait_or_stop(stop, self._heartbeat_seconds):
                break
            await self._write_heartbeat(ComponentStatus.HEALTHY)
        await self._write_heartbeat(ComponentStatus.STOPPING)

    async def _write_heartbeat(self, status: ComponentStatus) -> None:
        # Expose the backlog verdict to the administrator view.
        # The existing heartbeat row already carries a free-text reason, so no
        # new RPC, table or endpoint is needed to publish it.
        backlog = LedgerHealth.UNAVAILABLE if self._maintenance_failed else self._backlog_health
        if status is ComponentStatus.HEALTHY:
            status = _BACKLOG_HEARTBEAT_STATUS[backlog]
        try:
            async with self._session_factory() as session, session.begin():
                await ComponentHeartbeatRepository(session).write(
                    component=Component.LEDGER_WRITER,
                    instance_id=self._instance_id,
                    version=self._build_version,
                    status=status,
                    reason=encode_ledger_backlog_reason(backlog, self._backlog_details),
                )
        except SQLAlchemyError:
            _LOGGER.warning(
                "ledger writer heartbeat unavailable",
                extra={
                    "component": Component.LEDGER_WRITER.value,
                    "error_code": "heartbeat_failed",
                },
            )

    @staticmethod
    async def _wait_or_stop(stop: asyncio.Event, delay: float) -> bool:
        if stop.is_set():
            return True
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=delay)
        return stop.is_set()


async def run_until_signalled(writer: LedgerWriter) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for selected in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(selected, stop.set)
        except (NotImplementedError, RuntimeError):
            continue
        installed.append(selected)
    try:
        await writer.run(stop)
    finally:
        for selected in installed:
            loop.remove_signal_handler(selected)


__all__ = [
    "DATABASE_RETRY_BACKOFF_SECONDS",
    "DEFAULT_ACK_RETENTION_SECONDS",
    "DEFAULT_BACKLOG_HEALTH_SECONDS",
    "DEFAULT_MAINTENANCE_SECONDS",
    "DEFAULT_UNKNOWN_AFTER_SECONDS",
    "DEFAULT_UNKNOWN_SCAN_SECONDS",
    "DEFAULT_USAGE_AGGREGATE_SECONDS",
    "GATEWAY_IDLE_CONFIRM_SECONDS",
    "LedgerMaintenanceResult",
    "MAX_REPLAY_BATCH_SIZE",
    "MAX_UNKNOWN_SCAN_BATCH_SIZE",
    "WAKE_CHECK_SECONDS",
    "LedgerWriter",
    "run_until_signalled",
]

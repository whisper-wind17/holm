"""Bounded, in-memory diagnostic bundle assembly.

Every production adapter in this module consumes an existing sanitized service
or Repository result.  The assembler never opens configuration, log, database,
WAL, or secrets files.
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import re
import zipfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import partial
from typing import Final, Protocol, cast
from uuid import UUID

from holm_common import as_utc
from holm_contracts import VersionPayload
from holm_domain import PRODUCTION_EVENT_REGISTRY, DomainEvent
from holm_persistence import AsyncSessionFactory, DomainEventRepository

from holm_control_api.model_instance_service import ModelInstanceService
from holm_control_api.request_record_service import (
    RequestRecordQuery,
    RequestRecordQueryService,
)
from holm_control_api.system_status import SystemStatusService
from holm_control_api.telemetry_routes import OverviewTelemetryResponse
from holm_control_api.telemetry_service import TelemetryService

DEFAULT_WINDOW_MINUTES: Final[int] = 60
MAX_WINDOW_MINUTES: Final[int] = 24 * 60
MAX_REQUEST_IDS: Final[int] = 20
MAX_EVENT_COUNT: Final[int] = 100
MAX_LOG_LINE_COUNT: Final[int] = 500
MAX_ITEM_BYTES: Final[int] = 512 * 1024
MAX_CONTENT_BYTES: Final[int] = 2 * 1024 * 1024
SOURCE_TIMEOUT_SECONDS: Final[float] = 3.0
ARCHIVE_CHUNK_BYTES: Final[int] = 64 * 1024
REDACTED_CONFIG_VALUE: Final[str] = "<redacted>"

_MANIFEST_PATH: Final[str] = "manifest.json"
_BODY_FIELD_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b(prompt|messages?|request[_ -]?body|response[_ -]?body|"
    r"tool[_ -]?(arguments?|args)|generated[_ -]?(result|output)|content)\b"
)
_POSIX_ABSOLUTE_PATH_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z0-9_:/])/(?:[^\s/]+/)*[^\s]*"
)
_WINDOWS_ABSOLUTE_PATH_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?i)\b[A-Z]:\\[^\s]*")


type JsonValue = object


class DiagnosticItemStatus(StrEnum):
    INCLUDED = "included"
    NOT_INCLUDED = "not_included"


@dataclass(frozen=True, slots=True)
class DiagnosticQuery:
    from_time: datetime
    to_time: datetime
    request_ids: tuple[UUID, ...]
    instance_id: UUID | None


@dataclass(frozen=True, slots=True)
class DiagnosticEvent:
    id: int
    event: str
    aggregate_id: UUID
    version: int
    occurred_at: datetime
    payload: Mapping[str, JsonValue]

    def json(self) -> dict[str, JsonValue]:
        return {
            "id": self.id,
            "event": self.event,
            "aggregate_id": str(self.aggregate_id),
            "version": self.version,
            "occurred_at": as_utc(self.occurred_at).isoformat(),
            "payload": dict(self.payload),
        }


@dataclass(frozen=True, slots=True)
class DiagnosticRequestRecord:
    request_id: UUID
    received_at: datetime
    completed_at: datetime | None
    instance_id: UUID | None
    service_id: UUID | None
    user_id: UUID | None
    stream: bool | None
    status: str
    http_status: int | None
    error_code: str | None
    input_tokens: int | None
    output_tokens: int | None
    ttft_ms: int | None
    duration_ms: int | None

    def json(self) -> dict[str, JsonValue]:
        return {
            "request_id": str(self.request_id),
            "received_at": as_utc(self.received_at).isoformat(),
            "completed_at": (
                None if self.completed_at is None else as_utc(self.completed_at).isoformat()
            ),
            "instance_id": None if self.instance_id is None else str(self.instance_id),
            "service_id": None if self.service_id is None else str(self.service_id),
            "user_id": None if self.user_id is None else str(self.user_id),
            "stream": self.stream,
            "status": self.status,
            "http_status": self.http_status,
            "error_code": self.error_code,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "ttft_ms": self.ttft_ms,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True, slots=True)
class DiagnosticLogEntry:
    instance_id: UUID
    sequence: int
    text: str
    truncated: bool
    original_character_count: int


@dataclass(frozen=True, slots=True)
class SourcePage[ItemT]:
    items: tuple[ItemT, ...]
    truncated: bool = False


class HealthSource(Protocol):
    async def read(self) -> Mapping[str, JsonValue]: ...


class MetricSource(Protocol):
    async def read(self) -> Mapping[str, JsonValue]: ...


class EventSource(Protocol):
    async def read(
        self, *, from_time: datetime, to_time: datetime, limit: int
    ) -> SourcePage[DiagnosticEvent]: ...


class RequestRecordSource(Protocol):
    async def read(
        self,
        *,
        request_ids: tuple[UUID, ...],
        from_time: datetime,
        to_time: datetime,
    ) -> SourcePage[DiagnosticRequestRecord]: ...


class SanitizedLogSource(Protocol):
    async def read(self, *, instance_id: UUID, limit: int) -> SourcePage[DiagnosticLogEntry]: ...


@dataclass(frozen=True, slots=True)
class DiagnosticManifestItem:
    key: str
    path: str
    status: DiagnosticItemStatus
    count: int
    bytes: int
    truncated: bool
    reason: str | None = None

    def json(self) -> dict[str, JsonValue]:
        return {
            "key": self.key,
            "path": self.path,
            "status": self.status.value,
            "count": self.count,
            "bytes": self.bytes,
            "truncated": self.truncated,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class DiagnosticManifest:
    generated_at: datetime
    query: DiagnosticQuery
    items: tuple[DiagnosticManifestItem, ...]
    content_bytes: int
    archive_files: tuple[str, ...]
    max_item_bytes: int = MAX_ITEM_BYTES
    max_content_bytes: int = MAX_CONTENT_BYTES

    def json(self) -> dict[str, JsonValue]:
        return {
            "format_version": 1,
            "generated_at": as_utc(self.generated_at).isoformat(),
            "window": {
                "from": as_utc(self.query.from_time).isoformat(),
                "to": as_utc(self.query.to_time).isoformat(),
            },
            "request_ids": [str(value) for value in self.query.request_ids],
            "instance_id": (
                None if self.query.instance_id is None else str(self.query.instance_id)
            ),
            "limits": {
                "max_window_minutes": MAX_WINDOW_MINUTES,
                "max_request_ids": MAX_REQUEST_IDS,
                "max_events": MAX_EVENT_COUNT,
                "max_log_lines": MAX_LOG_LINE_COUNT,
                "max_item_bytes": self.max_item_bytes,
                "max_content_bytes": self.max_content_bytes,
            },
            "content_bytes": self.content_bytes,
            "archive_files": list(self.archive_files),
            "items": [item.json() for item in self.items],
        }


@dataclass(frozen=True, slots=True)
class DiagnosticAssembly:
    manifest: DiagnosticManifest
    files: tuple[tuple[str, bytes], ...]

    def archive_bytes(self) -> bytes:
        manifest_bytes = _json_bytes(self.manifest.json())
        target = io.BytesIO()
        with zipfile.ZipFile(
            target,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:
            _write_archive_entry(archive, _MANIFEST_PATH, manifest_bytes)
            for path, content in self.files:
                _write_archive_entry(archive, path, content)
        return target.getvalue()


class DiagnosticSourceUnavailable(RuntimeError):
    """A source cannot provide a proven-sanitized result."""

    def __init__(self, reason: str = "sanitized_source_unavailable") -> None:
        self.reason = reason
        super().__init__(reason)


class SystemHealthDiagnosticSource:
    def __init__(self, service: SystemStatusService) -> None:
        self._service = service

    async def read(self) -> Mapping[str, JsonValue]:
        value = await self._service.read_components()
        return cast(Mapping[str, JsonValue], value.model_dump(mode="json"))


class TelemetryOverviewDiagnosticSource:
    def __init__(self, service: TelemetryService) -> None:
        self._service = service

    async def read(self) -> Mapping[str, JsonValue]:
        response = OverviewTelemetryResponse.from_snapshot(self._service.overview())
        return cast(Mapping[str, JsonValue], response.model_dump(mode="json"))


class RepositoryDiagnosticEventSource:
    """Use only the existing stable Repository page and its bounded ID window."""

    def __init__(self, session_factory: AsyncSessionFactory) -> None:
        self._session_factory = session_factory

    async def read(
        self, *, from_time: datetime, to_time: datetime, limit: int
    ) -> SourcePage[DiagnosticEvent]:
        fetch_limit = limit + 1
        async with self._session_factory() as session:
            repository = DomainEventRepository(session)
            frontier = await repository.stable_frontier()
            # IDs are monotonic.  Restricting the lower cursor to one bounded
            # ID window avoids an oldest-first scan of the full event table.
            after_id = max(0, frontier - fetch_limit)
            records = await repository.stable_page(
                after_id=after_id,
                through_id=frontier,
                limit=fetch_limit,
            )
        selected: list[DiagnosticEvent] = []
        for record in records:
            occurred_at = as_utc(record.occurred_at)
            if not from_time <= occurred_at <= to_time:
                continue
            selected.append(_diagnostic_event(record))
        return SourcePage(tuple(selected[-limit:]), truncated=len(selected) > limit)


class QueryServiceRequestRecordSource:
    def __init__(self, service: RequestRecordQueryService) -> None:
        self._service = service

    async def read(
        self,
        *,
        request_ids: tuple[UUID, ...],
        from_time: datetime,
        to_time: datetime,
    ) -> SourcePage[DiagnosticRequestRecord]:
        records: list[DiagnosticRequestRecord] = []
        for request_id in request_ids:
            page = await self._service.list(
                RequestRecordQuery(
                    request_id=request_id,
                    from_time=from_time,
                    to_time=to_time,
                    limit=1,
                )
            )
            if not page.items:
                continue
            item = page.items[0]
            records.append(
                DiagnosticRequestRecord(
                    request_id=item.request_id,
                    received_at=item.received_at,
                    completed_at=item.completed_at,
                    instance_id=item.instance_id,
                    service_id=item.service_id,
                    user_id=item.user_id,
                    stream=item.stream,
                    status=item.status.value,
                    http_status=item.http_status,
                    error_code=None if item.error_code is None else item.error_code.value,
                    input_tokens=item.input_tokens,
                    output_tokens=item.output_tokens,
                    ttft_ms=item.ttft_ms,
                    duration_ms=item.duration_ms,
                )
            )
        return SourcePage(tuple(records))


class ModelInstanceSanitizedLogSource:
    """Page only through ModelInstanceService and therefore HostAgentLogClient."""

    def __init__(self, service: ModelInstanceService) -> None:
        self._service = service

    async def read(self, *, instance_id: UUID, limit: int) -> SourcePage[DiagnosticLogEntry]:
        entries: list[DiagnosticLogEntry] = []
        cursor: str | None = None
        while len(entries) <= limit:
            page_size = min(100, limit + 1 - len(entries))
            page = await self._service.logs(
                instance_id,
                cursor=cursor,
                page_size=page_size,
            )
            entries.extend(
                DiagnosticLogEntry(
                    instance_id=instance_id,
                    sequence=entry.sequence,
                    text=entry.text,
                    truncated=entry.truncated,
                    original_character_count=entry.original_character_count,
                )
                for entry in page.entries
            )
            cursor = page.next_cursor
            if cursor is None:
                break
        return SourcePage(
            tuple(entries[:limit]),
            truncated=cursor is not None or len(entries) > limit,
        )


class DiagnosticBundleService:
    """Create preview and archive inputs through one fail-closed assembly path."""

    def __init__(
        self,
        *,
        version: VersionPayload,
        config: Mapping[str, object] | None,
        health: HealthSource,
        events: EventSource,
        metrics: MetricSource,
        request_records: RequestRecordSource,
        logs: SanitizedLogSource,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_item_bytes: int = MAX_ITEM_BYTES,
        max_content_bytes: int = MAX_CONTENT_BYTES,
        source_timeout_seconds: float = SOURCE_TIMEOUT_SECONDS,
    ) -> None:
        if max_item_bytes < 1 or max_content_bytes < 1:
            raise ValueError("diagnostic byte limits must be positive")
        if source_timeout_seconds <= 0:
            raise ValueError("diagnostic source timeout must be positive")
        self._version = version
        self._config = config
        self._health = health
        self._events = events
        self._metrics = metrics
        self._request_records = request_records
        self._logs = logs
        self._clock = clock
        self._max_item_bytes = max_item_bytes
        self._max_content_bytes = max_content_bytes
        self._source_timeout_seconds = source_timeout_seconds

    def query(
        self,
        *,
        window_minutes: int = DEFAULT_WINDOW_MINUTES,
        end_at: datetime | None = None,
        request_ids: Sequence[UUID] = (),
        instance_id: UUID | None = None,
    ) -> DiagnosticQuery:
        if not 1 <= window_minutes <= MAX_WINDOW_MINUTES:
            raise ValueError("diagnostic window is out of range")
        normalized_ids = tuple(dict.fromkeys(request_ids))
        if len(normalized_ids) > MAX_REQUEST_IDS:
            raise ValueError("too many diagnostic request IDs")
        to_time = as_utc(self._clock() if end_at is None else end_at)
        return DiagnosticQuery(
            from_time=to_time - timedelta(minutes=window_minutes),
            to_time=to_time,
            request_ids=normalized_ids,
            instance_id=instance_id,
        )

    async def assemble(self, query: DiagnosticQuery) -> DiagnosticAssembly:
        generated_at = as_utc(self._clock())
        files: list[tuple[str, bytes]] = []
        manifest_items: list[DiagnosticManifestItem] = []
        remaining = self._max_content_bytes

        def add_fixed(key: str, path: str, payload: Mapping[str, JsonValue]) -> None:
            nonlocal remaining
            content = _json_bytes(dict(payload))
            limit = min(self._max_item_bytes, remaining)
            if len(content) > limit:
                manifest_items.append(_omitted(key, path, "sanitized_content_exceeds_limit"))
                return
            files.append((path, content))
            remaining -= len(content)
            manifest_items.append(
                DiagnosticManifestItem(
                    key=key,
                    path=path,
                    status=DiagnosticItemStatus.INCLUDED,
                    count=1,
                    bytes=len(content),
                    truncated=False,
                )
            )

        def add_rows(
            key: str,
            path: str,
            rows: Sequence[Mapping[str, JsonValue]],
            *,
            source_truncated: bool = False,
        ) -> None:
            nonlocal remaining
            included: list[Mapping[str, JsonValue]] = []
            limit = min(self._max_item_bytes, remaining)
            content = _json_bytes({"items": []})
            for row in rows:
                candidate = _json_bytes({"items": [*included, dict(row)]})
                if len(candidate) > limit:
                    break
                included.append(dict(row))
                content = candidate
            truncated = source_truncated or len(included) < len(rows)
            if len(content) > limit:
                manifest_items.append(_omitted(key, path, "sanitized_content_exceeds_limit"))
                return
            files.append((path, content))
            remaining -= len(content)
            manifest_items.append(
                DiagnosticManifestItem(
                    key=key,
                    path=path,
                    status=DiagnosticItemStatus.INCLUDED,
                    count=len(included),
                    bytes=len(content),
                    truncated=truncated,
                    reason="byte_or_count_limit_reached" if truncated else None,
                )
            )

        add_fixed(
            "component_versions",
            "components/version.json",
            self._version.model_dump(mode="json"),
        )
        if self._config is None:
            manifest_items.append(
                _omitted("configuration", "config/redacted.json", "sanitized_config_unavailable")
            )
        else:
            try:
                sanitized_config = _sanitized_config(self._config)
                add_fixed("configuration", "config/redacted.json", sanitized_config)
            except (TypeError, ValueError):
                manifest_items.append(
                    _omitted(
                        "configuration",
                        "config/redacted.json",
                        "sanitized_config_unavailable",
                    )
                )

        health = await self._read_source(self._health.read)
        if isinstance(health, DiagnosticSourceUnavailable):
            manifest_items.append(_omitted("health", "health/components.json", health.reason))
        else:
            add_fixed("health", "health/components.json", health)

        event_page = await self._read_source(
            lambda: self._events.read(
                from_time=query.from_time,
                to_time=query.to_time,
                limit=MAX_EVENT_COUNT,
            )
        )
        if isinstance(event_page, DiagnosticSourceUnavailable):
            manifest_items.append(
                _omitted("structured_events", "events/domain-events.json", event_page.reason)
            )
        else:
            add_rows(
                "structured_events",
                "events/domain-events.json",
                [event.json() for event in event_page.items],
                source_truncated=event_page.truncated,
            )

        metrics = await self._read_source(self._metrics.read)
        if isinstance(metrics, DiagnosticSourceUnavailable):
            manifest_items.append(_omitted("metrics", "metrics/overview.json", metrics.reason))
        else:
            add_fixed("metrics", "metrics/overview.json", metrics)

        records = await self._read_source(
            lambda: self._request_records.read(
                request_ids=query.request_ids,
                from_time=query.from_time,
                to_time=query.to_time,
            )
        )
        record_items: tuple[DiagnosticRequestRecord, ...] = ()
        if isinstance(records, DiagnosticSourceUnavailable):
            manifest_items.append(
                _omitted("request_records", "requests/metadata.json", records.reason)
            )
        else:
            record_items = records.items
            add_rows(
                "request_records",
                "requests/metadata.json",
                [record.json() for record in record_items],
                source_truncated=records.truncated,
            )

        log_rows, log_truncated, log_failure = await self._collect_logs(query, record_items)
        if log_failure is not None:
            manifest_items.append(_omitted("sanitized_logs", "logs/request-logs.json", log_failure))
        else:
            add_rows(
                "sanitized_logs",
                "logs/request-logs.json",
                log_rows,
                source_truncated=log_truncated,
            )

        content_bytes = sum(len(content) for _, content in files)
        archive_files = (_MANIFEST_PATH, *(path for path, _ in files))
        manifest = DiagnosticManifest(
            generated_at=generated_at,
            query=query,
            items=tuple(manifest_items),
            content_bytes=content_bytes,
            archive_files=archive_files,
            max_item_bytes=self._max_item_bytes,
            max_content_bytes=self._max_content_bytes,
        )
        return DiagnosticAssembly(manifest=manifest, files=tuple(files))

    async def _read_source[ValueT](
        self, operation: Callable[[], Awaitable[ValueT]]
    ) -> ValueT | DiagnosticSourceUnavailable:
        try:
            async with asyncio.timeout(self._source_timeout_seconds):
                return await operation()
        except DiagnosticSourceUnavailable as failure:
            return failure
        except TimeoutError:
            return DiagnosticSourceUnavailable("sanitized_source_timeout")
        except Exception:  # noqa: BLE001 - raw dependency details must not escape
            # Raw dependency exceptions can contain credentials, internal
            # endpoints, and paths.  Only a fixed reason enters the manifest.
            return DiagnosticSourceUnavailable()

    async def _collect_logs(
        self,
        query: DiagnosticQuery,
        records: tuple[DiagnosticRequestRecord, ...],
    ) -> tuple[list[Mapping[str, JsonValue]], bool, str | None]:
        if not query.request_ids:
            return [], False, None
        record_instances = {
            record.instance_id
            for record in records
            if record.instance_id is not None
            and (query.instance_id is None or record.instance_id == query.instance_id)
        }
        if not record_instances:
            return [], False, "no_sanitized_log_source_for_requested_records"
        requested = {str(request_id) for request_id in query.request_ids}
        rows: list[Mapping[str, JsonValue]] = []
        truncated = False
        for instance_id in sorted(record_instances, key=str):
            page = await self._read_source(
                partial(
                    self._logs.read,
                    instance_id=instance_id,
                    limit=MAX_LOG_LINE_COUNT,
                )
            )
            if isinstance(page, DiagnosticSourceUnavailable):
                return [], False, page.reason
            truncated = truncated or page.truncated
            for entry in page.items:
                matches = sorted(request_id for request_id in requested if request_id in entry.text)
                if not matches:
                    continue
                rows.append(
                    {
                        "instance_id": str(entry.instance_id),
                        "sequence": entry.sequence,
                        "request_ids": matches,
                        "text": _sanitize_diagnostic_log_text(entry.text),
                        "truncated": entry.truncated,
                        "original_character_count": entry.original_character_count,
                    }
                )
        return rows[:MAX_LOG_LINE_COUNT], truncated or len(rows) > MAX_LOG_LINE_COUNT, None


def _sanitized_config(config: Mapping[str, object]) -> Mapping[str, JsonValue]:
    """Project from an explicit allowlist; never inspect secret values."""

    result: dict[str, JsonValue] = {
        "secrets": {
            "api_key_pepper": REDACTED_CONFIG_VALUE,
            "database_password": REDACTED_CONFIG_VALUE,
            "master_key": REDACTED_CONFIG_VALUE,
            "model_source_token": REDACTED_CONFIG_VALUE,
            "tls_private_key": REDACTED_CONFIG_VALUE,
        }
    }
    timezone = config.get("timezone_default")
    if isinstance(timezone, str):
        result["timezone_default"] = timezone
    sections: dict[str, tuple[str, ...]] = {
        "storage": (
            "disk_warning_percent",
            "disk_critical_percent",
            "disk_critical_free_gib",
        ),
        "gateway": (
            "max_request_body_mib",
            "authorization_snapshot_max_age_seconds",
            "connect_timeout_seconds",
            "first_token_timeout_seconds",
            "total_timeout_seconds",
            "max_inflight_requests",
            "max_inflight_per_instance",
            "runtime_pool_timeout_ms",
            "runtime_pool_max_connections_per_instance",
            "runtime_pool_max_keepalive_per_instance",
            "tokenizer_cache_max_mib",
            "tokenizer_worker_threads",
            "tokenizer_fallback_timeout_seconds",
            "ledger_group_commit_max_wait_ms",
            "ledger_group_commit_max_requests",
        ),
        "runtime": (
            "start_timeout_seconds",
            "stop_grace_seconds",
            "restart_limit",
            "restart_window_seconds",
            "max_gpus_per_instance",
        ),
        "telemetry": (
            "scrape_interval_seconds",
            "scrape_timeout_seconds",
            "raw_retention_seconds",
            "aggregate_retention_days",
        ),
        "retention": ("request_records_days", "download_tasks_days"),
    }
    for section, fields in sections.items():
        source = config.get(section)
        if not isinstance(source, Mapping):
            continue
        selected: dict[str, JsonValue] = {}
        for field in fields:
            value = source.get(field)
            if isinstance(value, float) and not math.isfinite(value):
                continue
            if isinstance(value, (str, int, float, bool)):
                selected[field] = value
        if section == "storage":
            selected.update(
                {
                    "model_root": REDACTED_CONFIG_VALUE,
                    "tokenizer_root": REDACTED_CONFIG_VALUE,
                    "external_model_roots": REDACTED_CONFIG_VALUE,
                }
            )
        result[section] = selected
    return result


def _diagnostic_event(record: DomainEvent) -> DiagnosticEvent:
    """Re-apply the production payload allowlist at the export boundary."""

    declaration = PRODUCTION_EVENT_REGISTRY.declaration_for(record.event)
    payload = {
        name: record.payload[name] for name in declaration.payload_fields if name in record.payload
    }
    return DiagnosticEvent(
        id=record.id,
        event=record.event,
        aggregate_id=record.aggregate_id,
        version=record.version,
        occurred_at=as_utc(record.occurred_at),
        payload=payload,
    )


def _sanitize_diagnostic_log_text(value: str) -> str:
    if _BODY_FIELD_PATTERN.search(value):
        return "[REDACTED SENSITIVE LOG CONTENT]"
    without_windows_paths = _WINDOWS_ABSOLUTE_PATH_PATTERN.sub("[REDACTED ABSOLUTE PATH]", value)
    return _POSIX_ABSOLUTE_PATH_PATTERN.sub("[REDACTED ABSOLUTE PATH]", without_windows_paths)


def _omitted(key: str, path: str, reason: str) -> DiagnosticManifestItem:
    return DiagnosticManifestItem(
        key=key,
        path=path,
        status=DiagnosticItemStatus.NOT_INCLUDED,
        count=0,
        bytes=0,
        truncated=False,
        reason=reason,
    )


def _json_bytes(value: Mapping[str, JsonValue]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_archive_entry(archive: zipfile.ZipFile, path: str, content: bytes) -> None:
    info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    archive.writestr(info, content)


__all__ = [
    "ARCHIVE_CHUNK_BYTES",
    "DEFAULT_WINDOW_MINUTES",
    "MAX_CONTENT_BYTES",
    "MAX_EVENT_COUNT",
    "MAX_ITEM_BYTES",
    "MAX_LOG_LINE_COUNT",
    "MAX_REQUEST_IDS",
    "MAX_WINDOW_MINUTES",
    "DiagnosticAssembly",
    "DiagnosticBundleService",
    "DiagnosticEvent",
    "DiagnosticItemStatus",
    "DiagnosticLogEntry",
    "DiagnosticManifest",
    "DiagnosticManifestItem",
    "DiagnosticQuery",
    "DiagnosticRequestRecord",
    "DiagnosticSourceUnavailable",
    "ModelInstanceSanitizedLogSource",
    "QueryServiceRequestRecordSource",
    "RepositoryDiagnosticEventSource",
    "SourcePage",
    "SystemHealthDiagnosticSource",
    "TelemetryOverviewDiagnosticSource",
]

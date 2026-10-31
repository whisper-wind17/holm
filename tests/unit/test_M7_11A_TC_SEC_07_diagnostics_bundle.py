"""M7-11A / TC-SEC-07 bounded diagnostic bundle security tests."""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

import pytest
from holm_contracts import VersionPayload
from holm_control_api.diagnostics_service import (
    MAX_REQUEST_IDS,
    MAX_WINDOW_MINUTES,
    DiagnosticBundleService,
    DiagnosticEvent,
    DiagnosticLogEntry,
    DiagnosticRequestRecord,
    DiagnosticSourceUnavailable,
    EventSource,
    SourcePage,
    _diagnostic_event,
)
from holm_domain import DomainEvent

NOW: Final = datetime(2026, 9, 22, 8, 30, tzinfo=UTC)
REQUEST_ID: Final = UUID("01999a00-0000-7000-8000-000000000711")
INSTANCE_ID: Final = UUID("01999a00-0000-7000-8000-000000000712")
PROMPT_SENTINEL: Final = "PWSSNTL-PROMPT-TC-SEC-07-A1B2C3D4E5F6"
API_KEY_SENTINEL: Final = "PWSSNTL-APIKEY-TC-SEC-07-B2C3D4E5F6A1"
PATH_SENTINEL: Final = "PWSSNTL-FILE-TC-SEC-07-C3D4E5F6A1B2"


class _Health:
    async def read(self) -> dict[str, object]:
        return {"observed_at": NOW.isoformat(), "components": [], "summary": {}}


class _Metrics:
    async def read(self) -> dict[str, object]:
        return {
            "sampled_at": NOW.isoformat(),
            "telemetry_status": "available",
            "current": {"requests_running": 1.0},
        }


class _Events:
    async def read(
        self, *, from_time: datetime, to_time: datetime, limit: int
    ) -> SourcePage[DiagnosticEvent]:
        assert from_time <= NOW <= to_time
        assert limit > 0
        raw = DomainEvent(
            id=7,
            event="download.resolution_started",
            aggregate_id=REQUEST_ID,
            version=1,
            occurred_at=NOW,
            payload={"status": "started", "prompt": PROMPT_SENTINEL},
        )
        return SourcePage((_diagnostic_event(raw),))


class _UnavailableEvents:
    async def read(
        self, *, from_time: datetime, to_time: datetime, limit: int
    ) -> SourcePage[DiagnosticEvent]:
        del from_time, to_time, limit
        raise DiagnosticSourceUnavailable("redacted_event_projection_unavailable")


class _ManyEvents:
    async def read(
        self, *, from_time: datetime, to_time: datetime, limit: int
    ) -> SourcePage[DiagnosticEvent]:
        del from_time, to_time
        return SourcePage(
            tuple(
                DiagnosticEvent(
                    id=index,
                    event="api_key.changed",
                    aggregate_id=REQUEST_ID,
                    version=index,
                    occurred_at=NOW,
                    payload={"status": "x" * 64},
                )
                for index in range(1, limit + 1)
            ),
            truncated=True,
        )


class _Requests:
    async def read(
        self,
        *,
        request_ids: tuple[UUID, ...],
        from_time: datetime,
        to_time: datetime,
    ) -> SourcePage[DiagnosticRequestRecord]:
        assert request_ids in {(REQUEST_ID,), ()}
        assert from_time <= NOW <= to_time
        if not request_ids:
            return SourcePage(())
        return SourcePage(
            (
                DiagnosticRequestRecord(
                    request_id=REQUEST_ID,
                    received_at=NOW,
                    completed_at=NOW,
                    instance_id=INSTANCE_ID,
                    service_id=None,
                    user_id=None,
                    stream=False,
                    status="completed",
                    http_status=200,
                    error_code=None,
                    input_tokens=1,
                    output_tokens=1,
                    ttft_ms=1,
                    duration_ms=2,
                ),
            )
        )


class _Logs:
    async def read(self, *, instance_id: UUID, limit: int) -> SourcePage[DiagnosticLogEntry]:
        assert instance_id == INSTANCE_ID
        assert limit > 0
        path = f"/workspace/private/{PATH_SENTINEL}/model.bin"
        return SourcePage(
            (
                DiagnosticLogEntry(
                    instance_id=instance_id,
                    sequence=1,
                    text=f"request {REQUEST_ID} loaded {path}",
                    truncated=False,
                    original_character_count=120,
                ),
            )
        )


def _service(
    *,
    events: EventSource | None = None,
    max_item_bytes: int = 512 * 1024,
    max_content_bytes: int = 2 * 1024 * 1024,
) -> DiagnosticBundleService:
    return DiagnosticBundleService(
        version=VersionPayload(
            build_version="1.0.0+gm711a",
            commit="7" * 40,
            schema_version=1,
            runtime_compat_hash="8" * 64,
        ),
        config={
            "timezone_default": "Asia/Shanghai",
            "request_body": PROMPT_SENTINEL,
            "database_password": API_KEY_SENTINEL,
            "storage": {
                "model_root": f"/workspace/private/{PATH_SENTINEL}",
                "disk_warning_percent": 10,
            },
            "unknown": {"messages": PROMPT_SENTINEL},
        },
        health=_Health(),
        events=_Events() if events is None else events,
        metrics=_Metrics(),
        request_records=_Requests(),
        logs=_Logs(),
        clock=lambda: NOW,
        max_item_bytes=max_item_bytes,
        max_content_bytes=max_content_bytes,
    )


def _archive(service: DiagnosticBundleService) -> tuple[dict[str, object], dict[str, bytes]]:
    query = service.query(end_at=NOW, request_ids=(REQUEST_ID,), instance_id=INSTANCE_ID)
    assembly = asyncio.run(service.assemble(query))
    archive = assembly.archive_bytes()
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        files = {name: bundle.read(name) for name in bundle.namelist()}
    return json.loads(files["manifest.json"]), files


def test_M7_11A_TC_SEC_07_bundle_sentinel_scan_is_zero_for_body_secret_and_path() -> None:
    manifest, files = _archive(_service())

    assert set(files) == set(manifest["archive_files"])
    print("M7-11A archive files:", ", ".join(sorted(files)))
    scanned = b"\n".join(files.values())
    for sentinel in (PROMPT_SENTINEL, API_KEY_SENTINEL, PATH_SENTINEL):
        assert sentinel.encode() not in scanned
    assert b"<redacted>" in files["config/redacted.json"]
    assert b"[REDACTED ABSOLUTE PATH]" in files["logs/request-logs.json"]
    assert not any(
        name.endswith((".db", ".sqlite", ".sqlite3", "-wal", "-shm")) or name.startswith("secrets/")
        for name in files
    )


def test_M7_11A_guard_omits_item_when_no_sanitized_projection_exists() -> None:
    manifest, files = _archive(_service(events=_UnavailableEvents()))

    assert "events/domain-events.json" not in files
    event_item = next(item for item in manifest["items"] if item["key"] == "structured_events")
    assert event_item == {
        "bytes": 0,
        "count": 0,
        "key": "structured_events",
        "path": "events/domain-events.json",
        "reason": "redacted_event_projection_unavailable",
        "status": "not_included",
        "truncated": False,
    }


def test_M7_11A_preview_and_archive_manifest_share_the_same_assembly_contract() -> None:
    service = _service()
    query = service.query(end_at=NOW, request_ids=(REQUEST_ID,), instance_id=INSTANCE_ID)
    preview = asyncio.run(service.assemble(query)).manifest.json()
    manifest, _ = _archive(service)

    assert preview == manifest


def test_M7_11A_count_byte_and_time_limits_are_explicit_and_fail_closed() -> None:
    service = _service(events=_ManyEvents(), max_item_bytes=384, max_content_bytes=2_048)
    query = service.query(end_at=NOW)
    assembly = asyncio.run(service.assemble(query))
    manifest = assembly.manifest.json()
    event_item = next(item for item in manifest["items"] if item["key"] == "structured_events")

    assert event_item["truncated"] is True
    assert event_item["reason"] == "byte_or_count_limit_reached"
    assert assembly.manifest.content_bytes <= 2_048
    assert manifest["limits"]["max_window_minutes"] == MAX_WINDOW_MINUTES
    assert manifest["limits"]["max_request_ids"] == MAX_REQUEST_IDS
    assert manifest["limits"]["max_item_bytes"] == 384
    assert manifest["limits"]["max_content_bytes"] == 2_048

    one_byte_service = _service(max_item_bytes=1, max_content_bytes=1)
    one_byte_assembly = asyncio.run(one_byte_service.assemble(one_byte_service.query(end_at=NOW)))
    assert one_byte_assembly.manifest.content_bytes == 0
    assert one_byte_assembly.manifest.archive_files == ("manifest.json",)

    with pytest.raises(ValueError, match="window"):
        service.query(window_minutes=MAX_WINDOW_MINUTES + 1)
    with pytest.raises(ValueError, match="request IDs"):
        service.query(request_ids=tuple(UUID(int=index) for index in range(MAX_REQUEST_IDS + 1)))

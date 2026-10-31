"""M5-09 TDD 14.6 admission protection over a real SQLite Request Ledger."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from holm_domain import (
    Component,
    ComponentStatus,
    LedgerHealth,
    ModelInstanceStatus,
    decode_ledger_backlog_reason,
)
from holm_gateway.application import create_app  # type: ignore[import-untyped]
from holm_gateway.authentication import (  # type: ignore[import-untyped]
    ApiKeyAuthenticator,
)
from holm_gateway.routing import (  # type: ignore[import-untyped]
    CachedRouteDirectory,
    GatewayRoute,
)
from holm_ledger import (
    EventType,
    LedgerEvent,
    LedgerHealthMonitor,
    ReadOnlyLedgerBacklogSource,
    ReceivedEvent,
    RequestOperation,
    RequestProtocol,
    SpoolWriter,
    SpoolWriterState,
    open_ledger,
)
from holm_ledger_writer import writer as writer_module
from holm_ledger_writer.writer import LedgerWriter
from holm_runtimes import load_runtime_compatibility
from holm_security import (
    ApiKeyPepperKeyring,
    ApiKeyValidationSnapshot,
    issue_api_key,
)
from starlette.types import ASGIApp

SERVICE_ID = UUID("59000000-0000-4000-8000-000000000001")
INSTANCE_ID = UUID("59000000-0000-4000-8000-000000000003")
USER_ID = UUID("59000000-0000-4000-8000-000000000004")
KEY_ID = UUID("59000000-0000-4000-8000-000000000005")
SEEDED_REQUEST_ID = UUID("59000000-0000-4000-8000-000000000009")
NOW = datetime(2026, 9, 18, 11, 0, tzinfo=UTC)
SENTINEL = "M5_09_GATEWAY_BODY_SENTINEL_1f4ad2"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
VLLM_RUNTIME = load_runtime_compatibility(REPOSITORY_ROOT / "runtime-compat.yaml").runtime("vllm")
assert VLLM_RUNTIME is not None


class _RouteSource:
    def __init__(self, routes: tuple[GatewayRoute, ...]) -> None:
        self.routes = routes

    async def list_routes(self) -> tuple[GatewayRoute, ...]:
        return self.routes


def _route() -> GatewayRoute:
    return GatewayRoute(
        service_id=SERVICE_ID,
        instance_id=INSTANCE_ID,
        public_model_name="m5-09-model",
        enabled=True,
        instance_status=ModelInstanceStatus.RUNNING.value,
        runtime_name="vllm",
        internal_endpoint="runtime:8000",
        internal_model_name="internal-model",
    )


def _authenticator() -> tuple[ApiKeyAuthenticator, str]:
    keyring = ApiKeyPepperKeyring(current_key_id="m5-09", keys={"m5-09": b"p" * 32})
    issued = issue_api_key(keyring)

    async def lookup(lookup_id: str) -> ApiKeyValidationSnapshot | None:
        if lookup_id != issued.lookup_id:
            return None
        return ApiKeyValidationSnapshot(
            user_id=USER_ID,
            api_key_id=KEY_ID,
            pepper_key_id=issued.pepper_key_id,
            secret_hmac=issued.secret_hmac,
            revoked=False,
            expired_status=False,
            expires_at=None,
            user_active=True,
            scoped_service_ids=frozenset(),
            database_now=NOW,
            user_is_admin=True,
            authorized_service_ids=frozenset({SERVICE_ID}),
        )

    return ApiKeyAuthenticator(lookup=lookup, keyring=keyring), issued.plaintext


def _ledger_path(tmp_path: Path) -> Path:
    directory = tmp_path / "spool"
    directory.mkdir(mode=0o700)
    return directory / "request-ledger.sqlite3"


def _seed_unacked_event(path: Path, *, age_seconds: float) -> None:
    """Leave one unacknowledged event of a known age in a real Ledger file."""

    created_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    with open_ledger(path) as ledger:
        ledger.append(
            ReceivedEvent(
                request_id=SEEDED_REQUEST_ID,
                event_seq=1,
                created_at=created_at,
                received_at=created_at,
                protocol=RequestProtocol.OPENAI,
                operation=RequestOperation.CHAT_COMPLETIONS,
                endpoint="/v1/chat/completions",
                stream=False,
                input_bytes=1,
            )
        )


def _events(path: Path) -> tuple[LedgerEvent, ...]:
    with open_ledger(path) as ledger:
        return tuple(item.event for item in ledger.list_unacked())


class _Upstream:
    """Counts every Runtime contact so "not forwarded" can be asserted directly."""

    def __init__(
        self,
        responder: Callable[[], httpx.Response] | None = None,
        *,
        on_call: Callable[[], None] | None = None,
    ) -> None:
        self.calls = 0
        self._responder = responder
        self._on_call = on_call

    async def handle(self, request: httpx.Request) -> httpx.Response:
        del request
        self.calls += 1
        if self._on_call is not None:
            self._on_call()
        if self._responder is None:
            raise AssertionError("Runtime was contacted while the Ledger was unavailable")
        return self._responder()


def _completion() -> httpx.Response:
    return httpx.Response(
        200,
        content=b"data: "
        + json.dumps(
            {
                "id": "chatcmpl-m509",
                "object": "chat.completion.chunk",
                "model": "internal-model",
                "choices": [
                    {"delta": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            }
        ).encode()
        + b"\n\ndata: [DONE]\n\n",
    )


def _app(
    handler: Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]],
    *,
    ledger: Any,
    monitor: LedgerHealthMonitor | None,
) -> tuple[ASGIApp, str, httpx.AsyncClient]:
    assert VLLM_RUNTIME is not None
    authenticator, key = _authenticator()
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None)
    app = create_app(
        authenticator=authenticator,
        routes=CachedRouteDirectory(_RouteSource((_route(),))),
        runtime_capabilities={"vllm": VLLM_RUNTIME.request_capabilities},
        upstream_client=upstream,
        request_ledger=ledger,
        ledger_health=monitor,
    )
    return app, key, upstream


async def _post(app: ASGIApp, key: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        return await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "m5-09-model",
                "messages": [{"role": "user", "content": SENTINEL}],
                "stream": False,
                "max_tokens": 0,
            },
        )


def _health(monitor: LedgerHealthMonitor) -> LedgerHealth:
    """Read the verdict through a call so one assertion cannot narrow the next."""

    return monitor.health


def _monitor(path: Path, writer: SpoolWriter | None, **overrides: Any) -> LedgerHealthMonitor:
    options: dict[str, Any] = {
        "delayed_after_seconds": 30.0,
        "unavailable_after_seconds": 300.0,
        "interval_seconds": 1.0,
    }
    options.update(overrides)
    return LedgerHealthMonitor(path, writer=writer, **options)


def test_M5_09_control_side_reader_observes_the_same_ledger_without_a_writer_handle(
    tmp_path: Path,
) -> None:
    path = _ledger_path(tmp_path)
    _seed_unacked_event(path, age_seconds=60.0)
    reader = ReadOnlyLedgerBacklogSource(path)
    try:
        oldest = reader.oldest_unacked_created_at()
    finally:
        reader.close()
    assert oldest is not None
    assert 55.0 <= (datetime.now(UTC) - oldest).total_seconds() <= 65.0


def test_M5_09_unavailable_ledger_rejects_before_the_runtime_is_contacted(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = _ledger_path(tmp_path)
        _seed_unacked_event(path, age_seconds=600.0)
        spool = SpoolWriter(path)
        spool.start()
        monitor = _monitor(path, spool)
        await monitor.sample_once()
        assert _health(monitor) is LedgerHealth.UNAVAILABLE
        upstream = _Upstream()
        app, key, client = _app(upstream.handle, ledger=spool, monitor=monitor)
        try:
            response = await _post(app, key)
            assert response.status_code == 503
            body = json.loads(response.content)
            assert body["error"]["code"] == "usage_storage_unavailable"
            # The Runtime must never see a request the Ledger cannot record.
            assert upstream.calls == 0
            # Nothing was written either: the refusal precedes Received.
            assert [event.request_id for event in _events(path)] == [SEEDED_REQUEST_ID]
            # The error body may not leak the spool path, free space or SQLite.
            rendered = response.text
            for forbidden in (str(tmp_path), "sqlite", "spool", "disk", "free", "bytes"):
                assert forbidden.lower() not in rendered.lower()
            assert set(body["error"]) == {"code", "message", "details", "request_id"}
            assert body["error"]["details"] == {}
        finally:
            await client.aclose()
            await monitor.aclose()
            await spool.close()

    asyncio.run(scenario())


def test_M5_09_delayed_ledger_keeps_serving_calls(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = _ledger_path(tmp_path)
        _seed_unacked_event(path, age_seconds=60.0)
        spool = SpoolWriter(path)
        spool.start()
        monitor = _monitor(path, spool)
        await monitor.sample_once()
        assert _health(monitor) is LedgerHealth.DELAYED
        upstream = _Upstream(_completion)
        app, key, client = _app(upstream.handle, ledger=spool, monitor=monitor)
        try:
            response = await _post(app, key)
            assert response.status_code == 200
            assert upstream.calls == 1
            written = [
                event.event_type for event in _events(path) if event.request_id != SEEDED_REQUEST_ID
            ]
            assert written == [EventType.RECEIVED, EventType.IN_PROGRESS, EventType.COMPLETED]
        finally:
            await client.aclose()
            await monitor.aclose()
            await spool.close()

    asyncio.run(scenario())


def test_M5_09_in_flight_requests_still_write_their_terminal_event(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = _ledger_path(tmp_path)
        spool = SpoolWriter(path)
        spool.start()
        monitor = _monitor(path, spool)
        await monitor.sample_once()
        assert _health(monitor) is LedgerHealth.AVAILABLE

        # The Ledger turns Unavailable while this request is already InProgress.
        upstream = _Upstream(
            _completion,
            on_call=lambda: monitor.latch_unavailable("ledger_terminal_write_failed"),
        )
        app, key, client = _app(upstream.handle, ledger=spool, monitor=monitor)
        try:
            accepted = await _post(app, key)
            assert accepted.status_code == 200
            assert _health(monitor) is LedgerHealth.UNAVAILABLE
            first = list(_events(path))
            assert [event.event_type for event in first] == [
                EventType.RECEIVED,
                EventType.IN_PROGRESS,
                EventType.COMPLETED,
            ]

            # Only new admissions are refused; the terminal write above stayed.
            rejected = await _post(app, key)
            assert rejected.status_code == 503
            assert json.loads(rejected.content)["error"]["code"] == "usage_storage_unavailable"
            assert upstream.calls == 1
            assert [event.event_type for event in _events(path)] == [
                event.event_type for event in first
            ]
        finally:
            await client.aclose()
            await monitor.aclose()
            await spool.close()

    asyncio.run(scenario())


def test_M5_09_spool_writer_storage_protection_closes_admission(tmp_path: Path) -> None:
    def refuse(events: tuple[LedgerEvent, ...]) -> None:
        del events
        raise OSError("simulated fsync failure")

    async def scenario() -> None:
        path = _ledger_path(tmp_path)
        spool = SpoolWriter(path, before_commit=refuse)
        spool.start()
        monitor = _monitor(path, spool)
        await monitor.sample_once()
        assert _health(monitor) is LedgerHealth.AVAILABLE

        upstream = _Upstream(_completion)
        app, key, client = _app(upstream.handle, ledger=spool, monitor=monitor)
        try:
            first = await _post(app, key)
            assert first.status_code == 503
            assert json.loads(first.content)["error"]["code"] == "usage_storage_unavailable"
            for _ in range(200):
                if spool.state is SpoolWriterState.UNAVAILABLE:
                    break
                await asyncio.sleep(0.01)
            assert spool.state is SpoolWriterState.UNAVAILABLE
            assert _health(monitor) is LedgerHealth.UNAVAILABLE

            forwarded_before = upstream.calls
            second = await _post(app, key)
            assert second.status_code == 503
            # The second request never reaches the Runtime at all.
            assert upstream.calls == forwarded_before
        finally:
            await client.aclose()
            await monitor.aclose()
            await spool.close()

    asyncio.run(scenario())


class _RecordingHeartbeats:
    written: list[dict[str, Any]] = []

    def __init__(self, session: Any) -> None:
        del session

    async def write(self, **fields: Any) -> None:
        _RecordingHeartbeats.written.append(fields)


class _FakeSession:
    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    def begin(self) -> _FakeSession:
        return self


@pytest.mark.parametrize(
    ("age_seconds", "expected_health", "expected_status"),
    [
        (1.0, LedgerHealth.AVAILABLE, ComponentStatus.HEALTHY),
        (60.0, LedgerHealth.DELAYED, ComponentStatus.DEGRADED),
        (600.0, LedgerHealth.UNAVAILABLE, ComponentStatus.DEGRADED),
    ],
)
def test_M5_09_ledger_writer_publishes_the_backlog_verdict_on_its_heartbeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    age_seconds: float,
    expected_health: LedgerHealth,
    expected_status: ComponentStatus,
) -> None:
    path = _ledger_path(tmp_path)
    _seed_unacked_event(path, age_seconds=age_seconds)
    _RecordingHeartbeats.written = []
    monkeypatch.setattr(writer_module, "ComponentHeartbeatRepository", _RecordingHeartbeats)

    async def scenario() -> None:
        with open_ledger(path) as ledger:
            writer = LedgerWriter(
                ledger=ledger,
                session_factory=cast(Any, _FakeSession),
                engine=cast(Any, object()),
            )
            assert await writer.refresh_backlog_health() is expected_health
            await writer._write_heartbeat(ComponentStatus.HEALTHY)

    asyncio.run(scenario())

    assert len(_RecordingHeartbeats.written) == 1
    published = _RecordingHeartbeats.written[0]
    assert published["component"] is Component.LEDGER_WRITER
    assert published["status"] is expected_status
    assert decode_ledger_backlog_reason(published["reason"]) is expected_health

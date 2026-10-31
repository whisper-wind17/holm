"""FX-M7-11B: M7-11A safe synthetic sources plus real HTTP authorization/router."""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from fixtures.asgi_client import NO_CONFIGURED_CREDENTIAL_ROWS, AsgiTestClient
from holm_control_api import (
    AuthenticationService,
    DependencyCheck,
    create_app,
    diagnostics_routes,
)
from holm_control_api.authentication import AuthenticationFailure
from holm_control_api.diagnostics_preview import DiagnosticPreviewStore, DiagnosticsBusy
from holm_control_api.http_constants import SESSION_COOKIE_NAME
from holm_domain import UserRole
from security.test_M7_11A_diagnostics_authorization import (
    _Authenticator,
    _diagnostics,
    _readiness_ok,
)


class _Auth(_Authenticator):
    async def authenticate_session(
        self,
        *,
        session_token: str | None,
        csrf_token: str | None = None,
        require_csrf: bool = False,
    ):
        if session_token != "FX-M7-11B-session" or (
            require_csrf and csrf_token != "FX-M7-11B-csrf"
        ):
            raise AuthenticationFailure(
                code="authentication_required", message="请重新登录", status_code=401
            )
        return self.authenticated


def _client(monkeypatch: pytest.MonkeyPatch, store: DiagnosticPreviewStore | None = None):
    auth = _Auth(UserRole.ADMIN)
    service = _diagnostics()
    if store is not None:
        monkeypatch.setattr(diagnostics_routes, "DiagnosticPreviewStore", lambda: store)
    app = create_app(
        version_payload=service._version,
        readiness_checks=[DependencyCheck(name="database", probe=_readiness_ok)],
        credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
        authentication=cast(AuthenticationService, auth),
        diagnostics=service,
    )
    return AsgiTestClient(app), auth, service


HEADERS = {"Cookie": f"{SESSION_COOKIE_NAME}=FX-M7-11B-session", "X-CSRF-Token": "FX-M7-11B-csrf"}


def test_M7_11B_preview_freezes_changing_sources_and_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario():
        client, _, service = _client(monkeypatch)
        async with client.lifespan():
            preview = await client.get("/api/v1/diagnostics", headers=HEADERS)
            assert preview.status == 200
            manifest = preview.json()
            fixture = Path(__file__).parents[1] / "contract/snapshots/m7_11b_manifest.json"
            assert manifest == json.loads(fixture.read_text())

            async def changed():
                return {"status": "changed-after-preview"}

            service._health.read = changed
            download = await client.request(
                "POST",
                "/api/v1/diagnostics",
                headers=HEADERS,
                json_body={
                    "end_at": manifest["window"]["to"],
                    "preview_receipt": dict(preview.headers)["x-diagnostic-preview"],
                },
            )
            assert download.status == 200
            with zipfile.ZipFile(io.BytesIO(download.body)) as archive:
                assert json.loads(archive.read("manifest.json")) == manifest
                assert (
                    "changed-after-preview" not in archive.read("health/components.json").decode()
                )
                assert set(archive.namelist()) == set(manifest["archive_files"])
            # Legacy POST still assembles the current source.
            legacy = await client.request(
                "POST", "/api/v1/diagnostics", headers=HEADERS, json_body={}
            )
            with zipfile.ZipFile(io.BytesIO(legacy.body)) as archive:
                assert "changed-after-preview" in archive.read("health/components.json").decode()

    asyncio.run(scenario())


def test_M7_11B_receipt_identity_expiry_parameters_and_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario():
        now = [0.0]
        store = DiagnosticPreviewStore(clock=lambda: now[0])
        client, auth, _ = _client(monkeypatch, store)
        async with client.lifespan():
            assert (await client.get("/api/v1/diagnostics")).status == 401
            preview = await client.get("/api/v1/diagnostics", headers=HEADERS)
            body = {
                "end_at": preview.json()["window"]["to"],
                "preview_receipt": dict(preview.headers)["x-diagnostic-preview"],
            }

            async def post(value=body, headers=HEADERS):
                return await client.request(
                    "POST", "/api/v1/diagnostics", headers=headers, json_body=value
                )

            assert (await post(headers={"Cookie": HEADERS["Cookie"]})).status == 401
            original = auth.authenticated
            auth.authenticated = replace(
                original, session=replace(original.session, id=UUID(int=22))
            )
            assert (await post()).status == 409
            auth.authenticated = replace(original, user=replace(original.user, id=UUID(int=23)))
            assert (await post()).status == 409
            auth.authenticated = replace(original, user=replace(original.user, role=UserRole.USER))
            assert (await post()).status == 403
            assert (await client.get("/api/v1/diagnostics", headers=HEADERS)).status == 403
            auth.authenticated = original
            assert (await post({**body, "preview_receipt": "unknown"})).status == 409
            assert (await post({**body, "window_minutes": 59})).status == 409
            assert (await post({"preview_receipt": body["preview_receipt"]})).status == 409
            now[0] = 301
            expired = await post()
            assert expired.status == 409
            assert expired.json()["error"]["code"] == "diagnostic_preview_expired"
            assert body["preview_receipt"] not in expired.body.decode()
            for suffix in (
                "window_minutes=1441",
                "request_id=bad",
                "&".join(["request_id=" + str(UUID(int=1))] * 21),
            ):
                assert (
                    await client.get("/api/v1/diagnostics?" + suffix, headers=HEADERS)
                ).status == 400

    asyncio.run(scenario())


@pytest.mark.parametrize("capacity", ["entries", "bytes", "generating"])
def test_M7_11B_capacity_guards_fail_loudly(monkeypatch: pytest.MonkeyPatch, capacity: str) -> None:
    async def scenario():
        store = DiagnosticPreviewStore(
            max_entries=1, max_bytes=1 if capacity == "bytes" else 100000, max_generating=1
        )
        client, _, _ = _client(monkeypatch, store)
        async with client.lifespan():
            release = store.acquire() if capacity == "generating" else lambda: None
            if capacity == "entries":
                assert (await client.get("/api/v1/diagnostics", headers=HEADERS)).status == 200
            blocked = await client.get("/api/v1/diagnostics", headers=HEADERS)
            assert blocked.status == 503
            assert blocked.json()["error"]["code"] == "diagnostics_busy"
            if capacity == "generating":
                assert (
                    await client.request(
                        "POST", "/api/v1/diagnostics", headers=HEADERS, json_body={}
                    )
                ).status == 503
            release()

    asyncio.run(scenario())


def test_M7_11B_slow_stream_holds_capacity_until_disconnect() -> None:
    async def scenario():
        store = DiagnosticPreviewStore(max_generating=1)
        release = store.acquire()
        started = asyncio.Event()

        async def chunks():
            yield b"safe"

        async def send(message):
            if message["type"] == "http.response.body":
                started.set()
                await asyncio.Event().wait()

        async def receive():
            await asyncio.Event().wait()

        response = diagnostics_routes._LimitedStreamingResponse(
            chunks(), release=release, media_type="application/zip", headers={}
        )
        task = asyncio.create_task(
            response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        )
        await started.wait()
        with pytest.raises(DiagnosticsBusy):
            store.acquire()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        store.acquire()()

    asyncio.run(scenario())


def test_M7_11B_cancelled_compression_keeps_slot_until_worker_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    from holm_control_api.diagnostics_service import DiagnosticAssembly

    async def scenario():
        started = asyncio.Event()
        released = asyncio.Event()
        unblock = threading.Event()
        loop = asyncio.get_running_loop()

        class Store(DiagnosticPreviewStore):
            def acquire(self):
                release = super().acquire()

                def done():
                    release()
                    released.set()

                return done

        store = Store(max_generating=1)
        client, _, _ = _client(monkeypatch, store)

        def compress(self):
            loop.call_soon_threadsafe(started.set)
            assert unblock.wait(5), "test must release its synthetic compression worker"
            return b"safe ZIP fixture"

        monkeypatch.setattr(DiagnosticAssembly, "archive_bytes", compress)
        async with client.lifespan():
            task = asyncio.create_task(
                client.request("POST", "/api/v1/diagnostics", headers=HEADERS, json_body={})
            )
            try:
                await asyncio.wait_for(started.wait(), 3)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                with pytest.raises(DiagnosticsBusy):
                    store.acquire()
                assert not released.is_set()
            finally:
                unblock.set()
            await asyncio.wait_for(released.wait(), 3)
            store.acquire()()

    asyncio.run(scenario())

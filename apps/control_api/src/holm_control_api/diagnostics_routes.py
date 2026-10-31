"""Administrator-only diagnostic bundle preview and download endpoints."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Query
from holm_domain import Capability
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.types import Receive, Scope, Send

from holm_control_api.auth_routes import json_response
from holm_control_api.authorization import (
    capability_route,
    current_authenticated_session,
    current_request_id,
)
from holm_control_api.diagnostics_preview import (
    DiagnosticPreviewStore,
    DiagnosticsBusy,
    PreviewUnavailable,
)
from holm_control_api.diagnostics_service import (
    ARCHIVE_CHUNK_BYTES,
    DEFAULT_WINDOW_MINUTES,
    MAX_REQUEST_IDS,
    MAX_WINDOW_MINUTES,
    DiagnosticBundleService,
)
from holm_control_api.http_constants import CACHE_CONTROL_VALUE
from holm_control_api.model_catalog_routes import _error_response

DIAGNOSTICS_PATH: Final[str] = "/api/v1/diagnostics"
DIAGNOSTICS_PREVIEW_PATH: Final[str] = f"{DIAGNOSTICS_PATH}/preview"


class DiagnosticBundleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    window_minutes: Annotated[int, Field(strict=True, ge=1, le=MAX_WINDOW_MINUTES)] = (
        DEFAULT_WINDOW_MINUTES
    )
    end_at: AwareDatetime | None = None
    request_ids: Annotated[tuple[UUID, ...], Field(max_length=MAX_REQUEST_IDS)] = ()
    instance_id: UUID | None = None
    preview_receipt: Annotated[str | None, Field(min_length=1, max_length=128)] = None


def create_diagnostics_router(
    service: DiagnosticBundleService, *, previews: DiagnosticPreviewStore | None = None
) -> APIRouter:
    store = previews if previews is not None else DiagnosticPreviewStore()
    router = APIRouter(prefix=DIAGNOSTICS_PATH, tags=["diagnostics"])

    @router.get("")
    @router.get("/preview")
    @capability_route(Capability.VIEW_INFRASTRUCTURE)
    async def preview_bundle(
        request: Request,
        window_minutes: Annotated[int, Query(ge=1, le=MAX_WINDOW_MINUTES)] = DEFAULT_WINDOW_MINUTES,
        end_at: AwareDatetime | None = None,
        request_ids: Annotated[
            tuple[UUID, ...], Query(alias="request_id", max_length=MAX_REQUEST_IDS)
        ] = (),
        instance_id: UUID | None = None,
    ) -> JSONResponse:
        query = service.query(
            window_minutes=window_minutes,
            end_at=end_at,
            request_ids=request_ids,
            instance_id=instance_id,
        )
        try:
            with store.generating():
                assembly = await service.assemble(query)
                receipt = store.put(_owner(request), assembly)
        except DiagnosticsBusy:
            return _preview_error(request, busy=True)
        response = json_response(
            request_id=str(current_request_id(request)), content=assembly.manifest.json()
        )
        response.headers["X-Diagnostic-Preview"] = receipt
        response.headers["X-Diagnostic-Preview-TTL"] = str(store.ttl)
        return response

    @router.post("", response_model=None)
    @capability_route(Capability.VIEW_INFRASTRUCTURE)
    async def create_bundle(
        request: Request,
        value: DiagnosticBundleRequest,
    ) -> StreamingResponse | JSONResponse:
        query = service.query(
            window_minutes=value.window_minutes,
            end_at=value.end_at,
            request_ids=value.request_ids,
            instance_id=value.instance_id,
        )
        try:
            release = store.acquire()
        except DiagnosticsBusy:
            return _preview_error(request, busy=True)
        transferred = False
        try:
            if value.preview_receipt is not None:
                assembly = store.get(_owner(request), value.preview_receipt)
                if value.end_at is None or query != assembly.manifest.query:
                    raise PreviewUnavailable()
            else:
                assembly = await service.assemble(query)
            task = asyncio.create_task(asyncio.to_thread(assembly.archive_bytes))
            try:
                archive = await asyncio.shield(task)
            except asyncio.CancelledError:
                # Cancellation never frees capacity while compression is still running.
                def finished(completed: asyncio.Task[bytes]) -> None:
                    try:
                        if not completed.cancelled():
                            completed.exception()
                    finally:
                        release()

                task.add_done_callback(finished)
                transferred = True
                raise

            async def chunks() -> AsyncIterator[bytes]:
                for offset in range(0, len(archive), ARCHIVE_CHUNK_BYTES):
                    yield archive[offset : offset + ARCHIVE_CHUNK_BYTES]

            response = _LimitedStreamingResponse(
                chunks(),
                release=release,
                media_type="application/zip",
                headers={
                    "Cache-Control": CACHE_CONTROL_VALUE,
                    "Content-Disposition": 'attachment; filename="holm-diagnostics.zip"',
                    "Content-Length": str(len(archive)),
                    "x-request-id": str(current_request_id(request)),
                },
            )
            transferred = True
            return response
        except PreviewUnavailable:
            return _preview_error(request, busy=False)
        finally:
            if not transferred:
                release()

    return router


__all__ = [
    "DIAGNOSTICS_PATH",
    "DIAGNOSTICS_PREVIEW_PATH",
    "DiagnosticBundleRequest",
    "create_diagnostics_router",
]


def _owner(request: Request) -> tuple[UUID, UUID]:
    authenticated = current_authenticated_session(request)
    return authenticated.user.id, authenticated.session.id


def _preview_error(request: Request, *, busy: bool) -> JSONResponse:
    return _error_response(
        request,
        status_code=503 if busy else 409,
        code="diagnostics_busy" if busy else "diagnostic_preview_expired",
        message=(
            "诊断生成容量已满，本次操作未执行，请稍后重新预览"
            if busy
            else "诊断预览已失效，本次操作未执行，请重新预览后确认"
        ),
    )


class _LimitedStreamingResponse(StreamingResponse):
    def __init__(
        self,
        content: AsyncIterator[bytes],
        *,
        release: Callable[[], None],
        media_type: str,
        headers: dict[str, str],
    ) -> None:
        super().__init__(content, media_type=media_type, headers=headers)
        self._release = release

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._release()

"""Authenticated ``GET /api/v1/events`` SSE endpoint."""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Header, Request
from starlette.responses import StreamingResponse

from holm_control_api.authorization import authenticated_route, current_subject
from holm_control_api.event_stream import EventStreamService
from holm_control_api.http_constants import CACHE_CONTROL_VALUE

EVENTS_PATH: Final[str] = "/api/v1/events"


def create_events_router(service: EventStreamService) -> APIRouter:
    router = APIRouter()

    @router.get(EVENTS_PATH)
    @authenticated_route
    async def events(
        request: Request,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        return StreamingResponse(
            service.stream(current_subject(request), last_event_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": CACHE_CONTROL_VALUE,
                "X-Accel-Buffering": "no",
            },
        )

    return router


__all__ = ["EVENTS_PATH", "create_events_router"]

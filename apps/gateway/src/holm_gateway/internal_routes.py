"""Unauthenticated internal probes; readiness only reads process-local state."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter
from holm_contracts import VersionPayload
from holm_domain import LedgerHealth
from starlette.responses import JSONResponse

from holm_gateway.route_snapshot import RouteSnapshotUnavailable


def create_internal_router(
    *,
    snapshot_available: Callable[[], None] | None,
    ledger_health: Callable[[], LedgerHealth] | None,
    version: VersionPayload | None,
) -> APIRouter:
    router = APIRouter(include_in_schema=False)

    @router.get("/internal/health/live")
    async def live() -> dict[str, str]:
        return {"status": "live"}

    @router.get("/internal/health/ready")
    async def ready() -> JSONResponse:
        try:
            if snapshot_available is None:
                raise RouteSnapshotUnavailable()
            snapshot_available()
        except RouteSnapshotUnavailable:
            return JSONResponse({"reason": "route_snapshot_unavailable"}, status_code=503)
        if ledger_health is None or ledger_health() is LedgerHealth.UNAVAILABLE:
            return JSONResponse({"reason": "ledger_unavailable"}, status_code=503)
        return JSONResponse({"status": "ready"})

    @router.get("/internal/version")
    async def read_version() -> JSONResponse:
        # Legacy in-process assemblers may omit metadata; never invent a version.
        if version is None:
            return JSONResponse({"reason": "version_unavailable"}, status_code=503)
        return JSONResponse(version.model_dump(mode="json"))

    return router

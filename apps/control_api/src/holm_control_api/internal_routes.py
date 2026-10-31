"""The three internal operability endpoints.

These are ``/internal/`` endpoints, not public API. They carry no
authentication and they must
therefore disclose nothing an unauthenticated caller could use: no instance
name, address, path, Secret, dependency detail or exception text.
"""

from __future__ import annotations

from typing import Final, Literal

from fastapi import APIRouter
from holm_contracts import VersionPayload
from pydantic import BaseModel, ConfigDict
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.status import HTTP_200_OK, HTTP_503_SERVICE_UNAVAILABLE

from holm_control_api.authorization import public_route
from holm_control_api.readiness import DependencyStatus, ReadinessReason
from holm_control_api.state import application_state

LIVE_PATH: Final[str] = "/internal/health/live"
READY_PATH: Final[str] = "/internal/health/ready"
VERSION_PATH: Final[str] = "/internal/version"


class _ClosedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LivenessResponse(_ClosedResponse):
    """Liveness says one thing only: this process ran this coroutine."""

    status: Literal["live"]


class DependencyResponse(_ClosedResponse):
    """One readiness dependency, named and reasoned with stable values."""

    name: str
    status: DependencyStatus
    reason: ReadinessReason


class ReadinessResponse(_ClosedResponse):
    """Readiness plus the sanitized per-dependency detail behind it."""

    status: Literal["ready", "not_ready"]
    dependencies: tuple[DependencyResponse, ...]


def create_internal_router() -> APIRouter:
    """Build the internal operability router."""
    router = APIRouter(tags=["internal"])

    @router.get(LIVE_PATH)
    @public_route
    async def read_liveness() -> LivenessResponse:
        """Report Event Loop and process liveness.

        Liveness explicitly does *not* check external dependencies.
        This coroutine reads no application state and touches no database,
        because a liveness probe that fails when the database is down turns a
        dependency outage into a process kill loop, exactly when restarting
        helps least. Its correctness is that it has nothing to go wrong.
        """
        return LivenessResponse(status="live")

    @router.get(READY_PATH, response_model=None)
    @public_route
    async def read_readiness(request: Request) -> JSONResponse:
        """Report startup completion, Migration compatibility and dependencies."""
        state = application_state(request)
        report = await state.readiness.evaluate(startup_complete=state.startup_complete)
        payload = ReadinessResponse(
            status="ready" if report.ready else "not_ready",
            dependencies=tuple(
                DependencyResponse(
                    name=dependency.name,
                    status=dependency.status,
                    reason=dependency.reason,
                )
                for dependency in report.dependencies
            ),
        )
        return JSONResponse(
            status_code=HTTP_200_OK if report.ready else HTTP_503_SERVICE_UNAVAILABLE,
            content=payload.model_dump(mode="json"),
        )

    @router.get(VERSION_PATH)
    @public_route
    async def read_version(request: Request) -> VersionPayload:
        """Serve release metadata; this route only wires it to HTTP."""
        return application_state(request).version_payload

    return router


__all__ = [
    "LIVE_PATH",
    "READY_PATH",
    "VERSION_PATH",
    "DependencyResponse",
    "LivenessResponse",
    "ReadinessResponse",
    "create_internal_router",
]

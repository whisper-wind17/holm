"""The application state that the internal routes read.

Kept in its own module so the routes depend on a typed object instead of the
untyped ``request.app.state`` attribute bag.
"""

from __future__ import annotations

from typing import Final

from holm_contracts import VersionPayload
from starlette.requests import Request

from holm_control_api.readiness import ReadinessEvaluator

STATE_ATTRIBUTE: Final[str] = "holm_control_api"


class ControlApiState:
    """Process-local state: the frozen version payload and readiness wiring."""

    __slots__ = ("_startup_complete", "readiness", "version_payload")

    def __init__(
        self,
        *,
        version_payload: VersionPayload,
        readiness: ReadinessEvaluator,
    ) -> None:
        self.version_payload = version_payload
        self.readiness = readiness
        self._startup_complete = False

    @property
    def startup_complete(self) -> bool:
        return self._startup_complete

    def mark_startup_complete(self) -> None:
        self._startup_complete = True

    def mark_stopped(self) -> None:
        """Readiness must fail again while the process drains and shuts down."""
        self._startup_complete = False


def application_state(request: Request) -> ControlApiState:
    """Return the state installed by ``create_app`` for this application."""
    state = getattr(request.app.state, STATE_ATTRIBUTE, None)
    if not isinstance(state, ControlApiState):
        raise RuntimeError("control API state is missing; the app was not built by create_app")
    return state

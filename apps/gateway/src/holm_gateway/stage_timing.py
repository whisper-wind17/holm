"""Request-local Gateway stage timing with no body or identifier retention."""

from __future__ import annotations

import time
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class StageTimingSnapshot:
    """The fixed seven-slot timing payload exposed to an opt-in in-memory sink."""

    request_parse_ms: float | None
    auth_route_ms: float | None
    ledger_commit_ms: float | None
    runtime_pool_wait_ms: float | None
    runtime_connect_ms: float | None
    upstream_first_event_ms: float | None
    downstream_flush_ms: float | None


@dataclass(slots=True)
class GatewayStageTimings:
    """Mutable monotonic timings owned by exactly one request.

    The structure deliberately has seven explicit result slots instead of a
    request-labelled mapping.  Normal production assembly installs no sink, so
    snapshots are created only by an explicitly instrumented benchmark.
    """

    request_parse_ms: float | None = None
    auth_route_ms: float | None = None
    ledger_commit_ms: float | None = None
    runtime_pool_wait_ms: float | None = None
    runtime_connect_ms: float | None = None
    upstream_first_event_ms: float | None = None
    downstream_flush_ms: float | None = None
    _downstream_started_at: float | None = None

    @staticmethod
    def now() -> float:
        return time.monotonic()

    @staticmethod
    def _elapsed_ms(started_at: float) -> float:
        return max(0.0, (time.monotonic() - started_at) * 1000.0)

    def finish_request_parse(self, started_at: float) -> None:
        self.request_parse_ms = self._elapsed_ms(started_at)

    def finish_auth_route(self, started_at: float) -> None:
        self.auth_route_ms = self._elapsed_ms(started_at)

    def finish_ledger_commit(self, started_at: float) -> None:
        self.ledger_commit_ms = self._elapsed_ms(started_at)

    def finish_runtime_pool_wait(self, started_at: float) -> None:
        self.runtime_pool_wait_ms = self._elapsed_ms(started_at)

    def finish_runtime_connect(self, started_at: float) -> None:
        self.runtime_connect_ms = self._elapsed_ms(started_at)

    def finish_upstream_first_event(self, started_at: float) -> None:
        self.upstream_first_event_ms = self._elapsed_ms(started_at)

    def begin_downstream_flush(self) -> None:
        if self._downstream_started_at is None and self.downstream_flush_ms is None:
            self._downstream_started_at = time.monotonic()

    def finish_downstream_flush(self) -> None:
        started_at = self._downstream_started_at
        if started_at is not None and self.downstream_flush_ms is None:
            self.downstream_flush_ms = self._elapsed_ms(started_at)

    def snapshot(self) -> StageTimingSnapshot:
        return StageTimingSnapshot(
            request_parse_ms=self.request_parse_ms,
            auth_route_ms=self.auth_route_ms,
            ledger_commit_ms=self.ledger_commit_ms,
            runtime_pool_wait_ms=self.runtime_pool_wait_ms,
            runtime_connect_ms=self.runtime_connect_ms,
            upstream_first_event_ms=self.upstream_first_event_ms,
            downstream_flush_ms=self.downstream_flush_ms,
        )


_CURRENT_TIMINGS: Final[ContextVar[GatewayStageTimings | None]] = ContextVar(
    "holm_gateway_stage_timings", default=None
)


def bind_stage_timings(timings: GatewayStageTimings) -> Token[GatewayStageTimings | None]:
    return _CURRENT_TIMINGS.set(timings)


def reset_stage_timings(token: Token[GatewayStageTimings | None]) -> None:
    _CURRENT_TIMINGS.reset(token)


def current_stage_timings() -> GatewayStageTimings | None:
    return _CURRENT_TIMINGS.get()


__all__ = [
    "GatewayStageTimings",
    "StageTimingSnapshot",
    "bind_stage_timings",
    "current_stage_timings",
    "reset_stage_timings",
]

"""Readiness evaluation behind ``/internal/health/ready``.

The design splits the two health endpoints on purpose: liveness reports the
process and Event Loop only, while readiness reports "startup finished,
Migration compatible, critical dependencies reachable". This module owns the
readiness half; nothing here is imported by the liveness route.

The database probe is expressed as a port (`SchemaRevisionReader`) rather than
a SQLAlchemy engine, because ``packages/persistence`` is still a uv *virtual*
workspace member and is not part of the release artifact set.
The production composition binds the port when persistence is
available. The port
keeps the real check real -- it must open a connection and read the applied
Alembic revision -- while leaving the driver binding to the composition root.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

DEFAULT_READINESS_TIMEOUT_SECONDS: Final[float] = 5.0
DATABASE_DEPENDENCY_NAME: Final[str] = "database"


class DependencyStatus(StrEnum):
    """Outcome of one readiness dependency check."""

    PASS = "pass"
    FAIL = "fail"


class ReadinessReason(StrEnum):
    """Stable, sanitized reasons. No address, path or driver text is exposed."""

    OK = "ok"
    STARTUP_INCOMPLETE = "startup_incomplete"
    DEPENDENCY_UNREACHABLE = "dependency_unreachable"
    DEPENDENCY_TIMEOUT = "dependency_timeout"
    SCHEMA_REVISION_MISSING = "schema_revision_missing"
    SCHEMA_REVISION_INCOMPATIBLE = "schema_revision_incompatible"


type DependencyProbe = Callable[[], Awaitable[ReadinessReason]]
type SchemaRevisionReader = Callable[[], Awaitable[str | None]]


@dataclass(frozen=True, slots=True)
class DependencyCheck:
    """One named dependency and the awaitable that decides its reason."""

    name: str
    probe: DependencyProbe

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("dependency check name must not be empty")


@dataclass(frozen=True, slots=True)
class DependencyOutcome:
    """The sanitized result of one dependency check."""

    name: str
    status: DependencyStatus
    reason: ReadinessReason


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    """Aggregate readiness with per-dependency detail."""

    ready: bool
    dependencies: tuple[DependencyOutcome, ...]


def database_schema_check(
    read_applied_schema_revision: SchemaRevisionReader,
    *,
    compatible_revisions: Iterable[str],
    name: str = DATABASE_DEPENDENCY_NAME,
) -> DependencyCheck:
    """Build the check for "database reachable and Migration version compatible".

    Reachability is not asserted separately: the reader has to reach the
    database to answer at all, so a connection failure surfaces as an exception
    and the evaluator turns it into ``dependency_unreachable``.
    """
    compatible = frozenset(revision for revision in compatible_revisions if revision.strip())
    if not compatible:
        # An empty set would accept every revision, which is the "always ready"
        # readiness endpoint this check exists to prevent.
        raise ValueError("at least one compatible Migration revision is required")

    async def probe() -> ReadinessReason:
        applied = await read_applied_schema_revision()
        if applied is None:
            return ReadinessReason.SCHEMA_REVISION_MISSING
        if applied not in compatible:
            return ReadinessReason.SCHEMA_REVISION_INCOMPATIBLE
        return ReadinessReason.OK

    return DependencyCheck(name=name, probe=probe)


class ReadinessEvaluator:
    """Run every dependency check concurrently under one explicit timeout."""

    __slots__ = ("_checks", "_timeout_seconds")

    def __init__(
        self,
        checks: Sequence[DependencyCheck],
        *,
        timeout_seconds: float = DEFAULT_READINESS_TIMEOUT_SECONDS,
    ) -> None:
        if not checks:
            # A readiness endpoint with no dependency check is a constant 200.
            raise ValueError("readiness requires at least one dependency check")
        names = [check.name for check in checks]
        if len(names) != len(set(names)):
            raise ValueError("dependency check names must be unique")
        if timeout_seconds <= 0:
            raise ValueError("readiness timeout must be greater than zero")

        self._checks = tuple(checks)
        self._timeout_seconds = timeout_seconds

    @property
    def dependency_names(self) -> tuple[str, ...]:
        return tuple(check.name for check in self._checks)

    async def evaluate(self, *, startup_complete: bool) -> ReadinessReport:
        """Report readiness without ever raising for a failing dependency."""
        if not startup_complete:
            # Startup is a local precondition; probing dependencies before the
            # application finished starting would report on a process that
            # cannot serve traffic anyway.
            return ReadinessReport(
                ready=False,
                dependencies=tuple(
                    DependencyOutcome(
                        name=check.name,
                        status=DependencyStatus.FAIL,
                        reason=ReadinessReason.STARTUP_INCOMPLETE,
                    )
                    for check in self._checks
                ),
            )

        reasons = await asyncio.gather(*(self._run(check) for check in self._checks))
        outcomes = tuple(
            DependencyOutcome(
                name=check.name,
                status=(
                    DependencyStatus.PASS if reason is ReadinessReason.OK else DependencyStatus.FAIL
                ),
                reason=reason,
            )
            for check, reason in zip(self._checks, reasons, strict=True)
        )
        ready = all(outcome.status is DependencyStatus.PASS for outcome in outcomes)
        return ReadinessReport(ready=ready, dependencies=outcomes)

    async def _run(self, check: DependencyCheck) -> ReadinessReason:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                return await check.probe()
        except TimeoutError:
            return ReadinessReason.DEPENDENCY_TIMEOUT
        except Exception:
            # Deliberate broad catch, not a swallowed exception: an unreachable
            # dependency is the normal, expected answer of a readiness probe and
            # must become a 503 body, not a 500 with a driver message in it. The
            # driver's exception text is dropped precisely because it can carry
            # host names, sockets and credentials. asyncio.CancelledError derives
            # from BaseException and still propagates.
            return ReadinessReason.DEPENDENCY_UNREACHABLE

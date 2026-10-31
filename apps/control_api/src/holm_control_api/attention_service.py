"""Body-free administrator attention projections."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

import sqlalchemy as sa
from holm_domain import (
    AiServiceRuntimeStatus,
    DownloadTaskStatus,
    ModelInstanceStatus,
    derive_ai_service_runtime_status,
    download_error_stage,
)
from holm_persistence import (
    AI_SERVICE_BACKENDS,
    AI_SERVICES,
    MODEL_INSTANCES,
    AsyncSessionFactory,
    database_utc_now,
    summarize_unknown_request_records,
)
from holm_persistence.model_domain import DOWNLOAD_TASKS, MODELS
from holm_persistence.model_instance_events import MODEL_INSTANCE_EVENTS
from holm_telemetry import OverviewSnapshot, TelemetryStatus

from holm_control_api.system_status import (
    AgentAvailableResponse,
    GpuHealthState,
    SystemServerResponse,
)

UNKNOWN_REQUEST_WINDOW: Final = timedelta(hours=24)
_LOGGER = logging.getLogger(__name__)


class AttentionKind(StrEnum):
    SERVICE_UNAVAILABLE = "service_unavailable"
    INSTANCE_FAILED = "instance_failed"
    INSTANCE_UNHEALTHY = "instance_unhealthy"
    GPU_UNHEALTHY = "gpu_unhealthy"
    DOWNLOAD_FAILED = "download_failed"
    TELEMETRY_INTERRUPTED = "telemetry_interrupted"
    UNKNOWN_REQUESTS = "unknown_requests"


class AttentionSeverity(StrEnum):
    NEGATIVE = "negative"
    NOTICE = "notice"


@dataclass(frozen=True, slots=True)
class AttentionItem:
    kind: AttentionKind
    count: int
    latest_at: datetime | None
    severity: AttentionSeverity = AttentionSeverity.NOTICE
    target_id: str | None = None
    target_name: str | None = None
    detail: str | None = None


class AttentionService:
    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory,
        system_server: Callable[[], Awaitable[SystemServerResponse]] | None = None,
        telemetry_overview: Callable[[], OverviewSnapshot] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._system_server = system_server
        self._telemetry_overview = telemetry_overview

    async def items(self) -> tuple[AttentionItem, ...]:
        # Independent sessions prevent one failed SQL transaction from poisoning
        # other projections. Logs intentionally exclude exception text and objects.
        sources = (
            ("services", self._services),
            ("instances", self._instances),
            ("gpus", self._gpus),
            ("downloads", self._downloads),
            ("telemetry", self._telemetry),
            ("unknown_requests", self._unknown_requests),
        )
        items: list[AttentionItem] = []
        for source, read in sources:
            try:
                items.extend(await read())
            except Exception:
                _LOGGER.warning("attention source unavailable", extra={"source": source})
        return tuple(
            sorted(
                items,
                key=lambda item: (
                    item.severity is not AttentionSeverity.NEGATIVE,
                    item.latest_at is None,
                    -item.latest_at.timestamp() if item.latest_at is not None else 0,
                ),
            )
        )

    async def _unknown_requests(self) -> tuple[AttentionItem, ...]:
        async with self._session_factory() as session:
            now = await database_utc_now(session)
            unknown = await summarize_unknown_request_records(
                session, since=now - UNKNOWN_REQUEST_WINDOW
            )
        if unknown.count == 0:
            return ()
        return (
            AttentionItem(
                kind=AttentionKind.UNKNOWN_REQUESTS,
                count=unknown.count,
                latest_at=unknown.latest_received_at,
            ),
        )

    async def _instances(self) -> tuple[AttentionItem, ...]:
        instances = MODEL_INSTANCES.c
        events = MODEL_INSTANCE_EVENTS.c
        entered_at = (
            sa.select(sa.func.max(events.occurred_at))
            .where(
                events.instance_id == instances.id,
                events.to_status == instances.status,
                events.from_status.is_distinct_from(events.to_status),
            )
            .correlate(MODEL_INSTANCES)
            .scalar_subquery()
        )
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    sa.select(instances.id, instances.name, instances.status, entered_at).where(
                        instances.status.in_(
                            [ModelInstanceStatus.FAILED.value, ModelInstanceStatus.UNHEALTHY.value]
                        )
                    )
                )
            ).all()
        # Instances are physically deleted; historical events alone never emit items.
        return tuple(
            AttentionItem(
                kind=(
                    AttentionKind.INSTANCE_FAILED
                    if status == ModelInstanceStatus.FAILED.value
                    else AttentionKind.INSTANCE_UNHEALTHY
                ),
                severity=AttentionSeverity.NEGATIVE,
                target_id=str(identity),
                target_name=name,
                count=1,
                latest_at=entered_at,
            )
            for identity, name, status, entered_at in rows
        )

    async def _services(self) -> tuple[AttentionItem, ...]:
        services, backends, instances = AI_SERVICES.c, AI_SERVICE_BACKENDS.c, MODEL_INSTANCES.c
        # An outer join is essential: the ordinary service list omits missing primary backends.
        source = AI_SERVICES.outerjoin(
            AI_SERVICE_BACKENDS,
            sa.and_(
                backends.service_id == services.id,
                backends.backend_key == "primary",
                backends.retired_at.is_(None),
            ),
        ).outerjoin(MODEL_INSTANCES, instances.id == backends.instance_id)
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    sa.select(services.id, services.name, instances.status)
                    .select_from(source)
                    .where(services.deleted_at.is_(None), services.enabled.is_(True))
                )
            ).all()
        return tuple(
            AttentionItem(
                kind=AttentionKind.SERVICE_UNAVAILABLE,
                severity=AttentionSeverity.NEGATIVE,
                target_id=str(identity),
                target_name=name,
                count=1,
                # No persisted service runtime-state transition timestamp exists.
                latest_at=None,
                detail=status if status is not None else "no_backend",
            )
            for identity, name, status in rows
            if derive_ai_service_runtime_status(
                enabled=True,
                primary_instance_status=None if status is None else ModelInstanceStatus(status),
            )
            is AiServiceRuntimeStatus.UNAVAILABLE
        )

    async def _downloads(self) -> tuple[AttentionItem, ...]:
        tasks = DOWNLOAD_TASKS.c
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    sa.select(tasks.id, MODELS.c.display_name, tasks.updated_at, tasks.error_code)
                    .join(MODELS, MODELS.c.id == tasks.model_id)
                    .where(tasks.status == DownloadTaskStatus.FAILED.value)
                )
            ).all()
        return tuple(
            AttentionItem(
                kind=AttentionKind.DOWNLOAD_FAILED,
                target_id=str(identity),
                target_name=name,
                count=1,
                latest_at=updated_at,
                detail=None if (stage := download_error_stage(code)) is None else stage.value,
            )
            for identity, name, updated_at, code in rows
        )

    async def _gpus(self) -> tuple[AttentionItem, ...]:
        if self._system_server is None:
            return ()
        server = await self._system_server()
        if not isinstance(server.agent, AgentAvailableResponse):
            return ()
        return tuple(
            AttentionItem(
                kind=AttentionKind.GPU_UNHEALTHY,
                severity=AttentionSeverity.NEGATIVE,
                target_id=device.uuid,
                target_name=(
                    None if device.display_index is None else f"GPU {device.display_index}"
                ),
                count=1,
                latest_at=None,
            )
            for device in server.agent.gpus.devices
            if device.health is GpuHealthState.UNHEALTHY
        )

    async def _telemetry(self) -> tuple[AttentionItem, ...]:
        if self._telemetry_overview is None:
            return ()
        snapshot = self._telemetry_overview()
        if snapshot.telemetry_status is not TelemetryStatus.UNAVAILABLE:
            return ()
        return (
            AttentionItem(
                kind=AttentionKind.TELEMETRY_INTERRUPTED,
                count=1,
                latest_at=snapshot.sampled_at,
            ),
        )


__all__ = [
    "UNKNOWN_REQUEST_WINDOW",
    "AttentionItem",
    "AttentionKind",
    "AttentionService",
    "AttentionSeverity",
]

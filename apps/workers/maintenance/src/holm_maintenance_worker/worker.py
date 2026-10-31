"""Serial retention work with independent, bounded database heartbeats."""

from __future__ import annotations

import asyncio
import logging
import math
import signal
import threading
from contextlib import suppress
from pathlib import Path
from typing import Final

from holm_domain import Component, ComponentStatus
from holm_persistence import AsyncSessionFactory, ComponentHeartbeatRepository
from holm_persistence.artifact_check_repository import (
    ArtifactCheckRepository,
    ArtifactCheckSchedule,
)
from holm_persistence.backup_format import BackupError
from holm_persistence.backup_retention import BackupRetentionResult, retain_backups

_LOGGER: Final = logging.getLogger("holm.maintenance_worker")
DEFAULT_RETENTION_SECONDS: Final = 3600.0
DEFAULT_ARTIFACT_CHECK_SECONDS: Final = 60.0
DATABASE_TIMEOUT_SECONDS: Final = 5.0
SHUTDOWN_TIMEOUT_SECONDS: Final = 5.0


class MaintenanceWorker:
    """Healthy means retention and check coordination; not model health or backup RPO."""

    def __init__(
        self,
        *,
        backup_root: Path,
        session_factory: AsyncSessionFactory,
        instance_id: str,
        build_version: str,
        retention_seconds: float = DEFAULT_RETENTION_SECONDS,
        heartbeat_seconds: float = 10.0,
        artifact_check_seconds: float = DEFAULT_ARTIFACT_CHECK_SECONDS,
    ) -> None:
        if not math.isfinite(artifact_check_seconds) or artifact_check_seconds <= 0:
            raise ValueError("invalid artifact check period")
        if not math.isfinite(retention_seconds) or retention_seconds <= 0:
            raise ValueError("invalid retention period")
        if not math.isfinite(heartbeat_seconds) or not 0 < heartbeat_seconds <= 10:
            raise ValueError("invalid heartbeat period")
        if any(
            not value or value != value.strip() or len(value) > 128
            for value in (instance_id, build_version)
        ):
            raise ValueError("invalid heartbeat identity")
        self._backup_root = backup_root
        self._session_factory = session_factory
        self._instance_id = instance_id
        self._build_version = build_version
        self._retention_seconds = retention_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._artifact_check_seconds = artifact_check_seconds
        self._retention_reason: str | None = "backup_retention_pending"
        self._artifact_reason: str | None = "artifact_check_schedule_pending"
        self._stopping = threading.Event()

    @property
    def _reason(self) -> str | None:
        # An independently successful task must never clear another task's failure.
        return self._retention_reason or self._artifact_reason

    @property
    def _status(self) -> ComponentStatus:
        return ComponentStatus.DEGRADED if self._reason else ComponentStatus.HEALTHY

    async def schedule_artifact_checks(self) -> ArtifactCheckSchedule:
        async with asyncio.timeout(DATABASE_TIMEOUT_SECONDS):
            async with self._session_factory() as session, session.begin():
                return await ArtifactCheckRepository(session).schedule_due()

    async def _artifact_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                result = await self.schedule_artifact_checks()
            except Exception:
                self._artifact_reason = "artifact_check_schedule_failed"
                _LOGGER.warning("artifact check scheduling failed")
            else:
                self._artifact_reason = "artifact_check_failed" if result.failed else None
                _LOGGER.info("artifact check scheduling complete: scheduled=%d", result.scheduled)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._artifact_check_seconds)
            except TimeoutError:
                continue

    async def _heartbeat(self, status: ComponentStatus, reason: str | None) -> None:
        async with asyncio.timeout(DATABASE_TIMEOUT_SECONDS):
            async with self._session_factory() as session, session.begin():
                await ComponentHeartbeatRepository(session).write(
                    component=Component.MAINTENANCE_WORKER,
                    instance_id=self._instance_id,
                    version=self._build_version,
                    status=status,
                    reason=reason,
                )

    async def _check(self) -> BackupRetentionResult:
        loop = asyncio.get_running_loop()
        result: asyncio.Future[BackupRetentionResult] = loop.create_future()

        def finish(value: BackupRetentionResult | BackupError) -> None:
            if result.done():
                return
            if isinstance(value, BackupError):
                result.set_exception(value)
            else:
                result.set_result(value)

        def perform() -> None:
            try:
                value: BackupRetentionResult | BackupError = retain_backups(
                    self._backup_root, stopping=self._stopping
                )
            except BackupError as error:
                value = error
            except Exception:
                value = BackupError("backup_retention_failed")
            # No queue: the next pass begins only after this one completes.
            # A daemon thread plus cooperative chunk checks keeps shutdown
            # bounded even if a filesystem call itself stops responding.
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(finish, value)

        threading.Thread(target=perform, name="backup-retention", daemon=True).start()
        return await result

    async def _maintenance_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                result = await self._check()
            except BackupError as error:
                self._retention_reason = error.code
                _LOGGER.warning("backup retention failed: %s", error.code)
            else:
                self._retention_reason = None
                _LOGGER.info(
                    "backup retention complete: validated=%d retained=%d deleted=%d",
                    result.validated,
                    result.retained,
                    result.deleted,
                )
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._retention_seconds)
            except TimeoutError:
                continue

    async def run(self, stop: asyncio.Event) -> None:
        task = asyncio.gather(self._maintenance_loop(stop), self._artifact_loop(stop))
        try:
            while not stop.is_set():
                heartbeat_started = asyncio.get_running_loop().time()
                try:
                    await self._heartbeat(self._status, self._reason)
                except Exception:
                    # A DB outage cannot produce a fresh Healthy heartbeat.
                    # Reconnection happens at the next bounded heartbeat pass.
                    _LOGGER.warning("maintenance heartbeat failed: database_unavailable")
                try:
                    elapsed = asyncio.get_running_loop().time() - heartbeat_started
                    await asyncio.wait_for(
                        stop.wait(), timeout=max(0.05, self._heartbeat_seconds - elapsed)
                    )
                except TimeoutError:
                    continue
        finally:
            self._stopping.set()
            try:
                await asyncio.wait_for(asyncio.shield(task), SHUTDOWN_TIMEOUT_SECONDS)
            except TimeoutError:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            try:
                await self._heartbeat(ComponentStatus.STOPPING, "maintenance_stopping")
            except Exception:
                _LOGGER.warning("maintenance stopping heartbeat failed: database_unavailable")


async def run_until_signalled(worker: MaintenanceWorker) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    try:
        await worker.run(stop)
    finally:
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)

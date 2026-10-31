"""Independent database heartbeats for the instance controller."""

from __future__ import annotations

import asyncio
import logging
import math
from contextlib import suppress

from holm_domain import Component, ComponentStatus
from holm_persistence import AsyncSessionFactory, ComponentHeartbeatRepository

_LOGGER = logging.getLogger("holm.instance_controller")


class ControllerHeartbeat:
    def __init__(
        self,
        session_factory: AsyncSessionFactory,
        *,
        instance_id: str,
        build_version: str,
        interval_seconds: float = 10.0,
    ) -> None:
        if not math.isfinite(interval_seconds) or not 0 < interval_seconds <= 10:
            raise ValueError("invalid heartbeat period")
        if any(
            not value or value != value.strip() or len(value) > 128
            for value in (instance_id, build_version)
        ):
            raise ValueError("invalid heartbeat identity")
        self._session_factory = session_factory
        self._instance_id = instance_id
        self._build_version = build_version
        self._interval_seconds = interval_seconds

    async def write(self, status: ComponentStatus) -> None:
        try:
            async with asyncio.timeout(5.0):
                async with self._session_factory() as session, session.begin():
                    await ComponentHeartbeatRepository(session).write(
                        component=Component.INSTANCE_CONTROLLER,
                        instance_id=self._instance_id,
                        version=self._build_version,
                        status=status,
                        reason=(
                            "instance_controller_stopping"
                            if status is ComponentStatus.STOPPING
                            else None
                        ),
                    )
        except Exception:
            _LOGGER.warning(
                "instance controller heartbeat failed",
                extra={"error_code": "heartbeat_failed"},
            )

    async def run(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                started = asyncio.get_running_loop().time()
                await self.write(ComponentStatus.HEALTHY)
                elapsed = asyncio.get_running_loop().time() - started
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        stop.wait(), timeout=max(0.05, self._interval_seconds - elapsed)
                    )
        finally:
            await self.write(ComponentStatus.STOPPING)

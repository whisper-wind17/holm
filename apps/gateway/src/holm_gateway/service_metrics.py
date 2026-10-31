"""Bounded in-memory service counters and an isolated periodic PostgreSQL writer."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final
from uuid import UUID, uuid4

from holm_domain import ErrorCode, RequestRecordStatus
from holm_persistence import (
    MAX_GATEWAY_SERVICE_SAMPLES,
    SERVICE_METRIC_BUCKET_BOUNDS,
    AsyncSessionFactory,
    GatewayServiceMetricSample,
    GatewayServiceMetricSampleRepository,
)

_LOGGER: Final = logging.getLogger("holm.gateway.service_metrics")
WRITE_INTERVAL_SECONDS: Final = 5.0
WRITE_TIMEOUT_SECONDS: Final = 2.0


@dataclass(slots=True)
class _Counters:
    ttft_buckets: list[int] = field(default_factory=lambda: [0] * len(SERVICE_METRIC_BUCKET_BOUNDS))
    tpot_buckets: list[int] = field(default_factory=lambda: [0] * len(SERVICE_METRIC_BUCKET_BOUNDS))
    e2e_buckets: list[int] = field(default_factory=lambda: [0] * len(SERVICE_METRIC_BUCKET_BOUNDS))
    output_tokens_total: int = 0
    accepted_requests: int = 0
    rejected_requests: int = 0
    client_disconnects: int = 0
    gateway_errors: int = 0
    runtime_errors: int = 0
    timeout_errors: int = 0
    inflight: int = 0


def _observe_bucket(buckets: list[int], seconds: float) -> None:
    for index, bound in enumerate(SERVICE_METRIC_BUCKET_BOUNDS):
        if seconds <= bound:
            buckets[index] += 1


class GatewayServiceMetrics:
    """Single event-loop updates only; an upper bound prevents metric cardinality growth."""

    def __init__(self) -> None:
        self.gateway_start_id = uuid4()
        self._series: dict[UUID, _Counters] = {}
        self.dropped_series_total = 0

    def _get(self, service_id: UUID) -> _Counters | None:
        current = self._series.get(service_id)
        if current is not None:
            return current
        if len(self._series) >= MAX_GATEWAY_SERVICE_SAMPLES:
            self.dropped_series_total += 1
            return None
        current = _Counters()
        self._series[service_id] = current
        return current

    def forwarded(self, service_id: UUID) -> None:
        counters = self._get(service_id)
        if counters is not None:
            counters.inflight += 1

    def finished(
        self,
        service_id: UUID,
        *,
        forwarded: bool,
        status: RequestRecordStatus | None,
        http_status: int,
        error_code: ErrorCode | None,
        duration_seconds: float,
        first_token_seconds: float | None,
        last_token_seconds: float | None = None,
        output_tokens: int | None,
    ) -> None:
        counters = self._get(service_id)
        if counters is None:
            return
        if forwarded:
            counters.inflight = max(0, counters.inflight - 1)
        _observe_bucket(counters.e2e_buckets, duration_seconds)
        if first_token_seconds is not None:
            _observe_bucket(counters.ttft_buckets, first_token_seconds)
        if output_tokens is not None and output_tokens > 0:
            counters.output_tokens_total += output_tokens
            if (
                output_tokens > 1
                and first_token_seconds is not None
                and last_token_seconds is not None
            ):
                _observe_bucket(
                    counters.tpot_buckets,
                    max(0.0, last_token_seconds - first_token_seconds) / (output_tokens - 1),
                )
        if status is RequestRecordStatus.ABORTED or http_status == 499:
            counters.client_disconnects += 1
            return
        if 400 <= http_status < 500:
            counters.rejected_requests += 1
            return
        counters.accepted_requests += 1
        if status is RequestRecordStatus.FAILED or http_status >= 500:
            if error_code is ErrorCode.INFERENCE_TIMEOUT:
                counters.timeout_errors += 1
            elif error_code is ErrorCode.BACKEND_UNAVAILABLE:
                counters.runtime_errors += 1
            else:
                counters.gateway_errors += 1

    def snapshot(self, *, sampled_at: datetime) -> tuple[GatewayServiceMetricSample, ...]:
        return tuple(
            GatewayServiceMetricSample(
                service_id=service_id,
                gateway_start_id=self.gateway_start_id,
                sampled_at=sampled_at,
                ttft_buckets=tuple(counters.ttft_buckets),
                tpot_buckets=tuple(counters.tpot_buckets),
                e2e_buckets=tuple(counters.e2e_buckets),
                output_tokens_total=counters.output_tokens_total,
                accepted_requests=counters.accepted_requests,
                rejected_requests=counters.rejected_requests,
                client_disconnects=counters.client_disconnects,
                gateway_errors=counters.gateway_errors,
                runtime_errors=counters.runtime_errors,
                timeout_errors=counters.timeout_errors,
                inflight=counters.inflight,
            )
            for service_id, counters in self._series.items()
        )


class ServiceMetricWriter:
    def __init__(
        self, metrics: GatewayServiceMetrics, session_factory: AsyncSessionFactory
    ) -> None:
        self._metrics = metrics
        self._session_factory = session_factory
        self._task: asyncio.Task[None] | None = None
        self.write_failures_total = 0

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="gateway-service-metric-writer")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self._task = None

    async def flush_once(self) -> None:
        samples = self._metrics.snapshot(sampled_at=datetime.now(UTC))
        if not samples:
            return
        async with asyncio.timeout(WRITE_TIMEOUT_SECONDS):
            async with self._session_factory() as session, session.begin():
                await GatewayServiceMetricSampleRepository(session).upsert(samples)

    async def _run(self) -> None:
        while True:
            started = time.monotonic()
            try:
                await self.flush_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.write_failures_total += 1
                _LOGGER.warning(
                    "gateway service metric sample write failed",
                    extra={"error_code": type(error).__name__[:128]},
                )
            interval = WRITE_INTERVAL_SECONDS + random.uniform(-0.5, 0.5)
            await asyncio.sleep(max(0.0, interval - (time.monotonic() - started)))

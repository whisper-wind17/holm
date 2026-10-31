"""Gateway assembly for the OpenAI Protocol Adapter and inference core."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from holm_contracts import VersionPayload
from holm_ledger import LedgerHealthMonitor, SpoolWriter
from holm_protocols import (
    AdmissionController,
    CircuitBreaker,
    InferenceCore,
    InferenceFailure,
    ModelReasoningPolicy,
)
from holm_runtimes import RuntimeRequestCapabilities
from starlette.types import ASGIApp

from holm_gateway.anthropic_adapter import AnthropicProtocolAdapter
from holm_gateway.authentication import GatewayAuthenticator, InferenceAuthenticator
from holm_gateway.configuration import GatewayConfiguration
from holm_gateway.internal_routes import create_internal_router
from holm_gateway.metering import MeteringService
from holm_gateway.openai_adapter import OpenAIProtocolAdapter
from holm_gateway.path_alias import AnthropicPathAliasMiddleware
from holm_gateway.request_context import (
    LedgerEventSink,
    RequestContextMiddleware,
    StageTimingSink,
)
from holm_gateway.route_snapshot import RouteSnapshotManager
from holm_gateway.routing import GatewayRouteDirectory, InferenceRouteDirectory
from holm_gateway.runtime_adapter_registry import (
    RuntimeAdapterRegistry,
    create_runtime_adapter_registry,
)
from holm_gateway.service_metrics import GatewayServiceMetrics, ServiceMetricWriter

# Existing callers import this name. The failure itself is now protocol-independent
# and carries no HTTP status or OpenAI response shape.
GatewayFailure = InferenceFailure


def create_app(
    *,
    authenticator: GatewayAuthenticator,
    routes: GatewayRouteDirectory,
    runtime_capabilities: Mapping[str, RuntimeRequestCapabilities],
    reasoning_models: Mapping[str, ModelReasoningPolicy] | None = None,
    configuration: GatewayConfiguration | None = None,
    upstream_client: httpx.AsyncClient | None = None,
    runtime_adapter: RuntimeAdapterRegistry | None = None,
    admission: AdmissionController | None = None,
    circuit_breaker: CircuitBreaker | None = None,
    request_ledger: LedgerEventSink | None = None,
    ledger_health: LedgerHealthMonitor | None = None,
    snapshot_available: Callable[[], None] | None = None,
    version_payload: VersionPayload | None = None,
    metering: MeteringService | None = None,
    stage_timing_sink: StageTimingSink | None = None,
    service_metrics: GatewayServiceMetrics | None = None,
    service_metric_writer: ServiceMetricWriter | None = None,
) -> ASGIApp:
    """Assemble both external protocols around one inference core."""

    if configuration is None:
        configuration = GatewayConfiguration()
    runtime = runtime_adapter or create_runtime_adapter_registry(
        upstream_client,
        max_response_bytes=configuration.max_response_body_bytes,
        max_sse_event_bytes=configuration.max_sse_event_bytes,
        pool_max_connections=configuration.resolved_max_inflight_per_instance,
        pool_timeout_seconds=configuration.runtime_pool_timeout_ms / 1000,
        connect_timeout_seconds=configuration.runtime_connect_timeout_seconds,
    )
    core = InferenceCore(
        reasoning_models=reasoning_models,
        authenticator=InferenceAuthenticator(authenticator),
        routes=InferenceRouteDirectory(routes, runtime.bind_route),
        runtime=runtime,
        admission=admission
        or AdmissionController(
            configuration.max_inflight_requests,
            configuration.resolved_max_inflight_per_instance,
        ),
        circuit_breaker=circuit_breaker
        or CircuitBreaker(
            failure_threshold=configuration.breaker_failure_threshold,
            failure_window_seconds=configuration.breaker_failure_window_seconds,
            cooldown_seconds=configuration.breaker_cooldown_seconds,
        ),
        first_token_timeout_seconds=configuration.first_token_timeout_seconds,
        total_timeout_seconds=configuration.total_timeout_seconds,
    )
    metering_service = metering or MeteringService(
        cache_max_bytes=configuration.tokenizer_cache_max_bytes,
        worker_threads=configuration.tokenizer_worker_threads,
        timeout_seconds=configuration.tokenizer_fallback_timeout_seconds,
        text_max_bytes=configuration.metering_text_max_bytes,
    )
    protocol = OpenAIProtocolAdapter(
        runtime_capabilities,
        maximum_json_depth=configuration.max_json_depth,
    )
    anthropic = AnthropicProtocolAdapter(
        maximum_json_depth=configuration.max_json_depth,
        maximum_response_bytes=configuration.max_response_body_bytes,
    )
    owns_runtime = runtime_adapter is None

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        request_ledger_started = False
        ledger_health_started = False
        service_metric_writer_started = False
        try:
            if service_metric_writer is not None:
                await service_metric_writer.start()
                service_metric_writer_started = True
            if isinstance(request_ledger, SpoolWriter):
                request_ledger.start()
                request_ledger_started = True
            if ledger_health is not None:
                # Started after the Spool Writer so the first sample observes a
                # Ledger that already exists.
                await ledger_health.start()
                ledger_health_started = True
            yield
        finally:
            if service_metric_writer_started and service_metric_writer is not None:
                await service_metric_writer.stop()
            if ledger_health_started and ledger_health is not None:
                await ledger_health.aclose()
            # Deferred terminal writes must finish before the Ledger closes:
            # a cold Tokenizer load may still owe a Completed/Aborted event.
            await metering_service.aclose()
            if request_ledger_started:
                assert isinstance(request_ledger, SpoolWriter)
                await request_ledger.close()
            if owns_runtime:
                await runtime.aclose()

    application = FastAPI(
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    application.include_router(
        create_internal_router(
            snapshot_available=snapshot_available,
            ledger_health=None if ledger_health is None else (lambda: ledger_health.health),
            version=version_payload,
        )
    )
    anthropic.install(
        application,
        core,
        max_request_body_bytes=configuration.max_request_body_bytes,
        current_limits=routes.call_limits if isinstance(routes, RouteSnapshotManager) else None,
    )
    protocol.install(
        application,
        core,
        anthropic=anthropic,
        max_request_body_bytes=configuration.max_request_body_bytes,
        current_limits=routes.call_limits if isinstance(routes, RouteSnapshotManager) else None,
    )

    request_context = RequestContextMiddleware(
        application,
        request_ledger,
        protocol.failure_response,
        protocol.failure_status,
        metering_service,
        ledger_health=None if ledger_health is None else (lambda: ledger_health.health),
        terminal_write_failure=(None if ledger_health is None else ledger_health.latch_unavailable),
        stage_timing_sink=stage_timing_sink,
        service_metrics=service_metrics,
        anthropic_failure_renderer=anthropic.failure_response,
    )
    return AnthropicPathAliasMiddleware(request_context)


__all__ = ["AdmissionController", "GatewayFailure", "create_app"]

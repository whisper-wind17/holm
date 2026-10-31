"""Gateway process entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from types import MappingProxyType

import uvicorn
from holm_common import SecretFileError, startup_failure_code
from holm_contracts import ReleaseManifest, VersionPayload, version_payload_from_manifest
from holm_domain import (
    LEDGER_DELAYED_AFTER_SECONDS,
    LEDGER_UNAVAILABLE_AFTER_SECONDS,
    ModelInstanceStatus,
)
from holm_ledger import (
    DEFAULT_LEDGER_HEALTH_INTERVAL_SECONDS,
    LedgerHealthMonitor,
    SpoolWriter,
)
from holm_persistence import (
    PsycopgNotificationListener,
    create_database_engine,
    create_session_factory,
)
from holm_protocols import CircuitBreaker, ModelReasoningPolicy, ReasoningEffort
from holm_runtimes import RuntimeCompatibilityCatalog, load_runtime_compatibility
from holm_security import (
    ApiKeyPepperKeyringError,
    ApiKeyPepperStartupError,
    load_api_key_pepper_keyring,
)

from holm_gateway.api_key_pepper_startup import check_api_key_pepper_references
from holm_gateway.application import create_app
from holm_gateway.authentication import ApiKeyAuthenticator
from holm_gateway.configuration import (
    GatewayConfiguration,
    GatewayConfigurationError,
    load_database_url,
)
from holm_gateway.metering import (
    DEFAULT_TOKENIZER_CACHE_BYTES,
    DEFAULT_TOKENIZER_TIMEOUT_SECONDS,
    DEFAULT_TOKENIZER_WORKER_THREADS,
)
from holm_gateway.route_snapshot import (
    PostgresRouteSnapshotSource,
    RouteSnapshot,
    RouteSnapshotManager,
    SnapshotApiKeyValidationLookup,
)
from holm_gateway.runtime_adapter_registry import create_runtime_adapter_registry
from holm_gateway.service_metrics import GatewayServiceMetrics, ServiceMetricWriter


def _per_instance_limit(value: str) -> int | str:
    if value == "auto":
        return value
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected 'auto' or a positive integer") from None
    if parsed < 1:
        raise argparse.ArgumentTypeError("expected 'auto' or a positive integer")
    return parsed


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url-file", required=True, type=Path)
    parser.add_argument("--api-key-pepper-keyring-file", required=True, type=Path)
    parser.add_argument("--runtime-compatibility", required=True, type=Path)
    parser.add_argument(
        "--release-manifest",
        type=Path,
        default=Path("/etc/holm/platform.yaml"),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--max-inflight-requests", type=int, default=64)
    parser.add_argument("--max-inflight-per-instance", type=_per_instance_limit, default="auto")
    parser.add_argument("--runtime-pool-timeout-ms", type=int, default=100)
    parser.add_argument("--runtime-connect-timeout-seconds", type=float, default=3.0)
    parser.add_argument("--first-token-timeout-seconds", type=float, default=600.0)
    parser.add_argument("--total-timeout-seconds", type=float, default=3600.0)
    parser.add_argument("--breaker-failure-threshold", type=int, default=3)
    parser.add_argument("--breaker-failure-window-seconds", type=float, default=10.0)
    parser.add_argument("--breaker-cooldown-seconds", type=float, default=5.0)
    parser.add_argument("--route-snapshot-poll-seconds", type=float, default=1.0)
    parser.add_argument("--route-snapshot-max-age-seconds", type=float, default=60.0)
    parser.add_argument("--max-request-body-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--max-json-depth", type=int, default=32)
    parser.add_argument("--max-response-body-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--request-record-queue-size", type=int, default=10_000)
    parser.add_argument(
        "--tokenizer-cache-max-mib",
        type=int,
        default=DEFAULT_TOKENIZER_CACHE_BYTES // (1024 * 1024),
    )
    parser.add_argument(
        "--tokenizer-worker-threads",
        type=int,
        default=DEFAULT_TOKENIZER_WORKER_THREADS,
    )
    parser.add_argument(
        "--tokenizer-fallback-timeout-seconds",
        type=float,
        default=DEFAULT_TOKENIZER_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--request-ledger-path",
        type=Path,
        default=Path("/var/lib/holm/spool/request-ledger.sqlite3"),
    )
    parser.add_argument(
        "--ledger-delayed-after-seconds",
        type=float,
        default=LEDGER_DELAYED_AFTER_SECONDS,
    )
    parser.add_argument(
        "--ledger-unavailable-after-seconds",
        type=float,
        default=LEDGER_UNAVAILABLE_AFTER_SECONDS,
    )
    parser.add_argument(
        "--ledger-health-interval-seconds",
        type=float,
        default=DEFAULT_LEDGER_HEALTH_INTERVAL_SECONDS,
    )
    return parser.parse_args(argv)


def _load_version(path: Path) -> VersionPayload:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        payload = document.get("release_manifest", document)
        return version_payload_from_manifest(ReleaseManifest.model_validate(payload))
    except (OSError, ValueError, AttributeError):
        raise GatewayConfigurationError("release_manifest_invalid") from None


def _reasoning_models(catalog: RuntimeCompatibilityCatalog) -> dict[str, ModelReasoningPolicy]:
    """Translate the dependency-free catalog into protocol-owned reasoning values."""
    return {
        architecture: ModelReasoningPolicy(
            efforts=tuple(ReasoningEffort(value) for value in policy.efforts),
            aliases=MappingProxyType(
                {
                    ReasoningEffort(key): ReasoningEffort(value)
                    for key, value in policy.aliases.items()
                }
            ),
            can_disable=policy.can_disable,
        )
        for architecture, policy in catalog.reasoning_models.items()
    }


async def _run(args: argparse.Namespace) -> None:
    version = _load_version(args.release_manifest)
    configuration = GatewayConfiguration(
        max_inflight_requests=args.max_inflight_requests,
        max_inflight_per_instance=args.max_inflight_per_instance,
        runtime_pool_timeout_ms=args.runtime_pool_timeout_ms,
        runtime_connect_timeout_seconds=args.runtime_connect_timeout_seconds,
        first_token_timeout_seconds=args.first_token_timeout_seconds,
        total_timeout_seconds=args.total_timeout_seconds,
        breaker_failure_threshold=args.breaker_failure_threshold,
        breaker_failure_window_seconds=args.breaker_failure_window_seconds,
        breaker_cooldown_seconds=args.breaker_cooldown_seconds,
        route_snapshot_poll_seconds=args.route_snapshot_poll_seconds,
        route_snapshot_max_age_seconds=args.route_snapshot_max_age_seconds,
        max_request_body_bytes=args.max_request_body_bytes,
        max_json_depth=args.max_json_depth,
        max_response_body_bytes=args.max_response_body_bytes,
        request_record_queue_size=args.request_record_queue_size,
        tokenizer_cache_max_bytes=args.tokenizer_cache_max_mib * 1024 * 1024,
        tokenizer_worker_threads=args.tokenizer_worker_threads,
        tokenizer_fallback_timeout_seconds=args.tokenizer_fallback_timeout_seconds,
        ledger_delayed_after_seconds=args.ledger_delayed_after_seconds,
        ledger_unavailable_after_seconds=args.ledger_unavailable_after_seconds,
        ledger_health_interval_seconds=args.ledger_health_interval_seconds,
    )
    keyring = load_api_key_pepper_keyring(args.api_key_pepper_keyring_file)
    runtime_catalog = load_runtime_compatibility(args.runtime_compatibility)
    database_url = load_database_url(args.database_url_file)
    engine = create_database_engine(database_url)
    snapshot_manager: RouteSnapshotManager | None = None
    circuit_breaker = CircuitBreaker(
        failure_threshold=configuration.breaker_failure_threshold,
        failure_window_seconds=configuration.breaker_failure_window_seconds,
        cooldown_seconds=configuration.breaker_cooldown_seconds,
    )
    runtime = create_runtime_adapter_registry(
        max_response_bytes=configuration.max_response_body_bytes,
        max_sse_event_bytes=configuration.max_sse_event_bytes,
        pool_max_connections=configuration.resolved_max_inflight_per_instance,
        pool_timeout_seconds=configuration.runtime_pool_timeout_ms / 1000,
        connect_timeout_seconds=configuration.runtime_connect_timeout_seconds,
    )
    try:
        factory = create_session_factory(engine)
        route_sync_lock = asyncio.Lock()
        last_synced_route_version = -1

        async def sync_routes(snapshot: RouteSnapshot) -> None:
            nonlocal last_synced_route_version
            async with route_sync_lock:
                if snapshot.version < last_synced_route_version:
                    return
                routes = snapshot.routes_by_model.values()
                await runtime.sync_routes(routes)
                await circuit_breaker.sync_instances(
                    route.instance_id
                    for route in routes
                    if route.enabled
                    and route.instance_id is not None
                    and route.instance_status == ModelInstanceStatus.RUNNING.value
                    and route.internal_endpoint is not None
                )
                last_synced_route_version = snapshot.version

        snapshot_manager = RouteSnapshotManager(
            PostgresRouteSnapshotSource(factory),
            listener=PsycopgNotificationListener(database_url),
            poll_seconds=configuration.route_snapshot_poll_seconds,
            max_age_seconds=configuration.route_snapshot_max_age_seconds,
            snapshot_observer=sync_routes,
        )
        # Uvicorn does not bind its socket until the first complete Snapshot exists.
        await snapshot_manager.start()
        await check_api_key_pepper_references(factory, keyring)
        await snapshot_manager.wait_for_snapshot_observers()
        spool_writer = SpoolWriter(args.request_ledger_path)
        service_metrics = GatewayServiceMetrics()
        app = create_app(
            authenticator=ApiKeyAuthenticator(
                lookup=SnapshotApiKeyValidationLookup(snapshot_manager),
                keyring=keyring,
                availability_check=snapshot_manager.ensure_available,
            ),
            routes=snapshot_manager,
            snapshot_available=snapshot_manager.ensure_available,
            version_payload=version,
            reasoning_models=_reasoning_models(runtime_catalog),
            runtime_capabilities={
                name: runtime.request_capabilities
                for name, runtime in runtime_catalog.runtimes.items()
            },
            configuration=configuration,
            runtime_adapter=runtime,
            circuit_breaker=circuit_breaker,
            service_metrics=service_metrics,
            service_metric_writer=ServiceMetricWriter(service_metrics, factory),
            request_ledger=spool_writer,
            ledger_health=LedgerHealthMonitor(
                args.request_ledger_path,
                writer=spool_writer,
                delayed_after_seconds=configuration.ledger_delayed_after_seconds,
                unavailable_after_seconds=configuration.ledger_unavailable_after_seconds,
                interval_seconds=configuration.ledger_health_interval_seconds,
            ),
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=args.host,
                port=args.port,
                access_log=False,
                proxy_headers=False,
                server_header=False,
            )
        )
        await server.serve()
    finally:
        if snapshot_manager is not None:
            await snapshot_manager.stop()
        await runtime.aclose()
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    try:
        asyncio.run(_run(_parse_args(argv)))
    except KeyboardInterrupt:
        return
    except ApiKeyPepperStartupError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as error:
        code = startup_failure_code(
            error,
            (GatewayConfigurationError, SecretFileError, ApiKeyPepperKeyringError),
        )
        print(f"gateway startup failed: {code}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

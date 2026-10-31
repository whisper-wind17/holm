"""Fail-closed Gateway configuration loaded without exposing Secret values."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from holm_common import SecretFileError, read_secret_file
from holm_domain import (
    LEDGER_DELAYED_AFTER_SECONDS,
    LEDGER_UNAVAILABLE_AFTER_SECONDS,
)
from holm_ledger import DEFAULT_LEDGER_HEALTH_INTERVAL_SECONDS
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

from holm_gateway.metering import (
    DEFAULT_METERING_TEXT_MAX_BYTES,
    DEFAULT_TOKENIZER_CACHE_BYTES,
    DEFAULT_TOKENIZER_TIMEOUT_SECONDS,
    DEFAULT_TOKENIZER_WORKER_THREADS,
)


class GatewayConfigurationError(RuntimeError):
    """A Gateway setting or Secret file is unusable."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("gateway configuration rejected")


def load_database_url(path: Path) -> URL:
    """Read a role-specific PostgreSQL DSN without retaining or rendering it."""

    try:
        raw = read_secret_file(path)
    except SecretFileError as error:
        raise GatewayConfigurationError(f"database_secret_unavailable:{error.code}") from None
    try:
        value = raw.decode("utf-8").strip()
        url = make_url(value)
    except (ArgumentError, UnicodeDecodeError, ValueError, TypeError):
        raise GatewayConfigurationError("database_url_invalid") from None
    if not value or url.drivername != "postgresql+psycopg" or not url.username:
        raise GatewayConfigurationError("database_url_invalid")
    return url


@dataclass(frozen=True, slots=True)
class GatewayConfiguration:
    """Bound every in-memory buffer, request lifetime, and admission counter."""

    max_inflight_requests: int = 64
    max_inflight_per_instance: int | str = "auto"
    runtime_pool_timeout_ms: int = 100
    runtime_connect_timeout_seconds: float = 3.0
    first_token_timeout_seconds: float = 600.0
    total_timeout_seconds: float = 3600.0
    breaker_failure_threshold: int = 3
    breaker_failure_window_seconds: float = 10.0
    breaker_cooldown_seconds: float = 5.0
    route_snapshot_poll_seconds: float = 1.0
    route_snapshot_max_age_seconds: float = 60.0
    max_request_body_bytes: int = 16 * 1024 * 1024
    max_json_depth: int = 32
    max_response_body_bytes: int = 16 * 1024 * 1024
    max_sse_event_bytes: int = 1024 * 1024
    request_record_queue_size: int = 10_000
    tokenizer_cache_max_bytes: int = DEFAULT_TOKENIZER_CACHE_BYTES
    tokenizer_worker_threads: int = DEFAULT_TOKENIZER_WORKER_THREADS
    tokenizer_fallback_timeout_seconds: float = DEFAULT_TOKENIZER_TIMEOUT_SECONDS
    metering_text_max_bytes: int = DEFAULT_METERING_TEXT_MAX_BYTES
    # Ledger backlog protection. The defaults are the product contract; they
    # exist as settings only so tests and smoke runs can compress the timeline.
    ledger_delayed_after_seconds: float = LEDGER_DELAYED_AFTER_SECONDS
    ledger_unavailable_after_seconds: float = LEDGER_UNAVAILABLE_AFTER_SECONDS
    ledger_health_interval_seconds: float = DEFAULT_LEDGER_HEALTH_INTERVAL_SECONDS

    def __post_init__(self) -> None:
        if self.max_inflight_requests < 1:
            raise GatewayConfigurationError("max_inflight_invalid")
        if not (
            self.max_inflight_per_instance == "auto"
            or (
                isinstance(self.max_inflight_per_instance, int)
                and not isinstance(self.max_inflight_per_instance, bool)
                and self.max_inflight_per_instance >= 1
            )
        ):
            raise GatewayConfigurationError("max_inflight_per_instance_invalid")
        if (
            not isinstance(self.runtime_pool_timeout_ms, int)
            or isinstance(self.runtime_pool_timeout_ms, bool)
            or self.runtime_pool_timeout_ms <= 0
        ):
            raise GatewayConfigurationError("runtime_pool_timeout_invalid")
        if (
            not isinstance(self.runtime_connect_timeout_seconds, (int, float))
            or isinstance(self.runtime_connect_timeout_seconds, bool)
            or not 0.1 <= self.runtime_connect_timeout_seconds <= 60.0
        ):
            raise GatewayConfigurationError("runtime_connect_timeout_invalid")
        if self.first_token_timeout_seconds <= 0 or self.total_timeout_seconds <= 0:
            raise GatewayConfigurationError("timeout_invalid")
        if self.first_token_timeout_seconds > self.total_timeout_seconds:
            raise GatewayConfigurationError("timeout_order_invalid")
        if (
            not isinstance(self.breaker_failure_threshold, int)
            or isinstance(self.breaker_failure_threshold, bool)
            or not 1 <= self.breaker_failure_threshold <= 100
        ):
            raise GatewayConfigurationError("breaker_threshold_invalid")
        if (
            not isinstance(self.breaker_failure_window_seconds, (int, float))
            or isinstance(self.breaker_failure_window_seconds, bool)
            or not 0.1 <= self.breaker_failure_window_seconds <= 300.0
        ):
            raise GatewayConfigurationError("breaker_window_invalid")
        if (
            not isinstance(self.breaker_cooldown_seconds, (int, float))
            or isinstance(self.breaker_cooldown_seconds, bool)
            or not 0.1 <= self.breaker_cooldown_seconds <= 300.0
        ):
            raise GatewayConfigurationError("breaker_cooldown_invalid")
        if not 0.1 <= self.route_snapshot_poll_seconds <= 2.0:
            raise GatewayConfigurationError("route_snapshot_poll_invalid")
        if not 1.0 <= self.route_snapshot_max_age_seconds <= 300.0:
            raise GatewayConfigurationError("route_snapshot_max_age_invalid")
        if (
            min(
                self.max_request_body_bytes,
                self.max_response_body_bytes,
                self.max_sse_event_bytes,
            )
            < 1
        ):
            raise GatewayConfigurationError("body_limit_invalid")
        if self.request_record_queue_size < 1:
            raise GatewayConfigurationError("request_record_queue_size_invalid")
        if self.max_json_depth < 1:
            raise GatewayConfigurationError("json_depth_invalid")
        if self.tokenizer_cache_max_bytes < 1 or self.metering_text_max_bytes < 1:
            raise GatewayConfigurationError("tokenizer_cache_invalid")
        if self.tokenizer_worker_threads < 1:
            raise GatewayConfigurationError("tokenizer_worker_threads_invalid")
        if not 0.1 <= self.tokenizer_fallback_timeout_seconds <= 60.0:
            raise GatewayConfigurationError("tokenizer_timeout_invalid")
        if self.ledger_delayed_after_seconds <= 0 or self.ledger_unavailable_after_seconds <= 0:
            raise GatewayConfigurationError("ledger_health_threshold_invalid")
        if self.ledger_delayed_after_seconds > self.ledger_unavailable_after_seconds:
            raise GatewayConfigurationError("ledger_health_threshold_order_invalid")
        if not 0.05 <= self.ledger_health_interval_seconds <= 60.0:
            raise GatewayConfigurationError("ledger_health_interval_invalid")

    @property
    def resolved_max_inflight_per_instance(self) -> int:
        instance_limit = self.max_inflight_per_instance
        if instance_limit == "auto":
            return self.max_inflight_requests
        if not isinstance(instance_limit, int):
            raise GatewayConfigurationError("max_inflight_per_instance_invalid")
        return min(self.max_inflight_requests, instance_limit)


__all__ = ["GatewayConfiguration", "GatewayConfigurationError", "load_database_url"]

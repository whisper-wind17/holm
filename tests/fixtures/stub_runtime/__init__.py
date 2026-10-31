"""Standard-library Stub Runtime fixture for M0-06A and M0-06B.

All values exposed by this constructed fixture are ``待真机复核``. They must be
reconciled with certified Runtime behavior during G1 as required by development
plan section 16.2.
"""

from .server import (
    CANONICAL_METRIC_FAMILIES,
    FIRST_TOKEN_TIMEOUT_BOUNDARY_SECONDS,
    FIXTURE_REVIEW_STATUS,
    METRICS_COLLECTION_TIMEOUT_SECONDS,
    METRICS_RESPONSE_LIMIT_BYTES,
    REQUEST_ID_HEADER,
    RUNTIME_START_TIME_METRIC,
    DisconnectPoint,
    HTTPFailurePoint,
    MetricDrift,
    MetricsFault,
    RuntimeMode,
    RuntimeShape,
    StubRuntime,
    StubRuntimeConfig,
    StubTransportTiming,
    UsageVariant,
)

__all__ = [
    "CANONICAL_METRIC_FAMILIES",
    "DisconnectPoint",
    "FIRST_TOKEN_TIMEOUT_BOUNDARY_SECONDS",
    "FIXTURE_REVIEW_STATUS",
    "HTTPFailurePoint",
    "METRICS_COLLECTION_TIMEOUT_SECONDS",
    "METRICS_RESPONSE_LIMIT_BYTES",
    "MetricDrift",
    "MetricsFault",
    "REQUEST_ID_HEADER",
    "RUNTIME_START_TIME_METRIC",
    "RuntimeMode",
    "RuntimeShape",
    "StubRuntime",
    "StubRuntimeConfig",
    "StubTransportTiming",
    "UsageVariant",
]

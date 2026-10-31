"""Bounded Runtime metric parsing, derivation, and in-memory retention."""

from __future__ import annotations

import math
import re
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final

MAX_METRICS_BYTES: Final = 2 * 1024 * 1024
MAX_MODEL_NAMES: Final = 16
MAX_MODEL_NAME_LENGTH: Final = 255
PERCENTILE_WINDOW_SECONDS: Final = 60.0

_SAMPLE_RE: Final = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>.*)\})?\s+"
    r"(?P<value>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|[+-]Inf|NaN)"
    r"(?:\s+\d+)?$"
)
_LABEL_RE: Final = re.compile(r'(?:^|,)\s*([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"\\])*)"')
_TYPE_RE: Final = re.compile(
    r"^# TYPE (?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*) "
    r"(?P<type>counter|gauge|histogram|summary|untyped)$"
)


class CanonicalRuntimeMetric(StrEnum):
    TTFT_SECONDS = "runtime_ttft_seconds"
    TPOT_SECONDS = "runtime_tpot_seconds"
    E2E_LATENCY_SECONDS = "runtime_e2e_latency_seconds"
    OUTPUT_TOKENS_TOTAL = "runtime_output_tokens_total"
    REQUESTS_TOTAL = "runtime_requests_total"
    REQUESTS_RUNNING = "runtime_requests_running"
    REQUESTS_WAITING = "runtime_requests_waiting"
    QUEUE_TIME_SECONDS = "runtime_queue_time_seconds"
    KV_CACHE_USAGE_RATIO = "runtime_kv_cache_usage_ratio"
    ERRORS_TOTAL = "runtime_errors_total"


class RuntimeMetricType(StrEnum):
    GAUGE = "gauge"
    COUNTER = "counter"
    HISTOGRAM = "histogram"


class TelemetryQuality(StrEnum):
    FULL = "full"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class RuntimeMetricSource:
    source: str
    metric_type: RuntimeMetricType
    labels: Mapping[str, frozenset[str]] = field(default_factory=lambda: MappingProxyType({}))


type RuntimeMetricMapping = Mapping[CanonicalRuntimeMetric, RuntimeMetricSource | None]


def _legacy_vllm_mapping() -> RuntimeMetricMapping:
    """Keep the pre-M6 parser entry point compatible; production uses the manifest."""

    return MappingProxyType(
        {
            CanonicalRuntimeMetric.TTFT_SECONDS: RuntimeMetricSource(
                "vllm:time_to_first_token_seconds", RuntimeMetricType.HISTOGRAM
            ),
            CanonicalRuntimeMetric.TPOT_SECONDS: None,
            CanonicalRuntimeMetric.E2E_LATENCY_SECONDS: None,
            CanonicalRuntimeMetric.OUTPUT_TOKENS_TOTAL: RuntimeMetricSource(
                "vllm:generation_tokens_total", RuntimeMetricType.COUNTER
            ),
            CanonicalRuntimeMetric.REQUESTS_TOTAL: None,
            CanonicalRuntimeMetric.REQUESTS_RUNNING: RuntimeMetricSource(
                "vllm:num_requests_running", RuntimeMetricType.GAUGE
            ),
            CanonicalRuntimeMetric.REQUESTS_WAITING: RuntimeMetricSource(
                "vllm:num_requests_waiting", RuntimeMetricType.GAUGE
            ),
            CanonicalRuntimeMetric.QUEUE_TIME_SECONDS: RuntimeMetricSource(
                "vllm:request_queue_time_seconds", RuntimeMetricType.HISTOGRAM
            ),
            CanonicalRuntimeMetric.KV_CACHE_USAGE_RATIO: RuntimeMetricSource(
                "vllm:kv_cache_usage_perc", RuntimeMetricType.GAUGE
            ),
            CanonicalRuntimeMetric.ERRORS_TOTAL: None,
        }
    )


LEGACY_VLLM_METRIC_MAPPING: Final = _legacy_vllm_mapping()


class MetricsPayloadTooLarge(ValueError):
    """The Runtime response exceeded the fixed safety limit."""


@dataclass(frozen=True, slots=True)
class RuntimeMetrics:
    running: float | None
    waiting: float | None
    kv_cache_usage_ratio: float | None
    output_tokens_total: float | None
    ttft_buckets: tuple[tuple[float, float], ...]
    queue_time_buckets: tuple[tuple[float, float], ...]
    model_names: tuple[str, ...]
    tpot_buckets: tuple[tuple[float, float], ...] = ()
    e2e_latency_buckets: tuple[tuple[float, float], ...] = ()
    requests_total: float | None = None
    errors_total: float | None = None
    unsupported: tuple[CanonicalRuntimeMetric, ...] = ()


@dataclass(frozen=True, slots=True)
class DerivedRuntimeMetrics:
    running: float | None
    waiting: float | None
    kv_cache_usage_ratio: float | None
    output_tps: float | None
    ttft_p50_seconds: float | None
    ttft_p95_seconds: float | None
    ttft_p99_seconds: float | None
    queue_time_p50_seconds: float | None
    queue_time_p95_seconds: float | None
    unsupported: tuple[CanonicalRuntimeMetric, ...] | None = None


@dataclass(frozen=True, slots=True)
class GaugeAggregate:
    average: float | None
    maximum: float | None
    last: float | None


@dataclass(frozen=True, slots=True)
class CounterAggregate:
    delta: float | None
    rate_average: float | None
    rate_maximum: float | None


def _labels(value: str | None) -> dict[str, str] | None:
    if value is None or not value.strip():
        return {}
    result: dict[str, str] = {}
    consumed = 0
    for match in _LABEL_RE.finditer(value):
        if value[consumed : match.start()].strip(" ,"):
            return None
        name, raw = match.groups()
        decoded = re.sub(
            r"\\(.)",
            lambda escaped: {"\\": "\\", '"': '"', "n": "\n"}.get(
                escaped.group(1), escaped.group(1)
            ),
            raw,
        )
        result[name] = decoded
        consumed = match.end()
    if value[consumed:].strip(" ,"):
        return None
    return result


def _number(value: str) -> float | None:
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _bucket_bound(value: str | None) -> float | None:
    if value == "+Inf":
        return math.inf
    if value is None:
        return None
    return _number(value)


def _type_family_names(source: RuntimeMetricSource) -> tuple[str, ...]:
    """Return valid TYPE family names for one configured sample name."""

    if source.metric_type is RuntimeMetricType.COUNTER and source.source.endswith("_total"):
        return (source.source, source.source.removesuffix("_total"))
    return (source.source,)


def parse_runtime_metrics(
    payload: bytes,
    mapping: RuntimeMetricMapping,
    *,
    require_type_declarations: bool = True,
) -> RuntimeMetrics:
    """Parse only manifest-approved Runtime families into canonical values."""

    if len(payload) > MAX_METRICS_BYTES:
        raise MetricsPayloadTooLarge("metrics response exceeds 2 MiB")
    if frozenset(mapping) != frozenset(CanonicalRuntimeMetric):
        raise ValueError("Runtime metric mapping must define every canonical metric")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("metrics response is not UTF-8") from error

    declared_types: dict[str, str] = {}
    sample_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        type_match = _TYPE_RE.fullmatch(stripped)
        if type_match is not None:
            declared_types[type_match.group("name")] = type_match.group("type")
        elif stripped and not stripped.startswith("#"):
            sample_lines.append(stripped)

    unsupported: set[CanonicalRuntimeMetric] = set()
    for canonical, source in mapping.items():
        if source is None:
            unsupported.add(canonical)
            continue
        declared = {
            declared_types[name] for name in _type_family_names(source) if name in declared_types
        }
        if declared and declared != {source.metric_type.value}:
            unsupported.add(canonical)
    gauges: dict[CanonicalRuntimeMetric, list[float]] = {}
    counters: dict[CanonicalRuntimeMetric, list[float]] = {}
    histograms: dict[CanonicalRuntimeMetric, dict[float, float]] = {}
    model_names: set[str] = set()

    for line in sample_lines:
        match = _SAMPLE_RE.fullmatch(line)
        if match is None:
            continue
        name = match.group("name")
        labels = _labels(match.group("labels"))
        sample_value = _number(match.group("value"))
        if labels is None or sample_value is None:
            continue
        model_name = labels.get("model_name")
        if model_name is not None:
            if not model_name or len(model_name) > MAX_MODEL_NAME_LENGTH:
                continue
            if model_name not in model_names and len(model_names) >= MAX_MODEL_NAMES:
                continue
            model_names.add(model_name)
        for canonical, source in mapping.items():
            if source is None or canonical in unsupported:
                continue
            sample_name = (
                f"{source.source}_bucket"
                if source.metric_type is RuntimeMetricType.HISTOGRAM
                else source.source
            )
            if (
                name == sample_name
                and require_type_declarations
                and not any(
                    family_name in declared_types for family_name in _type_family_names(source)
                )
            ):
                unsupported.add(canonical)
                continue
            if name != sample_name or any(
                labels.get(label_name) not in accepted
                for label_name, accepted in source.labels.items()
            ):
                continue
            if source.metric_type is RuntimeMetricType.GAUGE:
                gauges.setdefault(canonical, []).append(sample_value)
            elif source.metric_type is RuntimeMetricType.COUNTER:
                counters.setdefault(canonical, []).append(sample_value)
            else:
                bound = _bucket_bound(labels.get("le"))
                if bound is not None:
                    family = histograms.setdefault(canonical, {})
                    family[bound] = family.get(bound, 0.0) + sample_value

    def aggregate_gauge(name: CanonicalRuntimeMetric) -> float | None:
        values = gauges.get(name, [])
        if not values:
            return None
        if name is CanonicalRuntimeMetric.KV_CACHE_USAGE_RATIO:
            return max(values)
        return sum(values)

    def aggregate_counter(name: CanonicalRuntimeMetric) -> float | None:
        values = counters.get(name, [])
        return sum(values) if values else None

    def buckets(name: CanonicalRuntimeMetric) -> tuple[tuple[float, float], ...]:
        return tuple(sorted(histograms.get(name, {}).items()))

    return RuntimeMetrics(
        running=aggregate_gauge(CanonicalRuntimeMetric.REQUESTS_RUNNING),
        waiting=aggregate_gauge(CanonicalRuntimeMetric.REQUESTS_WAITING),
        kv_cache_usage_ratio=aggregate_gauge(CanonicalRuntimeMetric.KV_CACHE_USAGE_RATIO),
        output_tokens_total=aggregate_counter(CanonicalRuntimeMetric.OUTPUT_TOKENS_TOTAL),
        ttft_buckets=buckets(CanonicalRuntimeMetric.TTFT_SECONDS),
        queue_time_buckets=buckets(CanonicalRuntimeMetric.QUEUE_TIME_SECONDS),
        model_names=tuple(sorted(model_names)),
        tpot_buckets=buckets(CanonicalRuntimeMetric.TPOT_SECONDS),
        e2e_latency_buckets=buckets(CanonicalRuntimeMetric.E2E_LATENCY_SECONDS),
        requests_total=aggregate_counter(CanonicalRuntimeMetric.REQUESTS_TOTAL),
        errors_total=aggregate_counter(CanonicalRuntimeMetric.ERRORS_TOTAL),
        unsupported=tuple(
            canonical for canonical in CanonicalRuntimeMetric if canonical in unsupported
        ),
    )


def parse_vllm_metrics(payload: bytes) -> RuntimeMetrics:
    """Backward-compatible parser for callers predating manifest injection."""

    return parse_runtime_metrics(
        payload,
        LEGACY_VLLM_METRIC_MAPPING,
        require_type_declarations=False,
    )


def _percentile(
    current: tuple[tuple[float, float], ...],
    previous: tuple[tuple[float, float], ...],
    quantile: float,
) -> float | None:
    if not current or tuple(bound for bound, _ in current) != tuple(bound for bound, _ in previous):
        return None
    deltas = tuple(
        (bound, current_count - previous_count)
        for (bound, current_count), (_, previous_count) in zip(current, previous, strict=True)
    )
    if any(delta < 0 for _, delta in deltas):
        return None
    total = deltas[-1][1]
    if total <= 0:
        return None
    rank = quantile * total
    previous_bound = 0.0
    previous_count = 0.0
    for bound, cumulative_count in deltas:
        if cumulative_count < rank:
            if math.isfinite(bound):
                previous_bound = bound
            previous_count = cumulative_count
            continue
        if not math.isfinite(bound):
            return previous_bound
        bucket_count = cumulative_count - previous_count
        if bucket_count <= 0:
            return bound
        position = (rank - previous_count) / bucket_count
        return previous_bound + (bound - previous_bound) * position
    return None


def histogram_percentile_delta(
    current: tuple[tuple[float, float], ...],
    previous: tuple[tuple[float, float], ...],
    quantile: float,
) -> float | None:
    """Calculate a percentile from cumulative whole-window bucket deltas."""

    if not 0 < quantile <= 1:
        raise ValueError("histogram quantile must be in (0, 1]")
    return _percentile(current, previous, quantile)


def aggregate_gauge(values: tuple[float | int | None, ...]) -> GaugeAggregate:
    """Aggregate present Gauge observations without inventing missing zeroes."""

    present = tuple(float(value) for value in values if value is not None)
    if not present:
        return GaugeAggregate(None, None, None)
    return GaugeAggregate(sum(present) / len(present), max(present), present[-1])


def aggregate_counter(
    samples: tuple[tuple[datetime, float | int | None], ...],
) -> CounterAggregate:
    """Aggregate cumulative Counter deltas and rates, rebaselining on resets."""

    present = tuple(
        (sampled_at, float(value)) for sampled_at, value in samples if value is not None
    )
    if len(present) < 2:
        return CounterAggregate(None, None, None)
    deltas: list[float] = []
    rates: list[float] = []
    total_elapsed = 0.0
    for (previous_at, previous), (current_at, current) in zip(present, present[1:], strict=False):
        elapsed = (current_at - previous_at).total_seconds()
        if elapsed <= 0 or current < previous:
            continue
        delta = current - previous
        deltas.append(delta)
        rates.append(delta / elapsed)
        total_elapsed += elapsed
    if not deltas or total_elapsed <= 0:
        return CounterAggregate(None, None, None)
    total_delta = sum(deltas)
    return CounterAggregate(total_delta, total_delta / total_elapsed, max(rates))


def minute_quality(sample_count: int, *, expected_count: int = 12) -> TelemetryQuality:
    """Classify the 80% minute sampling coverage threshold."""

    if sample_count < 0 or expected_count <= 0:
        raise ValueError("minute sample counts must be non-negative and bounded")
    return (
        TelemetryQuality.FULL if sample_count / expected_count >= 0.8 else TelemetryQuality.PARTIAL
    )


class RuntimeMetricDeriver:
    """Keep rates per sample and histogram percentiles over the recent window."""

    def __init__(self) -> None:
        self._previous: tuple[float, RuntimeMetrics] | None = None
        # Retain one baseline before the cutoff, with the same hard cap as the ring.
        self._samples: deque[tuple[float, RuntimeMetrics]] = deque(maxlen=720)

    def derive(
        self, metrics: RuntimeMetrics, *, observed_monotonic: float
    ) -> DerivedRuntimeMetrics:
        previous = self._previous
        self._previous = (observed_monotonic, metrics)
        self._samples.append(self._previous)
        if previous is None:
            return self._result(metrics)
        previous_at, previous_metrics = previous
        elapsed = observed_monotonic - previous_at
        counter_reset = (
            metrics.output_tokens_total is not None
            and previous_metrics.output_tokens_total is not None
            and metrics.output_tokens_total < previous_metrics.output_tokens_total
        )
        if elapsed <= 0 or counter_reset:
            self._samples.clear()
            self._samples.append(self._previous)
            return self._result(metrics)

        cutoff = observed_monotonic - PERCENTILE_WINDOW_SECONDS
        while len(self._samples) > 2 and self._samples[1][0] <= cutoff:
            self._samples.popleft()
        ttft = self._window_buckets(tuple(sample.ttft_buckets for _, sample in self._samples))
        queue = self._window_buckets(
            tuple(sample.queue_time_buckets for _, sample in self._samples)
        )
        ttft_zero = tuple((bound, 0.0) for bound, _ in ttft)
        queue_zero = tuple((bound, 0.0) for bound, _ in queue)
        output_tps = None
        if (
            metrics.output_tokens_total is not None
            and previous_metrics.output_tokens_total is not None
        ):
            output_tps = (
                metrics.output_tokens_total - previous_metrics.output_tokens_total
            ) / elapsed
        return self._result(
            metrics,
            output_tps=output_tps,
            ttft_p50_seconds=histogram_percentile_delta(ttft, ttft_zero, 0.5),
            ttft_p95_seconds=histogram_percentile_delta(ttft, ttft_zero, 0.95),
            ttft_p99_seconds=histogram_percentile_delta(ttft, ttft_zero, 0.99),
            queue_time_p50_seconds=histogram_percentile_delta(queue, queue_zero, 0.5),
            queue_time_p95_seconds=histogram_percentile_delta(queue, queue_zero, 0.95),
        )

    @staticmethod
    def _window_buckets(
        samples: tuple[tuple[tuple[float, float], ...], ...],
    ) -> tuple[tuple[float, float], ...]:
        """Merge adjacent deltas backwards, stopping at a histogram rebaseline."""
        bounds = tuple(bound for bound, _ in samples[-1])
        totals = [0.0] * len(bounds)
        for previous, current in zip(reversed(samples[:-1]), reversed(samples[1:]), strict=True):
            if tuple(bound for bound, _ in previous) != bounds:
                break
            delta = tuple(new - old for (_, old), (_, new) in zip(previous, current, strict=True))
            if any(value < 0 for value in delta):
                break
            for index, value in enumerate(delta):
                totals[index] += value
        return tuple(zip(bounds, totals, strict=True))

    @staticmethod
    def _result(
        metrics: RuntimeMetrics,
        *,
        output_tps: float | None = None,
        ttft_p50_seconds: float | None = None,
        ttft_p95_seconds: float | None = None,
        ttft_p99_seconds: float | None = None,
        queue_time_p50_seconds: float | None = None,
        queue_time_p95_seconds: float | None = None,
    ) -> DerivedRuntimeMetrics:
        return DerivedRuntimeMetrics(
            running=metrics.running,
            waiting=metrics.waiting,
            kv_cache_usage_ratio=metrics.kv_cache_usage_ratio,
            output_tps=output_tps,
            ttft_p50_seconds=ttft_p50_seconds,
            ttft_p95_seconds=ttft_p95_seconds,
            ttft_p99_seconds=ttft_p99_seconds,
            queue_time_p50_seconds=queue_time_p50_seconds,
            queue_time_p95_seconds=queue_time_p95_seconds,
            unsupported=metrics.unsupported,
        )


class RingBuffer[T]:
    """A fixed-size, oldest-first in-memory series."""

    def __init__(self, capacity: int = 720) -> None:
        if capacity <= 0:
            raise ValueError("ring buffer capacity must be positive")
        self._items: deque[T] = deque(maxlen=capacity)

    def append(self, item: T) -> None:
        self._items.append(item)

    def values(self) -> tuple[T, ...]:
        return tuple(self._items)

    def __len__(self) -> int:
        return len(self._items)


class TelemetryStatus(StrEnum):
    AVAILABLE = "available"
    DELAYED = "delayed"
    UNAVAILABLE = "unavailable"


class TelemetryStatusTracker:
    """Classify freshness without changing the health of the observed target."""

    def __init__(self) -> None:
        self._last_success: float | None = None
        self._consecutive_failures = 0

    def success(self, observed_monotonic: float) -> None:
        self._last_success = observed_monotonic
        self._consecutive_failures = 0

    def failure(self) -> None:
        self._consecutive_failures += 1

    def status(self, now_monotonic: float) -> TelemetryStatus:
        if self._last_success is None or self._consecutive_failures >= 3:
            return TelemetryStatus.UNAVAILABLE
        age = max(0.0, now_monotonic - self._last_success)
        if age <= 10:
            return TelemetryStatus.AVAILABLE
        if age <= 30:
            return TelemetryStatus.DELAYED
        return TelemetryStatus.UNAVAILABLE

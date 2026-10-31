from __future__ import annotations

import math
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fixtures.fake_nvml import FakeNvml, FixtureId, scenario_for
from holm_control_api.telemetry_service import (
    OverviewValues,
    TelemetryStore,
    _runtime_metrics_url,
)
from holm_host_agent.authorization import ROLE_METHOD_ALLOWLIST
from holm_host_agent.gpu_discovery import (
    NvmlCallError,
    NvmlErrorKind,
    discover_gpus,
    inventory_to_proto,
)
from holm_telemetry import (
    MAX_METRICS_BYTES,
    MetricsPayloadTooLarge,
    RingBuffer,
    RuntimeMetricDeriver,
    TelemetryStatus,
    TelemetryStatusTracker,
    parse_vllm_metrics,
)


def _metrics(
    *,
    generation_tokens: int,
    ttft: tuple[int, int, int, int],
    queue: tuple[int, int, int],
) -> bytes:
    labels = 'model_name="fixture",engine="0"'
    lines = (
        "# ignored_help request body must never be retained",
        f'vllm:num_requests_running{{{labels},unknown="drop-me"}} 2',
        f"vllm:num_requests_waiting{{{labels}}} 1",
        f"vllm:kv_cache_usage_perc{{{labels}}} 0.25",
        f"vllm:generation_tokens_total{{{labels}}} {generation_tokens}",
        f'vllm:time_to_first_token_seconds_bucket{{{labels},le="0.1"}} {ttft[0]}',
        f'vllm:time_to_first_token_seconds_bucket{{{labels},le="0.5"}} {ttft[1]}',
        f'vllm:time_to_first_token_seconds_bucket{{{labels},le="1"}} {ttft[2]}',
        f'vllm:time_to_first_token_seconds_bucket{{{labels},le="+Inf"}} {ttft[3]}',
        f'vllm:request_queue_time_seconds_bucket{{{labels},le="0.1"}} {queue[0]}',
        f'vllm:request_queue_time_seconds_bucket{{{labels},le="1"}} {queue[1]}',
        f'vllm:request_queue_time_seconds_bucket{{{labels},le="+Inf"}} {queue[2]}',
        'vllm:unknown_metric{request_id="must-not-survive"} 999',
    )
    return ("\n".join(lines) + "\n").encode()


def test_PROTO_14_metrics_parser_allowlists_families_and_discards_unknown_labels() -> None:
    payload = _metrics(generation_tokens=100, ttft=(0, 0, 0, 0), queue=(0, 0, 0))
    parsed = parse_vllm_metrics(payload)

    assert parsed.running == 2
    assert parsed.waiting == 1
    assert parsed.kv_cache_usage_ratio == 0.25
    assert parsed.output_tokens_total == 100
    assert parsed.model_names == ("fixture",)
    assert "request_id" not in repr(parsed)
    with pytest.raises(MetricsPayloadTooLarge):
        parse_vllm_metrics(b"x" * (MAX_METRICS_BYTES + 1))


def test_PROTO_14_runtime_endpoint_uses_host_agent_authority_shape() -> None:
    assert _runtime_metrics_url("127.0.0.1:8123") == "http://127.0.0.1:8123/metrics"
    assert _runtime_metrics_url("http://localhost:8123") == "http://localhost:8123/metrics"
    with pytest.raises(ValueError):
        _runtime_metrics_url("http://example.com:8123")


def test_PROTO_14_p95_and_tps_use_counter_deltas_and_reset_without_negative_values() -> None:
    deriver = RuntimeMetricDeriver()
    first_payload = _metrics(generation_tokens=100, ttft=(0, 0, 0, 0), queue=(0, 0, 0))
    first = parse_vllm_metrics(first_payload)
    second = parse_vllm_metrics(
        _metrics(generation_tokens=160, ttft=(50, 90, 100, 100), queue=(80, 100, 100))
    )

    baseline = deriver.derive(first, observed_monotonic=10)
    derived = deriver.derive(second, observed_monotonic=15)
    reset = deriver.derive(first, observed_monotonic=20)

    assert baseline.output_tps is None
    assert derived.output_tps == 12
    assert derived.ttft_p50_seconds == pytest.approx(0.1)
    assert derived.ttft_p95_seconds == pytest.approx(0.75)
    assert derived.queue_time_p95_seconds == pytest.approx(0.775)
    assert reset.output_tps is None
    assert reset.ttft_p95_seconds is None


def test_PROTO_14_ring_buffer_caps_each_target_at_720_points() -> None:
    values = RingBuffer[int]()
    for value in range(721):
        values.append(value)

    assert len(values) == 720
    assert values.values()[0] == 1
    assert values.values()[-1] == 720


@pytest.mark.parametrize(
    ("age", "failures", "expected"),
    (
        (10.0, 0, TelemetryStatus.AVAILABLE),
        (10.01, 0, TelemetryStatus.DELAYED),
        (30.0, 0, TelemetryStatus.DELAYED),
        (30.01, 0, TelemetryStatus.UNAVAILABLE),
        (1.0, 3, TelemetryStatus.UNAVAILABLE),
    ),
)
def test_PROTO_14_status_thresholds(age: float, failures: int, expected: TelemetryStatus) -> None:
    tracker = TelemetryStatusTracker()
    tracker.success(100.0)
    for _ in range(failures):
        tracker.failure()
    assert tracker.status(100.0 + age) is expected


class _TemperatureFailureFake(FakeNvml):
    def device_get_temperature_celsius(self, handle: object) -> float:
        self.device_get_uuid(handle)
        raise NvmlCallError(NvmlErrorKind.OTHER)


def test_PROTO_14_nvml_single_metric_failure_omits_only_that_optional_field() -> None:
    scenario = scenario_for(FixtureId.FX_NV_02)
    response = inventory_to_proto(discover_gpus(_TemperatureFailureFake(scenario.snapshots)))
    device = response.devices[0]

    assert device.HasField("utilization_percent")
    assert device.HasField("memory_used_bytes")
    assert not device.HasField("temperature_celsius")
    assert device.HasField("power_watts")
    assert device.health != 0


def test_PROTO_14_host_agent_authorization_table_is_unchanged() -> None:
    assert ROLE_METHOD_ALLOWLIST["control-api"] == frozenset(
        {"GetHostInfo", "ListGPUs", "StreamLogs"}
    )


def test_PROTO_14_unavailable_telemetry_does_not_change_service_availability() -> None:
    store = TelemetryStore()
    instance_id = UUID("00000000-0000-7000-8000-000000001414")
    store.record_overview(
        OverviewValues(1, 3, 4, 0, 0, 1, 0, 0, 1),
        sampled_at=datetime(2026, 9, 15, tzinfo=UTC),
        observed_monotonic=100,
    )
    for _ in range(3):
        store.fail_instance(instance_id)

    assert store.instance(instance_id, now_monotonic=101).telemetry_status is (
        TelemetryStatus.UNAVAILABLE
    )
    overview = store.overview(now_monotonic=101)
    assert overview.telemetry_status is TelemetryStatus.AVAILABLE
    assert overview.values.services_available == 1
    assert math.isclose(overview.values.requests_running or 0, 0)


# FX-FIX-32: TDD 9.7 container aliases plus existing loopback URL compatibility.
@pytest.mark.parametrize(
    "authority",
    [
        "runtime-abcdef12-3456-7890-abcd-ef1234567890:8000",
        "127.0.0.1:8123",
        "localhost:8123",
        "[::1]:8123",
    ],
)
@pytest.mark.parametrize("scheme", ["", "http://"])
def test_FIX_32_telemetry_accepts_alias_and_preserves_loopback(authority: str, scheme: str) -> None:
    assert _runtime_metrics_url(f"{scheme}{authority}") == f"http://{authority}/metrics"


@pytest.mark.parametrize(
    "endpoint",
    [
        "runtime-ABCDEF12-3456-7890-abcd-ef1234567890:8000",
        "RUNTIME-abcdef12-3456-7890-abcd-ef1234567890:8000",
        "runtime-abcdef1234567890abcdef1234567890:8000",
        "runtime-not-a-uuid:8000",
        "runtime-abcdef12-3456-7890-abcd-ef1234567890.example.com:8000",
        "example.com:8000",
        "http://example.com:8123",
        "192.0.2.1:8000",
        "https://runtime-abcdef12-3456-7890-abcd-ef1234567890:8000",
        "user@runtime-abcdef12-3456-7890-abcd-ef1234567890:8000",
        "runtime-abcdef12-3456-7890-abcd-ef1234567890:8000?query",
        "runtime-abcdef12-3456-7890-abcd-ef1234567890:8000#fragment",
    ],
)
def test_FIX_32_telemetry_rejects_unapproved_aliases_and_url_components(endpoint: str) -> None:
    with pytest.raises(ValueError):
        _runtime_metrics_url(endpoint)

"""M6-04 / TC-TM-02 manifest-driven parsing and Unsupported semantics."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from holm_contracts.agent.v1 import agent_pb2
from holm_control_api.telemetry_routes import InstanceMetricValuesResponse
from holm_control_api.telemetry_service import runtime_metric_mapping_resolver
from holm_domain import ModelInstanceStatus, RuntimeType
from holm_persistence import AiServiceRecord, ModelInstanceRecord
from holm_runtimes import load_runtime_compatibility
from holm_telemetry import (
    MAX_METRICS_BYTES,
    CanonicalRuntimeMetric,
    DerivedRuntimeMetrics,
    MetricsPayloadTooLarge,
    TelemetryCollector,
    parse_runtime_metrics,
)

ROOT = Path(__file__).resolve().parents[2]
CATALOG = load_runtime_compatibility(ROOT / "runtime-compat.yaml")
RESOLVE = runtime_metric_mapping_resolver(CATALOG)
INSTANCE_ID = UUID("00000000-0000-7000-8000-000000006004")
ARTIFACT_ID = UUID("00000000-0000-7000-8000-000000006014")
NOW = datetime(2026, 9, 21, tzinfo=UTC)
SGLANG_METRICS = b"""\
# TYPE sglang:time_to_first_token_seconds histogram
sglang:time_to_first_token_seconds_bucket{le=\"0.1\",model_name=\"m\"} 1
sglang:time_to_first_token_seconds_bucket{le=\"+Inf\",model_name=\"m\"} 1
# TYPE sglang:generation_tokens counter
sglang:generation_tokens_total{model_name=\"m\"} 0
# TYPE sglang:e2e_request_latency_seconds histogram
sglang:e2e_request_latency_seconds_bucket{le=\"0.1\",model_name=\"m\"} 1
sglang:e2e_request_latency_seconds_bucket{le=\"+Inf\",model_name=\"m\"} 1
# TYPE sglang:num_requests counter
sglang:num_requests_total{model_name=\"m\"} 2
# TYPE sglang:num_running_reqs gauge
sglang:num_running_reqs{model_name=\"m\"} 0
# TYPE sglang:num_queue_reqs gauge
sglang:num_queue_reqs{model_name=\"m\"} 2
# TYPE sglang:queue_time_seconds histogram
sglang:queue_time_seconds_bucket{le=\"0.1\",model_name=\"m\"} 1
sglang:queue_time_seconds_bucket{le=\"+Inf\",model_name=\"m\"} 1
# TYPE sglang:token_usage gauge
sglang:token_usage{model_name=\"m\"} 0
# TYPE sglang:gen_throughput gauge
sglang:gen_throughput{model_name=\"m\"} 999
"""


def _mapping():  # type: ignore[no-untyped-def]
    mapping = RESOLVE("sglang")
    assert mapping is not None
    return mapping


def test_M6_04_TC_TM_02_sglang_zeroes_parse_without_using_throughput_gauge() -> None:
    parsed = parse_runtime_metrics(SGLANG_METRICS, _mapping())

    assert parsed.running == 0
    assert parsed.waiting == 2
    assert parsed.kv_cache_usage_ratio == 0
    assert parsed.output_tokens_total == 0
    assert parsed.requests_total == 2
    assert parsed.ttft_buckets == ((0.1, 1.0), (float("inf"), 1.0))
    assert parsed.e2e_latency_buckets == ((0.1, 1.0), (float("inf"), 1.0))
    assert set(parsed.unsupported) == {
        CanonicalRuntimeMetric.TPOT_SECONDS,
        CanonicalRuntimeMetric.ERRORS_TOTAL,
    }


def test_M6_04_TC_TM_02_type_drift_becomes_explicit_unsupported() -> None:
    drifted = SGLANG_METRICS.replace(
        b"# TYPE sglang:num_running_reqs gauge",
        b"# TYPE sglang:num_running_reqs counter",
    )
    parsed = parse_runtime_metrics(drifted, _mapping())

    assert parsed.running is None
    assert CanonicalRuntimeMetric.REQUESTS_RUNNING in parsed.unsupported


def test_M6_04_TC_TM_02_missing_type_declaration_becomes_explicit_unsupported() -> None:
    undeclared = SGLANG_METRICS.replace(
        b"# TYPE sglang:num_running_reqs gauge\n",
        b"",
    )
    parsed = parse_runtime_metrics(undeclared, _mapping())

    assert parsed.running is None
    assert CanonicalRuntimeMetric.REQUESTS_RUNNING in parsed.unsupported


def test_M6_04_TC_TM_01_size_limit_is_still_fail_loud() -> None:
    with pytest.raises(MetricsPayloadTooLarge):
        parse_runtime_metrics(b"x" * (MAX_METRICS_BYTES + 1), _mapping())


class _Gpus:
    async def observe_gpus(self) -> agent_pb2.ListGPUsResponse:
        return agent_pb2.ListGPUsResponse(discovery_state=agent_pb2.GPU_DISCOVERY_STATE_READY)


class _Instances:
    async def list(self) -> tuple[ModelInstanceRecord, ...]:
        return (
            ModelInstanceRecord(
                name="FX-M3-10B instance",
                id=INSTANCE_ID,
                artifact_id=ARTIFACT_ID,
                runtime_type=RuntimeType.SGLANG,
                expected_gpu_uuids=("GPU-M6-04",),
                tensor_parallel_size=1,
                context_length=4096,
                gpu_memory_utilization=0.9,
                internal_model_name="mi_m604",
                status=ModelInstanceStatus.RUNNING,
                error_code=None,
                internal_endpoint="127.0.0.1:8201",
                config_version=1,
                created_at=NOW,
                updated_at=NOW,
            ),
        )


class _Services:
    async def list(self) -> tuple[AiServiceRecord, ...]:
        return ()


def test_M6_04A_collector_requires_explicit_metric_mapping_resolver() -> None:
    with pytest.raises(TypeError, match="metric_mapping_resolver"):
        TelemetryCollector(  # type: ignore[call-arg]
            gpus=_Gpus(),
            instances=_Instances(),
            ai_services=_Services(),
        )


def test_M6_04_TC_TM_02_collector_selects_mapping_from_instance_runtime() -> None:
    collector = TelemetryCollector(
        gpus=_Gpus(),
        instances=_Instances(),
        ai_services=_Services(),
        metrics_reader=lambda _endpoint: asyncio.sleep(0, result=SGLANG_METRICS),
        metric_mapping_resolver=RESOLVE,
    )

    asyncio.run(collector.collect_once())
    current = collector.store.instance(INSTANCE_ID, now_monotonic=0).current

    assert current.running == 0
    assert current.waiting == 2
    assert CanonicalRuntimeMetric.TPOT_SECONDS in (current.unsupported or ())


def test_M6_04_unsupported_and_collected_zero_are_distinct_in_admin_response() -> None:
    response = InstanceMetricValuesResponse.from_value(
        DerivedRuntimeMetrics(
            running=0,
            waiting=0,
            kv_cache_usage_ratio=0,
            output_tps=0,
            ttft_p50_seconds=None,
            ttft_p95_seconds=None,
            ttft_p99_seconds=None,
            queue_time_p50_seconds=None,
            queue_time_p95_seconds=None,
            unsupported=(CanonicalRuntimeMetric.TTFT_SECONDS,),
        )
    )

    assert response.running == 0
    assert response.output_tps == 0
    assert response.ttft_p99_seconds is None
    assert response.unsupported == (CanonicalRuntimeMetric.TTFT_SECONDS,)

"""PERF-02 regression coverage for FX-RT-03 first-event transport behavior."""

from __future__ import annotations

import http.client
import json
from uuid import uuid4

import pytest
from fixtures.stub_runtime import (
    REQUEST_ID_HEADER,
    RuntimeMode,
    StubRuntime,
    StubRuntimeConfig,
    StubTransportTiming,
)
from holm_gateway.stage_timing import StageTimingSnapshot
from performance import harness


def _empty_stage_snapshot() -> StageTimingSnapshot:
    return StageTimingSnapshot(None, None, None, None, None, None, None)


def test_PERF_02_FX_RT_03_disables_nagle_before_separate_header_and_chunk_writes() -> None:
    config = StubRuntimeConfig(
        mode=RuntimeMode.FX_RT_03,
        completion_tokens=1,
        event_interval_seconds=0,
    )
    with StubRuntime(config) as runtime:
        request_id = str(uuid4())
        connection = http.client.HTTPConnection(runtime.host, runtime.port, timeout=2.0)
        try:
            body = json.dumps({"model": "fixture", "messages": [], "stream": True}).encode("utf-8")
            connection.request(
                "POST",
                "/v1/chat/completions",
                body=body,
                headers={
                    "Content-Type": "application/json",
                    REQUEST_ID_HEADER: request_id,
                },
            )
            response = connection.getresponse()
            assert response.status == 200
            response.read()

            timings = runtime.drain_transport_timings()
            assert len(timings) == 1
            assert timings[0].request_id == request_id
            assert timings[0].tcp_nodelay == 1
            assert timings[0].headers_written_at_ns <= timings[0].first_chunk_written_at_ns
        finally:
            connection.close()


def test_PERF_02_four_timestamp_diagnostic_attributes_the_wire_gap() -> None:
    request_id = uuid4()
    sample = harness._GatewaySample(
        ttft_ms=40.0,
        request_id=request_id,
        stages=_empty_stage_snapshot(),
        transport_timing=harness._GatewayTransportTiming(
            headers_received_at_ns=12_000_000,
            first_chunk_received_at_ns=52_000_000,
            tcp_nodelay=1,
        ),
    )
    stub = StubTransportTiming(
        request_id=str(request_id),
        headers_written_at_ns=10_000_000,
        first_chunk_written_at_ns=11_000_000,
        tcp_nodelay=0,
    )

    diagnostics = harness._transport_diagnostics([sample], [stub])

    assert diagnostics["stub_write_gap_ms"] == {"p50": 1.0, "p95": 1.0, "p99": 1.0}
    assert diagnostics["gateway_receive_gap_ms"] == {
        "p50": 40.0,
        "p95": 40.0,
        "p99": 40.0,
    }
    assert diagnostics["first_chunk_transit_ms"] == {
        "p50": 41.0,
        "p95": 41.0,
        "p99": 41.0,
    }


def test_PERF_02_four_timestamp_diagnostic_fails_loud_when_stub_sample_is_missing() -> None:
    sample = harness._GatewaySample(
        ttft_ms=40.0,
        request_id=uuid4(),
        stages=_empty_stage_snapshot(),
        transport_timing=harness._GatewayTransportTiming(
            headers_received_at_ns=12_000_000,
            first_chunk_received_at_ns=52_000_000,
            tcp_nodelay=1,
        ),
    )

    with pytest.raises(harness.BaselineReportError, match=r"missing_stub=1"):
        harness._transport_diagnostics([sample], [])

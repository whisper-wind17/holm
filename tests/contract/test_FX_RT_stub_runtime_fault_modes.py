"""M0-06B contract checks for Stub Runtime fault and drift injection modes."""

from __future__ import annotations

import http.client
import json
import re
import socket
import struct
import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import call, patch

import pytest
from fixtures.stub_runtime import (
    FIRST_TOKEN_TIMEOUT_BOUNDARY_SECONDS,
    METRICS_COLLECTION_TIMEOUT_SECONDS,
    METRICS_RESPONSE_LIMIT_BYTES,
    REQUEST_ID_HEADER,
    RUNTIME_START_TIME_METRIC,
    DisconnectPoint,
    HTTPFailurePoint,
    MetricDrift,
    MetricsFault,
    RuntimeMode,
    StubRuntime,
    StubRuntimeConfig,
)

_TTFT_METRIC = "vllm:time_to_first_token_seconds"
_OUTPUT_COUNTER = "vllm:generation_tokens_total"


def _request(
    runtime: StubRuntime,
    method: str,
    path: str,
    payload: Mapping[str, object] | None = None,
    *,
    headers: Mapping[str, str] | None = None,
    timeout_seconds: float = 2.0,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection(
        runtime.host,
        runtime.port,
        timeout=timeout_seconds,
    )
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = dict(headers or {})
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    try:
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def _stream_exchange(
    runtime: StubRuntime,
    *,
    request_id: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    headers = None if request_id is None else {REQUEST_ID_HEADER: request_id}
    return _request(
        runtime,
        "POST",
        "/v1/chat/completions",
        {
            "model": runtime.config.model_id,
            "stream": True,
            "messages": [{"role": "user", "content": "kept-in-memory-only"}],
        },
        headers=headers,
    )


def _parse_sse(raw_stream: bytes) -> list[dict[str, Any] | str]:
    text = raw_stream.decode("utf-8")
    assert text.endswith("\n\n")
    events: list[dict[str, Any] | str] = []
    for block in text.removesuffix("\n\n").split("\n\n"):
        assert block.startswith("data: ")
        data = block.removeprefix("data: ")
        if data == "[DONE]":
            events.append(data)
            continue
        parsed = json.loads(data)
        assert isinstance(parsed, dict)
        events.append(parsed)
    return events


def _metrics(runtime: StubRuntime) -> str:
    status, _, body = _request(runtime, "GET", "/metrics", timeout_seconds=3.0)
    assert status == 200
    return body.decode("utf-8")


def _sample_value(metrics: str, metric_name: str) -> float:
    match = re.search(
        rf"^{re.escape(metric_name)}(?:\{{[^}}]*\}})? ([0-9.]+)$", metrics, re.MULTILINE
    )
    assert match is not None
    return float(match.group(1))


def _assert_released(runtime: StubRuntime, port: int) -> None:
    assert runtime.active_request_count == 0
    assert runtime.live_handler_thread_count == 0
    assert not runtime.is_alive
    with pytest.raises(OSError), socket.create_connection(("127.0.0.1", port), timeout=0.1):
        pass


def test_FX_RT_02_slow_first_event_exceeds_600_second_boundary_without_waiting() -> None:
    with pytest.raises(ValueError, match="exceed the 600 second boundary"):
        StubRuntimeConfig(
            mode=RuntimeMode.FX_RT_02,
            first_event_delay_seconds=FIRST_TOKEN_TIMEOUT_BOUNDARY_SECONDS,
        )

    configured_delay = FIRST_TOKEN_TIMEOUT_BOUNDARY_SECONDS + 1.0
    runtime = StubRuntime(
        StubRuntimeConfig(
            mode=RuntimeMode.FX_RT_02,
            first_event_delay_seconds=configured_delay,
            event_interval_seconds=0.0,
        )
    )
    port = runtime.port
    observed_delays: list[float] = []
    delay_observed = threading.Event()
    release_delay = threading.Event()

    def observe_delay(delay_seconds: float) -> bool:
        observed_delays.append(delay_seconds)
        delay_observed.set()
        return release_delay.wait(1.0)

    with (
        patch.object(runtime, "_wait_for_shutdown", side_effect=observe_delay) as wait_for_delay,
        runtime,
    ):
        request_body = json.dumps(
            {
                "model": runtime.config.model_id,
                "stream": True,
                "messages": [{"role": "user", "content": "slow-in-memory-only"}],
            }
        ).encode("utf-8")
        request_head = (
            "POST /v1/chat/completions HTTP/1.1\r\n"
            f"Host: {runtime.host}:{runtime.port}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(request_body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        connected_socket = socket.create_connection((runtime.host, runtime.port), timeout=2.0)
        try:
            connected_socket.sendall(request_head + request_body)
            response_head = b""
            while b"\r\n\r\n" not in response_head:
                chunk = connected_socket.recv(4096)
                assert chunk
                response_head += chunk
            headers, _, buffered_body = response_head.partition(b"\r\n\r\n")
            assert headers.startswith(b"HTTP/1.1 200")
            assert b"Content-Type: text/event-stream" in headers
            assert not buffered_body
            assert delay_observed.wait(0.2)
            assert observed_delays == [configured_delay]
            assert wait_for_delay.call_args_list == [call(configured_delay)]
            connected_socket.settimeout(0.05)
            with pytest.raises(TimeoutError):
                connected_socket.recv(1)
        finally:
            release_delay.set()
            connected_socket.close()
        assert runtime.wait_for_idle()
    _assert_released(runtime, port)


def test_FX_RT_04_returns_5xx_before_first_event() -> None:
    config = StubRuntimeConfig(
        mode=RuntimeMode.FX_RT_04,
        http_failure_point=HTTPFailurePoint.BEFORE_FIRST_EVENT,
        first_event_delay_seconds=0.0,
        event_interval_seconds=0.0,
    )
    with StubRuntime(config) as runtime:
        status, _, body = _stream_exchange(runtime)

    error = json.loads(body)
    assert status == 500
    assert error["error"]["status_code"] == 500


def test_FX_RT_04_returns_in_stream_5xx_after_first_event() -> None:
    config = StubRuntimeConfig(
        mode=RuntimeMode.FX_RT_04,
        http_failure_point=HTTPFailurePoint.AFTER_FIRST_EVENT,
        first_event_delay_seconds=0.0,
        event_interval_seconds=0.0,
    )
    with StubRuntime(config) as runtime:
        status, _, body = _stream_exchange(runtime)

    events = _parse_sse(body)
    assert status == 200
    assert len(events) == 2
    assert events[0]["choices"][0]["delta"]["content"] == "stub-1"
    assert events[1]["error"]["status_code"] == 500


@pytest.mark.parametrize(
    ("disconnect_point", "expected_token_events"),
    (
        (DisconnectPoint.ON_CONNECT, 0),
        (DisconnectPoint.AFTER_FIRST_EVENT, 1),
        (DisconnectPoint.BEFORE_TERMINAL, 3),
    ),
    ids=("on-connect", "after-first-event", "before-terminal"),
)
def test_FX_RT_05_connection_interruptions_are_independently_injected_and_released(
    disconnect_point: DisconnectPoint,
    expected_token_events: int,
) -> None:
    config = StubRuntimeConfig(
        mode=RuntimeMode.FX_RT_05,
        disconnect_point=disconnect_point,
        first_event_delay_seconds=0.0,
        event_interval_seconds=0.0,
        completion_tokens=3,
    )
    runtime = StubRuntime(config)
    port = runtime.port
    with runtime:
        if disconnect_point is DisconnectPoint.ON_CONNECT:
            with pytest.raises(http.client.RemoteDisconnected):
                _stream_exchange(runtime)
        else:
            status, _, body = _stream_exchange(runtime)
            events = _parse_sse(body)
            assert status == 200
            assert len(events) == expected_token_events
            assert all(event["choices"][0]["finish_reason"] is None for event in events)
        assert runtime.wait_for_idle()
        assert runtime.active_request_count == 0
    _assert_released(runtime, port)


def test_FX_RT_06_client_cancellation_reaches_upstream_and_stops_production() -> None:
    config = StubRuntimeConfig(
        mode=RuntimeMode.FX_RT_06,
        first_event_delay_seconds=0.0,
        event_interval_seconds=0.02,
        completion_tokens=500,
    )
    runtime = StubRuntime(config)
    port = runtime.port
    with runtime:
        request_body = json.dumps(
            {
                "model": config.model_id,
                "stream": True,
                "messages": [{"role": "user", "content": "cancel-without-persistence"}],
            }
        ).encode("utf-8")
        request_head = (
            "POST /v1/chat/completions HTTP/1.1\r\n"
            f"Host: {runtime.host}:{runtime.port}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(request_body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        connected_socket = socket.create_connection((runtime.host, runtime.port), timeout=2.0)
        try:
            connected_socket.sendall(request_head + request_body)
            received = b""
            while b"\r\n\r\n" not in received or b"\n\n" not in received.partition(b"\r\n\r\n")[2]:
                chunk = connected_socket.recv(4096)
                assert chunk
                received += chunk
            response_head, _, first_event = received.partition(b"\r\n\r\n")
            assert response_head.startswith(b"HTTP/1.1 200")
            assert first_event.startswith(b"data: ")
        finally:
            connected_socket.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_LINGER,
                struct.pack("ii", 1, 0),
            )
            connected_socket.close()

        assert runtime.wait_for_client_cancellation()
        generated_after_cancel = runtime.generated_event_count
        time.sleep(0.05)
        assert runtime.generated_event_count == generated_after_cancel
        assert runtime.wait_for_idle()
        assert runtime.active_request_count == 0
        assert runtime.live_handler_thread_count == 0
    _assert_released(runtime, port)


@pytest.mark.parametrize(
    "metric_drift",
    (
        MetricDrift.HISTOGRAM_TO_GAUGE,
        MetricDrift.SECONDS_TO_MILLISECONDS,
        MetricDrift.LABEL_SET_CHANGE,
    ),
    ids=("histogram-to-gauge", "seconds-to-milliseconds", "label-set-change"),
)
def test_FX_RT_08_metric_drifts_are_independently_selectable_and_unsupported(
    metric_drift: MetricDrift,
) -> None:
    config = StubRuntimeConfig(
        mode=RuntimeMode.FX_RT_08,
        metric_drifts=frozenset({metric_drift}),
    )
    with StubRuntime(config) as runtime:
        status, headers, body = _request(runtime, "GET", "/metrics")

    assert status == 200
    metrics = body.decode("utf-8")

    if metric_drift is MetricDrift.HISTOGRAM_TO_GAUGE:
        assert headers["Content-Type"] == "text/plain; version=0.0.4; charset=utf-8"
        assert f"# TYPE {_TTFT_METRIC} gauge" in metrics
        assert f"{_TTFT_METRIC}_bucket" not in metrics
        assert f"# UNIT {_TTFT_METRIC}" not in metrics
        assert 'replica="drifted"' not in metrics
    elif metric_drift is MetricDrift.SECONDS_TO_MILLISECONDS:
        assert (
            headers["Content-Type"] == "application/openmetrics-text; version=1.0.0; charset=utf-8"
        )
        assert f"# TYPE {_TTFT_METRIC} histogram" in metrics
        assert f"# UNIT {_TTFT_METRIC} milliseconds" in metrics
        assert metrics.index(f"# TYPE {_TTFT_METRIC}") < metrics.index(f"# UNIT {_TTFT_METRIC}")
        assert metrics.endswith("# EOF\n")
        assert f'{_TTFT_METRIC}_sum{{model_name="{config.model_id}"}} 50.0' in metrics
        assert 'replica="drifted"' not in metrics
    else:
        assert headers["Content-Type"] == "text/plain; version=0.0.4; charset=utf-8"
        family_lines = [
            line
            for line in metrics.splitlines()
            if line.startswith(_TTFT_METRIC) and not line.startswith("#")
        ]
        assert family_lines
        assert all('replica="drifted"' in line for line in family_lines)
        assert f"# TYPE {_TTFT_METRIC} histogram" in metrics
        assert f"# UNIT {_TTFT_METRIC}" not in metrics


def test_FX_RT_09_restart_resets_counters_and_changes_runtime_start_time() -> None:
    with StubRuntime(StubRuntimeConfig(mode=RuntimeMode.FX_RT_09)) as runtime:
        before_restart = _metrics(runtime)
        runtime.restart_runtime()
        after_restart = _metrics(runtime)

    assert _sample_value(before_restart, _OUTPUT_COUNTER) == 8.0
    assert _sample_value(after_restart, _OUTPUT_COUNTER) == 0.0
    assert _sample_value(after_restart, RUNTIME_START_TIME_METRIC) > _sample_value(
        before_restart,
        RUNTIME_START_TIME_METRIC,
    )
    counter_names = re.findall(r"^# TYPE ([^ ]+) counter$", after_restart, re.MULTILINE)
    for counter_name in counter_names:
        samples = re.findall(
            rf"^{re.escape(counter_name)}(?:\{{[^}}]*\}})? ([0-9.]+)$",
            after_restart,
            re.MULTILINE,
        )
        assert samples
        assert all(float(sample) == 0.0 for sample in samples)


@pytest.mark.parametrize(
    "metrics_fault",
    (
        MetricsFault.OVERSIZED,
        MetricsFault.DUPLICATE_FAMILY,
        MetricsFault.INVALID_TEXT,
    ),
    ids=("over-2-mib", "duplicate-family", "invalid-text"),
)
def test_FX_RT_10_abnormal_metrics_payloads_are_independently_generated(
    metrics_fault: MetricsFault,
) -> None:
    config = StubRuntimeConfig(mode=RuntimeMode.FX_RT_10, metrics_fault=metrics_fault)
    with StubRuntime(config) as runtime:
        status, _, body = _request(runtime, "GET", "/metrics", timeout_seconds=3.0)

    assert status == 200
    if metrics_fault is MetricsFault.OVERSIZED:
        assert len(body) > METRICS_RESPONSE_LIMIT_BYTES
    elif metrics_fault is MetricsFault.DUPLICATE_FAMILY:
        marker = f"# TYPE {_TTFT_METRIC} histogram".encode()
        assert body.count(marker) == 2
    else:
        assert body == b'# TYPE malformed counter\nmalformed{label="unterminated" 1\n'


def test_FX_RT_10_metrics_collection_delay_exceeds_two_second_timeout() -> None:
    configured_delay = METRICS_COLLECTION_TIMEOUT_SECONDS + 0.1
    config = StubRuntimeConfig(
        mode=RuntimeMode.FX_RT_10,
        metrics_fault=MetricsFault.TIMEOUT,
        metrics_delay_seconds=configured_delay,
    )
    runtime = StubRuntime(config)
    port = runtime.port
    started_at = time.monotonic()
    with runtime:
        with pytest.raises(TimeoutError):
            _request(
                runtime,
                "GET",
                "/metrics",
                timeout_seconds=METRICS_COLLECTION_TIMEOUT_SECONDS,
            )
        elapsed = time.monotonic() - started_at
        assert elapsed >= METRICS_COLLECTION_TIMEOUT_SECONDS * 0.9
        assert configured_delay > METRICS_COLLECTION_TIMEOUT_SECONDS
    _assert_released(runtime, port)


def test_FX_RT_11_concurrent_streams_echo_request_ids_without_cross_talk() -> None:
    config = StubRuntimeConfig(
        mode=RuntimeMode.FX_RT_11,
        first_event_delay_seconds=0.0,
        event_interval_seconds=0.001,
        completion_tokens=3,
    )
    request_ids = [f"018f47f2-8c00-7000-8000-{index:012d}" for index in range(24)]

    def exchange(request_id: str) -> tuple[str, list[str]]:
        status, headers, body = _stream_exchange(runtime, request_id=request_id)
        normalized_headers = {name.lower(): value for name, value in headers.items()}
        events = _parse_sse(body)
        echoed_event_ids = [event["request_id"] for event in events if event != "[DONE]"]
        assert status == 200
        return normalized_headers[REQUEST_ID_HEADER], echoed_event_ids

    with StubRuntime(config) as runtime, ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(exchange, request_ids))

    for expected_request_id, (header_request_id, event_request_ids) in zip(
        request_ids,
        results,
        strict=True,
    ):
        assert header_request_id == expected_request_id
        assert event_request_ids
        assert set(event_request_ids) == {expected_request_id}


def test_FX_RT_11_request_id_echo_can_be_configured_as_unsupported() -> None:
    config = StubRuntimeConfig(
        mode=RuntimeMode.FX_RT_11,
        request_id_echo_supported=False,
        first_event_delay_seconds=0.0,
        event_interval_seconds=0.0,
    )
    with StubRuntime(config) as runtime:
        status, headers, body = _stream_exchange(
            runtime,
            request_id="018f47f2-8c00-7000-8000-000000000001",
        )

    normalized_headers = {name.lower(): value for name, value in headers.items()}
    events = _parse_sse(body)
    assert status == 200
    assert REQUEST_ID_HEADER not in normalized_headers
    assert all("request_id" not in event for event in events if event != "[DONE]")


def test_FX_RT_fault_paths_do_not_log_request_or_response_bodies(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sensitive_body = "HOLM-FAULT-SENSITIVE-BODY-MUST-NOT-BE-LOGGED"
    config = StubRuntimeConfig(
        mode=RuntimeMode.FX_RT_04,
        http_failure_point=HTTPFailurePoint.AFTER_FIRST_EVENT,
        first_event_delay_seconds=0.0,
        event_interval_seconds=0.0,
    )
    with StubRuntime(config) as runtime:
        status, _, _ = _request(
            runtime,
            "POST",
            "/v1/chat/completions",
            {
                "model": config.model_id,
                "stream": True,
                "messages": [{"role": "user", "content": sensitive_body}],
            },
        )

    captured = capsys.readouterr()
    assert status == 200
    assert sensitive_body not in captured.out
    assert sensitive_body not in captured.err

"""M0-06A Contract checks for FX-RT-01, FX-RT-03, and FX-RT-07."""

from __future__ import annotations

import http.client
import json
import re
import socket
import time
from collections.abc import Mapping
from typing import Any

import pytest
from fixtures.stub_runtime import (
    CANONICAL_METRIC_FAMILIES,
    FIXTURE_REVIEW_STATUS,
    RuntimeMode,
    RuntimeShape,
    StubRuntime,
    StubRuntimeConfig,
    UsageVariant,
)

_PROMETHEUS_SAMPLE = re.compile(
    r"[a-zA-Z_:][a-zA-Z0-9_:]*(?:\{[^{}]*\})? (?:-?[0-9]+(?:\.[0-9]+)?|\+Inf)"
)
_CANONICAL_NAMES = {
    "runtime_ttft_seconds",
    "runtime_tpot_seconds",
    "runtime_e2e_latency_seconds",
    "runtime_output_tokens_total",
    "runtime_requests_total",
    "runtime_requests_running",
    "runtime_requests_waiting",
    "runtime_queue_time_seconds",
    "runtime_kv_cache_usage_ratio",
    "runtime_errors_total",
}


def _request(
    runtime: StubRuntime,
    method: str,
    path: str,
    payload: Mapping[str, object] | None = None,
) -> tuple[int, Mapping[str, str], bytes]:
    connection = http.client.HTTPConnection(runtime.host, runtime.port, timeout=2.0)
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {} if body is None else {"Content-Type": "application/json"}
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        response_body = response.read()
        return response.status, dict(response.getheaders()), response_body
    finally:
        connection.close()


def _json_response(body: bytes) -> dict[str, Any]:
    parsed = json.loads(body)
    assert isinstance(parsed, dict)
    return parsed


def _stream_request(runtime: StubRuntime) -> tuple[float, str, list[dict[str, Any] | str]]:
    connection = http.client.HTTPConnection(runtime.host, runtime.port, timeout=2.0)
    request_body = json.dumps(
        {
            "model": runtime.config.model_id,
            "stream": True,
            "messages": [{"role": "user", "content": "sensitive-test-prompt"}],
        }
    ).encode("utf-8")
    started_at = time.perf_counter()
    try:
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=request_body,
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        assert response.status == HTTPStatus.OK
        content_type = response.getheader("Content-Type")
        assert content_type is not None
        first_line = response.readline()
        first_event_at = time.perf_counter()
        raw_stream = first_line + response.read()
    finally:
        connection.close()

    return first_event_at - started_at, content_type, _parse_sse(raw_stream)


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


def _prometheus_types(metrics: str) -> dict[str, str]:
    metric_types: dict[str, str] = {}
    for line in metrics.splitlines():
        if line.startswith("# HELP "):
            _, _, name, help_text = line.split(" ", 3)
            assert name not in metric_types
            assert help_text
            continue
        if line.startswith("# TYPE "):
            _, _, name, metric_type = line.split(" ", 3)
            assert name not in metric_types
            assert metric_type in {"counter", "gauge", "histogram"}
            metric_types[name] = metric_type
            continue
        assert _PROMETHEUS_SAMPLE.fullmatch(line), line
    return metric_types


class HTTPStatus:
    OK = 200


def test_FX_RT_01_openai_compatible_health_models_and_non_streaming() -> None:
    config = StubRuntimeConfig(prompt_tokens=5, completion_tokens=4)
    with StubRuntime(config) as runtime:
        health_status, _, health_body = _request(runtime, "GET", "/health")
        models_status, _, models_body = _request(runtime, "GET", "/v1/models")
        completion_status, _, completion_body = _request(
            runtime,
            "POST",
            "/v1/chat/completions",
            {
                "model": config.model_id,
                "stream": False,
                "messages": [{"role": "user", "content": "not-persisted"}],
            },
        )

    assert health_status == HTTPStatus.OK
    assert _json_response(health_body) == {"status": "ok"}
    assert models_status == HTTPStatus.OK
    assert _json_response(models_body) == {
        "object": "list",
        "data": [
            {
                "id": config.model_id,
                "object": "model",
                "created": 0,
                "owned_by": "holm-stub-runtime",
            }
        ],
    }
    assert completion_status == HTTPStatus.OK
    completion = _json_response(completion_body)
    assert completion["object"] == "chat.completion"
    assert completion["choices"][0]["message"] == {
        "role": "assistant",
        "content": "stub-1 stub-2 stub-3 stub-4",
    }
    assert completion["usage"] == {
        "prompt_tokens": 5,
        "completion_tokens": 4,
        "total_tokens": 9,
    }


def test_FX_RT_01_stream_is_valid_sse_with_configured_timing_tokens_and_usage() -> None:
    config = StubRuntimeConfig(
        first_event_delay_seconds=0.06,
        event_interval_seconds=0.002,
        prompt_tokens=6,
        completion_tokens=4,
    )
    with StubRuntime(config) as runtime:
        first_event_delay, content_type, events = _stream_request(runtime)

    assert first_event_delay >= 0.04
    assert content_type == "text/event-stream; charset=utf-8"
    assert events[-1] == "[DONE]"
    chunks = events[:-1]
    token_chunks = [chunk for chunk in chunks if chunk["choices"][0]["delta"].get("content")]
    assert len(token_chunks) == 4
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["usage"] == {
        "prompt_tokens": 6,
        "completion_tokens": 4,
        "total_tokens": 10,
    }


def test_FX_RT_03_first_sse_event_is_immediate_independent_of_delay_setting() -> None:
    config = StubRuntimeConfig(
        mode=RuntimeMode.FX_RT_03,
        first_event_delay_seconds=0.75,
        event_interval_seconds=0.0,
    )
    with StubRuntime(config) as runtime:
        first_event_delay, _, events = _stream_request(runtime)

    assert first_event_delay < 0.4
    assert events[0] != "[DONE]"
    assert events[0]["choices"][0]["delta"]["content"] == "stub-1"


@pytest.mark.parametrize(
    ("usage_variant", "expected_usage"),
    (
        (UsageVariant.MISSING, None),
        (UsageVariant.PROMPT_ONLY, {"prompt_tokens": 7}),
        (UsageVariant.COMPLETION_ONLY, {"completion_tokens": 2}),
    ),
)
def test_FX_RT_07_terminal_usage_is_missing_or_partial(
    usage_variant: UsageVariant,
    expected_usage: dict[str, int] | None,
) -> None:
    config = StubRuntimeConfig(
        mode=RuntimeMode.FX_RT_07,
        usage_variant=usage_variant,
        first_event_delay_seconds=0.0,
        event_interval_seconds=0.0,
        prompt_tokens=7,
        completion_tokens=2,
    )
    with StubRuntime(config) as runtime:
        _, _, events = _stream_request(runtime)
        _, _, non_stream_body = _request(
            runtime,
            "POST",
            "/v1/chat/completions",
            {"model": config.model_id, "stream": False, "messages": []},
        )

    terminal_chunk = events[-2]
    non_stream = _json_response(non_stream_body)
    if expected_usage is None:
        assert "usage" not in terminal_chunk
        assert "usage" not in non_stream
    else:
        assert terminal_chunk["usage"] == expected_usage
        assert non_stream["usage"] == expected_usage


@pytest.mark.parametrize("runtime_shape", tuple(RuntimeShape))
def test_FX_RT_metrics_cover_TDD_13_2_canonical_families_with_native_types(
    runtime_shape: RuntimeShape,
) -> None:
    with StubRuntime(StubRuntimeConfig(runtime_shape=runtime_shape)) as runtime:
        status, headers, body = _request(runtime, "GET", "/metrics")

    assert status == HTTPStatus.OK
    assert headers["Content-Type"] == "text/plain; version=0.0.4; charset=utf-8"
    metrics = body.decode("utf-8")
    assert metrics.endswith("\n")
    assert "gen_throughput" not in metrics
    metric_types = _prometheus_types(metrics)
    sources = CANONICAL_METRIC_FAMILIES[runtime_shape]
    assert len(sources) == 10
    assert {source.canonical for source in sources} == _CANONICAL_NAMES

    for source in sources:
        assert metric_types[source.native_name] == source.metric_type
        if source.metric_type == "histogram":
            assert f"{source.native_name}_count" in metrics
        else:
            assert re.search(rf"^{re.escape(source.native_name)}\{{", metrics, re.MULTILINE)
        if source.selector is not None:
            assert source.selector in metrics


def test_FX_RT_fixture_is_pending_real_runtime_review_and_releases_ports() -> None:
    first = StubRuntime()
    second = StubRuntime()
    first_port = first.port
    second_port = second.port

    with first, second:
        assert first_port != second_port
        assert first.is_alive
        assert second.is_alive

    assert FIXTURE_REVIEW_STATUS == "待真机复核"
    assert not first.is_alive
    assert not second.is_alive
    for port in (first_port, second_port):
        with pytest.raises(OSError), socket.create_connection(("127.0.0.1", port), timeout=0.1):
            pass


def test_FX_RT_request_and_response_bodies_are_not_logged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sensitive_body = "HOLM-SENSITIVE-BODY-MUST-NOT-BE-LOGGED"
    with StubRuntime() as runtime:
        status, _, _ = _request(
            runtime,
            "POST",
            "/v1/chat/completions",
            {
                "model": runtime.config.model_id,
                "messages": [{"role": "user", "content": sensitive_body}],
            },
        )

    captured = capsys.readouterr()
    assert status == HTTPStatus.OK
    assert sensitive_body not in captured.out
    assert sensitive_body not in captured.err

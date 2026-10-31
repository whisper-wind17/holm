"""PERF-01 diagnostics used by the M4-14 performance harness."""

from __future__ import annotations

import http.client
import json

from fixtures.stub_runtime import RuntimeMode, StubRuntime, StubRuntimeConfig


def test_PERF_01_stub_reports_tcp_accepts_not_http_requests() -> None:
    config = StubRuntimeConfig(
        mode=RuntimeMode.FX_RT_03,
        completion_tokens=1,
        event_interval_seconds=0,
    )
    with StubRuntime(config) as runtime:
        connection = http.client.HTTPConnection(runtime.host, runtime.port, timeout=2.0)
        try:
            for _ in range(2):
                body = json.dumps({"model": "fixture", "messages": [], "stream": True}).encode(
                    "utf-8"
                )
                connection.request(
                    "POST",
                    "/v1/chat/completions",
                    body=body,
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                assert response.status == 200
                response.read()

            assert runtime.accepted_connection_count == 1
        finally:
            connection.close()

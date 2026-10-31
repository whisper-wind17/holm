"""FIX-06: Runtime readiness includes one direct, bounded inference warmup."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from collections import deque
from types import TracebackType
from typing import Self

import holm_instance_controller.health as health_module
import pytest
from holm_instance_controller import HttpRuntimeHealthProbe


class _Response:
    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self._body = body
        self.status = status

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback

    def read(self, amount: int) -> bytes:
        assert amount == 1024 * 1024 + 1
        return self._body


def test_FIX_06_TC_RT_01_ready_then_direct_warmup_is_bounded_and_not_logged(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    prompt_sentinel = "PWSSNTL-PROMPT-FIX06A-123456789ABC"
    output_sentinel = "PWSSNTL-OUTPUT-FIX06A-123456789ABC"
    responses = deque(
        (
            _Response(b"{}"),
            _Response(b'{"data":[{"id":"mi_fix_06"}]}'),
            _Response(
                json.dumps({"choices": [{"message": {"content": output_sentinel}}]}).encode()
            ),
        )
    )
    requests: list[tuple[urllib.request.Request, float]] = []

    def fake_urlopen(
        request: urllib.request.Request,
        *,
        timeout: float,
    ) -> _Response:
        requests.append((request, timeout))
        return responses.popleft()

    monkeypatch.setattr(health_module, "_WARMUP_PROMPT", prompt_sentinel)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    probe = HttpRuntimeHealthProbe(timeout_seconds=1.5)

    with caplog.at_level("INFO"):
        assert asyncio.run(probe.ready("127.0.0.1:8123", "mi_fix_06"))
        assert asyncio.run(probe.warmup("127.0.0.1:8123", "mi_fix_06"))

    assert [request.get_method() for request, _ in requests] == ["GET", "GET", "POST"]
    assert [request.full_url for request, _ in requests] == [
        "http://127.0.0.1:8123/health",
        "http://127.0.0.1:8123/v1/models",
        "http://127.0.0.1:8123/v1/chat/completions",
    ]
    assert [timeout for _, timeout in requests] == [1.5, 1.5, 1.5]
    warmup_request = requests[-1][0]
    assert warmup_request.get_header("Authorization") is None
    assert json.loads(warmup_request.data or b"") == {
        "model": "mi_fix_06",
        "messages": [{"role": "user", "content": prompt_sentinel}],
        "max_tokens": 1,
        "stream": False,
    }
    assert all(prompt_sentinel not in record.getMessage() for record in caplog.records)
    assert all(output_sentinel not in record.getMessage() for record in caplog.records)


def test_FIX_06_TC_RT_01_warmup_transport_failure_is_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_urlopen(
        request: urllib.request.Request,
        *,
        timeout: float,
    ) -> _Response:
        del request, timeout
        raise urllib.error.URLError("connection closed before response")

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)

    assert not asyncio.run(
        HttpRuntimeHealthProbe(timeout_seconds=1.5).warmup(
            "127.0.0.1:8123",
            "mi_fix_06",
        )
    )


def test_FIX_06_TC_RT_01_warmup_rejects_a_non_inference_success_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(
        request: urllib.request.Request,
        *,
        timeout: float,
    ) -> _Response:
        del request, timeout
        return _Response(b'{"choices":[]}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert not asyncio.run(
        HttpRuntimeHealthProbe(timeout_seconds=1.5).warmup(
            "127.0.0.1:8123",
            "mi_fix_06",
        )
    )

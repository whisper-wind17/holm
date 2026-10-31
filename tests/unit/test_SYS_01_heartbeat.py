"""FX-SYS-01: dependency failures expose only a stable heartbeat error code."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from holm_instance_controller.heartbeat import ControllerHeartbeat


def test_SYS_01_heartbeat_failure_is_sanitized_and_does_not_stop_loop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = 0
    stop = asyncio.Event()

    def unavailable_database() -> Any:
        nonlocal calls
        calls += 1
        stop.set()
        raise OSError("private-secret-path-sentinel")

    heartbeat = ControllerHeartbeat(
        cast(Any, unavailable_database), instance_id="FX-SYS-01", build_version="1.0.0"
    )
    with caplog.at_level("WARNING", logger="holm.instance_controller"):
        asyncio.run(heartbeat.run(stop))
    assert calls == 2  # Healthy attempt and final Stopping attempt.
    assert [getattr(record, "error_code", None) for record in caplog.records] == [
        "heartbeat_failed",
        "heartbeat_failed",
    ]
    assert "private-secret-path-sentinel" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize("interval", [0, -1, 11, float("nan"), float("inf")])
def test_SYS_01_invalid_heartbeat_interval_fails_loudly(interval: float) -> None:
    with pytest.raises(ValueError, match="invalid heartbeat period"):
        ControllerHeartbeat(
            cast(Any, object()),
            instance_id="FX-SYS-01",
            build_version="1.0.0",
            interval_seconds=interval,
        )

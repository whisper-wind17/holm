"""UI-13 / TC-TM-05: FX-UI-13 reuses M6-11 service samples, no request bodies."""

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from holm_control_api.attention_routes import AttentionItemResponse
from holm_control_api.attention_service import (
    AttentionItem,
    AttentionKind,
    AttentionService,
    AttentionSeverity,
)
from holm_control_api.system_status import (
    AgentAvailableResponse,
    GpuDeviceResponse,
    GpuDiscoveryFailureReason,
    GpuDiscoveryState,
    GpuHealthState,
    GpuInventoryResponse,
    HostInfoResponse,
    ObservationUnknownReason,
    SystemServerResponse,
    UnknownString,
    UnknownUInt64,
)
from holm_telemetry import TelemetryStatus, TelemetryStore
from test_M6_11_TC_TM_05_06_overview_response import _collector, _sample

NOW = datetime(2026, 10, 10, tzinfo=UTC)


def test_UI_13B_TC_TM_05_success_with_only_old_sample_is_zero() -> None:
    """FX-UI-13B reuses M6-11's sample, aged outside the one-hour window."""
    collector = _collector()
    old_sample = _sample(sampled_at=datetime.now(UTC) - timedelta(hours=2))

    async def scenario() -> None:
        with patch.object(
            collector, "_safe_service_samples", new=AsyncMock(return_value=(old_sample,))
        ):
            await collector.collect_once()
        values = collector.store.overview(now_monotonic=time.monotonic()).values
        assert values.requests_1h == 0
        assert values.error_rate_1h is None
        assert values.window_started_at is None

    asyncio.run(scenario())


def test_UI_13_TC_TM_05_empty_success_failure_and_first_sample() -> None:
    async def scenario() -> None:
        collector = _collector()
        assert collector.store.overview(now_monotonic=time.monotonic()).values.requests_1h is None
        for samples, expected in (
            (None, None),
            ((), 0),
            (None, None),
            ((_sample(sampled_at=datetime.now(UTC)),), None),
        ):
            with patch.object(
                collector, "_safe_service_samples", new=AsyncMock(return_value=samples)
            ):
                await collector.collect_once()
            values = collector.store.overview(now_monotonic=time.monotonic()).values
            assert values.requests_1h == expected
            assert values.error_rate_1h is None
        assert TelemetryStore().overview_request_window(sampled_at=NOW) == (None, None, None)

    asyncio.run(scenario())


def test_UI_13_attention_isolates_sources_sorts_all_and_never_logs_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = AttentionService(session_factory=AsyncMock())
    notices = tuple(
        AttentionItem(
            kind=AttentionKind.DOWNLOAD_FAILED, count=1, latest_at=NOW + timedelta(seconds=index)
        )
        for index in range(105)
    )
    negative = AttentionItem(
        kind=AttentionKind.INSTANCE_FAILED,
        count=1,
        latest_at=None,
        severity=AttentionSeverity.NEGATIVE,
    )
    newer = AttentionItem(
        kind=AttentionKind.INSTANCE_UNHEALTHY,
        count=1,
        latest_at=NOW,
        severity=AttentionSeverity.NEGATIVE,
    )
    with (
        patch.object(
            service,
            "_services",
            new=AsyncMock(side_effect=RuntimeError("FX-UI-13-private-path-body")),
        ),
        patch.object(service, "_instances", new=AsyncMock(return_value=(negative, newer))),
        patch.object(service, "_downloads", new=AsyncMock(return_value=notices)),
        patch.object(service, "_unknown_requests", new=AsyncMock(return_value=())),
        caplog.at_level(logging.WARNING),
    ):
        items = asyncio.run(service.items())
    assert items == (newer, negative, *reversed(notices))
    assert "FX-UI-13-private-path-body" not in caplog.text
    assert sum(record.message == "attention source unavailable" for record in caplog.records) == 1
    assert set(AttentionItemResponse.model_fields) == {
        "kind",
        "severity",
        "target_id",
        "target_name",
        "count",
        "latest_at",
        "detail",
    }


@pytest.mark.parametrize(
    "elapsed,status,present",
    [
        (10, TelemetryStatus.AVAILABLE, False),
        (20, TelemetryStatus.DELAYED, False),
        (90, TelemetryStatus.UNAVAILABLE, True),
    ],
)
def test_UI_13_telemetry_interrupted_only_unavailable(
    elapsed: int, status: TelemetryStatus, present: bool
) -> None:
    store = TelemetryStore()
    store.record_overview(
        store.overview(now_monotonic=0).values, sampled_at=NOW, observed_monotonic=0
    )
    snapshot = store.overview(now_monotonic=elapsed)
    assert snapshot.telemetry_status is status
    service = AttentionService(session_factory=AsyncMock(), telemetry_overview=lambda: snapshot)
    items = asyncio.run(service._telemetry())
    assert bool(items) is present
    if present:
        assert items == (
            AttentionItem(kind=AttentionKind.TELEMETRY_INTERRUPTED, count=1, latest_at=NOW),
        )


def test_UI_13_gpu_only_unhealthy_uses_uuid_and_display_index() -> None:
    unknown = UnknownUInt64(reason=ObservationUnknownReason.NOT_COLLECTED)
    unknown_string = UnknownString(reason=ObservationUnknownReason.NOT_COLLECTED)
    server = SystemServerResponse(
        observed_at=NOW,
        agent=AgentAvailableResponse(
            host=HostInfoResponse(
                logical_cpu_count=unknown,
                total_memory_bytes=unknown,
                total_disk_bytes=unknown,
                available_disk_bytes=unknown,
                driver_version=unknown_string,
                cuda_driver_version=unknown_string,
            ),
            gpus=GpuInventoryResponse(
                discovery_state=GpuDiscoveryState.READY,
                failure_reason=GpuDiscoveryFailureReason.NONE,
                devices=tuple(
                    GpuDeviceResponse(
                        uuid=f"GPU-fx-{index}",
                        display_index=index,
                        model="FX-UI-13",
                        memory_bytes=1,
                        health=health,
                        health_reasons=(),
                    )
                    for index, health in enumerate(GpuHealthState)
                ),
                topology_links=(),
            ),
        ),
    )
    service = AttentionService(
        session_factory=AsyncMock(), system_server=AsyncMock(return_value=server)
    )
    items = asyncio.run(service._gpus())
    assert items == (
        AttentionItem(
            kind=AttentionKind.GPU_UNHEALTHY,
            severity=AttentionSeverity.NEGATIVE,
            target_id="GPU-fx-1",
            target_name="GPU 1",
            count=1,
            latest_at=None,
        ),
    )

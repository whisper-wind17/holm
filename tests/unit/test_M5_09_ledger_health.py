"""M5-09 TDD 14.6 backlog thresholds, disk protection, and the admin mapping."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

import pytest
from holm_contracts import VersionPayload
from holm_control_api.agent_client import AgentUnavailable, AgentUnavailableReason
from holm_control_api.system_status import (
    COMPONENT_SIGNAL_RULES,
    ComponentSignalSource,
    SystemComponentId,
    SystemComponentReason,
    SystemComponentStatus,
    SystemStatusService,
    ledger_backlog_component_state,
)
from holm_domain import (
    LEDGER_DELAYED_AFTER_SECONDS,
    LEDGER_DISK_CRITICAL_FREE_BYTES,
    LEDGER_DISK_CRITICAL_FREE_RATIO,
    LEDGER_UNAVAILABLE_AFTER_SECONDS,
    Component,
    ComponentStatus,
    LedgerHealth,
    classify_backlog_age,
    decode_ledger_backlog_reason,
    encode_ledger_backlog_reason,
    is_disk_critical,
    worst_ledger_health,
)
from holm_ledger import (
    LedgerHealthMonitor,
    LedgerStorageError,
    SpoolWriterState,
    probe_ledger_disk,
)
from holm_persistence import LatestHeartbeat, SystemStatusSnapshot

GIB = 1024 * 1024 * 1024
NOW = datetime(2026, 9, 18, 10, 0, tzinfo=UTC)


@dataclass
class _FakeWriter:
    state: SpoolWriterState = SpoolWriterState.AVAILABLE


class _FakeSource:
    """A backlog source whose answers and failures the test controls."""

    def __init__(self, oldest: datetime | None = None) -> None:
        self.oldest = oldest
        self.calls = 0
        self.fail = False

    def oldest_unacked_created_at(self) -> datetime | None:
        self.calls += 1
        if self.fail:
            raise LedgerStorageError("probe failed")
        return self.oldest

    def close(self) -> None:
        return None


class _ManualClock:
    def __init__(self) -> None:
        self.monotonic = 1000.0
        self.wall = NOW

    def advance(self, seconds: float) -> None:
        self.monotonic += seconds
        self.wall += timedelta(seconds=seconds)


def _monitor(
    tmp_path: Path,
    *,
    source: _FakeSource,
    clock: _ManualClock,
    writer: _FakeWriter | None = None,
    free_bytes: int = 500 * GIB,
    total_bytes: int = 1000 * GIB,
    delayed_after_seconds: float = LEDGER_DELAYED_AFTER_SECONDS,
    unavailable_after_seconds: float = LEDGER_UNAVAILABLE_AFTER_SECONDS,
) -> LedgerHealthMonitor:
    return LedgerHealthMonitor(
        tmp_path / "request-ledger.sqlite3",
        writer=cast(Any, writer),
        delayed_after_seconds=delayed_after_seconds,
        unavailable_after_seconds=unavailable_after_seconds,
        interval_seconds=1.0,
        clock=lambda: clock.monotonic,
        utc_now=lambda: clock.wall,
        disk_usage=lambda _path: (total_bytes, total_bytes - free_bytes, free_bytes),
        source_factory=lambda: source,
    )


@pytest.mark.parametrize(
    ("age_seconds", "expected"),
    [
        (None, LedgerHealth.AVAILABLE),
        (0.0, LedgerHealth.AVAILABLE),
        (29.0, LedgerHealth.AVAILABLE),
        (30.0, LedgerHealth.AVAILABLE),
        (31.0, LedgerHealth.DELAYED),
        (299.0, LedgerHealth.DELAYED),
        (300.0, LedgerHealth.DELAYED),
        (301.0, LedgerHealth.UNAVAILABLE),
    ],
)
def test_M5_09_backlog_age_boundaries_follow_tdd_14_6(
    age_seconds: float | None, expected: LedgerHealth
) -> None:
    assert LEDGER_DELAYED_AFTER_SECONDS == 30.0
    assert LEDGER_UNAVAILABLE_AFTER_SECONDS == 300.0
    assert classify_backlog_age(age_seconds) is expected


def test_M5_09_backlog_thresholds_must_be_ordered_and_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        classify_backlog_age(1.0, delayed_after=0.0)
    with pytest.raises(ValueError, match="must not exceed"):
        classify_backlog_age(1.0, delayed_after=10.0, unavailable_after=5.0)


@pytest.mark.parametrize(
    ("total_bytes", "free_bytes", "expected"),
    [
        # Below 5 percent while still far above 5 GiB.
        (1000 * GIB, 40 * GIB, True),
        # Below 5 GiB while still far above 5 percent.
        (20 * GIB, 4 * GIB, True),
        # Neither condition triggers.
        (1000 * GIB, 100 * GIB, False),
        # Exactly at both boundaries is not yet critical.
        (1000 * GIB, 50 * GIB, False),
    ],
)
def test_M5_09_disk_critical_takes_whichever_condition_triggers_first(
    total_bytes: int, free_bytes: int, expected: bool
) -> None:
    assert LEDGER_DISK_CRITICAL_FREE_RATIO == 0.05
    assert LEDGER_DISK_CRITICAL_FREE_BYTES == 5 * GIB
    assert is_disk_critical(total_bytes=total_bytes, free_bytes=free_bytes) is expected


def test_M5_09_disk_probe_measures_the_directory_not_the_missing_file(tmp_path: Path) -> None:
    observed: list[Path] = []

    def usage(path: Any) -> tuple[int, int, int]:
        observed.append(Path(path))
        return (20 * GIB, 17 * GIB, 3 * GIB)

    status = probe_ledger_disk(tmp_path / "spool" / "request-ledger.sqlite3", disk_usage=usage)
    assert observed == [(tmp_path / "spool").absolute()]
    assert (status.total_bytes, status.free_bytes, status.critical) == (20 * GIB, 3 * GIB, True)


def test_M5_09_monitor_classifies_the_sampled_backlog_age(tmp_path: Path) -> None:
    clock = _ManualClock()
    source = _FakeSource(oldest=NOW - timedelta(seconds=10))
    monitor = _monitor(tmp_path, source=source, clock=clock)

    asyncio.run(monitor.sample_once())
    assert monitor.health is LedgerHealth.AVAILABLE

    source.oldest = clock.wall - timedelta(seconds=45)
    asyncio.run(monitor.sample_once())
    assert monitor.health is LedgerHealth.DELAYED

    source.oldest = clock.wall - timedelta(seconds=600)
    asyncio.run(monitor.sample_once())
    assert monitor.health is LedgerHealth.UNAVAILABLE


def test_M5_09_monitor_ages_its_last_sample_with_the_monotonic_clock(tmp_path: Path) -> None:
    clock = _ManualClock()
    source = _FakeSource(oldest=NOW - timedelta(seconds=25))
    monitor = _monitor(tmp_path, source=source, clock=clock)
    asyncio.run(monitor.sample_once())
    assert monitor.health is LedgerHealth.AVAILABLE

    # No new sample: the known backlog keeps ageing instead of staying fresh.
    clock.advance(10)
    assert monitor.health is LedgerHealth.DELAYED
    assert source.calls == 1


def test_M5_09_monitor_escalates_when_the_ledger_cannot_be_observed(tmp_path: Path) -> None:
    clock = _ManualClock()
    source = _FakeSource()
    monitor = _monitor(tmp_path, source=source, clock=clock)
    asyncio.run(monitor.sample_once())
    assert monitor.health is LedgerHealth.AVAILABLE

    source.fail = True
    clock.advance(120)
    asyncio.run(monitor.sample_once())
    assert monitor.health is LedgerHealth.AVAILABLE

    clock.advance(200)
    asyncio.run(monitor.sample_once())
    assert monitor.health is LedgerHealth.UNAVAILABLE


def test_M5_09_monitor_reports_unavailable_while_the_spool_writer_is(tmp_path: Path) -> None:
    clock = _ManualClock()
    source = _FakeSource()
    writer = _FakeWriter()
    monitor = _monitor(tmp_path, source=source, clock=clock, writer=writer)
    asyncio.run(monitor.sample_once())
    assert monitor.health is LedgerHealth.AVAILABLE

    writer.state = SpoolWriterState.UNAVAILABLE
    assert monitor.health is LedgerHealth.UNAVAILABLE

    writer.state = SpoolWriterState.AVAILABLE
    assert monitor.health is LedgerHealth.AVAILABLE


def test_M5_09_monitor_reports_unavailable_on_a_critical_disk(tmp_path: Path) -> None:
    clock = _ManualClock()
    source = _FakeSource()
    monitor = _monitor(
        tmp_path,
        source=source,
        clock=clock,
        total_bytes=20 * GIB,
        free_bytes=4 * GIB,
    )
    asyncio.run(monitor.sample_once())
    assert monitor.health is LedgerHealth.UNAVAILABLE


def test_M5_09_health_reads_never_touch_storage(tmp_path: Path) -> None:
    clock = _ManualClock()
    source = _FakeSource(oldest=NOW - timedelta(seconds=5))
    monitor = _monitor(tmp_path, source=source, clock=clock)
    asyncio.run(monitor.sample_once())
    assert source.calls == 1

    for _ in range(100):
        assert monitor.health is LedgerHealth.AVAILABLE
    assert source.calls == 1


def test_M5_09_terminal_write_failure_latches_unavailable(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    clock = _ManualClock()
    source = _FakeSource()
    monitor = _monitor(tmp_path, source=source, clock=clock)
    asyncio.run(monitor.sample_once())
    assert monitor.health is LedgerHealth.AVAILABLE

    with caplog.at_level("CRITICAL", logger="holm.ledger.health"):
        monitor.latch_unavailable("ledger_terminal_write_failed")
    emitted = [record for record in caplog.records if record.name == "holm.ledger.health"]
    assert [record.levelname for record in emitted] == ["CRITICAL"]
    assert getattr(emitted[0], "error_code", None) == "ledger_terminal_write_failed"
    assert monitor.health is LedgerHealth.UNAVAILABLE

    # A later healthy sample must not silently reopen admission.
    asyncio.run(monitor.sample_once())
    assert monitor.health is LedgerHealth.UNAVAILABLE


def test_M5_09_worst_health_wins_across_independent_signals() -> None:
    assert worst_ledger_health() is LedgerHealth.AVAILABLE
    assert worst_ledger_health(LedgerHealth.AVAILABLE, LedgerHealth.DELAYED) is LedgerHealth.DELAYED
    assert (
        worst_ledger_health(LedgerHealth.DELAYED, LedgerHealth.UNAVAILABLE)
        is LedgerHealth.UNAVAILABLE
    )


@pytest.mark.parametrize("health", list(LedgerHealth))
def test_M5_09_heartbeat_reason_round_trips_every_state(health: LedgerHealth) -> None:
    assert decode_ledger_backlog_reason(encode_ledger_backlog_reason(health)) is health


@pytest.mark.parametrize("reason", [None, "", "scheduler_backlog", "ledger_backlog=nonsense"])
def test_M5_09_unknown_heartbeat_reasons_are_not_a_backlog_signal(reason: str | None) -> None:
    assert decode_ledger_backlog_reason(reason) is None


@pytest.mark.parametrize(
    ("backlog", "expected_status", "expected_reason"),
    [
        (
            LedgerHealth.AVAILABLE,
            SystemComponentStatus.READY,
            SystemComponentReason.HEARTBEAT_FRESH,
        ),
        (
            LedgerHealth.DELAYED,
            SystemComponentStatus.DEGRADED,
            SystemComponentReason.LEDGER_BACKLOG,
        ),
        (
            LedgerHealth.UNAVAILABLE,
            SystemComponentStatus.UNAVAILABLE,
            SystemComponentReason.LEDGER_UNAVAILABLE,
        ),
        (None, SystemComponentStatus.READY, SystemComponentReason.HEARTBEAT_FRESH),
    ],
)
def test_M5_09_admin_component_mapping_covers_all_three_states(
    backlog: LedgerHealth | None,
    expected_status: SystemComponentStatus,
    expected_reason: SystemComponentReason,
) -> None:
    status, reason = ledger_backlog_component_state(
        SystemComponentStatus.READY,
        SystemComponentReason.HEARTBEAT_FRESH,
        backlog,
    )
    assert (status, reason) == (expected_status, expected_reason)


def test_M5_09_backlog_signal_never_masks_an_unavailable_dependency() -> None:
    status, reason = ledger_backlog_component_state(
        SystemComponentStatus.UNAVAILABLE,
        SystemComponentReason.SIGNAL_UNAVAILABLE,
        LedgerHealth.DELAYED,
    )
    assert (status, reason) == (
        SystemComponentStatus.UNAVAILABLE,
        SystemComponentReason.SIGNAL_UNAVAILABLE,
    )


def test_M5_09_ledger_writer_component_declares_the_backlog_signal_source() -> None:
    rule = COMPONENT_SIGNAL_RULES[SystemComponentId.REQUEST_LEDGER_WRITER]
    assert ComponentSignalSource.LEDGER_BACKLOG in rule.sources
    assert ComponentSignalSource.HEARTBEAT in rule.sources


def test_M5_09_control_api_local_probe_stays_current_when_writer_heartbeat_does_not() -> None:
    class _Agent:
        async def observe(self) -> AgentUnavailable:
            return AgentUnavailable(AgentUnavailableReason.CONNECTION_FAILED)

    class _Database:
        async def read(self) -> SystemStatusSnapshot:
            return SystemStatusSnapshot(
                postgres_version="16.15",
                heartbeats=MappingProxyType(
                    {
                        Component.LEDGER_WRITER: LatestHeartbeat(
                            component=Component.LEDGER_WRITER,
                            instance_id="m5-09-writer",
                            version="0.0.0+m509",
                            status=ComponentStatus.HEALTHY,
                            reason=encode_ledger_backlog_reason(LedgerHealth.AVAILABLE),
                            age_seconds=10.0,
                        )
                    }
                ),
                oldest_executable_job_age_seconds=None,
                expired_lease_count=0,
                running_job_count=0,
            )

    service = SystemStatusService(
        agent=_Agent(),
        database=_Database(),
        version=VersionPayload(
            build_version="0.0.0+m509",
            commit="5" * 40,
            schema_version=1,
            runtime_compat_hash="9" * 64,
        ),
        # The writer has stopped, so its last fresh heartbeat still says
        # Available while the Control API's read-only Ledger probe sees delay.
        ledger_health=lambda: LedgerHealth.DELAYED,
    )
    response = asyncio.run(service.read_components())
    component = next(
        item for item in response.components if item.id is SystemComponentId.REQUEST_LEDGER_WRITER
    )
    assert component.status is SystemComponentStatus.DEGRADED
    assert component.reason is SystemComponentReason.LEDGER_BACKLOG

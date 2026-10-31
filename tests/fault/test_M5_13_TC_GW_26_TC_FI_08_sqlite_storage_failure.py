"""TC-GW-26／TC-FI-08：SQLite 写入失败（Full／CANTOPEN）下的账本保护（M5-13，A 档骨架）。

真实 `fsync(2)` 的 EIO 无法用标准库注入，TST 3.6「已知替代」把 `FX-SQ-04` 的
`SQLITE_FULL` 与 `SQLITE_CANTOPEN` 定为替代形态——本卡沿用，**不要**改用 Mock 掉写入。
可复用的注入器已存在：`tests/fixtures/sqlite_ledger/faults.py`
（`exhaust_database_capacity`、`block_wal_path`）。

夹具需额外提供：``exhaust_ledger_storage()``、``block_ledger_wal()``、``restore_ledger_storage()``、
``fail_terminal_writes()``、``runtime_request_count()``、``accepts_new_connections()``、
``admin_signals()``、``open_streaming_call(sentinel=...)``。
"""

from __future__ import annotations

from typing import Final

from fault.judgement import (
    STORAGE_UNAVAILABLE,
    assert_call_succeeded,
    assert_no_unacked_event_lost,
    assert_platform_error,
    assert_scan_detects_positive_control,
    assert_zero_body,
    load_harness,
)

SENTINEL: Final[str] = "HOLM-SENTINEL-M5-13-TC-GW-26"


def test_M5_13_TC_GW_26_TC_FI_08_full_ledger_rejects_before_the_runtime_is_contacted() -> None:
    """账本不可写时，Runtime 必须一个新请求都收不到，客户端拿 503 usage_storage_unavailable。"""
    harness = load_harness()
    with harness.fault_stack() as stack:
        assert_call_succeeded(stack.call(sentinel=SENTINEL))
        before = stack.runtime_request_count()

        stack.exhaust_ledger_storage()
        assert_platform_error(stack.call(sentinel=SENTINEL), STORAGE_UNAVAILABLE)
        assert stack.runtime_request_count() == before, "账本不可写期间 Runtime 不得收到新请求"


def test_M5_13_TC_GW_26_TC_FI_08_blocked_wal_is_also_a_storage_outage() -> None:
    """WAL 无法打开（`SQLITE_CANTOPEN`）与容量耗尽（`SQLITE_FULL`）必须收敛到同一保护路径。"""
    harness = load_harness()
    with harness.fault_stack() as stack:
        stack.block_ledger_wal()
        assert_platform_error(stack.call(sentinel=SENTINEL), STORAGE_UNAVAILABLE)


def test_M5_13_TC_GW_26_TC_FI_08_in_flight_calls_keep_writing_their_terminal_event() -> None:
    """保留既有连接的终态写入能力：正在流式的请求不因新请求被拒而丢终态。"""
    harness = load_harness()
    with harness.fault_stack() as stack:
        in_flight = stack.open_streaming_call(sentinel=SENTINEL)
        stack.exhaust_ledger_storage()
        assert_platform_error(stack.call(sentinel=SENTINEL), STORAGE_UNAVAILABLE)
        assert in_flight.finish().terminal_event_written, "既有连接的终态事件必须仍然落盘"


def test_M5_13_TC_GW_26_TC_FI_08_terminal_write_failure_stops_accepting_connections() -> None:
    """连终态都写不下去时：停止接受新连接，并在管理员侧给出不可用级信号（TDD 18.5）。"""
    harness = load_harness()
    with harness.fault_stack() as stack:
        stack.fail_terminal_writes()
        stack.call(sentinel=SENTINEL)
        assert not stack.accepts_new_connections(), "终态不可写时必须停止接受新连接"
        signals = stack.admin_signals()
        assert signals, "必须在管理员可见面上给出信号，不能只写进进程日志"
        assert any("unavailable" in str(signal).lower() for signal in signals), (
            f"管理员侧必须出现不可用级信号，实际是 {signals}"
        )


def test_M5_13_TC_GW_26_TC_FI_08_recovery_loses_no_unacked_event() -> None:
    harness = load_harness()
    with harness.fault_stack() as stack:
        assert_call_succeeded(stack.call(sentinel=SENTINEL))
        before = stack.unacked_event_ids()
        stack.exhaust_ledger_storage()
        stack.call(sentinel=SENTINEL)
        stack.restore_ledger_storage()
        assert_no_unacked_event_lost(before, stack.unacked_event_ids())


def test_M5_13_TC_GW_26_TC_FI_08_storage_failure_writes_no_request_body() -> None:
    harness = load_harness()
    with harness.fault_stack() as stack:
        stack.call(sentinel=SENTINEL)
        stack.exhaust_ledger_storage()
        stack.call(sentinel=SENTINEL)
        stack.restore_ledger_storage()
        stack.drain_ledger_writer()
        assert_zero_body(stack.scan_for_sentinel(SENTINEL), SENTINEL)


def test_M5_13_the_sentinel_scan_finds_a_planted_body() -> None:
    """阳性对照（AGENTS 第 10 章）：扫描器必须能找到故意写入的正文，

    否则上面每一条「零命中」都只是「没有发现问题的能力」。
    """
    harness = load_harness()
    planted = f"{SENTINEL}-POSITIVE-CONTROL"
    with harness.fault_stack() as stack:
        stack.plant_sentinel_positive_control(planted)
        assert_scan_detects_positive_control(stack.scan_for_sentinel(planted), planted)

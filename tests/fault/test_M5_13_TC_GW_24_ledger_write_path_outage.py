"""TC-GW-24：账本写路径中断 3 分钟，控制面仍能刷新 Snapshot（M5-13，A 档骨架）。

判定（TDD 18.5 第 5 条）：WAL 容量范围内调用继续；管理员显示入账延迟；
恢复后 Usage 完整且无重复。

中断时长用注入时钟推进，**不要真睡 180 秒**；账本三态阈值（30／300 秒）由
`assert_product_defaults_unchanged` 钉住默认值，测试内允许调小。

夹具需额外提供：``stop_ledger_writer()``、``start_ledger_writer()``、``drain_ledger_writer()``、
``ledger_health()``、``admin_signals()``、``wal_event_count()``。
"""

from __future__ import annotations

from typing import Final

from fault.judgement import (
    WRITE_PATH_OUTAGE_SECONDS,
    assert_call_succeeded,
    assert_product_defaults_unchanged,
    assert_usage_complete,
    assert_usage_unchanged,
    assert_zero_body,
    load_harness,
)

SENTINEL: Final[str] = "HOLM-SENTINEL-M5-13-TC-GW-24"
CALLS_DURING_OUTAGE: Final[int] = 3


def test_M5_13_TC_GW_24_product_defaults_are_not_weakened_by_the_suite() -> None:
    assert_product_defaults_unchanged()


def test_M5_13_TC_GW_24_calls_continue_while_the_writer_is_down() -> None:
    """Ledger Writer 停了不等于账本不可写：WAL 容量范围内调用必须继续成功。"""
    harness = load_harness()
    with harness.fault_stack() as stack:
        stack.stop_ledger_writer()
        stack.advance_clock(WRITE_PATH_OUTAGE_SECONDS)
        for _ in range(CALLS_DURING_OUTAGE):
            assert_call_succeeded(stack.call(sentinel=SENTINEL))
        assert stack.wal_event_count() > 0, "中断期间的事件必须留在本地 WAL 里等回放"


def test_M5_13_TC_GW_24_admin_sees_the_accounting_delay() -> None:
    """管理员必须看得到「入账延迟」，而不是以为一切正常（TDD 14.6 三态）。"""
    harness = load_harness()
    with harness.fault_stack(ledger_delayed_after_seconds=1.0) as stack:
        assert_call_succeeded(stack.call(sentinel=SENTINEL))
        stack.stop_ledger_writer()
        stack.advance_clock(WRITE_PATH_OUTAGE_SECONDS)
        stack.call(sentinel=SENTINEL)
        assert str(stack.ledger_health()) != "available", (
            f"写路径中断 {WRITE_PATH_OUTAGE_SECONDS} 秒后账本状态不能仍是 available"
        )
        assert stack.admin_signals(), "账本延迟必须出现在管理员可见面上"


def test_M5_13_TC_GW_24_usage_is_complete_and_not_double_counted_after_recovery() -> None:
    harness = load_harness()
    with harness.fault_stack() as stack:
        stack.stop_ledger_writer()
        stack.advance_clock(WRITE_PATH_OUTAGE_SECONDS)
        for _ in range(CALLS_DURING_OUTAGE):
            assert_call_succeeded(stack.call(sentinel=SENTINEL))

        stack.start_ledger_writer()
        stack.drain_ledger_writer()
        totals = stack.usage_totals()
        assert_usage_complete(totals, expected_requests=CALLS_DURING_OUTAGE)

        stack.drain_ledger_writer()
        assert_usage_unchanged(totals, stack.usage_totals())


def test_M5_13_TC_GW_24_the_outage_writes_no_request_body() -> None:
    harness = load_harness()
    with harness.fault_stack() as stack:
        stack.stop_ledger_writer()
        stack.advance_clock(WRITE_PATH_OUTAGE_SECONDS)
        stack.call(sentinel=SENTINEL)
        stack.start_ledger_writer()
        stack.drain_ledger_writer()
        assert_zero_body(stack.scan_for_sentinel(SENTINEL), SENTINEL)

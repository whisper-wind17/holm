"""TC-GW-27／TC-FI-04：Ledger Writer 在 PostgreSQL Commit 与 SQLite Ack 之间被 Kill。

判定（TDD 18.5、TST 7.2）：恢复后重复 Upsert 结果不变，无重复 Requests／Tokens，
未 Ack 事件不丢失。这是**已观测过的关键点位**，用确定性注入排出交错顺序，
不靠随机调度，也不允许给测试加重试。

夹具需额外提供：``kill_writer_between_commit_and_ack()``、``restart_ledger_writer()``。
"""

from __future__ import annotations

from typing import Final

from fault.judgement import (
    assert_call_succeeded,
    assert_no_unacked_event_lost,
    assert_usage_complete,
    assert_usage_unchanged,
    assert_zero_body,
    load_harness,
)

SENTINEL: Final[str] = "HOLM-SENTINEL-M5-13-TC-GW-27"


def test_M5_13_TC_GW_27_TC_FI_04_kill_between_commit_and_ack_does_not_duplicate_usage() -> None:
    harness = load_harness()
    with harness.fault_stack() as stack:
        assert_call_succeeded(stack.call(sentinel=SENTINEL))

        stack.kill_writer_between_commit_and_ack()
        committed = stack.usage_totals()
        assert_usage_complete(committed, expected_requests=1)

        stack.restart_ledger_writer()
        stack.drain_ledger_writer()
        assert_usage_unchanged(committed, stack.usage_totals())


def test_M5_13_TC_GW_27_TC_FI_04_unacked_events_survive_the_kill() -> None:
    """Commit 已成功但 Ack 未落盘的事件必须还在，重放一次后才允许被 Ack 清理。"""
    harness = load_harness()
    with harness.fault_stack() as stack:
        assert_call_succeeded(stack.call(sentinel=SENTINEL))
        before = stack.unacked_event_ids()
        stack.kill_writer_between_commit_and_ack()
        assert_no_unacked_event_lost(before, stack.unacked_event_ids())


def test_M5_13_TC_GW_27_TC_FI_04_repeated_replay_is_idempotent() -> None:
    """同一批事件回放任意次，Request／Token 结果不变（TDD 18.2 不变量 12）。"""
    harness = load_harness()
    with harness.fault_stack() as stack:
        assert_call_succeeded(stack.call(sentinel=SENTINEL))
        stack.kill_writer_between_commit_and_ack()
        stack.restart_ledger_writer()
        stack.drain_ledger_writer()
        first = stack.usage_totals()
        for _ in range(3):
            stack.replay_unacked_events()
            assert_usage_unchanged(first, stack.usage_totals())


def test_M5_13_TC_GW_27_TC_FI_04_kill_path_writes_no_request_body() -> None:
    harness = load_harness()
    with harness.fault_stack() as stack:
        stack.call(sentinel=SENTINEL)
        stack.kill_writer_between_commit_and_ack()
        stack.restart_ledger_writer()
        stack.drain_ledger_writer()
        assert_zero_body(stack.scan_for_sentinel(SENTINEL), SENTINEL)

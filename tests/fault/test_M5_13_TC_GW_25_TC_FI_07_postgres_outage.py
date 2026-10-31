"""TC-GW-25／TC-FI-07：PostgreSQL 整体中断与恢复（M5-13，A 档骨架）。

依赖 `tests/fault/harness.py`；缺失时必须明确失败，不能绕过断言。

需要的夹具（`tests/fault/harness.py`）：

* ``fault_stack(**overrides)``：上下文管理器，起一套真实栈——真实临时 PostgreSQL 集群
  （复用 `tests/integration/conftest.py` 的引导逻辑）、真实磁盘 SQLite 账本、Ledger Writer、
  `FX-RT-03` Stub Runtime 与 Gateway；`overrides` 至少允许调小
  ``route_snapshot_max_age_seconds``，用来避免真等 60 秒；
* 句柄方法：``call(sentinel=...)``、``stop_postgres()``、``start_postgres()``、
  ``wait_until_ready(timeout)``、``usage_totals()``、``unacked_event_ids()``、
  ``scan_for_sentinel(sentinel)``、``plant_sentinel_positive_control(sentinel)``；
* ``CallOutcome``：带 ``status_code``、``error_code``、``request_id``、``message``。

夹具 API 的名字可以调整（要在 dispatch note 写明），断言必须逐字成立。
"""

from __future__ import annotations

from typing import Final

from fault.judgement import (
    CONTROL_PLANE_UNAVAILABLE,
    READY_WITHIN_SECONDS,
    assert_call_succeeded,
    assert_platform_error,
    assert_product_defaults_unchanged,
    assert_usage_complete,
    assert_usage_unchanged,
    assert_zero_body,
    load_harness,
)

#: 缩短后的 Snapshot 有效期：判定语义不变，只是不必真等 60 秒（默认值另有断言钉住）。
SHORT_SNAPSHOT_MAX_AGE_SECONDS: Final[float] = 2.0
SENTINEL: Final[str] = "HOLM-SENTINEL-M5-13-TC-GW-25"


def test_M5_13_TC_GW_25_product_defaults_are_not_weakened_by_the_suite() -> None:
    assert_product_defaults_unchanged()


def test_M5_13_TC_GW_25_TC_FI_07_fresh_snapshot_keeps_serving_calls() -> None:
    """快照年龄在安全有效期内：数据库停了也照样能调用（TDD 18.5 第 6 条）。"""
    harness = load_harness()
    with harness.fault_stack(
        route_snapshot_max_age_seconds=SHORT_SNAPSHOT_MAX_AGE_SECONDS
    ) as stack:
        assert_call_succeeded(stack.call(sentinel=SENTINEL))
        stack.stop_postgres()
        assert_call_succeeded(stack.call(sentinel=SENTINEL))


def test_M5_13_TC_GW_25_TC_FI_07_stale_snapshot_returns_control_plane_unavailable() -> None:
    """超过安全有效期后必须快速失败为 `control_plane_unavailable`，不得继续用过期路由。"""
    harness = load_harness()
    with harness.fault_stack(
        route_snapshot_max_age_seconds=SHORT_SNAPSHOT_MAX_AGE_SECONDS
    ) as stack:
        assert_call_succeeded(stack.call(sentinel=SENTINEL))
        stack.stop_postgres()
        stack.advance_clock(SHORT_SNAPSHOT_MAX_AGE_SECONDS * 2)
        assert_platform_error(stack.call(sentinel=SENTINEL), CONTROL_PLANE_UNAVAILABLE)


def test_M5_13_TC_GW_25_TC_FI_07_recovery_restores_ready_and_replays_the_wal() -> None:
    """恢复后 15 秒内重建就绪与路由；WAL 完整回放，Usage 完整且不重复计量。"""
    harness = load_harness()
    with harness.fault_stack(
        route_snapshot_max_age_seconds=SHORT_SNAPSHOT_MAX_AGE_SECONDS
    ) as stack:
        assert_call_succeeded(stack.call(sentinel=SENTINEL))
        stack.stop_postgres()
        assert_call_succeeded(stack.call(sentinel=SENTINEL))
        stack.advance_clock(SHORT_SNAPSHOT_MAX_AGE_SECONDS * 2)
        assert_platform_error(stack.call(sentinel=SENTINEL), CONTROL_PLANE_UNAVAILABLE)

        stack.start_postgres()
        assert stack.wait_until_ready(timeout=READY_WITHIN_SECONDS), (
            f"恢复后必须在 {READY_WITHIN_SECONDS} 秒内重新就绪并重建 Route Snapshot"
        )
        stack.drain_ledger_writer()

        totals = stack.usage_totals()
        assert_usage_complete(totals, expected_requests=2)
        stack.drain_ledger_writer()
        assert_usage_unchanged(totals, stack.usage_totals())


def test_M5_13_TC_GW_25_TC_FI_07_outage_and_recovery_write_no_request_body() -> None:
    harness = load_harness()
    with harness.fault_stack(
        route_snapshot_max_age_seconds=SHORT_SNAPSHOT_MAX_AGE_SECONDS
    ) as stack:
        assert_call_succeeded(stack.call(sentinel=SENTINEL))
        stack.stop_postgres()
        stack.call(sentinel=SENTINEL)
        stack.advance_clock(SHORT_SNAPSHOT_MAX_AGE_SECONDS * 2)
        stack.call(sentinel=SENTINEL)
        stack.start_postgres()
        stack.wait_until_ready(timeout=READY_WITHIN_SECONDS)
        stack.drain_ledger_writer()
        assert_zero_body(stack.scan_for_sentinel(SENTINEL), SENTINEL)

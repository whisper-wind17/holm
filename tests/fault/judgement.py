"""M5-13 的判定标准：TST 7.2（`TC-FI-04/07/08`）与 TST 5.3（`TC-GW-24～27`），已冻结。

本模块属于 TST 1.5 的 **A 档**产出（开发计划 2.5 把 M5-13 列为高风险卡）：
时限、默认值与判定逻辑固定，故障注入实现不得放宽这些要求。
故障注入机制位于 `tests/fault/harness.py`。

时限只有一个来源：TDD 2.2「故障恢复」行与 18.7 末条；错误码取自 TDD 12.8 冻结表。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from holm_domain import ErrorCode

#: TDD 2.2：依赖健康时控制组件重启后 15 秒内恢复就绪与路由。
READY_WITHIN_SECONDS: Final[float] = 15.0

#: TDD 2.2：60 秒内完成任务、容器、GPU 与不超过 10,000 条未确认事件的账本对账。
RECONCILE_WITHIN_SECONDS: Final[float] = 60.0
RECONCILE_UNACKED_EVENT_BUDGET: Final[int] = 10_000

#: TDD 12.8／18.5：Route Snapshot 年龄不超过安全有效期时继续调用，
#: 超过后返回 control_plane_unavailable。
#: 这里冻结的是**产品默认值**；测试可以把配置调小来避免真等 60 秒，但默认值本身必须仍是 60 秒。
SNAPSHOT_MAX_AGE_DEFAULT_SECONDS: Final[float] = 60.0

#: TDD 14.6：账本积压 30 秒转 Delayed、300 秒转 Unavailable（同样只允许调小配置，不允许改默认值）。
LEDGER_DELAYED_AFTER_DEFAULT_SECONDS: Final[float] = 30.0
LEDGER_UNAVAILABLE_AFTER_DEFAULT_SECONDS: Final[float] = 300.0

#: TST TC-GW-24：账本写路径中断 3 分钟，WAL 容量范围内调用继续。
WRITE_PATH_OUTAGE_SECONDS: Final[float] = 180.0

STORAGE_UNAVAILABLE: Final[ErrorCode] = ErrorCode.USAGE_STORAGE_UNAVAILABLE
CONTROL_PLANE_UNAVAILABLE: Final[ErrorCode] = ErrorCode.CONTROL_PLANE_UNAVAILABLE


def assert_product_defaults_unchanged() -> None:
    """守卫：故障套件靠调小配置换取确定性，所以必须同时钉住产品默认值。

    少了这一条，把 `route_snapshot_max_age_seconds` 默认值从 60 秒改成 1 秒
    也能让整套故障测试继续绿，判定就失去了意义。
    """
    from holm_domain import (
        LEDGER_DELAYED_AFTER_SECONDS,
        LEDGER_UNAVAILABLE_AFTER_SECONDS,
    )
    from holm_gateway.configuration import GatewayConfiguration

    defaults = GatewayConfiguration()
    if defaults.route_snapshot_max_age_seconds != SNAPSHOT_MAX_AGE_DEFAULT_SECONDS:
        raise AssertionError(
            "Route Snapshot 安全有效期默认值必须仍是 "
            f"{SNAPSHOT_MAX_AGE_DEFAULT_SECONDS} 秒，实际是 "
            f"{defaults.route_snapshot_max_age_seconds}"
        )
    if LEDGER_DELAYED_AFTER_SECONDS != LEDGER_DELAYED_AFTER_DEFAULT_SECONDS:
        raise AssertionError(f"账本 Delayed 阈值默认值被改动：{LEDGER_DELAYED_AFTER_SECONDS}")
    if LEDGER_UNAVAILABLE_AFTER_SECONDS != LEDGER_UNAVAILABLE_AFTER_DEFAULT_SECONDS:
        raise AssertionError(
            f"账本 Unavailable 阈值默认值被改动：{LEDGER_UNAVAILABLE_AFTER_SECONDS}"
        )


def assert_platform_error(outcome: Any, expected: ErrorCode) -> None:
    """对外错误必须是冻结错误码 + Request ID，且不泄露内部细节（TDD 12.8、第 5 章）。"""
    if outcome.error_code != expected.value:
        raise AssertionError(f"期望错误码 {expected.value}，实际是 {outcome.error_code!r}")
    if outcome.status_code != 503:
        raise AssertionError(
            f"{expected.value} 的 HTTP 状态必须是 503，实际是 {outcome.status_code}"
        )
    if not outcome.request_id:
        raise AssertionError("对外错误必须带 Request ID")
    leaked = [
        token
        for token in ("runtime", "container", "docker", "gpu", "/workspace", "sqlite", "postgres")
        if token in str(getattr(outcome, "message", "")).lower()
    ]
    if leaked:
        raise AssertionError(f"错误文案泄露内部细节：{leaked}")


def assert_call_succeeded(outcome: Any) -> None:
    if outcome.status_code != 200 or outcome.error_code is not None:
        raise AssertionError(
            f"这一步的调用应当成功，实际是 {outcome.status_code} / {outcome.error_code!r}"
        )


def assert_usage_unchanged(before: Mapping[str, int], after: Mapping[str, int]) -> None:
    """重放／重启后 Usage 逐字不变（TDD 4.3 不变量 12：同 Request ID 重放任意次结果不变）。"""
    if dict(before) != dict(after):
        raise AssertionError(f"Usage 发生了重复计量：{dict(before)} → {dict(after)}")


def assert_usage_complete(totals: Mapping[str, int], expected_requests: int) -> None:
    if totals.get("requests") != expected_requests:
        raise AssertionError(f"恢复后 Usage 不完整：期望 {expected_requests} 次，实际 {totals!r}")
    for field in ("input_tokens", "output_tokens"):
        if not isinstance(totals.get(field), int):
            raise AssertionError(f"Usage 必须拆分输入与输出 Token，缺 {field}：{totals!r}")


def assert_no_unacked_event_lost(before: Sequence[Any], after: Sequence[Any]) -> None:
    """未 Ack 事件永不自动删除（TDD 14、M5-10）：恢复后一条不少。"""
    missing = set(map(str, before)) - set(map(str, after))
    if missing:
        raise AssertionError(f"恢复后丢失了未 Ack 事件：{sorted(missing)}")


def assert_zero_body(findings: Sequence[str], sentinel: str) -> None:
    """第 5 章红线：故障与恢复路径同样不得把正文写进库、WAL、日志或诊断输出。

    `findings` 是故障注入后的扫描结果；**空列表只有在扫描本身被证明有效时才有意义**，
    所以每条零正文断言都必须配一次阳性对照（见各场景测试）。
    """
    if findings:
        raise AssertionError(f"哨兵 {sentinel} 在以下位置被找到，违反第 5 章红线：{list(findings)}")


def assert_scan_detects_positive_control(findings: Sequence[str], sentinel: str) -> None:
    if not findings:
        raise AssertionError(
            f"阳性对照失败：故意写入的哨兵 {sentinel} 没有被扫描发现，说明「零命中」不携带信息"
        )


def load_harness() -> Any:
    """取回故障注入夹具；缺失时大声失败，不 skip（skip 会让测试检查失去信息）。"""
    try:
        from fault import harness
    except ModuleNotFoundError as exc:  # pragma: no cover - 缺少测试夹具时的失败路径
        raise AssertionError(
            "M5-13 未实现：缺少 tests/fault/harness.py（导出清单见各场景测试的文档字符串）"
        ) from exc
    return harness

"""M4-14 的判定标准：TST 7.1 `TC-PF-01` 与 TST 5.3 `TC-GW-29`，已冻结。

本模块属于 TST 1.5 的 **A 档**产出（开发计划 2.5 把 M4-14 列为高风险卡）：
50 ms 是报告必须声明的发布目标，不作为 CI 的绝对通过门槛；
阶段清单与其余判定逻辑仍不得放宽。
测量机制（负载发生、分位计算、报告落盘）位于 `tests/performance/harness.py`。

阈值只有一个来源：TDD 2.2「Gateway 开销」行。七个阶段名逐字取自 TDD 18.5 与 TST TC-PF-01；
**本模块不新增任何逐阶段阈值**——初始版本只建立基线（TDD 2.2 末段），
逐阶段回退阈值属于 Release Manifest 冻结项（TDD 20.4），由后续发布卡承接。
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Any, Final

#: 请求实际流经的七个阶段，顺序即 TDD 18.5 列出的顺序。
REQUIRED_STAGES: Final[tuple[str, ...]] = (
    "request_parse",
    "auth_route",
    "ledger_commit",
    "runtime_pool_wait",
    "runtime_connect",
    "upstream_first_event",
    "downstream_flush",
)

REQUIRED_PERCENTILES: Final[tuple[str, ...]] = ("p50", "p95", "p99")

#: TDD 2.2：客户端观测的「平台额外 TTFT」P95 目标，单位毫秒。
#: 初始基线可以标记为未达标；M8-05 发布测试检查负责阻止未达标版本发布。
PLATFORM_EXTRA_TTFT_TARGET_MS: Final[float] = 50.0
PLATFORM_EXTRA_TTFT_STATUSES: Final[tuple[str, str]] = ("MET", "NOT_MET")

#: TST TC-PF-01 的测量条件：200 并发、预热连接、`FX-RT-03` 立即返回首事件的 Stub Runtime。
REQUIRED_CONCURRENCY: Final[int] = 200
REQUIRED_STUB_FIXTURE: Final[str] = "FX-RT-03"

#: 项目安全与测试约束红线：报告、阶段计时与其元数据都不得携带正文。
#: 出现以下任一键（任意嵌套层级）即判失败，不看取值。
FORBIDDEN_BODY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "prompt",
        "prompts",
        "messages",
        "message",
        "content",
        "completion",
        "completions",
        "choices",
        "delta",
        "text",
        "request_body",
        "response_body",
        "tool_arguments",
    }
)

_COMMIT_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")
_UTC_TIMESTAMP_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|\+00:00)$"
)


def numeric(value: Any, label: str) -> float:
    """取回一个真实测得的数值；布尔、字符串与缺失都判失败，避免把占位符当成测量结果。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AssertionError(f"{label} 必须是实测数值，实际是 {value!r}")
    return float(value)


def assert_commit(report: Mapping[str, Any]) -> None:
    commit = report.get("commit")
    if not isinstance(commit, str) or not _COMMIT_PATTERN.match(commit):
        raise AssertionError(f"报告必须声明测量所用的 40 位 commit，实际是 {commit!r}")


def assert_measured_at(report: Mapping[str, Any]) -> None:
    measured_at = report.get("measured_at")
    if not isinstance(measured_at, str) or not _UTC_TIMESTAMP_PATTERN.match(measured_at):
        raise AssertionError(f"报告必须声明 UTC 测量时间，实际是 {measured_at!r}")


def assert_ordered_percentiles(label: str, bucket: Mapping[str, Any]) -> tuple[float, float, float]:
    """三个分位必须齐全、非负且单调不减；分位错序说明统计口径本身错了。"""
    missing = [name for name in REQUIRED_PERCENTILES if name not in bucket]
    if missing:
        raise AssertionError(f"{label} 缺少分位 {missing}")
    p50, p95, p99 = (numeric(bucket[name], f"{label}.{name}") for name in REQUIRED_PERCENTILES)
    if min(p50, p95, p99) < 0:
        raise AssertionError(f"{label} 出现负分位：{(p50, p95, p99)}")
    if not p50 <= p95 <= p99:
        raise AssertionError(f"{label} 分位未单调不减：{(p50, p95, p99)}")
    return p50, p95, p99


def assert_declares_load_interval(profile: Mapping[str, Any]) -> None:
    """TDD 2.2 末段：报告必须声明结果对应的负载区间，且负载阶梯逐级递增到饱和点。"""
    concurrency = profile.get("concurrency")
    if concurrency != REQUIRED_CONCURRENCY:
        raise AssertionError(f"基准并发必须是 {REQUIRED_CONCURRENCY}，实际是 {concurrency!r}")
    if profile.get("stub_fixture") != REQUIRED_STUB_FIXTURE:
        raise AssertionError(
            f"Stub Runtime 必须是 {REQUIRED_STUB_FIXTURE}，实际是 {profile.get('stub_fixture')!r}"
        )
    warmup = profile.get("warmup_requests")
    if not isinstance(warmup, int) or isinstance(warmup, bool) or warmup <= 0:
        raise AssertionError(f"必须声明预热请求数且大于 0，实际是 {warmup!r}")
    if not isinstance(profile.get("edge_in_path"), bool):
        raise AssertionError("必须声明客户端观测口径是否经过 Edge（Caddy），布尔值，不得省略")
    if not isinstance(profile.get("request_mix"), str) or not profile["request_mix"].strip():
        raise AssertionError("必须声明请求组合（request_mix）")
    levels = profile.get("load_levels")
    if not isinstance(levels, list) or len(levels) < 2:
        raise AssertionError("必须声明至少两级负载阶梯（TDD 2.2 末段：逐级增加到饱和点）")
    concurrencies = [
        numeric(level.get("concurrency"), "load_levels[].concurrency") for level in levels
    ]
    if concurrencies != sorted(concurrencies) or len(set(concurrencies)) != len(concurrencies):
        raise AssertionError(f"负载阶梯必须严格递增，实际是 {concurrencies}")
    if REQUIRED_CONCURRENCY not in concurrencies:
        raise AssertionError(f"负载阶梯必须包含基准并发 {REQUIRED_CONCURRENCY}：{concurrencies}")


def iter_keys(payload: Any) -> Iterator[str]:
    """逐层遍历报告的全部键名，供第 5 章红线扫描使用。"""
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            yield str(key)
            yield from iter_keys(value)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            yield from iter_keys(item)


def assert_no_body(payload: Mapping[str, Any]) -> None:
    offenders = sorted({key for key in iter_keys(payload) if key.lower() in FORBIDDEN_BODY_KEYS})
    if offenders:
        raise AssertionError(f"报告携带正文相关键，违反第 5 章红线：{offenders}")


def assert_platform_extra_ttft_target_status(
    payload: Mapping[str, Any], measured_p95: float, label: str
) -> None:
    """目标与达标状态必须显式、合法，并与实测 P95 完全一致。"""
    target = numeric(
        payload.get("platform_extra_ttft_target_ms"),
        f"{label}.platform_extra_ttft_target_ms",
    )
    if target != PLATFORM_EXTRA_TTFT_TARGET_MS:
        raise AssertionError(
            f"{label}.platform_extra_ttft_target_ms 必须是 TDD 2.2 的 "
            f"{PLATFORM_EXTRA_TTFT_TARGET_MS} ms，实际是 {target}"
        )
    status = payload.get("status")
    if status not in PLATFORM_EXTRA_TTFT_STATUSES:
        raise AssertionError(f"{label}.status 必须是 MET 或 NOT_MET，实际是 {status!r}")
    expected = "MET" if measured_p95 <= target else "NOT_MET"
    if status != expected:
        raise AssertionError(
            f"{label}.status={status} 与平台额外 TTFT P95 {measured_p95} ms、"
            f"目标 {target} ms 不一致，应为 {expected}"
        )


def assert_baseline_judgement(report: Mapping[str, Any]) -> None:
    """TC-PF-01／TC-GW-29 的完整判定，对已冻结报告与实测报告使用同一套断言。"""
    assert_commit(report)
    assert_measured_at(report)

    profile = report.get("profile")
    if not isinstance(profile, Mapping):
        raise AssertionError("报告必须内嵌其负载 Profile")
    assert_declares_load_interval(profile)

    stages = report.get("stages")
    if not isinstance(stages, Mapping):
        raise AssertionError("报告必须包含 stages 段")
    missing = [stage for stage in REQUIRED_STAGES if stage not in stages]
    if missing:
        raise AssertionError(f"缺少阶段分位：{missing}")
    for stage in REQUIRED_STAGES:
        assert_ordered_percentiles(f"stages.{stage}", stages[stage])

    observed = report.get("client_observed")
    if not isinstance(observed, Mapping):
        raise AssertionError("报告必须包含 client_observed 段（客户端观测口径）")
    assert_ordered_percentiles("client_observed.ttft_ms", observed.get("ttft_ms", {}))
    extra = observed.get("platform_extra_ttft_ms", {})
    missing_extra = [name for name in REQUIRED_PERCENTILES if name not in extra]
    if missing_extra:
        raise AssertionError(f"client_observed.platform_extra_ttft_ms 缺少分位 {missing_extra}")
    extra_p95 = numeric(extra["p95"], "client_observed.platform_extra_ttft_ms.p95")
    assert_platform_extra_ttft_target_status(report, extra_p95, "report")

    totals = report.get("totals")
    if not isinstance(totals, Mapping):
        raise AssertionError("报告必须包含 totals 段（吞吐与错误率）")
    completed = numeric(totals.get("requests_completed"), "totals.requests_completed")
    if completed < REQUIRED_CONCURRENCY:
        raise AssertionError(f"完成请求数 {completed} 少于基准并发 {REQUIRED_CONCURRENCY}")
    error_rate = numeric(totals.get("error_rate"), "totals.error_rate")
    if error_rate != 0.0:
        raise AssertionError(
            f"Stub Runtime 始终可用，基准运行的错误率必须为 0，实际是 {error_rate}"
        )

    assert_level_measurements(report)
    assert_measurement_rig_not_saturated(report)
    assert_no_body(report)


#: 每一级负载都要同时给出「经 Gateway」与「直连 Stub」两组观测，否则无法把
#: 测量装置自身的开销从「平台额外」里减掉（TDD 2.2 的口径是「额外」）。
REQUIRED_LEVEL_BLOCKS: Final[tuple[str, ...]] = ("gateway", "direct")

#: 测量装置饱和判据：并发从最低一级升到 200 时，直连吞吐至少要涨到 1.5 倍。
#: 涨不上去说明瓶颈在客户端／Edge 替身／Stub，这一轮的分位反映排队而非平台开销，
#: 报告必须大声失败，而不是把排队时延写成 Gateway 的开销。
MIN_DIRECT_THROUGHPUT_SCALING: Final[float] = 1.5

#: platform_extra 必须逐分位等于 gateway − direct，容差只留浮点误差。
PLATFORM_EXTRA_TOLERANCE_MS: Final[float] = 0.001


def assert_level_measurements(report: Mapping[str, Any]) -> None:
    """逐级负载的两组观测齐全，且 platform_extra 的定义没有被偷换。"""
    levels = report.get("levels")
    if not isinstance(levels, list) or len(levels) < 2:
        raise AssertionError("报告必须逐级记录负载结果（levels），至少两级")
    for index, level in enumerate(levels):
        label = f"levels[{index}]"
        numeric(level.get("concurrency"), f"{label}.concurrency")
        for block in REQUIRED_LEVEL_BLOCKS:
            measured = level.get(block)
            if not isinstance(measured, Mapping):
                raise AssertionError(f"{label} 缺少 {block} 观测（直连对照是「额外」的前提）")
            assert_ordered_percentiles(f"{label}.{block}.ttft_ms", measured.get("ttft_ms", {}))
            throughput = numeric(measured.get("throughput_rps"), f"{label}.{block}.throughput_rps")
            if throughput <= 0:
                raise AssertionError(f"{label}.{block} 吞吐必须为正，实际 {throughput}")
        extra = level.get("platform_extra_ttft_ms")
        if not isinstance(extra, Mapping):
            raise AssertionError(f"{label} 缺少 platform_extra_ttft_ms")
        for name in REQUIRED_PERCENTILES:
            gateway_value = numeric(level["gateway"]["ttft_ms"][name], f"{label}.gateway.{name}")
            direct_value = numeric(level["direct"]["ttft_ms"][name], f"{label}.direct.{name}")
            reported = numeric(extra[name], f"{label}.platform_extra_ttft_ms.{name}")
            if abs(reported - (gateway_value - direct_value)) > PLATFORM_EXTRA_TOLERANCE_MS:
                raise AssertionError(
                    f"{label} 的 platform_extra_ttft_ms.{name} 必须等于 gateway − direct："
                    f"{gateway_value} − {direct_value} ≠ {reported}"
                )
        # 差值是两个独立分布逐分位相减得到的派生量，没有单调性可言
        # （实测出现过 P95 > P99），所以这里只取数值、不做分位排序断言。
        p95 = numeric(extra["p95"], f"{label}.platform_extra_ttft_ms.p95")
        assert_platform_extra_ttft_target_status(level, p95, label)


def assert_measurement_rig_not_saturated(report: Mapping[str, Any]) -> None:
    """守卫：吞吐不随并发增长时，这一轮测的是装置排队，不是平台开销。

    M4-14 第一棒实测到的就是这种情形——并发 8→200 吞吐恒在 78 rps 上下，
    平台额外 TTFT 恰好等于「并发 ÷ 吞吐」，是 Little's law 的排队时延。
    这条守卫存在的意义是让那种结果**失败并说明原因**，而不是被当成 Gateway 的开销。
    """
    levels = report.get("levels")
    if not isinstance(levels, list) or len(levels) < 2:
        raise AssertionError("饱和判定需要至少两级负载")
    ordered = sorted(levels, key=lambda level: numeric(level["concurrency"], "concurrency"))
    lowest = numeric(ordered[0]["direct"]["throughput_rps"], "lowest direct throughput")
    highest = numeric(ordered[-1]["direct"]["throughput_rps"], "highest direct throughput")
    if highest < lowest * MIN_DIRECT_THROUGHPUT_SCALING:
        lowest_concurrency = numeric(ordered[0]["concurrency"], "lowest concurrency")
        highest_concurrency = numeric(ordered[-1]["concurrency"], "highest concurrency")
        raise AssertionError(
            "测量装置饱和：并发 "
            f"{lowest_concurrency} → {highest_concurrency} 的直连吞吐"
            f"只从 {lowest} 升到 {highest} rps"
            f"（要求 ≥{MIN_DIRECT_THROUGHPUT_SCALING} 倍）。"
            "此时的分位是客户端／Edge 替身／Stub 的排队时延，不能当作平台额外 TTFT。"
        )

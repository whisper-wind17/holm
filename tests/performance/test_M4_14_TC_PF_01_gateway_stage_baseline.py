"""TC-PF-01／TC-GW-29：Gateway 七阶段分位基线（M4-14，A 档骨架）。

依赖 `tests/performance/harness.py`，缺失时必须明确失败。50 ms 保留为显式目标，
冻结报告允许标记为未达标；其余判定不得放宽。

`tests/performance/harness.py` 导出：

* ``BaselineReportError``：报告不合法时抛出的异常类型；
* ``validate_report(payload) -> None``：报告校验守卫，不合法时**大声失败**
  （抛 ``BaselineReportError``）；
* ``load_baseline_report() -> Mapping``：读回已冻结的基准报告（仓库内 JSON）；
* ``load_frozen_profile() -> Mapping``：读回已冻结的负载 Profile（仓库内 JSON）；
* ``measure(profile) -> Mapping``：按 Profile 实测一次并返回同一形状的报告。

夹具 API 的形状可以调整（改这几个名字要在 dispatch note 写明），
但每一条断言必须逐字成立，`judgement.py` 只能读、不能改。
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

import pytest
from performance.judgement import (
    PLATFORM_EXTRA_TTFT_TARGET_MS,
    REQUIRED_PERCENTILES,
    REQUIRED_STAGES,
    assert_baseline_judgement,
    assert_declares_load_interval,
    assert_no_body,
    assert_ordered_percentiles,
    numeric,
)


def _harness() -> Any:
    try:
        from performance import harness
    except ModuleNotFoundError as exc:  # pragma: no cover - 缺少测试夹具时的失败路径
        raise AssertionError(
            "M4-14 未实现：缺少 tests/performance/harness.py（见本模块文档字符串的导出清单）"
        ) from exc
    return harness


def _report() -> Mapping[str, Any]:
    return _harness().load_baseline_report()


def _mutated(**overrides: Any) -> dict[str, Any]:
    payload = copy.deepcopy(dict(_report()))
    payload.update(overrides)
    return payload


def test_M4_14_TC_PF_01_frozen_profile_declares_the_tdd_2_2_load_interval() -> None:
    """负载区间必须被声明并冻结，否则分位数无法被后续版本比较（TDD 2.2 末段）。"""
    assert_declares_load_interval(_harness().load_frozen_profile())


def test_M4_14_TC_PF_01_report_is_bound_to_a_commit_and_an_environment() -> None:
    report = _report()
    assert_baseline_judgement(report)
    environment = report.get("environment")
    assert isinstance(environment, Mapping), "报告必须声明测量环境（硬件与运行时）"
    for field in ("host", "cpu", "memory_mib", "python"):
        assert environment.get(field), f"环境声明缺少 {field}"


def test_M4_14_TC_PF_01_report_profile_equals_the_frozen_profile() -> None:
    """报告内嵌的 Profile 必须与冻结 Profile 一致，避免用别的负载冒充基准。"""
    assert dict(_report()["profile"]) == dict(_harness().load_frozen_profile())


@pytest.mark.parametrize("stage", REQUIRED_STAGES)
def test_M4_14_TC_PF_01_TC_GW_29_stage_reports_p50_p95_p99(stage: str) -> None:
    """七个阶段逐个报告 P50/P95/P99（TDD 18.5 逐字列出的那七个）。"""
    assert_ordered_percentiles(f"stages.{stage}", _report()["stages"][stage])


def test_M4_14_TC_PF_01_TC_GW_29_target_status_matches_measured_p95() -> None:
    """50 ms 仍是目标；顶层或逐级状态与实测不一致都必须大声失败。"""
    harness = _harness()
    report = _report()
    assert report["platform_extra_ttft_target_ms"] == PLATFORM_EXTRA_TTFT_TARGET_MS
    assert report["status"] == "NOT_MET"
    assert report["levels"][0]["status"] == "MET"

    top_level_lie = _mutated(status="MET")
    with pytest.raises(harness.BaselineReportError, match="不一致"):
        harness.validate_report(top_level_lie)

    level_lie = copy.deepcopy(dict(report))
    level_lie["levels"][-1]["status"] = "MET"
    with pytest.raises(harness.BaselineReportError, match="不一致"):
        harness.validate_report(level_lie)


def test_M4_14_TC_PF_01_TC_GW_29_reports_throughput_and_zero_error_rate() -> None:
    totals = _report()["totals"]
    assert numeric(totals["requests_completed"], "requests_completed") > 0
    assert numeric(totals["error_rate"], "error_rate") == 0.0


def test_M4_14_TC_PF_01_report_carries_no_request_or_response_body() -> None:
    """第 5 章红线：性能报告是会被长期保存并随发布分发的产物，正文一律不得出现。"""
    assert_no_body(_report())


@pytest.mark.parametrize("stage", REQUIRED_STAGES)
def test_M4_14_TC_PF_01_validator_rejects_a_report_missing_a_stage(stage: str) -> None:
    """阳性对照（AGENTS 第 10 章）：少一个阶段必须被守卫拒绝，而不是静默通过。"""
    harness = _harness()
    stages = copy.deepcopy(dict(_report()["stages"]))
    del stages[stage]
    with pytest.raises(harness.BaselineReportError):
        harness.validate_report(_mutated(stages=stages))


@pytest.mark.parametrize("percentile", REQUIRED_PERCENTILES)
def test_M4_14_TC_PF_01_validator_rejects_a_missing_percentile(percentile: str) -> None:
    harness = _harness()
    stages = copy.deepcopy(dict(_report()["stages"]))
    del stages[REQUIRED_STAGES[0]][percentile]
    with pytest.raises(harness.BaselineReportError):
        harness.validate_report(_mutated(stages=stages))


def test_M4_14_TC_PF_01_validator_rejects_a_body_carrying_report() -> None:
    harness = _harness()
    totals = copy.deepcopy(dict(_report()["totals"]))
    totals["messages"] = ["<任何正文都不该出现在报告里>"]
    with pytest.raises(harness.BaselineReportError):
        harness.validate_report(_mutated(totals=totals))


def test_M4_14_TC_PF_01_validator_rejects_a_profile_that_is_not_the_frozen_one() -> None:
    harness = _harness()
    profile = copy.deepcopy(dict(_report()["profile"]))
    profile["concurrency"] = 8
    with pytest.raises(harness.BaselineReportError):
        harness.validate_report(_mutated(profile=profile))


def test_M4_14_TC_PF_01_validator_rejects_a_placeholder_measurement() -> None:
    """占位符不能冒充测量值：字符串、布尔与 None 都必须被拒绝。"""
    harness = _harness()
    stages = copy.deepcopy(dict(_report()["stages"]))
    stages[REQUIRED_STAGES[0]]["p95"] = "0"
    with pytest.raises(harness.BaselineReportError):
        harness.validate_report(_mutated(stages=stages))


@pytest.mark.perf
def test_M4_14_TC_PF_01_TC_GW_29_measured_run_meets_the_same_judgement() -> None:
    """实测一次（`./dev perf`）：与冻结报告使用完全相同的判定，不另设宽松口径。"""
    harness = _harness()
    report = harness.measure(harness.load_frozen_profile())
    harness.validate_report(report)
    assert_baseline_judgement(report)

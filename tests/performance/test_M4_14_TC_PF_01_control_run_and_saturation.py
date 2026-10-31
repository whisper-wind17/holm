"""TC-PF-01 第二棒：直连对照与测量装置饱和守卫（M4-14，A 档骨架追加）。

第一棒实测到并发 8→200 的吞吐恒在 78 rps 上下、平台额外 TTFT 恰等于「并发 ÷ 吞吐」，
说明瓶颈是测量装置而不是 Gateway，因此需要以下判定：

1. **直连对照**——每一级负载都要同时测「经 Gateway」与「直连 Stub Runtime」两条路径，
   `platform_extra_ttft_ms` 逐分位等于两者之差。没有对照就无法称之为「额外」（TDD 2.2 口径）。
2. **饱和守卫**——直连吞吐必须随并发增长，否则报告大声失败并说明这轮测的是排队时延。

夹具需在 `tests/performance/harness.py` 追加：``measure_direct(profile)``（同一客户端、同一负载
阶梯，不经 Gateway，直接打 Stub Runtime），并让 ``measure``／报告输出 `levels` 段。
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

import pytest
from performance.judgement import (
    MIN_DIRECT_THROUGHPUT_SCALING,
    REQUIRED_LEVEL_BLOCKS,
    assert_level_measurements,
    assert_measurement_rig_not_saturated,
)


def _harness() -> Any:
    try:
        from performance import harness
    except ModuleNotFoundError as exc:  # pragma: no cover - 缺少测试夹具时的失败路径
        raise AssertionError("M4-14 未实现：缺少 tests/performance/harness.py") from exc
    return harness


def _report() -> Mapping[str, Any]:
    return _harness().load_baseline_report()


def _mutated(**overrides: Any) -> dict[str, Any]:
    payload = copy.deepcopy(dict(_report()))
    payload.update(overrides)
    return payload


def _levels() -> list[dict[str, Any]]:
    return copy.deepcopy(list(_report()["levels"]))


def test_M4_14_TC_PF_01_every_level_carries_gateway_and_direct_observations() -> None:
    assert_level_measurements(_report())
    for level in _levels():
        for block in REQUIRED_LEVEL_BLOCKS:
            assert isinstance(level.get(block), Mapping), (
                f"并发 {level['concurrency']} 缺 {block} 观测"
            )


def test_M4_14_TC_PF_01_measurement_rig_is_not_the_bottleneck() -> None:
    """直连吞吐必须随并发增长；不增长说明测的是装置排队，不是平台开销。"""
    assert_measurement_rig_not_saturated(_report())


def test_M4_14_TC_PF_01_validator_rejects_a_level_without_the_direct_control() -> None:
    """阳性对照：少了直连对照必须被守卫拒绝。"""
    harness = _harness()
    levels = _levels()
    del levels[-1]["direct"]
    with pytest.raises(harness.BaselineReportError):
        harness.validate_report(_mutated(levels=levels))


def test_M4_14_TC_PF_01_validator_rejects_a_redefined_platform_extra() -> None:
    """阳性对照：platform_extra 不等于 gateway − direct 时必须被拒绝，防止偷换定义。"""
    harness = _harness()
    levels = _levels()
    levels[-1]["platform_extra_ttft_ms"]["p95"] = 1.0
    with pytest.raises(harness.BaselineReportError):
        harness.validate_report(_mutated(levels=levels))


def test_M4_14_TC_PF_01_validator_rejects_a_saturated_measurement_rig() -> None:
    """阳性对照：吞吐随并发持平（第一棒的实测形态）必须失败，且理由是装置饱和。"""
    harness = _harness()
    levels = _levels()
    flat = levels[0]["direct"]["throughput_rps"]
    for level in levels:
        level["direct"]["throughput_rps"] = flat
    with pytest.raises(harness.BaselineReportError):
        harness.validate_report(_mutated(levels=levels))
    with pytest.raises(AssertionError, match="测量装置饱和"):
        assert_measurement_rig_not_saturated({"levels": levels})


def test_M4_14_TC_PF_01_saturation_threshold_is_the_frozen_one() -> None:
    assert MIN_DIRECT_THROUGHPUT_SCALING == 1.5

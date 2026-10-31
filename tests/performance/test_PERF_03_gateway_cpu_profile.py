"""PERF-03 PEP 669 disturbance decision and fail-loud report checks."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from performance import cpu_profile
from performance.harness import BaselineReportError


def _report() -> dict[str, Any]:
    return copy.deepcopy(dict(cpu_profile.load_cpu_profile_report()))


def test_PERF_03_profile_declares_fixture_load_and_two_concurrency_points() -> None:
    report = _report()
    cpu_profile.validate_cpu_profile_report(report)
    assert report["workload"]["concurrencies"] == [1, 32]
    assert report["workload"]["stub_fixture"] == "FX-RT-03"
    assert report["workload"]["edge_in_path"] is False
    assert report["environment"]["load_before"]["uptime"]
    assert report["environment"]["load_after"]["uptime"]
    assert report["method"]["timer"] == "time.process_time_ns"


def test_PERF_03_report_never_presents_distorted_function_rankings() -> None:
    report = _report()
    if report["status"] == "FALLBACK_DISTORTED":
        assert report["answers"]["function_level_composition_answered"] is False
        assert any(
            point["monitoring_overhead_percent"] > cpu_profile.MAX_ACCEPTABLE_OVERHEAD_PERCENT
            for point in report["points"]
        )
        assert all("top_functions_by_self_cpu" not in point for point in report["points"])
        assert all("module_self_cpu" not in point for point in report["points"])
    else:
        for point in report["points"]:
            assert len(point["top_functions_by_self_cpu"]) >= 10
            assert sum(row["self_percent"] for row in point["module_self_cpu"]) == pytest.approx(
                100.0, abs=1e-6
            )


def test_PERF_03_profile_switch_defaults_off_and_only_enabled_writes(
    tmp_path: Path,
) -> None:
    output = tmp_path / "profile.json"
    calls = 0

    def measure() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return _report()

    assert cpu_profile.write_cpu_profile_if_enabled(output=output, measure=measure) is False
    assert calls == 0
    assert not output.exists()

    assert (
        cpu_profile.write_cpu_profile_if_enabled(enabled=True, output=output, measure=measure)
        is True
    )
    assert calls == 1
    assert output.is_file()


def test_PERF_03_validator_rejects_fallback_without_distortion() -> None:
    report = _report()
    report["status"] = "FALLBACK_DISTORTED"
    report["answers"]["function_level_composition_answered"] = False
    for point in report["points"]:
        point["monitoring_overhead_percent"] = 0.0
        point.pop("top_functions_by_self_cpu", None)
        point.pop("module_self_cpu", None)
    with pytest.raises(BaselineReportError, match="失真证据"):
        cpu_profile.validate_cpu_profile_report(report)


def test_PERF_03_validator_rejects_polluted_rankings_in_fallback() -> None:
    report = _report()
    report["status"] = "FALLBACK_DISTORTED"
    report["answers"]["function_level_composition_answered"] = False
    report["points"][0]["monitoring_overhead_percent"] = 31.0
    report["points"][0]["top_functions_by_self_cpu"] = []
    with pytest.raises(BaselineReportError, match="不得冻结受污染"):
        cpu_profile.validate_cpu_profile_report(report)


def test_PERF_03_validator_rejects_inconsistent_concurrency_answer() -> None:
    report = _report()
    current = report["answers"]["does_concurrency_increase_cpu_per_request"]
    report["answers"]["does_concurrency_increase_cpu_per_request"] = not current
    with pytest.raises(BaselineReportError, match="并发结论"):
        cpu_profile.validate_cpu_profile_report(report)


def test_PERF_03_validator_rejects_a_high_load_snapshot() -> None:
    report = _report()
    report["environment"]["load_before"]["load_1m"] = 2.01
    with pytest.raises(BaselineReportError, match="负载高于 2"):
        cpu_profile.validate_cpu_profile_report(report)


def test_PERF_03_validator_rejects_body_or_absolute_path_leakage() -> None:
    body_report = _report()
    body_report["messages"] = ["must never be stored"]
    with pytest.raises(BaselineReportError, match="正文"):
        cpu_profile.validate_cpu_profile_report(body_report)

    path_report = _report()
    path_report["environment"]["diagnostic"] = "/workspace/private/module.py"
    with pytest.raises(BaselineReportError, match="绝对路径"):
        cpu_profile.validate_cpu_profile_report(path_report)


def test_PERF_03_freezes_both_standard_library_distortion_observations() -> None:
    report = _report()
    evidence = report["distortion_evidence"]["cprofile_first_leg"]
    assert evidence["concurrency_1"]["overhead_percent"] == 193.316
    assert evidence["concurrency_32"]["overhead_percent"] == 396.784
    assert report["method"]["maximum_acceptable_overhead_percent"] == 30.0

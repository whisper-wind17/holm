"""PERF-03 low-disturbance Gateway CPU diagnosis over the TC-PF-01 rig."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sysconfig
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

from performance import harness
from performance.harness import (
    BaselineReportError,
    CpuMonitoringFunction,
    CpuMonitoringWindow,
)
from performance.judgement import assert_no_body, numeric

REPORT_PATH: Final[Path] = harness.BASELINE_ROOT / "TC_PERF_03_gateway_cpu_profile.json"
CASE: Final[str] = "PERF-03-GATEWAY-CPU-PROFILE"
PROFILE_CONCURRENCIES: Final[tuple[int, int]] = (1, 32)
REQUESTS_PER_POINT: Final[int] = 128
TOP_FUNCTION_COUNT: Final[int] = 10
MAX_ACCEPTABLE_OVERHEAD_PERCENT: Final[float] = 30.0
MODULE_GROUPS: Final[tuple[str, ...]] = (
    "holm_gateway",
    "holm_protocols",
    "holm_ledger",
    "holm_other",
    "httpx_httpcore",
    "starlette",
    "asyncio",
    "uvicorn",
    "standard_library",
    "third_party_other",
    "performance_harness",
    "other_gateway_threads_or_unattributed",
)
CPROFILE_DISTORTION_EVIDENCE: Final[dict[str, Any]] = {
    "method": "cProfile + pstats",
    "concurrency_1": {
        "control_cpu_ms_per_request": 6.946613,
        "profiled_cpu_ms_per_request": 20.375525,
        "overhead_percent": 193.316,
        "amplification_factor": 2.93316,
    },
    "concurrency_32": {
        "control_cpu_ms_per_request": 6.149889,
        "profiled_cpu_ms_per_request": 30.551683,
        "overhead_percent": 396.784,
        "amplification_factor": 4.96784,
    },
}


def _uptime_snapshot() -> dict[str, float | str]:
    rendered = subprocess.run(
        ["uptime"], check=True, capture_output=True, text=True, timeout=5
    ).stdout.strip()
    return {"uptime": rendered, "load_1m": round(os.getloadavg()[0], 2)}


def _module_for(filename: str) -> tuple[str, str]:
    normalized = filename.replace("\\", "/")
    if normalized == "~" or normalized.startswith("<"):
        return "standard_library", "interpreter"

    holm_packages = (
        "holm_gateway",
        "holm_protocols",
        "holm_ledger",
        "holm_runtimes",
        "holm_security",
        "holm_domain",
        "holm_contracts",
        "holm_persistence",
    )
    for package in holm_packages:
        marker = f"/{package}/"
        if marker in normalized:
            suffix = normalized.split(marker, 1)[1].removesuffix(".py").replace("/", ".")
            group = package if package in MODULE_GROUPS else "holm_other"
            return group, f"{package}.{suffix}"

    if "/tests/performance/" in normalized:
        suffix = normalized.split("/tests/performance/", 1)[1]
        module = suffix.removesuffix(".py").replace("/", ".")
        return "performance_harness", f"performance.{module}"

    third_party_prefixes = {
        "httpx": "httpx_httpcore",
        "httpcore": "httpx_httpcore",
        "starlette": "starlette",
        "uvicorn": "uvicorn",
    }
    for package, group in third_party_prefixes.items():
        marker = f"/{package}/"
        if marker in normalized:
            suffix = normalized.split(marker, 1)[1].removesuffix(".py").replace("/", ".")
            return group, f"{package}.{suffix}"

    stdlib = str(sysconfig.get_paths()["stdlib"]).replace("\\", "/").rstrip("/")
    if normalized.startswith(f"{stdlib}/") and "/site-packages/" not in normalized:
        suffix = normalized[len(stdlib) + 1 :].removesuffix(".py").replace("/", ".")
        if suffix == "asyncio" or suffix.startswith("asyncio."):
            return "asyncio", suffix
        return "standard_library", suffix

    if "/site-packages/" in normalized:
        suffix = normalized.split("/site-packages/", 1)[1]
        return "third_party_other", suffix.removesuffix(".py").replace("/", ".")
    return "standard_library", Path(normalized).stem


def _rounded_module_percentages(
    seconds_by_group: Mapping[str, float], total_seconds: float
) -> list[dict[str, float | str]]:
    if total_seconds <= 0:
        raise BaselineReportError("PERF-03 sys.monitoring 没有记录到 Gateway CPU")
    rows: list[dict[str, float | str]] = []
    for group in MODULE_GROUPS:
        seconds = float(seconds_by_group.get(group, 0.0))
        rows.append(
            {
                "module_group": group,
                "self_seconds": round(seconds, 9),
                "self_percent": round(seconds / total_seconds * 100.0, 6),
            }
        )
    correction = round(100.0 - sum(float(row["self_percent"]) for row in rows), 6)
    correction_index = max(range(len(rows)), key=lambda index: float(rows[index]["self_percent"]))
    rows[correction_index]["self_percent"] = round(
        float(rows[correction_index]["self_percent"]) + correction, 6
    )
    return rows


def _monitoring_point(
    concurrency: int,
    requests: int,
    control_process_cpu_seconds: float,
    monitored: CpuMonitoringWindow,
) -> dict[str, Any]:
    if requests % concurrency:
        raise BaselineReportError("PERF-03 每点请求数必须能被并发整除")
    seconds_by_group: defaultdict[str, float] = defaultdict(float)
    decorated: list[tuple[CpuMonitoringFunction, str, str]] = []
    for function in monitored.functions:
        group, module = _module_for(function.filename)
        seconds_by_group[group] += function.self_seconds
        decorated.append((function, group, module))

    attributed_seconds = sum(item.self_seconds for item in monitored.functions)
    total_seconds = max(monitored.process_cpu_seconds, attributed_seconds)
    seconds_by_group["other_gateway_threads_or_unattributed"] += max(
        0.0, total_seconds - attributed_seconds
    )
    modules = _rounded_module_percentages(seconds_by_group, total_seconds)
    functions = [
        {
            "function": f"{module}:{function.line_number}:{function.function_name}",
            "module": module,
            "module_group": group,
            "entry_events": function.entry_events,
            "self_seconds": round(function.self_seconds, 9),
            "self_percent_of_process": round(function.self_seconds / total_seconds * 100.0, 6),
        }
        for function, group, module in sorted(
            decorated, key=lambda item: item[0].self_seconds, reverse=True
        )[:TOP_FUNCTION_COUNT]
    ]

    control_per_request = control_process_cpu_seconds * 1000.0 / requests
    monitored_per_request = monitored.process_cpu_seconds * 1000.0 / requests
    overhead_percent = (
        (monitored_per_request - control_per_request) / control_per_request * 100.0
        if control_per_request > 0
        else 0.0
    )
    return {
        "concurrency": concurrency,
        "requests": requests,
        "batches": requests // concurrency,
        "control_process_cpu_seconds": round(control_process_cpu_seconds, 9),
        "control_process_cpu_ms_per_request": round(control_per_request, 6),
        "monitored_process_cpu_seconds": round(monitored.process_cpu_seconds, 9),
        "monitored_process_cpu_ms_per_request": round(monitored_per_request, 6),
        "monitoring_overhead_percent": round(overhead_percent, 3),
        "monitoring_amplification_factor": round(monitored_per_request / control_per_request, 6),
        "monitoring_events": monitored.monitoring_events,
        "stack_mismatches": monitored.stack_mismatches,
        "attributed_process_cpu_percent": round(
            monitored.attributed_cpu_seconds / total_seconds * 100.0, 6
        ),
        "top_functions_by_self_cpu": functions,
        "module_self_cpu": modules,
    }


def _run_gateway_batches(rig: Any, collector: Any, concurrency: int, requests: int) -> None:
    observations, _elapsed = rig.run("gateway", concurrency, repetitions=requests // concurrency)
    harness._gateway_samples(cast(Sequence[Any], observations), collector)


def _fallback_point(point: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze clean measurements and distortion evidence, never polluted rankings."""

    return {
        key: value
        for key, value in point.items()
        if key not in {"top_functions_by_self_cpu", "module_self_cpu"}
    }


def measure_cpu_profile(*, source_commit: str | None = None) -> Mapping[str, Any]:
    """Measure a clean control then an explicitly enabled PEP 669 window."""

    source_profile = harness.load_frozen_profile()
    load_before = _uptime_snapshot()
    points: list[dict[str, Any]] = []
    max_concurrency = max(PROFILE_CONCURRENCIES)
    max_inflight = int(source_profile["gateway_max_inflight"])
    with (
        harness._ephemeral_postgres() as (fixture, snapshot_fixture),
        harness._runtime_process() as runtime,
    ):
        harness._seed_runtime_endpoint(fixture, snapshot_fixture, runtime.base_url)
        with harness._gateway_process(fixture, max_inflight, enable_cpu_monitoring=True) as gateway:
            collector = harness._TimingCollector(gateway.timing_queue, gateway.overflowed)
            with harness._load_client_rig(
                max_concurrency,
                runtime.base_url,
                gateway.gateway_url,
                gateway.key,
            ) as rig:
                harness._warmup_rig(
                    rig,
                    "direct",
                    int(source_profile["warmup_requests"]),
                    max_concurrency,
                )
                harness._warmup_rig(
                    rig,
                    "gateway",
                    int(source_profile["warmup_requests"]),
                    max_concurrency,
                    collector,
                )
                runtime.drain_transport_timings()
                for concurrency in PROFILE_CONCURRENCIES:
                    gateway.start_cpu_window()
                    _run_gateway_batches(rig, collector, concurrency, REQUESTS_PER_POINT)
                    control_process_cpu_seconds = gateway.stop_cpu_window()
                    runtime.drain_transport_timings()

                    gateway.start_cpu_monitoring()
                    _run_gateway_batches(rig, collector, concurrency, REQUESTS_PER_POINT)
                    monitoring_window = gateway.stop_cpu_monitoring()
                    runtime.drain_transport_timings()
                    points.append(
                        _monitoring_point(
                            concurrency,
                            REQUESTS_PER_POINT,
                            control_process_cpu_seconds,
                            monitoring_window,
                        )
                    )
    load_after = _uptime_snapshot()
    distorted = any(
        float(point["monitoring_overhead_percent"]) > MAX_ACCEPTABLE_OVERHEAD_PERCENT
        for point in points
    )
    status = "FALLBACK_DISTORTED" if distorted else "FUNCTION_ATTRIBUTION_ACCEPTED"
    frozen_points = [_fallback_point(point) for point in points] if distorted else points
    clean_delta = round(
        float(points[1]["control_process_cpu_ms_per_request"])
        - float(points[0]["control_process_cpu_ms_per_request"]),
        6,
    )
    report = {
        "case": CASE,
        "status": status,
        "commit": source_commit or harness._commit(),
        "measured_at": datetime.now(UTC).isoformat(),
        "environment": {
            **harness._environment(),
            "load_before": load_before,
            "load_after": load_after,
        },
        "method": {
            "profiler": "sys.monitoring (PEP 669)",
            "timer": "time.process_time_ns",
            "events": ["PY_START", "PY_RESUME", "PY_RETURN", "PY_YIELD", "PY_UNWIND"],
            "attribution": (
                "process CPU intervals between lifecycle events on the Gateway loop thread"
            ),
            "production_gateway_hook": False,
            "default_enabled": False,
            "maximum_acceptable_overhead_percent": MAX_ACCEPTABLE_OVERHEAD_PERCENT,
        },
        "workload": {
            "concurrencies": list(PROFILE_CONCURRENCIES),
            "requests_per_point": REQUESTS_PER_POINT,
            "warmup_requests": int(source_profile["warmup_requests"]),
            "request_mix": source_profile["request_mix"],
            "stub_fixture": source_profile["stub_fixture"],
            "edge_in_path": source_profile["edge_in_path"],
            "process_topology": source_profile["process_topology"],
            "gateway_max_inflight": source_profile["gateway_max_inflight"],
        },
        "points": frozen_points,
        "clean_concurrency_32_minus_1_cpu_ms_per_request": clean_delta,
        "answers": {
            "does_concurrency_increase_cpu_per_request": clean_delta > 0,
            "concurrency_question_answered": True,
            "function_level_composition_answered": not distorted,
            "function_level_unanswered_reason": (
                "sys.monitoring exceeded the 30% disturbance limit; cProfile was already distorted"
                if distorted
                else None
            ),
        },
        "distortion_evidence": {
            "sys_monitoring_threshold_percent": MAX_ACCEPTABLE_OVERHEAD_PERCENT,
            "cprofile_first_leg": CPROFILE_DISTORTION_EVIDENCE,
        },
    }
    validate_cpu_profile_report(report)
    return report


def _validate_point_common(point: Mapping[str, Any]) -> None:
    numeric(point.get("control_process_cpu_ms_per_request"), "control CPU/request")
    numeric(point.get("monitored_process_cpu_ms_per_request"), "monitored CPU/request")
    numeric(point.get("monitoring_overhead_percent"), "monitoring overhead")
    if numeric(point.get("monitoring_events"), "monitoring events") <= 0:
        raise AssertionError("PERF-03 sys.monitoring 没有捕获事件")
    if numeric(point.get("monitoring_amplification_factor"), "monitoring factor") <= 0:
        raise AssertionError("PERF-03 sys.monitoring 放大倍数无效")


def validate_cpu_profile_report(payload: Mapping[str, Any]) -> None:
    try:
        if payload.get("case") != CASE:
            raise AssertionError("PERF-03 报告 case 不匹配")
        if not re.fullmatch(r"[0-9a-f]{40}", str(payload.get("commit", ""))):
            raise AssertionError("PERF-03 报告 commit 不是完整 Git SHA")
        status = payload.get("status")
        if status not in {"FALLBACK_DISTORTED", "FUNCTION_ATTRIBUTION_ACCEPTED"}:
            raise AssertionError("PERF-03 报告 status 无效")
        method = payload.get("method")
        if not isinstance(method, Mapping) or method.get("default_enabled") is not False:
            raise AssertionError("PERF-03 剖析开关必须默认关闭")
        if method.get("production_gateway_hook") is not False:
            raise AssertionError("PERF-03 不得在生产 Gateway 增加剖析钩子")
        if method.get("profiler") != "sys.monitoring (PEP 669)":
            raise AssertionError("PERF-03 第二棒必须使用 sys.monitoring")
        workload = payload.get("workload")
        if not isinstance(workload, Mapping):
            raise AssertionError("PERF-03 报告缺少 workload")
        if workload.get("concurrencies") != list(PROFILE_CONCURRENCIES):
            raise AssertionError("PERF-03 必须测量并发 1 与 32")
        if workload.get("stub_fixture") != "FX-RT-03":
            raise AssertionError("PERF-03 必须复用 FX-RT-03")
        if not isinstance(workload.get("edge_in_path"), bool):
            raise AssertionError("PERF-03 必须声明是否经过 Edge")
        if not isinstance(workload.get("request_mix"), str) or not workload["request_mix"]:
            raise AssertionError("PERF-03 必须声明请求组合")

        environment = payload.get("environment")
        if not isinstance(environment, Mapping):
            raise AssertionError("PERF-03 报告缺少 environment")
        for label in ("load_before", "load_after"):
            snapshot = environment.get(label)
            if not isinstance(snapshot, Mapping) or not snapshot.get("uptime"):
                raise AssertionError(f"PERF-03 缺少 {label} uptime 快照")
            load_1m = numeric(snapshot.get("load_1m"), f"environment.{label}.load_1m")
            if load_1m > 2.0:
                raise AssertionError(f"PERF-03 {label} 1 分钟负载高于 2：{load_1m}")

        points = payload.get("points")
        if not isinstance(points, list) or [point.get("concurrency") for point in points] != [
            1,
            32,
        ]:
            raise AssertionError("PERF-03 并发点不完整或顺序错误")
        for point in points:
            _validate_point_common(point)

        answers = payload.get("answers")
        if (
            not isinstance(answers, Mapping)
            or answers.get("concurrency_question_answered") is not True
        ):
            raise AssertionError("PERF-03 必须回答并发是否增加 CPU/请求")
        delta = numeric(
            payload.get("clean_concurrency_32_minus_1_cpu_ms_per_request"),
            "clean concurrency delta",
        )
        if answers.get("does_concurrency_increase_cpu_per_request") is not (delta > 0):
            raise AssertionError("PERF-03 并发结论与干净测量不一致")

        if status == "FALLBACK_DISTORTED":
            if not any(
                numeric(point.get("monitoring_overhead_percent"), "monitoring overhead")
                > MAX_ACCEPTABLE_OVERHEAD_PERCENT
                for point in points
            ):
                raise AssertionError("PERF-03 兜底报告没有超过 30% 的失真证据")
            if answers.get("function_level_composition_answered") is not False:
                raise AssertionError("PERF-03 兜底报告不得声称已回答函数级构成")
            if any(
                "top_functions_by_self_cpu" in point or "module_self_cpu" in point
                for point in points
            ):
                raise AssertionError("PERF-03 兜底报告不得冻结受污染的函数榜")
        else:
            if any(
                numeric(point.get("monitoring_overhead_percent"), "monitoring overhead")
                > MAX_ACCEPTABLE_OVERHEAD_PERCENT
                for point in points
            ):
                raise AssertionError("PERF-03 超过 30% 时必须走兜底")
            if answers.get("function_level_composition_answered") is not True:
                raise AssertionError("PERF-03 可接受报告必须回答函数级构成")
            for point in points:
                functions = point.get("top_functions_by_self_cpu")
                if not isinstance(functions, list) or len(functions) < TOP_FUNCTION_COUNT:
                    raise AssertionError("PERF-03 每点至少需要前 10 个函数")
                modules = point.get("module_self_cpu")
                if not isinstance(modules, list):
                    raise AssertionError("PERF-03 缺少模块 CPU 归并")
                module_names = [row.get("module_group") for row in modules]
                if module_names != list(MODULE_GROUPS):
                    raise AssertionError("PERF-03 模块归并集合不完整")
                total_percent = sum(
                    numeric(row.get("self_percent"), "module self percent") for row in modules
                )
                if abs(total_percent - 100.0) > 0.000001:
                    raise AssertionError(f"PERF-03 模块占比之和不是 100：{total_percent}")
                known_groups = set(module_names)
                if any(function.get("module_group") not in known_groups for function in functions):
                    raise AssertionError("PERF-03 函数明细无法归并到模块")

        distortion = payload.get("distortion_evidence")
        if not isinstance(distortion, Mapping) or not isinstance(
            distortion.get("cprofile_first_leg"), Mapping
        ):
            raise AssertionError("PERF-03 缺少第一棒 cProfile 失真证据")
        rendered = json.dumps(payload, ensure_ascii=False)
        if "/workspace/" in rendered or str(harness.REPOSITORY_ROOT) in rendered:
            raise AssertionError("PERF-03 报告泄露绝对路径")
        assert_no_body(payload)
    except (AssertionError, KeyError, TypeError, ValueError) as error:
        raise BaselineReportError(str(error)) from error


def load_cpu_profile_report() -> Mapping[str, Any]:
    report = harness._load_mapping(REPORT_PATH, "PERF-03 冻结剖析报告")
    validate_cpu_profile_report(report)
    return report


def write_cpu_profile_if_enabled(
    *,
    enabled: bool = False,
    output: Path,
    measure: Callable[[], Mapping[str, Any]] = measure_cpu_profile,
) -> bool:
    """Write the artifact only when the diagnostic switch is explicit."""

    if not enabled:
        return False
    report = measure()
    validate_cpu_profile_report(report)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enabled", action="store_true", help="explicitly enable sys.monitoring")
    parser.add_argument(
        "--source-commit", help="source worktree commit when remote Git metadata is stale"
    )
    parser.add_argument("--write", type=Path, required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    wrote = write_cpu_profile_if_enabled(
        enabled=args.enabled,
        output=args.write,
        measure=lambda: measure_cpu_profile(source_commit=args.source_commit),
    )
    print(
        f"PERF-03 profile {'written' if wrote else 'disabled; artifact not written'}: "
        f"{args.write.name}"
    )


if __name__ == "__main__":
    main()

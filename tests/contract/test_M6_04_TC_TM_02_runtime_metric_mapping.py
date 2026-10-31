"""M6-04 / TC-TM-02 closed, versioned Runtime metric mappings."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from holm_runtimes import RuntimeCompatibilityError, load_runtime_compatibility

ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = ROOT / "runtime-compat.yaml"
CANONICAL = {
    "runtime_ttft_seconds",
    "runtime_tpot_seconds",
    "runtime_e2e_latency_seconds",
    "runtime_output_tokens_total",
    "runtime_requests_total",
    "runtime_requests_running",
    "runtime_requests_waiting",
    "runtime_queue_time_seconds",
    "runtime_kv_cache_usage_ratio",
    "runtime_errors_total",
}


def _payload() -> dict[str, object]:
    return cast(dict[str, object], json.loads(CATALOG_PATH.read_text(encoding="utf-8")))


def _metrics(payload: dict[str, object], runtime_index: int = 1) -> dict[str, object]:
    runtimes = cast(list[dict[str, object]], payload["runtimes"])
    return cast(dict[str, object], runtimes[runtime_index]["metrics"])


def _write(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "runtime-compat.yaml"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_M6_04_TC_TM_02_catalog_covers_full_table_and_marks_sglang_unsupported() -> None:
    catalog = load_runtime_compatibility(CATALOG_PATH)
    vllm = catalog.runtime("vllm")
    sglang = catalog.runtime("sglang")
    assert vllm is not None and vllm.metrics is not None
    assert sglang is not None and sglang.metrics is not None
    assert set(vllm.metrics) == CANONICAL
    assert set(sglang.metrics) == CANONICAL
    assert {name for name, source in sglang.metrics.items() if source is None} == {
        "runtime_tpot_seconds",
        "runtime_errors_total",
    }
    assert all(
        source is None or source.source != "sglang:gen_throughput"
        for runtime in (vllm, sglang)
        for source in runtime.metrics.values()
    )


@pytest.mark.parametrize("mutation", ("canonical", "type", "key", "throughput"))
def test_M6_04_TC_TM_02_manifest_guard_rejects_invalid_mapping_positive_controls(
    tmp_path: Path,
    mutation: str,
) -> None:
    payload = _payload()
    metrics = _metrics(payload)
    if mutation == "canonical":
        metrics["runtime_future_metric"] = metrics.pop("runtime_errors_total")
    elif mutation == "type":
        source = cast(dict[str, object], metrics["runtime_ttft_seconds"])
        source["type"] = "gauge"
    elif mutation == "key":
        source = cast(dict[str, object], metrics["runtime_ttft_seconds"])
        source["unit"] = "seconds"
    else:
        source = cast(dict[str, object], metrics["runtime_output_tokens_total"])
        source["source"] = "sglang:gen_throughput"

    with pytest.raises(RuntimeCompatibilityError):
        load_runtime_compatibility(_write(tmp_path, payload))

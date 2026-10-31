"""Exact mapping from collected certification tests to their primary report cases."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Final

CASE_BY_NODE_ID: Final[dict[str, str]] = {
    (
        "tests/certification/test_M2_14D_TC_DL_22_27_real_modelscope.py::"
        "test_M2_14D_real_small_model_reaches_ready_with_exactly_the_listed_files"
    ): "TC-DL-25",
    (
        "tests/certification/test_M2_14D_TC_DL_22_27_real_modelscope.py::"
        "test_TC_DL_22_M2_14D_deferred_run_is_reported_suspended_never_omitted"
    ): "TC-DL-22",
    (
        "tests/certification/test_M2_14D_TC_DL_22_27_real_modelscope.py::"
        "test_FX_MS_09_M2_14D_online_source_still_has_the_four_recorded_properties"
    ): "FX-MS-09",
    (
        "tests/certification/test_M2_14D_TC_DL_22_27_real_modelscope.py::"
        "test_TC_DL_27_M2_14D_sentinel_reaches_no_sink_on_a_real_success_and_failure"
    ): "TC-DL-27",
}


class CertificationMappingError(ValueError):
    """The collected certification suite and its report mapping diverged."""


def require_exact_mapping(collected_node_ids: Iterable[str]) -> None:
    """Fail unless every collected test has exactly one live mapping and vice versa."""

    collected = tuple(collected_node_ids)
    counts = Counter(collected)
    duplicates = sorted(node_id for node_id, count in counts.items() if count != 1)
    collected_set = set(collected)
    mapped_set = set(CASE_BY_NODE_ID)
    missing = sorted(collected_set - mapped_set)
    stale = sorted(mapped_set - collected_set)
    duplicate_cases = sorted(
        case_id for case_id, count in Counter(CASE_BY_NODE_ID.values()).items() if count != 1
    )
    if duplicates or missing or stale or duplicate_cases:
        raise CertificationMappingError(
            "certification report mapping is not an exact bijection: "
            f"duplicate_collected={duplicates}; unmapped_collected={missing}; "
            f"mapped_but_not_collected={stale}; duplicate_primary_cases={duplicate_cases}"
        )


__all__ = ["CASE_BY_NODE_ID", "CertificationMappingError", "require_exact_mapping"]

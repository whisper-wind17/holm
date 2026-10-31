"""Strict, machine-readable certification report construction."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

REPORT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "case_id",
        "suite",
        "layer",
        "gate",
        "environment",
        "release_ref",
        "fixture_refs",
        "parameters",
        "started_at",
        "finished_at",
        "result",
        "assertions",
        "evidence_uri",
        "deviation",
    }
)
RESULTS: Final[frozenset[str]] = frozenset({"passed", "failed", "suspended"})


def build_report(records: Sequence[Mapping[str, object]]) -> dict[str, Any]:
    """Validate TST 8.3 records and return their completion summary."""

    validated: list[dict[str, object]] = []
    for record in records:
        if set(record) != REPORT_FIELDS:
            raise ValueError("certification record fields do not match TST 8.3")
        result = record["result"]
        if not isinstance(result, str) or result not in RESULTS:
            raise ValueError("certification result is invalid")
        validated.append(dict(record))
    complete = bool(validated) and all(record["result"] == "passed" for record in validated)
    return {"records": validated, "certification_complete": complete}


def report_exit_code(report: Mapping[str, object]) -> int:
    """Return success only for a mechanically complete certification report."""

    return 0 if report.get("certification_complete") is True else 1


__all__ = ["REPORT_FIELDS", "RESULTS", "build_report", "report_exit_code"]

"""M2-14D: certification report's contract (TST 8.3).

Pure and offline, so it runs in CI even though the certification tier does not.

Interface provided by ``tests/certification/report.py``:

* ``build_report(records) -> dict`` -- validates every record, raising
  ``ValueError`` on a missing or extra field or a ``result`` outside the three
  TST 8.3 values, and returns a JSON-serialisable report with a top-level
  ``records`` list and a boolean ``certification_complete``.
* ``report_exit_code(report) -> int`` -- the exit status ``./dev cert-download``
  ends with.

The field names below are copied verbatim from TST 8.3 (lines 964-977: twelve
rows, two of which carry two fields each), not from the implementation.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pytest

TST_8_3_FIELDS = (
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
)


def _report_module() -> Any:
    # Imported per test, so a missing module is a red test, never a collection skip.
    return importlib.import_module("certification.report")


def _build() -> Callable[[Sequence[Mapping[str, object]]], dict[str, Any]]:
    return _report_module().build_report


def _exit_code(report: Mapping[str, object]) -> int:
    return int(_report_module().report_exit_code(report))


def _record(result: object = "passed", case_id: str = "TC-DL-22") -> dict[str, object]:
    return {
        "case_id": case_id,
        "suite": "TST 4.x download certification",
        "layer": "Fault",
        "gate": "P0",
        "environment": "C: development container",
        "release_ref": "0000000",
        "fixture_refs": [],
        "parameters": {},
        "started_at": "2026-09-14T00:00:00+00:00",
        "finished_at": "2026-09-14T01:00:00+00:00",
        "result": result,
        "assertions": {},
        "evidence_uri": "",
        "deviation": "",
    }


def test_M2_14D_TC_DL_22_record_fields_are_exactly_tst_8_3() -> None:
    assert len(TST_8_3_FIELDS) == 14
    report = json.loads(json.dumps(_build()([_record(), _record(case_id="TC-DL-27")])))

    assert [set(record) for record in report["records"]] == [set(TST_8_3_FIELDS)] * 2
    assert report["certification_complete"] is True
    assert _exit_code(report) == 0


@pytest.mark.parametrize("field", TST_8_3_FIELDS)
def test_M2_14D_TC_DL_22_a_record_missing_any_field_is_rejected(field: str) -> None:
    record = _record()
    del record[field]

    with pytest.raises(ValueError):
        _build()([record])


def test_M2_14D_TC_DL_22_a_record_with_an_extra_field_is_rejected() -> None:
    with pytest.raises(ValueError):
        _build()([{**_record(), "skipped": True}])


@pytest.mark.parametrize("result", ["skipped", "PASSED", "pass", "blocked_env", "", None, True])
def test_M2_14D_TC_DL_22_result_outside_the_three_values_is_rejected(result: object) -> None:
    with pytest.raises(ValueError):
        _build()([_record(result)])


@pytest.mark.parametrize(
    "results",
    [("suspended",), ("passed", "suspended"), ("suspended", "passed", "passed")],
)
def test_M2_14D_TC_DL_22_suspended_never_counts_as_a_pass(results: tuple[str, ...]) -> None:
    report = json.loads(json.dumps(_build()([_record(value) for value in results])))

    assert report["certification_complete"] is False
    assert _exit_code(report) != 0


@pytest.mark.parametrize("results", [("failed",), ("passed", "failed")])
def test_M2_14D_TC_DL_22_a_failed_case_exits_non_zero(results: tuple[str, ...]) -> None:
    assert _exit_code(_build()([_record(value) for value in results])) != 0


def test_M2_14D_TC_DL_22_an_empty_report_is_not_a_complete_certification() -> None:
    report = _build()([])

    assert report["certification_complete"] is False
    assert _exit_code(report) != 0

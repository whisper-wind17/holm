"""M2-14D rework 2: collected certification tests and report mapping are exact.

The baseline reads pytest's real collection and the real mapping.  Positive
controls mutate the mapping source itself, then load that changed source; they
do not inject a replacement mapping into the judge.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from certification import case_mapping
from fixtures.source_mutation import mutate_source

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CERTIFICATION_ROOT = PROJECT_ROOT / "tests" / "certification"
MAPPING_SOURCE = CERTIFICATION_ROOT / "case_mapping.py"


def _collect_certification(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["HOLM_CERTIFICATION_ROOT"] = str(tmp_path / "collection-report")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "tests/certification",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def _collected_node_ids(completed: subprocess.CompletedProcess[str]) -> tuple[str, ...]:
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return tuple(
        line.strip()
        for line in completed.stdout.splitlines()
        if line.startswith("tests/certification/") and "::" in line
    )


@contextmanager
def _mutated_real_mapping(anchor: str, replacement: str) -> Iterator[None]:
    """Mutate the actual configuration read by the collection subprocess, then restore it."""

    original = MAPPING_SOURCE.read_text(encoding="utf-8")
    mutated = mutate_source(MAPPING_SOURCE, [(anchor, replacement)])
    MAPPING_SOURCE.write_text(mutated, encoding="utf-8")
    try:
        yield
    finally:
        MAPPING_SOURCE.write_text(original, encoding="utf-8")
        assert MAPPING_SOURCE.read_text(encoding="utf-8") == original


def test_M2_14D_TC_DL_22_collected_certification_tests_exactly_match_report_mapping(
    tmp_path: Path,
) -> None:
    collected = _collected_node_ids(_collect_certification(tmp_path))

    assert collected
    assert set(collected) == set(case_mapping.CASE_BY_NODE_ID)
    case_mapping.require_exact_mapping(collected)


def test_M2_14D_TC_DL_22_positive_control_mapping_missing_a_real_test_fails(
    tmp_path: Path,
) -> None:
    anchor = """    (
        "tests/certification/test_M2_14D_TC_DL_22_27_real_modelscope.py::"
        "test_FX_MS_09_M2_14D_online_source_still_has_the_four_recorded_properties"
    ): "FX-MS-09",
"""
    with _mutated_real_mapping(anchor, ""):
        completed = _collect_certification(tmp_path)

    output = completed.stdout + completed.stderr
    assert completed.returncode != 0, output
    assert "unmapped_collected=" in output, output
    assert "online_source_still_has_the_four_recorded_properties" in output, output


def test_M2_14D_TC_DL_22_positive_control_mapping_with_nonexistent_test_fails(
    tmp_path: Path,
) -> None:
    anchor = "}\n\n\nclass CertificationMappingError"
    replacement = (
        '    "tests/certification/test_M2_14D_nonexistent.py::test_M2_14D_nonexistent": '
        '"TC-DL-NONEXISTENT",\n}\n\n\nclass CertificationMappingError'
    )
    with _mutated_real_mapping(anchor, replacement):
        completed = _collect_certification(tmp_path)

    output = completed.stdout + completed.stderr
    assert completed.returncode != 0, output
    assert "mapped_but_not_collected=" in output, output
    assert "test_M2_14D_nonexistent" in output, output

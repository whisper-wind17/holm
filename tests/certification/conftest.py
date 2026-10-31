"""Certification-only reuse of the real integration environment and report sink."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

_TESTS_ROOT = Path(__file__).resolve().parents[1]
_INTEGRATION_ROOT = _TESTS_ROOT / "integration"
for _path in (_TESTS_ROOT, _INTEGRATION_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from certification.case_mapping import (  # noqa: E402
    CASE_BY_NODE_ID,
    CertificationMappingError,
    require_exact_mapping,
)
from integration.conftest import EphemeralPostgres  # noqa: E402
from integration.conftest import ephemeral_postgres as ephemeral_postgres  # noqa: E402, F401
from migration_test_support import upgrade_head  # noqa: E402
from test_M2_14B_TC_DL_23_24_real_worker_end_to_end import _clear_database  # noqa: E402

_COLLECTED_NODE_IDS: list[str] = []
_RECORD_START_BY_NODE_ID: dict[str, int] = {}


def _certification_root() -> Path:
    return Path(
        os.environ.get("HOLM_CERTIFICATION_ROOT", _TESTS_ROOT.parent / "build/certification")
    )


def pytest_sessionstart() -> None:
    from certification import harness

    harness.reset_records()
    _COLLECTED_NODE_IDS.clear()
    _RECORD_START_BY_NODE_ID.clear()


def pytest_itemcollected(item: pytest.Item) -> None:
    """Capture every item before ``-k`` deselection can narrow the execution set."""

    _COLLECTED_NODE_IDS.append(item.nodeid)


def pytest_collection_finish() -> None:
    try:
        require_exact_mapping(_COLLECTED_NODE_IDS)
    except CertificationMappingError as exc:
        raise pytest.UsageError(str(exc)) from exc


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    from certification import harness

    _RECORD_START_BY_NODE_ID[item.nodeid] = harness.record_cursor()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> Iterator[None]:
    """Keep collected certification cases present in the report on every outcome."""

    outcome = yield
    test_report = outcome.get_result()
    case_id = CASE_BY_NODE_ID.get(item.nodeid)
    if case_id is None:
        raise pytest.UsageError(
            f"collected certification test has no report mapping: {item.nodeid}"
        )

    from certification import harness

    record_start = _RECORD_START_BY_NODE_ID[item.nodeid]
    exception = call.excinfo.value if call.excinfo is not None else None
    if isinstance(exception, harness.CertificationSourceUnavailable):
        harness.record_pytest_outcome(
            case_id,
            since=record_start,
            result="suspended",
            phase=call.when,
            exception_category=type(exception).__name__,
            deviation=exception.observation,
        )
        # Pytest has no suspended outcome. As with deferred TC-DL-22, the test
        # body ends normally while report_exit_code keeps the command non-zero.
        test_report.outcome = "passed"
        test_report.longrepr = None
        return

    if test_report.failed:
        category = type(exception).__name__ if exception is not None else "PytestFailure"
        harness.record_pytest_outcome(
            case_id,
            since=record_start,
            result="failed",
            phase=call.when,
            exception_category=category,
            deviation=f"pytest {call.when} failed; exception_category={category}",
        )
        return

    if (
        call.when == "call"
        and test_report.passed
        and not harness.has_record(case_id, since=record_start)
    ):
        harness.record_pytest_outcome(
            case_id,
            since=record_start,
            result="failed",
            phase=call.when,
            exception_category="MissingCertificationRecord",
            deviation="collected certification case completed without a report record",
        )
        test_report.outcome = "failed"
        test_report.longrepr = "collected certification case completed without a report record"


@pytest.fixture(autouse=True)
def _certification_context(
    ephemeral_postgres: EphemeralPostgres,
    request: pytest.FixtureRequest,
) -> Iterator[None]:
    from certification import harness

    upgrade_head(ephemeral_postgres.engine)
    _clear_database(ephemeral_postgres.engine)
    run_root = _certification_root() / "runs" / f"{request.node.name}-{uuid4().hex}"
    run_root.mkdir(parents=True)
    harness.install_context(ephemeral_postgres, run_root)
    try:
        yield
    finally:
        harness.clear_context()
        _clear_database(ephemeral_postgres.engine)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    from certification import harness, report

    if session.config.option.collectonly:
        return
    root = _certification_root()
    root.mkdir(parents=True, exist_ok=True)
    document = report.build_report(harness.records())
    destination = root / "report.json"
    temporary = root / f".report-{os.getpid()}.json"
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)
    if exitstatus == 0:
        session.exitstatus = report.report_exit_code(document)


__all__ = ["EphemeralPostgres", "ephemeral_postgres"]

"""M2-14D certification tier: real https://www.modelscope.cn, run only by ./dev cert-download.

Every assertion in this file is an acceptance criterion.

Every real download uses the
smallest repository that passes publication, ``BAAI/bge-small-zh-v1.5`` (14 files,
183.3 MiB, checked against the live listing), and TC-DL-22's >= 10 GiB
interruption run is deferred.  TC-DL-22 is still reported -- ``suspended``, with
the deferral written into ``deviation`` -- because a case that silently drops out
of the report is a pass by omission.

``tests/certification/harness.py`` provides the functions called below, each driving
the production download path against the real source.  Names and attributes are
the contract; how the harness obtains them is not prescribed.

``run_ready_download()`` -> object with ``repository``, ``artifact_status``,
    ``listed_files`` and ``listed_total_bytes`` (from the live listing),
    ``published_files``, ``staging_root_residue_before_publish``,
    ``delivered_bytes`` and ``elapsed_seconds``; staging starts empty.
``deferred_tc_dl_22_record()`` -> the TC-DL-22 record ./dev cert-download reports.
``probe_source_properties()`` -> object with the FX-MS-09 attributes used below.
``run_sentinel_downloads()`` -> object with ``repository``, ``sentinel``,
    ``success`` and ``failure`` (M2-14B ``_Surfaces``), ``success_status``,
    ``failure_status`` and ``report_bytes``.

Nothing here skips.  An unreachable source is a ``suspended`` record and a
non-zero exit from ./dev cert-download, never a pytest skip.
No assertion is about duration: throughput is recorded, not judged.
"""

from __future__ import annotations

import importlib
from typing import Any

from holm_domain import ModelArtifactStatus

SMALL_MODEL = "BAAI/bge-small-zh-v1.5"
MODELSCOPE_NOT_FOUND_CODE = 10010205001


def _harness() -> Any:
    # Imported per use, so a missing harness is a red test, never a collection skip.
    return importlib.import_module("certification.harness")


def test_M2_14D_real_small_model_reaches_ready_with_exactly_the_listed_files() -> None:
    run = _harness().run_ready_download()

    assert run.repository == SMALL_MODEL  # never a multi-GiB repository
    assert run.artifact_status == ModelArtifactStatus.READY.value
    assert run.listed_files, "the live listing was empty"
    assert "1_Pooling/config.json" in run.listed_files  # a nested path really travelled
    assert set(run.published_files) == set(run.listed_files)
    # Fresh staging: every listed byte was delivered once, no more and no less.
    assert run.delivered_bytes == run.listed_total_bytes
    assert run.elapsed_seconds > 0
    # Online publication: nothing left at the root for publication to trip on.
    assert tuple(run.staging_root_residue_before_publish) == ()


def test_TC_DL_22_M2_14D_deferred_run_is_reported_suspended_never_omitted() -> None:
    report_module = importlib.import_module("certification.report")
    record = _harness().deferred_tc_dl_22_record()

    report = report_module.build_report([record])
    assert record["case_id"] == "TC-DL-22"
    assert record["result"] == "suspended"
    assert "≥10 GiB" in str(record["deviation"])
    assert "延后" in str(record["deviation"])
    assert report["certification_complete"] is False
    assert report_module.report_exit_code(report) != 0


def test_FX_MS_09_M2_14D_online_source_still_has_the_four_recorded_properties() -> None:
    """TST line 253.  A live mismatch is a baseline conflict: stop and ask (card)."""

    live = _harness().probe_source_properties()

    # (a) no repository-level immutable commit; Revision is the branch name
    assert live.repository_level_commit is None
    assert live.metadata_revision_field == live.requested_branch
    assert live.per_file_commit_shas and all(live.per_file_commit_shas.values())
    # (b) a missing revision is 200 / Success:true / Files:null -- null, not []
    assert live.missing_revision_status == 200
    assert live.missing_revision_body["Success"] is True
    assert "Files" in live.missing_revision_body
    assert live.missing_revision_body["Files"] is None
    # (c) not_found is observable without a credential
    assert live.missing_repo_anonymous_status == 404
    assert live.missing_repo_anonymous_code == MODELSCOPE_NOT_FOUND_CODE
    # (d) the verdict comes from the numeric Code, not the Chinese Message
    assert live.verdict_live is not None
    assert live.verdict_with_message_replaced == live.verdict_live


def test_TC_DL_27_M2_14D_sentinel_reaches_no_sink_on_a_real_success_and_failure() -> None:
    from test_M2_14B_TC_DL_27_worker_path_and_sse_sentinel import _scan

    run = _harness().run_sentinel_downloads()

    assert run.repository == SMALL_MODEL
    assert run.success_status == ModelArtifactStatus.READY.value
    assert run.failure_status != ModelArtifactStatus.READY.value
    for surfaces in (run.success, run.failure):
        sizes = surfaces.sizes()
        assert sizes["sse_frames"] > 0, sizes
        assert sizes["process_command_lines"] > 0, sizes
        assert sizes["process_environments"] > 0, sizes
        assert sizes["database_plaintext"] > 0, sizes
        # pinned empty until the Worker configures logging.
        assert surfaces.worker_logs == ("",), surfaces.worker_logs
        assert _scan(surfaces, run.sentinel) == ()
    assert len(run.success.non_empty_files()) >= 5
    # The certification report is a sink too.
    assert run.report_bytes
    assert run.sentinel.encode("utf-8") not in run.report_bytes

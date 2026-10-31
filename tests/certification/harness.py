"""Real-network M2-14D harness built from the shipped Worker and source adapters."""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import httpx
from certification.report import build_report
from holm_control_api.model_resolution import ResolutionRequest, ResolutionSucceeded
from holm_control_api.modelscope_resolution import classify_reply
from holm_control_api.modelscope_source import (
    MODELSCOPE_OFFICIAL_ENDPOINT,
    ModelScopeReply,
    model_path,
    repository_files_path,
)
from holm_domain import (
    ARTIFACT_MANIFEST_FILENAME,
    Identified,
    ModelArtifactStatus,
    ModelOriginType,
    deserialize_artifact_manifest,
)
from holm_download_worker.artifact_publishing import judge_artifact_health
from holm_persistence import DEFAULT_REQUESTED_REVISION
from huggingface_hub import get_session
from test_M2_14B_TC_DL_23_24_real_worker_end_to_end import (
    _bench,
    _clear_database,
    _rows,
)
from test_M2_14B_TC_DL_27_worker_path_and_sse_sentinel import (
    _exercise_shipped_worker,
    _seed_credential,
    _sentinel,
)

SMALL_MODEL: Final[str] = "BAAI/bge-small-zh-v1.5"
DEFAULT_BRANCH: Final[str] = "master"
MISSING_REVISION: Final[str] = "holm-missing-revision-20260914"
MISSING_REPOSITORY: Final[str] = "holm-certification/missing-model-20260914"
HUGGING_FACE_UNUSED_ENDPOINT: Final[str] = "https://huggingface.co"
_SOURCE_UNAVAILABLE_HTTP_STATUSES: Final[frozenset[int]] = frozenset({502, 503, 504})


class CertificationSourceUnavailable(RuntimeError):
    """A measured transport outage, kept distinct from a failed judgement."""

    def __init__(self, observation: str) -> None:
        super().__init__(observation)
        self.observation = observation


@dataclass(frozen=True, slots=True)
class _Context:
    database: Any
    root: Path


@dataclass(frozen=True, slots=True)
class ReadyDownloadRun:
    repository: str
    artifact_status: str
    listed_files: tuple[str, ...]
    listed_total_bytes: int
    published_files: tuple[str, ...]
    staging_root_residue_before_publish: tuple[str, ...]
    delivered_bytes: int
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class SourceProperties:
    repository_level_commit: None
    metadata_revision_field: str
    requested_branch: str
    per_file_commit_shas: dict[str, str]
    missing_revision_status: int
    missing_revision_body: dict[str, object]
    missing_repo_anonymous_status: int
    missing_repo_anonymous_code: int | None
    verdict_live: str
    verdict_with_message_replaced: str


@dataclass(frozen=True, slots=True)
class SentinelDownloadsRun:
    repository: str
    sentinel: str
    success: Any
    failure: Any
    success_status: str
    failure_status: str
    report_bytes: bytes


_context: _Context | None = None
_records: list[dict[str, object]] = []
_release_ref: str | None = None


def install_context(database: Any, root: Path) -> None:
    global _context
    if _context is not None:
        raise RuntimeError("certification context is already installed")
    _context = _Context(database=database, root=root)


def clear_context() -> None:
    global _context
    _context = None


def reset_records() -> None:
    _records.clear()


def records() -> tuple[dict[str, object], ...]:
    return tuple(dict(record) for record in _records)


def record_cursor() -> int:
    """Return a stable boundary for records emitted by one pytest item."""

    return len(_records)


def has_record(case_id: str, *, since: int = 0) -> bool:
    return any(record["case_id"] == case_id for record in _records[since:])


def _require_context() -> _Context:
    if _context is None:
        raise RuntimeError("certification context is unavailable")
    return _context


def _commit() -> str:
    global _release_ref
    if _release_ref is None:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        _release_ref = result.stdout.strip()
    return _release_ref


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _record(
    case_id: str,
    *,
    started_at: str,
    result: str,
    assertions: dict[str, object],
    parameters: dict[str, object] | None = None,
    fixture_refs: list[object] | None = None,
    deviation: str = "",
) -> dict[str, object]:
    value: dict[str, object] = {
        "case_id": case_id,
        "suite": "TST Download certification",
        "layer": "Adapter/Fault/Security",
        "gate": "P0",
        "environment": "C: development container; real ModelScope network",
        "release_ref": _commit(),
        "fixture_refs": fixture_refs or [],
        "parameters": parameters or {},
        "started_at": started_at,
        "finished_at": _now(),
        "result": result,
        "assertions": assertions,
        "evidence_uri": "report.json",
        "deviation": deviation,
    }
    _records.append(value)
    return value


def record_pytest_outcome(
    case_id: str,
    *,
    since: int,
    result: str,
    phase: str,
    exception_category: str,
    deviation: str,
) -> None:
    """Make the report reflect a collected test even when its body aborts early."""

    assertions = {
        "pytest_phase": phase,
        "exception_category": exception_category,
    }
    if since < 0 or since > len(_records):
        raise ValueError("record cursor is outside the current certification run")
    emitted = _records[since:]
    if not any(record["case_id"] == case_id for record in emitted):
        _record(
            case_id,
            started_at=_now(),
            result=result,
            assertions=assertions,
            deviation=deviation,
        )
        emitted = _records[since:]
    for record in emitted:
        record["result"] = result
        prior_assertions = record.get("assertions")
        record["assertions"] = {
            **(prior_assertions if isinstance(prior_assertions, dict) else {}),
            **assertions,
        }
        record["deviation"] = deviation
        record["finished_at"] = _now()


def _source_unavailable_observation(
    *, exc: BaseException | None, status_code: int | None, elapsed_seconds: float
) -> str:
    categories: list[str] = []
    errno: int | None = None
    current = exc
    while current is not None:
        category = type(current).__name__
        if category not in categories:
            categories.append(category)
        if errno is None and isinstance(current, OSError):
            errno = current.errno
        current = current.__cause__ or current.__context__
    observations = [f"observed_wait_seconds={elapsed_seconds:.3f}"]
    if categories:
        observations.insert(0, f"exception_category={'/'.join(categories)}")
    if errno is not None:
        observations.append(f"errno={errno}")
    if status_code is not None:
        observations.insert(0, f"http_status={status_code}")
    return "source unreachable: " + "; ".join(observations)


def _record_source_suspended(case_id: str, exc: CertificationSourceUnavailable) -> None:
    _record(
        case_id,
        started_at=_now(),
        result="suspended",
        parameters={"repository": SMALL_MODEL},
        assertions={"source_reachability": "unavailable"},
        deviation=exc.observation,
    )


def _live_resolution() -> Any:
    from holm_control_api.modelscope_resolution import ModelScopeResolutionAdapter

    outcome = asyncio.run(
        ModelScopeResolutionAdapter(
            endpoint=MODELSCOPE_OFFICIAL_ENDPOINT, timeout_seconds=60
        ).resolve(
            ResolutionRequest(
                source=ModelOriginType.MODELSCOPE,
                repository_id=SMALL_MODEL,
                requested_revision_norm=DEFAULT_REQUESTED_REVISION,
            ),
            credential=None,
        )
    )
    if not isinstance(outcome, ResolutionSucceeded):
        raise RuntimeError("live ModelScope resolution did not succeed")
    if not isinstance(outcome.resolved.files, Identified) or not isinstance(
        outcome.resolved.total_size_bytes, Identified
    ):
        raise RuntimeError("live ModelScope listing was not identified")
    return outcome.resolved


def _artifact_result(database: Any) -> tuple[dict[str, Any], Any, tuple[str, ...]]:
    rows = _rows(database.engine, "model_artifacts")
    if len(rows) != 1:
        raise RuntimeError("certification expected exactly one artifact")
    row = rows[0]
    artifact_path = Path(str(row["path"]))
    manifest = deserialize_artifact_manifest(
        (artifact_path / ARTIFACT_MANIFEST_FILENAME).read_bytes()
    )
    health = judge_artifact_health(artifact_path, str(row["manifest_hash"]), full=True)
    if health.status is not ModelArtifactStatus.READY:
        raise RuntimeError("published artifact failed full health validation")
    return row, manifest, _publication_residue(artifact_path, tuple(e.path for e in manifest.files))


def _publication_residue(artifact_path: Path, listed_files: tuple[str, ...]) -> tuple[str, ...]:
    expected = set(listed_files)
    residue: list[str] = []
    for path in artifact_path.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(artifact_path).as_posix()
        if (
            relative == ARTIFACT_MANIFEST_FILENAME
            or relative in expected
            or relative.startswith(".cache/modelscope/")
        ):
            continue
        residue.append(relative)
    return tuple(sorted(residue))


def _bench_root(context: _Context, name: str) -> Path:
    root = context.root / name
    root.mkdir(mode=0o700)
    return root


def run_ready_download() -> ReadyDownloadRun:
    started_at = _now()
    started = time.monotonic()
    context = _require_context()
    try:
        _assert_source_reachable()
        resolved = _live_resolution()
    except CertificationSourceUnavailable as exc:
        _record_source_suspended("TC-DL-25", exc)
        raise
    bench = _bench(
        context.database,
        _bench_root(context, "ready"),
        HUGGING_FACE_UNUSED_ENDPOINT,
        modelscope_endpoint=MODELSCOPE_OFFICIAL_ENDPOINT,
    )
    _, terminal = asyncio.run(
        _exercise_shipped_worker(
            bench,
            context.database,
            worker_name="m2-14d-ready",
            sentinel="PWS_M214D_NO_CREDENTIAL",
            source=ModelOriginType.MODELSCOPE,
            repository_id=SMALL_MODEL,
            revision=DEFAULT_BRANCH,
            timeout_seconds=None,
        )
    )
    row, manifest, residue = _artifact_result(context.database)
    listed_files = tuple(file.path for file in resolved.files.value)
    published_files = tuple(entry.path for entry in manifest.files)
    elapsed = time.monotonic() - started
    delivered = int(terminal["downloaded_bytes"])
    total = int(resolved.total_size_bytes.value)
    result = ReadyDownloadRun(
        repository=SMALL_MODEL,
        artifact_status=str(row["status"]),
        listed_files=listed_files,
        listed_total_bytes=total,
        published_files=published_files,
        staging_root_residue_before_publish=residue,
        delivered_bytes=delivered,
        elapsed_seconds=elapsed,
    )
    _record(
        "TC-DL-25",
        started_at=started_at,
        result="passed",
        parameters={"repository": SMALL_MODEL},
        assertions={
            "artifact_status": result.artifact_status,
            "listed_files": len(listed_files),
            "published_files": len(published_files),
            "listed_total_bytes": total,
            "delivered_bytes": delivered,
            "elapsed_seconds": elapsed,
            "bytes_per_second": delivered / elapsed,
            "staging_root_residue": list(residue),
        },
    )
    return result


def deferred_tc_dl_22_record() -> dict[str, object]:
    return _record(
        "TC-DL-22",
        started_at=_now(),
        result="suspended",
        parameters={"minimum_repository_bytes": 10 * 1024**3},
        assertions={"interruption_phases_executed": 0},
        deviation="≥10 GiB 暂停、SIGKILL 与 DNS 屏蔽续传延后到后续版本认证。",
    )


def _json_response(
    path: str, *, params: dict[str, str] | None = None
) -> tuple[int, dict[str, Any]]:
    started = time.monotonic()
    try:
        response = get_session().get(
            f"{MODELSCOPE_OFFICIAL_ENDPOINT.rstrip('/')}{path}",
            params=params,
            timeout=60,
        )
    except httpx.TransportError as exc:
        raise CertificationSourceUnavailable(
            _source_unavailable_observation(
                exc=exc,
                status_code=None,
                elapsed_seconds=time.monotonic() - started,
            )
        ) from exc
    if response.status_code in _SOURCE_UNAVAILABLE_HTTP_STATUSES:
        raise CertificationSourceUnavailable(
            _source_unavailable_observation(
                exc=None,
                status_code=response.status_code,
                elapsed_seconds=time.monotonic() - started,
            )
        )
    document = json.loads(response.content)
    if not isinstance(document, dict):
        raise RuntimeError("ModelScope response was not an object")
    return response.status_code, document


def _assert_source_reachable() -> None:
    _json_response(model_path(SMALL_MODEL))


def _source_verdict(status: int, document: dict[str, Any]) -> str:
    code = document.get("Code")
    data = document.get("Data")
    reply = ModelScopeReply(
        http_status=status,
        code=code if isinstance(code, int) and not isinstance(code, bool) else None,
        data=data if isinstance(data, dict) else None,
    )
    return type(classify_reply(reply)).__name__


def probe_source_properties() -> SourceProperties:
    started_at = _now()
    try:
        metadata_status, metadata = _json_response(model_path(SMALL_MODEL))
        listing_status, listing = _json_response(
            repository_files_path(SMALL_MODEL),
            params={"Revision": DEFAULT_BRANCH, "Recursive": "True"},
        )
        missing_revision_status, missing_revision = _json_response(
            repository_files_path(SMALL_MODEL),
            params={"Revision": MISSING_REVISION, "Recursive": "True"},
        )
        missing_status, missing = _json_response(model_path(MISSING_REPOSITORY))
    except CertificationSourceUnavailable as exc:
        _record_source_suspended("FX-MS-09", exc)
        raise
    metadata_data = metadata.get("Data")
    listing_data = listing.get("Data")
    missing_revision_data = missing_revision.get("Data")
    if not isinstance(metadata_data, dict) or not isinstance(listing_data, dict):
        raise RuntimeError("live ModelScope metadata or listing was unreadable")
    files = listing_data.get("Files")
    if not isinstance(files, list):
        raise RuntimeError("live ModelScope listing did not contain files")
    shas = {
        str(entry["Path"]): str(entry["Revision"])
        for entry in files
        if isinstance(entry, dict) and entry.get("Type") == "blob"
    }
    if not isinstance(missing_revision_data, dict):
        raise RuntimeError("missing-revision response had no data object")
    missing_body = {
        "Success": missing_revision.get("Success"),
        "Files": missing_revision_data.get("Files"),
    }
    replaced = dict(missing)
    replaced["Message"] = "replacement ignored by numeric-code judgement"
    result = SourceProperties(
        repository_level_commit=None,
        metadata_revision_field=str(metadata_data.get("Revision")),
        requested_branch=DEFAULT_BRANCH,
        per_file_commit_shas=shas,
        missing_revision_status=missing_revision_status,
        missing_revision_body=missing_body,
        missing_repo_anonymous_status=missing_status,
        missing_repo_anonymous_code=(
            int(missing["Code"]) if isinstance(missing.get("Code"), int) else None
        ),
        verdict_live=_source_verdict(missing_status, missing),
        verdict_with_message_replaced=_source_verdict(missing_status, replaced),
    )
    common = {
        "metadata_status": metadata_status,
        "listing_status": listing_status,
        "metadata_revision": result.metadata_revision_field,
        "per_file_commit_shas": len(set(shas.values())),
    }
    for case_id, assertions in (
        ("TC-DL-28", {**common, "repository_level_commit": None}),
        ("TC-DL-29", {"missing_revision_status": missing_revision_status, **missing_body}),
        (
            "TC-DL-30",
            {
                "missing_repo_status": missing_status,
                "missing_repo_code": result.missing_repo_anonymous_code,
            },
        ),
        (
            "TC-DL-31",
            {
                "verdict_unchanged_after_message_replacement": result.verdict_live
                == result.verdict_with_message_replaced
            },
        ),
    ):
        _record(
            case_id,
            started_at=started_at,
            result="passed",
            fixture_refs=[{"id": "FX-MS-09", "source": "live", "recorded_at": "2026-09-04"}],
            assertions=assertions,
        )
    _record(
        "FX-MS-09",
        started_at=started_at,
        result="passed",
        fixture_refs=[{"id": "FX-MS-09", "source": "live", "recorded_at": "2026-09-04"}],
        assertions={"online_properties_checked": 4},
    )
    return result


def run_sentinel_downloads() -> SentinelDownloadsRun:
    started_at = _now()
    context = _require_context()
    try:
        # This measured probe prevents an unreachable source from entering the
        # Worker's unbounded real-download wait. It is not a download deadline:
        # once reachable, the real transfers below retain no total time limit.
        _assert_source_reachable()
    except CertificationSourceUnavailable as exc:
        _record_source_suspended("TC-DL-27", exc)
        raise
    sentinel = _sentinel()
    success_bench = _bench(
        context.database,
        _bench_root(context, "sentinel-success"),
        HUGGING_FACE_UNUSED_ENDPOINT,
        modelscope_endpoint=MODELSCOPE_OFFICIAL_ENDPOINT,
    )
    _seed_credential(context.database.engine, ModelOriginType.MODELSCOPE, sentinel)
    success_started = time.monotonic()
    success, success_terminal = asyncio.run(
        _exercise_shipped_worker(
            success_bench,
            context.database,
            worker_name="m2-14d-sentinel-success",
            sentinel=sentinel,
            source=ModelOriginType.MODELSCOPE,
            repository_id=SMALL_MODEL,
            revision=DEFAULT_BRANCH,
            timeout_seconds=None,
        )
    )
    success_elapsed = time.monotonic() - success_started
    success_rows = _rows(context.database.engine, "model_artifacts")
    success_status = str(success_rows[0]["status"]) if len(success_rows) == 1 else "Missing"

    _clear_database(context.database.engine)
    failure_root = _bench_root(context, "sentinel-failure")
    rejected_tokenizer_target = failure_root / "rejected-tokenizer-target"
    rejected_tokenizer_target.mkdir(mode=0o700)
    rejected_tokenizer_root = failure_root / "rejected-tokenizer-link"
    rejected_tokenizer_root.symlink_to(rejected_tokenizer_target, target_is_directory=True)
    failure_bench = replace(
        _bench(
            context.database,
            failure_root,
            HUGGING_FACE_UNUSED_ENDPOINT,
            modelscope_endpoint=MODELSCOPE_OFFICIAL_ENDPOINT,
        ),
        tokenizer_root=rejected_tokenizer_root,
    )
    _seed_credential(context.database.engine, ModelOriginType.MODELSCOPE, sentinel)
    failure_started = time.monotonic()
    failure, failure_terminal = asyncio.run(
        _exercise_shipped_worker(
            failure_bench,
            context.database,
            worker_name="m2-14d-sentinel-failure",
            sentinel=sentinel,
            source=ModelOriginType.MODELSCOPE,
            repository_id=SMALL_MODEL,
            revision=DEFAULT_BRANCH,
            timeout_seconds=None,
        )
    )
    failure_elapsed = time.monotonic() - failure_started
    failure_status = str(failure_terminal["status"])
    success_delivered = int(success_terminal["downloaded_bytes"])
    failure_delivered = int(failure_terminal["downloaded_bytes"])
    tc_record = _record(
        "TC-DL-27",
        started_at=started_at,
        result="passed",
        parameters={"repository": SMALL_MODEL, "success_runs": 1, "failure_runs": 1},
        assertions={
            "success_status": success_status,
            "failure_status": failure_status,
            "sink_count": 6,
            "worker_log_sink_empty": True,
            "success_delivered_bytes": success_delivered,
            "success_elapsed_seconds": success_elapsed,
            "success_bytes_per_second": success_delivered / success_elapsed,
            "failure_delivered_bytes": failure_delivered,
            "failure_elapsed_seconds": failure_elapsed,
            "failure_bytes_per_second": failure_delivered / failure_elapsed,
        },
    )
    report_bytes = json.dumps(build_report([tc_record]), ensure_ascii=False).encode("utf-8")
    return SentinelDownloadsRun(
        repository=SMALL_MODEL,
        sentinel=sentinel,
        success=success,
        failure=failure,
        success_status=success_status,
        failure_status=failure_status,
        report_bytes=report_bytes,
    )


__all__ = [
    "CertificationSourceUnavailable",
    "ReadyDownloadRun",
    "SentinelDownloadsRun",
    "SourceProperties",
    "clear_context",
    "deferred_tc_dl_22_record",
    "has_record",
    "install_context",
    "probe_source_properties",
    "record_cursor",
    "record_pytest_outcome",
    "records",
    "reset_records",
    "run_ready_download",
    "run_sentinel_downloads",
]

"""TC-DL-09/10/25: real validation inputs and observable publish ordering."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fixtures.fake_model_source import (
    ValidationFailureKind,
    materialize_validation_failure,
    validation_failure_sample,
)
from holm_domain import (
    ARTIFACT_MANIFEST_FILENAME,
    ArtifactManifest,
    ArtifactManifestEntry,
    ArtifactManifestError,
    DownloadFailureReason,
    Identified,
    ModelArtifactStatus,
    ModelOriginType,
    RemoteFile,
    ResolvedModel,
    RevisionImmutability,
    Unrecognized,
    artifact_manifest_hash,
    build_artifact_manifest,
    serialize_artifact_manifest,
)
from holm_download_worker.artifact_publishing import (
    CACHEDIR_SIGNATURE,
    ArtifactHealthReason,
    ArtifactSystemCalls,
    ArtifactValidationError,
    ArtifactVerifier,
    RuntimeCapability,
    RuntimeCompatibilityStatus,
    judge_artifact_health,
    judge_runtime_compatibility,
    publish_verified_artifact,
)


def _safetensors_bytes() -> bytes:
    header = json.dumps(
        {
            "fixture.weight": {
                "data_offsets": [0, 4],
                "dtype": "F32",
                "shape": [1],
            }
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return len(header).to_bytes(8, "little") + header + b"data"


def _write_valid(root: Path, *, extra: dict[str, bytes] | None = None) -> None:
    root.mkdir(parents=True)
    (root / "config.json").write_text(
        '{"architectures":["FixtureForCausalLM"],"model_type":"fixture"}',
        encoding="utf-8",
    )
    (root / "tokenizer.json").write_text('{"version":"1.0"}', encoding="utf-8")
    (root / "model.safetensors").write_bytes(_safetensors_bytes())
    for relative, content in (extra or {}).items():
        target = root.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def _remote_files(root: Path) -> tuple[RemoteFile, ...]:
    values: list[RemoteFile] = []
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        if CACHEDIR_SIGNATURE in path.read_bytes()[: len(CACHEDIR_SIGNATURE) + 2]:
            continue
        if any(
            (ancestor / "CACHEDIR.TAG").is_file()
            and (ancestor / "CACHEDIR.TAG").read_bytes().splitlines()[0] == CACHEDIR_SIGNATURE
            for ancestor in path.parents
            if ancestor != root and ancestor.is_relative_to(root)
        ):
            continue
        content = path.read_bytes()
        values.append(
            RemoteFile(
                path.relative_to(root).as_posix(),
                len(content),
                Identified(hashlib.sha256(content).hexdigest()),
            )
        )
    return tuple(values)


def _resolved(root: Path) -> ResolvedModel:
    return ResolvedModel(
        source=ModelOriginType.HUGGING_FACE,
        repository_id="fixture/model",
        requested_revision_norm="main",
        resolved_revision="a" * 40,
        revision_immutability=RevisionImmutability.IMMUTABLE,
        files=Identified(_remote_files(root)),
        total_size_bytes=Identified(sum(entry.size_bytes for entry in _remote_files(root))),
        architecture=Identified("FixtureForCausalLM"),
        modalities=Unrecognized(),
        precision=Unrecognized(),
        quantization=Unrecognized(),
        context_length=Unrecognized(),
        candidate_runtimes=Unrecognized(),
        license=Unrecognized(),
    )


def _resolved_materialized(materialized: Any) -> ResolvedModel:
    files = tuple(
        RemoteFile(value.relative_path, value.size_bytes, Identified(value.sha256))
        for value in materialized.expected_files
    )
    return replace(_resolved(materialized.root), files=Identified(files))


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        (
            ValidationFailureKind.HASH_MISMATCH,
            DownloadFailureReason.ARTIFACT_MANIFEST_HASH_FAILED,
        ),
        (
            ValidationFailureKind.SHARD_MISSING,
            DownloadFailureReason.ARTIFACT_SAFETENSORS_INVALID,
        ),
        (
            ValidationFailureKind.SAFETENSORS_HEADER_CORRUPT,
            DownloadFailureReason.ARTIFACT_SAFETENSORS_INVALID,
        ),
        (
            ValidationFailureKind.CONFIG_OR_TOKENIZER_MISSING,
            DownloadFailureReason.ARTIFACT_REQUIRED_FILES_INVALID,
        ),
    ],
)
def test_TC_DL_09_FX_MS_05_materialized_failures_are_distinguishable(
    tmp_path: Path,
    kind: ValidationFailureKind,
    reason: DownloadFailureReason,
) -> None:
    materialized = materialize_validation_failure(validation_failure_sample(kind), tmp_path)

    with pytest.raises(ArtifactValidationError) as captured:
        ArtifactVerifier().verify(materialized.root, _resolved_materialized(materialized))

    assert captured.value.reason is reason
    assert any(materialized.root.iterdir()), "FX-MS-05 remained descriptive instead of materialized"


def test_TC_DL_09_file_list_rejects_parent_path_and_size_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "artifact"
    _write_valid(root)
    resolved = _resolved(root)
    assert isinstance(resolved.files, Identified)
    unsafe = replace(
        resolved,
        files=Identified(
            (
                *resolved.files.value,
                RemoteFile("../escape", 0, Identified("0" * 64)),
            )
        ),
    )
    with pytest.raises(ArtifactValidationError) as captured:
        ArtifactVerifier().verify(root, unsafe)
    assert captured.value.reason is DownloadFailureReason.ARTIFACT_FILE_LIST_INVALID

    first = resolved.files.value[0]
    wrong_size = replace(
        resolved,
        files=Identified(
            (replace(first, size_bytes=first.size_bytes + 1), *resolved.files.value[1:])
        ),
    )
    with pytest.raises(ArtifactValidationError) as captured:
        ArtifactVerifier().verify(root, wrong_size)
    assert captured.value.reason is DownloadFailureReason.ARTIFACT_FILE_LIST_INVALID


@pytest.mark.parametrize(
    "remote_code_configuration",
    [
        '{"architectures":["FixtureForCausalLM"],"auto_map":{"AutoModel":"model.py"}}',
        '{"architectures":["FixtureForCausalLM"],"trust_remote_code":true}',
    ],
    ids=["auto-map", "explicit-trust-remote-code"],
)
def test_TC_DL_09_special_file_and_remote_code_each_fail_closed(
    tmp_path: Path,
    remote_code_configuration: str,
) -> None:
    special = tmp_path / "special"
    _write_valid(special)
    os.mkfifo(special / "source.fifo")
    with pytest.raises(ArtifactValidationError) as captured:
        ArtifactVerifier().verify(special, _resolved(special))
    assert captured.value.reason is DownloadFailureReason.ARTIFACT_UNSAFE_FILE

    remote_code = tmp_path / "remote-code"
    _write_valid(remote_code)
    (remote_code / "config.json").write_text(
        remote_code_configuration,
        encoding="utf-8",
    )
    with pytest.raises(ArtifactValidationError) as captured:
        ArtifactVerifier().verify(remote_code, _resolved(remote_code))
    assert captured.value.reason is DownloadFailureReason.REMOTE_CODE_NOT_ALLOWED


def test_TC_DL_09_runtime_match_is_explicit_and_nonempty_rules_really_decide(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifact"
    _write_valid(root)
    resolved = _resolved(root)
    assert judge_runtime_compatibility("FixtureForCausalLM", ()) is (
        RuntimeCompatibilityStatus.NO_MATCHABLE_ENTRIES
    )
    partial = ArtifactVerifier().verify(root, resolved)
    assert partial.runtime_compatibility is RuntimeCompatibilityStatus.NO_MATCHABLE_ENTRIES

    matching = RuntimeCapability("fixture", frozenset({"FixtureForCausalLM"}))
    assert ArtifactVerifier((matching,)).verify(root, resolved).runtime_compatibility is (
        RuntimeCompatibilityStatus.MATCHED
    )
    incompatible = RuntimeCapability("fixture", frozenset({"AnotherArchitecture"}))
    with pytest.raises(ArtifactValidationError) as captured:
        ArtifactVerifier((incompatible,)).verify(root, resolved)
    assert captured.value.reason is DownloadFailureReason.ARTIFACT_RUNTIME_INCOMPATIBLE


def test_TC_DL_10_manifest_scope_uses_cachedir_marker_and_keeps_unknown_files(
    tmp_path: Path,
) -> None:
    hashes: list[str] = []
    for ordinal in range(2):
        root = tmp_path / f"machine-{ordinal}"
        _write_valid(
            root,
            extra={
                ".gitattributes": b"*.safetensors filter=lfs",
                "future-sdk-file.unknown": b"must be evidence",
            },
        )
        cache = root / f"private-cache-{ordinal}"
        cache.mkdir()
        (cache / "CACHEDIR.TAG").write_bytes(CACHEDIR_SIGNATURE + b"\n")
        (cache / "download.metadata").write_text(
            f"revision\nblob\n{1720000000.0 + ordinal}", encoding="utf-8"
        )
        (cache / f"random-{ordinal}.incomplete").write_bytes(b"partial")

        verified = ArtifactVerifier().verify(root, _resolved(root))
        paths = {entry.path for entry in verified.manifest.files}
        assert ".gitattributes" in paths
        assert "future-sdk-file.unknown" in paths
        assert not any("private-cache" in path for path in paths)
        hashes.append(verified.manifest_hash)

    assert hashes[0] == hashes[1], "cache timestamps leaked into the canonical Manifest"

    naive_hashes = []
    for ordinal in range(2):
        root = tmp_path / f"machine-{ordinal}"
        entries = []
        for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
            content = path.read_bytes()
            entries.append(
                ArtifactManifestEntry(
                    path.relative_to(root).as_posix(),
                    len(content),
                    hashlib.sha256(content).hexdigest(),
                )
            )
        naive_hashes.append(
            artifact_manifest_hash(serialize_artifact_manifest(build_artifact_manifest(entries)))
        )
    assert naive_hashes[0] != naive_hashes[1], "positive control did not include cache entropy"


def test_TC_DL_10_non_signature_cachedir_tag_keeps_repository_files(tmp_path: Path) -> None:
    decoy_root = tmp_path / "repository-cachedir-tag"
    _write_valid(decoy_root)
    ordinary_directory = decoy_root / "ordinary-directory"
    ordinary_directory.mkdir()
    (ordinary_directory / "CACHEDIR.TAG").write_bytes(b"not a cache tag\n")
    (ordinary_directory / "repository-data.bin").write_bytes(b"must remain in the Manifest")
    decoy_manifest = ArtifactVerifier().verify(decoy_root, _resolved(decoy_root)).manifest
    decoy_paths = {entry.path for entry in decoy_manifest.files}
    assert "ordinary-directory/CACHEDIR.TAG" in decoy_paths
    assert "ordinary-directory/repository-data.bin" in decoy_paths


def test_TC_DL_10_manifest_is_deterministic_and_entry_order_is_hashed(tmp_path: Path) -> None:
    root = tmp_path / "artifact"
    _write_valid(root, extra={"z-file": b"z", "a-file": b"a"})
    verifier = ArtifactVerifier()
    first = verifier.verify(root, _resolved(root))
    second = verifier.verify(root, _resolved(root))
    assert first.manifest_bytes == second.manifest_bytes
    assert first.manifest_hash == second.manifest_hash

    payload = json.loads(first.manifest_bytes)
    payload["files"] = list(reversed(payload["files"]))
    shuffled = (
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()
    assert artifact_manifest_hash(shuffled) != first.manifest_hash


def test_TC_DL_10_manifest_rejects_boolean_integer_aliases() -> None:
    with pytest.raises(ArtifactManifestError, match="size"):
        ArtifactManifestEntry("model.safetensors", True, "0" * 64)
    with pytest.raises(ArtifactManifestError, match="schema version"):
        ArtifactManifest((), schema_version=True)


class _TracingSystemCalls(ArtifactSystemCalls):
    def __init__(self) -> None:
        self.trace: list[tuple[str, str]] = []

    def write_manifest(self, path: Path, content: bytes) -> None:
        self.trace.append(("write", path.name))
        super().write_manifest(path, content)

    def fsync_file(self, path: Path) -> None:
        self.trace.append(("fsync_file", path.name))
        super().fsync_file(path)

    def fsync_directory(self, path: Path) -> None:
        self.trace.append(("fsync_directory", path.name))
        super().fsync_directory(path)

    def replace(self, source: Path, destination: Path) -> None:
        self.trace.append(("replace", destination.name))
        super().replace(source, destination)


def _assert_publish_order(
    trace: list[tuple[str, str]], staging_name: str, parent_name: str
) -> None:
    write = trace.index(("write", ARTIFACT_MANIFEST_FILENAME))
    staging_sync = trace.index(("fsync_directory", staging_name))
    rename = next(index for index, event in enumerate(trace) if event[0] == "replace")
    parent_sync = trace.index(("fsync_directory", parent_name))
    assert write < staging_sync < rename < parent_sync
    assert any(event == ("fsync_file", ARTIFACT_MANIFEST_FILENAME) for event in trace)


def test_TC_DL_10_actual_publish_calls_follow_the_five_step_order(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    destination = tmp_path / "managed" / "artifact"
    destination.parent.mkdir()
    _write_valid(staging)
    verified = ArtifactVerifier().verify(staging, _resolved(staging))
    calls = _TracingSystemCalls()

    published = publish_verified_artifact(staging, destination, verified, system_calls=calls)

    assert published.path == destination and destination.is_dir() and not staging.exists()
    _assert_publish_order(calls.trace, staging.name, destination.parent.name)

    rename = next(index for index, event in enumerate(calls.trace) if event[0] == "replace")
    early_rename = list(calls.trace)
    early_rename.insert(1, early_rename.pop(rename))
    with pytest.raises(AssertionError):
        _assert_publish_order(early_rename, staging.name, destination.parent.name)

    no_parent_sync = [
        event for event in calls.trace if event != ("fsync_directory", destination.parent.name)
    ]
    with pytest.raises(ValueError):
        _assert_publish_order(no_parent_sync, staging.name, destination.parent.name)


def test_TC_DL_25_periodic_judgement_distinguishes_missing_and_corrupted(
    tmp_path: Path,
) -> None:
    missing = judge_artifact_health(tmp_path / "absent", "0" * 64, full=False)
    assert missing.status is ModelArtifactStatus.MISSING
    assert missing.reason is ArtifactHealthReason.ARTIFACT_DIRECTORY_MISSING

    staging = tmp_path / "staging"
    destination = tmp_path / "managed" / "artifact"
    destination.parent.mkdir()
    _write_valid(staging)
    verified = ArtifactVerifier().verify(staging, _resolved(staging))
    publish_verified_artifact(staging, destination, verified)
    assert judge_artifact_health(destination, verified.manifest_hash, full=False).status is (
        ModelArtifactStatus.READY
    )

    (destination / "model.safetensors").write_bytes(b"same path, damaged content")
    corrupted = judge_artifact_health(destination, verified.manifest_hash, full=True)
    assert corrupted.status is ModelArtifactStatus.CORRUPTED
    assert corrupted.reason is ArtifactHealthReason.CONTENT_CORRUPTED


def test_TC_DL_25_full_health_check_detects_equal_size_hash_change(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    destination = tmp_path / "managed" / "artifact"
    destination.parent.mkdir()
    _write_valid(staging)
    verified = ArtifactVerifier().verify(staging, _resolved(staging))
    publish_verified_artifact(staging, destination, verified)

    weight_path = destination / "model.safetensors"
    original = weight_path.read_bytes()
    tampered = original[:-1] + bytes([original[-1] ^ 0x01])
    weight_path.write_bytes(tampered)
    assert weight_path.stat().st_size == len(original)

    lightweight = judge_artifact_health(destination, verified.manifest_hash, full=False)
    assert lightweight.status is ModelArtifactStatus.READY
    assert lightweight.reason is ArtifactHealthReason.HEALTHY

    complete = judge_artifact_health(destination, verified.manifest_hash, full=True)
    assert complete.status is ModelArtifactStatus.CORRUPTED
    assert complete.reason is ArtifactHealthReason.CONTENT_CORRUPTED

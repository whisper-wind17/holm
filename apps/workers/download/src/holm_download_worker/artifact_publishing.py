"""Model Artifact verification, deterministic Manifest, and atomic publication."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Final, Literal, Protocol, overload
from urllib.parse import quote
from uuid import UUID

from holm_common import StagingLayout, StagingPathRejected, TokenizerStagingLayout
from holm_common.config import StorageConfig
from holm_contracts.external_paths import ExternalPathPolicy, judge_external_path
from holm_domain import (
    ARTIFACT_MANIFEST_FILENAME,
    ArtifactManifest,
    ArtifactManifestEntry,
    ArtifactManifestError,
    ArtifactStorageType,
    DownloadFailureReason,
    DownloadTaskStatus,
    Identified,
    ModelArtifactStatus,
    ModelOriginType,
    RemoteFile,
    ResolvedModel,
    artifact_manifest_hash,
    build_artifact_manifest,
    canonical_artifact_path,
    deserialize_artifact_manifest,
    serialize_artifact_manifest,
)
from holm_domain.model_layout import (
    TOKENIZER_CONFIGURATION_NAMES,
    WEIGHT_SUFFIXES,
    DeployableLayout,
    deployable_layout,
)
from holm_persistence import (
    AsyncSessionFactory,
    JobHandlerContext,
    JobRepository,
    LeaseLostError,
    database_utc_now,
)
from holm_persistence.download_task_repository import (
    ArtifactInspectionRecord,
    DownloadResolutionWorkRecord,
    DownloadTaskRepository,
)

from holm_download_worker.agent_client import PathValidator
from holm_download_worker.tokenizer_bundle import (
    TokenizerBundleBuilt,
    TokenizerBundleEmpty,
    TokenizerBundleRecoveryError,
    TokenizerBundleRejected,
    TokenizerBundleRejectionReason,
    build_managed_tokenizer_bundle,
    recover_tokenizer_bundle,
)

CACHEDIR_SIGNATURE: Final[bytes] = b"Signature: 8a477f597d28d172789f06886806bc55"
_HASH_CHUNK_BYTES: Final[int] = 1024 * 1024
_MAX_CONFIGURATION_BYTES: Final[int] = 4 * 1024 * 1024
_MAX_SHARD_INDEX_BYTES: Final[int] = 64 * 1024 * 1024
_MANAGED_SOURCE_DIRECTORIES: Final[Mapping[ModelOriginType, str]] = {
    ModelOriginType.HUGGING_FACE: "hugging-face",
    ModelOriginType.MODELSCOPE: "modelscope",
}


class ArtifactValidationError(RuntimeError):
    """A stable validation failure without a source or host path in its text."""

    def __init__(self, reason: DownloadFailureReason) -> None:
        self.reason = reason
        super().__init__("model artifact validation failed")


class ArtifactPublicationError(RuntimeError):
    """A publication I/O failure, including whether Rename already happened."""

    def __init__(self, *, renamed: bool) -> None:
        self.reason = DownloadFailureReason.ARTIFACT_PUBLISH_FAILED
        self.renamed = renamed
        super().__init__("model artifact publication failed")


class RuntimeCompatibilityStatus(StrEnum):
    MATCHED = "matched"
    NO_MATCHABLE_ENTRIES = "no_matchable_entries"
    ARCHITECTURE_UNRECOGNIZED = "architecture_unrecognized"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True, slots=True)
class RuntimeCapability:
    """The download-visible subset of one future runtime-compatibility entry."""

    name: str
    supported_architectures: frozenset[str]

    def __post_init__(self) -> None:
        if not self.name or self.name != self.name.strip():
            raise ValueError("Runtime capability name must be non-empty and trimmed")
        if not self.supported_architectures or any(
            not value or value != value.strip() for value in self.supported_architectures
        ):
            raise ValueError("Runtime supported architectures must be explicit")


def judge_runtime_compatibility(
    architecture: str | None,
    runtimes: Sequence[RuntimeCapability],
) -> RuntimeCompatibilityStatus:
    """Return an explicit non-success when no Runtime data exists or matches."""

    if not runtimes:
        # intentionally does not connect its catalog to this download path.
        # No injected capabilities remains an explicit partial judgement.
        return RuntimeCompatibilityStatus.NO_MATCHABLE_ENTRIES
    if architecture is None:
        return RuntimeCompatibilityStatus.ARCHITECTURE_UNRECOGNIZED
    if any(architecture in runtime.supported_architectures for runtime in runtimes):
        return RuntimeCompatibilityStatus.MATCHED
    return RuntimeCompatibilityStatus.INCOMPATIBLE


@dataclass(frozen=True, slots=True)
class VerifiedArtifact:
    manifest: ArtifactManifest
    manifest_bytes: bytes
    manifest_hash: str
    size_bytes: int
    runtime_compatibility: RuntimeCompatibilityStatus
    attention_heads: int | None
    kv_heads: int | None


@dataclass(frozen=True, slots=True)
class PublishedArtifact:
    path: Path
    manifest: ArtifactManifest
    manifest_bytes: bytes
    manifest_hash: str
    size_bytes: int
    already_published: bool
    attention_heads: int | None
    kv_heads: int | None


class _VerifiedPublication(Protocol):
    @property
    def manifest(self) -> ArtifactManifest: ...

    @property
    def manifest_bytes(self) -> bytes: ...

    @property
    def manifest_hash(self) -> str: ...

    @property
    def size_bytes(self) -> int: ...


class ArtifactHealthReason(StrEnum):
    HEALTHY = "healthy"
    ARTIFACT_DIRECTORY_MISSING = "artifact_directory_missing"
    MANIFEST_MISSING = "manifest_missing"
    FILE_MISSING = "file_missing"
    MANIFEST_INVALID = "manifest_invalid"
    CONTENT_CORRUPTED = "content_corrupted"


@dataclass(frozen=True, slots=True)
class ArtifactHealthJudgement:
    status: ModelArtifactStatus
    reason: ArtifactHealthReason


@dataclass(frozen=True, slots=True)
class _ScannedFile:
    entry: ArtifactManifestEntry
    path: Path


def _safe_relative(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        raise ArtifactValidationError(DownloadFailureReason.ARTIFACT_UNSAFE_FILE) from None
    try:
        return canonical_artifact_path(relative)
    except ArtifactManifestError:
        raise ArtifactValidationError(DownloadFailureReason.ARTIFACT_UNSAFE_FILE) from None


def _revision_directory(revision: str) -> str:
    """Encode one opaque source Revision as a single, non-special path component."""

    return quote(revision, safe="-_~").replace(".", "%2E")


def _managed_destination(layout: StagingLayout, task: DownloadResolutionWorkRecord) -> Path:
    try:
        source = _MANAGED_SOURCE_DIRECTORIES[task.source_type]
    except KeyError:
        raise ArtifactPublicationError(renamed=False) from None
    if task.resolved_revision is None:
        raise ArtifactPublicationError(renamed=False)
    repository_hash = hashlib.sha256(task.repository_id.encode("utf-8")).hexdigest()
    destination = (
        layout.managed_root / source / repository_hash / _revision_directory(task.resolved_revision)
    )
    resolved_root = layout.managed_root.resolve(strict=False)
    if layout.managed_root.is_symlink() or destination.resolve(strict=False) != (
        resolved_root / source / repository_hash / _revision_directory(task.resolved_revision)
    ):
        raise ArtifactPublicationError(renamed=False)
    return destination


def _ensure_managed_parent(destination: Path, managed_root: Path) -> None:
    """Create only ordinary directories below the platform-owned Managed root."""

    try:
        root_metadata = managed_root.lstat()
        if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_ISLNK(root_metadata.st_mode):
            raise OSError("managed publication root is unsafe")
        relative_parent = destination.parent.relative_to(managed_root)
        current = managed_root
        for component in relative_parent.parts:
            current /= component
            current.mkdir(exist_ok=True)
            metadata = current.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise OSError("managed publication parent is unsafe")
    except (OSError, ValueError) as error:
        raise ArtifactPublicationError(renamed=False) from error


def _read_first_line(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        data = os.read(descriptor, len(CACHEDIR_SIGNATURE) + 2)
    finally:
        os.close(descriptor)
    return data.splitlines()[0] if data else b""


def _is_tagged_cache(directory: Path) -> bool:
    marker = directory / "CACHEDIR.TAG"
    try:
        metadata = marker.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise ArtifactValidationError(DownloadFailureReason.ARTIFACT_UNSAFE_FILE) from error
    if not stat.S_ISREG(metadata.st_mode):
        return False
    try:
        return _read_first_line(marker) == CACHEDIR_SIGNATURE
    except OSError as error:
        raise ArtifactValidationError(DownloadFailureReason.ARTIFACT_UNSAFE_FILE) from error


def _validate_symlink(path: Path, root: Path) -> None:
    try:
        target = path.resolve(strict=True)
        resolved_root = root.resolve(strict=True)
    except OSError as error:
        raise ArtifactValidationError(DownloadFailureReason.ARTIFACT_UNSAFE_FILE) from error
    if target == resolved_root or not target.is_relative_to(resolved_root):
        raise ArtifactValidationError(DownloadFailureReason.ARTIFACT_UNSAFE_FILE)


def _hash_regular_file(path: Path) -> tuple[int, str]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise ArtifactValidationError(
            DownloadFailureReason.ARTIFACT_MANIFEST_HASH_FAILED
        ) from error
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_mode & stat.S_ISUID:
            raise ArtifactValidationError(DownloadFailureReason.ARTIFACT_UNSAFE_FILE)
        while chunk := os.read(descriptor, _HASH_CHUNK_BYTES):
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ):
            raise ArtifactValidationError(DownloadFailureReason.ARTIFACT_MANIFEST_HASH_FAILED)
        return after.st_size, digest.hexdigest()
    except OSError as error:
        raise ArtifactValidationError(
            DownloadFailureReason.ARTIFACT_MANIFEST_HASH_FAILED
        ) from error
    finally:
        os.close(descriptor)


def _scan_files(root: Path) -> tuple[_ScannedFile, ...]:
    try:
        root_metadata = root.lstat()
    except OSError as error:
        raise ArtifactValidationError(DownloadFailureReason.ARTIFACT_FILE_LIST_INVALID) from error
    if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_ISLNK(root_metadata.st_mode):
        raise ArtifactValidationError(DownloadFailureReason.ARTIFACT_UNSAFE_FILE)

    scanned: list[_ScannedFile] = []
    try:
        walker = os.walk(root, topdown=True, followlinks=False)
        for current_raw, directory_names, file_names in walker:
            current = Path(current_raw)
            if current != root and _is_tagged_cache(current):
                directory_names[:] = []
                continue
            retained_directories: list[str] = []
            for name in sorted(directory_names):
                candidate = current / name
                mode = candidate.lstat().st_mode
                _safe_relative(candidate, root)
                if stat.S_ISLNK(mode):
                    _validate_symlink(candidate, root)
                    continue
                if not stat.S_ISDIR(mode):
                    raise ArtifactValidationError(DownloadFailureReason.ARTIFACT_UNSAFE_FILE)
                if not _is_tagged_cache(candidate):
                    retained_directories.append(name)
            directory_names[:] = retained_directories

            for name in sorted(file_names):
                candidate = current / name
                relative = _safe_relative(candidate, root)
                mode = candidate.lstat().st_mode
                if stat.S_ISLNK(mode):
                    _validate_symlink(candidate, root)
                    continue
                if not stat.S_ISREG(mode) or mode & stat.S_ISUID:
                    raise ArtifactValidationError(DownloadFailureReason.ARTIFACT_UNSAFE_FILE)
                if relative == ARTIFACT_MANIFEST_FILENAME:
                    continue
                size, digest = _hash_regular_file(candidate)
                scanned.append(
                    _ScannedFile(
                        entry=ArtifactManifestEntry(relative, size, digest),
                        path=candidate,
                    )
                )
    except ArtifactValidationError:
        raise
    except OSError as error:
        raise ArtifactValidationError(DownloadFailureReason.ARTIFACT_UNSAFE_FILE) from error
    return tuple(sorted(scanned, key=lambda value: value.entry.path))


def _expected_snapshot(files: Sequence[RemoteFile]) -> Mapping[str, RemoteFile]:
    result: dict[str, RemoteFile] = {}
    for entry in files:
        try:
            path = canonical_artifact_path(entry.path)
        except ArtifactManifestError:
            raise ArtifactValidationError(
                DownloadFailureReason.ARTIFACT_FILE_LIST_INVALID
            ) from None
        if path == ARTIFACT_MANIFEST_FILENAME or path in result:
            raise ArtifactValidationError(DownloadFailureReason.ARTIFACT_FILE_LIST_INVALID)
        result[path] = entry
    return result


def _read_json(path: Path, *, maximum_bytes: int) -> object:
    try:
        metadata = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum_bytes:
            raise ValueError
        with path.open("rb") as stream:
            return json.load(stream)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ArtifactValidationError(
            DownloadFailureReason.ARTIFACT_REQUIRED_FILES_INVALID
        ) from None


def _required_configuration(scanned: Mapping[str, _ScannedFile]) -> Mapping[str, object]:
    if deployable_layout(scanned) is not DeployableLayout.SUPPORTED:
        raise ArtifactValidationError(DownloadFailureReason.ARTIFACT_REQUIRED_FILES_INVALID)
    config = scanned["config.json"]
    payload = _read_json(config.path, maximum_bytes=_MAX_CONFIGURATION_BYTES)
    if not isinstance(payload, dict):
        raise ArtifactValidationError(DownloadFailureReason.ARTIFACT_REQUIRED_FILES_INVALID)
    return payload


def _validate_safetensors(path: Path) -> None:
    try:
        with path.open("rb") as stream:
            length_bytes = stream.read(8)
            if len(length_bytes) != 8:
                raise ValueError
            header_length = int.from_bytes(length_bytes, byteorder="little", signed=False)
            file_size = path.stat(follow_symlinks=False).st_size
            if header_length == 0 or header_length > file_size - 8:
                raise ValueError
            header = json.loads(stream.read(header_length))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ArtifactValidationError(DownloadFailureReason.ARTIFACT_SAFETENSORS_INVALID) from None
    if not isinstance(header, dict):
        raise ArtifactValidationError(DownloadFailureReason.ARTIFACT_SAFETENSORS_INVALID)
    data_size = file_size - 8 - header_length
    for name, tensor in header.items():
        if name == "__metadata__":
            if not isinstance(tensor, dict):
                raise ArtifactValidationError(DownloadFailureReason.ARTIFACT_SAFETENSORS_INVALID)
            continue
        if not isinstance(tensor, dict) or set(tensor) != {"data_offsets", "dtype", "shape"}:
            raise ArtifactValidationError(DownloadFailureReason.ARTIFACT_SAFETENSORS_INVALID)
        offsets = tensor["data_offsets"]
        shape = tensor["shape"]
        if (
            not isinstance(tensor["dtype"], str)
            or not isinstance(shape, list)
            or any(
                isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in shape
            )
            or not isinstance(offsets, list)
            or len(offsets) != 2
            or any(isinstance(item, bool) or not isinstance(item, int) for item in offsets)
            or not 0 <= offsets[0] <= offsets[1] <= data_size
        ):
            raise ArtifactValidationError(DownloadFailureReason.ARTIFACT_SAFETENSORS_INVALID)


def _validate_shard_indexes(scanned: Mapping[str, _ScannedFile]) -> None:
    for relative, indexed in scanned.items():
        if not relative.endswith(".index.json"):
            continue
        try:
            payload = _read_json(indexed.path, maximum_bytes=_MAX_SHARD_INDEX_BYTES)
        except ArtifactValidationError:
            raise ArtifactValidationError(
                DownloadFailureReason.ARTIFACT_SAFETENSORS_INVALID
            ) from None
        if not isinstance(payload, dict) or not isinstance(payload.get("weight_map"), dict):
            raise ArtifactValidationError(DownloadFailureReason.ARTIFACT_SAFETENSORS_INVALID)
        referenced = payload["weight_map"].values()
        for value in referenced:
            if not isinstance(value, str):
                raise ArtifactValidationError(DownloadFailureReason.ARTIFACT_SAFETENSORS_INVALID)
            try:
                shard = canonical_artifact_path(value)
            except ArtifactManifestError:
                raise ArtifactValidationError(
                    DownloadFailureReason.ARTIFACT_SAFETENSORS_INVALID
                ) from None
            if shard not in scanned:
                raise ArtifactValidationError(DownloadFailureReason.ARTIFACT_SAFETENSORS_INVALID)


def _remote_code_requested(configuration: Mapping[str, object]) -> bool:
    return configuration.get("trust_remote_code") is True or bool(configuration.get("auto_map"))


def _configuration_architectures(configuration: Mapping[str, object]) -> tuple[str, ...]:
    values = configuration.get("architectures")
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        return ()
    return tuple(value for value in values if value)


def _configuration_partition_heads(
    configuration: Mapping[str, object],
) -> tuple[int | None, int | None]:
    """Collect only positive JSON integers; bool and guesses remain unknown."""

    def positive_integer(name: str) -> int | None:
        value = configuration.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return None
        return value

    return (
        positive_integer("num_attention_heads"),
        positive_integer("num_key_value_heads"),
    )


class ArtifactVerifier:
    """Apply the six artifact checks and produce the canonical Manifest."""

    __slots__ = ("_runtimes",)

    def __init__(self, runtimes: Sequence[RuntimeCapability] = ()) -> None:
        self._runtimes = tuple(runtimes)

    def verify(self, root: Path, resolved: ResolvedModel) -> VerifiedArtifact:
        if not isinstance(resolved.files, Identified):
            raise ArtifactValidationError(DownloadFailureReason.ARTIFACT_FILE_LIST_INVALID)
        scanned_values = _scan_files(root)
        scanned = {value.entry.path: value for value in scanned_values}
        expected = _expected_snapshot(resolved.files.value)

        if set(scanned) != set(expected):
            raise ArtifactValidationError(DownloadFailureReason.ARTIFACT_FILE_LIST_INVALID)
        for path, actual in scanned.items():
            source = expected[path]
            if actual.entry.size_bytes != source.size_bytes:
                raise ArtifactValidationError(DownloadFailureReason.ARTIFACT_FILE_LIST_INVALID)
            if isinstance(source.content_sha256, Identified) and (
                actual.entry.sha256 != source.content_sha256.value
            ):
                raise ArtifactValidationError(DownloadFailureReason.ARTIFACT_MANIFEST_HASH_FAILED)

        return self._verify_scanned(scanned_values, resolved)

    def verify_external(self, root: Path) -> VerifiedArtifact:
        """Build the manifest in memory; External never gets publication files."""
        return self._verify_scanned(_scan_files(root), None)

    def _verify_scanned(
        self, scanned_values: tuple[_ScannedFile, ...], resolved: ResolvedModel | None
    ) -> VerifiedArtifact:
        scanned = {value.entry.path: value for value in scanned_values}
        configuration = _required_configuration(scanned)
        for relative, value in scanned.items():
            if relative.endswith(".safetensors"):
                _validate_safetensors(value.path)
        _validate_shard_indexes(scanned)

        if _remote_code_requested(configuration):
            raise ArtifactValidationError(DownloadFailureReason.REMOTE_CODE_NOT_ALLOWED)
        configured_architectures = _configuration_architectures(configuration)
        resolved_architecture = (
            resolved.architecture.value
            if resolved is not None and isinstance(resolved.architecture, Identified)
            else None
        )
        if (
            resolved_architecture is not None
            and configured_architectures
            and resolved_architecture not in configured_architectures
        ):
            raise ArtifactValidationError(DownloadFailureReason.ARTIFACT_RUNTIME_INCOMPATIBLE)
        architecture = resolved_architecture or (
            configured_architectures[0] if configured_architectures else None
        )
        compatibility = judge_runtime_compatibility(architecture, self._runtimes)
        if compatibility in {
            RuntimeCompatibilityStatus.ARCHITECTURE_UNRECOGNIZED,
            RuntimeCompatibilityStatus.INCOMPATIBLE,
        }:
            raise ArtifactValidationError(DownloadFailureReason.ARTIFACT_RUNTIME_INCOMPATIBLE)

        manifest = build_artifact_manifest(value.entry for value in scanned_values)
        manifest_bytes = serialize_artifact_manifest(manifest)
        attention_heads, kv_heads = _configuration_partition_heads(configuration)
        return VerifiedArtifact(
            manifest=manifest,
            manifest_bytes=manifest_bytes,
            manifest_hash=artifact_manifest_hash(manifest_bytes),
            size_bytes=sum(entry.size_bytes for entry in manifest.files),
            runtime_compatibility=compatibility,
            attention_heads=attention_heads,
            kv_heads=kv_heads,
        )


class ArtifactSystemCalls:
    """The observable file-system boundary for the atomic publish order."""

    def write_manifest(self, path: Path, content: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o640)
        try:
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("manifest write made no progress")
                view = view[written:]
        finally:
            os.close(descriptor)

    def fsync_file(self, path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError("publication file is no longer regular")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def fsync_directory(self, path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def replace(self, source: Path, destination: Path) -> None:
        os.replace(source, destination)


def _load_published(path: Path) -> PublishedArtifact:
    manifest_path = path / ARTIFACT_MANIFEST_FILENAME
    try:
        raw = manifest_path.read_bytes()
        manifest = deserialize_artifact_manifest(raw)
    except (OSError, ArtifactManifestError) as error:
        raise ArtifactPublicationError(renamed=True) from error
    expected_hash = artifact_manifest_hash(raw)
    for entry in manifest.files:
        candidate = path.joinpath(*entry.path.split("/"))
        try:
            size, digest = _hash_regular_file(candidate)
        except ArtifactValidationError as error:
            raise ArtifactPublicationError(renamed=True) from error
        if size != entry.size_bytes or digest != entry.sha256:
            raise ArtifactPublicationError(renamed=True)
    try:
        configuration = _read_json(
            path / "config.json",
            maximum_bytes=_MAX_CONFIGURATION_BYTES,
        )
    except ArtifactValidationError as error:
        raise ArtifactPublicationError(renamed=True) from error
    if not isinstance(configuration, dict):
        raise ArtifactPublicationError(renamed=True)
    attention_heads, kv_heads = _configuration_partition_heads(configuration)
    return PublishedArtifact(
        path=path,
        manifest=manifest,
        manifest_bytes=raw,
        manifest_hash=expected_hash,
        size_bytes=sum(entry.size_bytes for entry in manifest.files),
        already_published=True,
        attention_heads=attention_heads,
        kv_heads=kv_heads,
    )


def publish_verified_artifact(
    staging: Path,
    destination: Path,
    verified: _VerifiedPublication,
    *,
    system_calls: ArtifactSystemCalls | None = None,
    ensure_owned: Callable[[], None] = lambda: None,
    attention_heads: int | None = None,
    kv_heads: int | None = None,
) -> PublishedArtifact:
    """Write → fsync files/root → replace → fsync parent, in that exact order."""

    calls = system_calls or ArtifactSystemCalls()
    if destination.exists():
        if staging.exists():
            raise ArtifactPublicationError(renamed=True)
        recovered = _load_published(destination)
        if recovered.manifest_hash != verified.manifest_hash:
            raise ArtifactPublicationError(renamed=True)
        return recovered
    if not staging.is_dir():
        raise ArtifactPublicationError(renamed=False)
    manifest_path = staging / ARTIFACT_MANIFEST_FILENAME
    renamed = False
    try:
        ensure_owned()
        calls.write_manifest(manifest_path, verified.manifest_bytes)
        for entry in verified.manifest.files:
            ensure_owned()
            calls.fsync_file(staging.joinpath(*entry.path.split("/")))
        ensure_owned()
        calls.fsync_file(manifest_path)
        ensure_owned()
        calls.fsync_directory(staging)
        ensure_owned()
        calls.replace(staging, destination)
        renamed = True
        ensure_owned()
        calls.fsync_directory(destination.parent)
    except Exception as error:
        if isinstance(error, ArtifactPublicationError):
            raise
        raise ArtifactPublicationError(renamed=renamed) from error
    return PublishedArtifact(
        path=destination,
        manifest=verified.manifest,
        manifest_bytes=verified.manifest_bytes,
        manifest_hash=verified.manifest_hash,
        size_bytes=verified.size_bytes,
        already_published=False,
        attention_heads=attention_heads,
        kv_heads=kv_heads,
    )


def recover_published_artifact(destination: Path) -> PublishedArtifact:
    """Recognize one Rename-complete, database-unclaimed publication."""

    return _load_published(destination)


def judge_artifact_health(
    path: Path,
    expected_manifest_hash: str,
    *,
    full: bool,
    stored_manifest: ArtifactManifest | None = None,
) -> ArtifactHealthJudgement:
    """Judge directory/Manifest/key files, with optional full per-file hashing."""

    if not path.is_dir() or path.is_symlink():
        return ArtifactHealthJudgement(
            ModelArtifactStatus.MISSING,
            ArtifactHealthReason.ARTIFACT_DIRECTORY_MISSING,
        )
    if stored_manifest is not None:
        raw = serialize_artifact_manifest(stored_manifest)
        manifest = stored_manifest
        if not manifest.files or artifact_manifest_hash(raw) != expected_manifest_hash:
            return ArtifactHealthJudgement(
                ModelArtifactStatus.CORRUPTED, ArtifactHealthReason.MANIFEST_INVALID
            )
    else:
        manifest_path = path / ARTIFACT_MANIFEST_FILENAME
        if not manifest_path.is_file() or manifest_path.is_symlink():
            return ArtifactHealthJudgement(
                ModelArtifactStatus.MISSING,
                ArtifactHealthReason.MANIFEST_MISSING,
            )
        try:
            raw = manifest_path.read_bytes()
            manifest = deserialize_artifact_manifest(raw)
        except ArtifactManifestError:
            return ArtifactHealthJudgement(
                ModelArtifactStatus.CORRUPTED,
                ArtifactHealthReason.MANIFEST_INVALID,
            )
        if artifact_manifest_hash(raw) != expected_manifest_hash:
            return ArtifactHealthJudgement(
                ModelArtifactStatus.CORRUPTED,
                ArtifactHealthReason.MANIFEST_INVALID,
            )

    key_paths = {"config.json"}
    key_paths.update(
        entry.path
        for entry in manifest.files
        if entry.path in TOKENIZER_CONFIGURATION_NAMES
        or entry.path.endswith(WEIGHT_SUFFIXES)
        or entry.path.endswith(".index.json")
    )
    entries = {entry.path: entry for entry in manifest.files}
    for relative in sorted(key_paths):
        entry = entries.get(relative)
        candidate = path.joinpath(*relative.split("/"))
        if candidate.resolve(strict=False) != candidate:
            return ArtifactHealthJudgement(
                ModelArtifactStatus.CORRUPTED, ArtifactHealthReason.CONTENT_CORRUPTED
            )
        if entry is None or not candidate.exists():
            return ArtifactHealthJudgement(
                ModelArtifactStatus.MISSING,
                ArtifactHealthReason.FILE_MISSING,
            )
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            return ArtifactHealthJudgement(
                ModelArtifactStatus.MISSING,
                ArtifactHealthReason.FILE_MISSING,
            )
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != entry.size_bytes:
            return ArtifactHealthJudgement(
                ModelArtifactStatus.CORRUPTED,
                ArtifactHealthReason.CONTENT_CORRUPTED,
            )

    if full:
        for entry in manifest.files:
            candidate = path.joinpath(*entry.path.split("/"))
            if candidate.resolve(strict=False) != candidate:
                return ArtifactHealthJudgement(
                    ModelArtifactStatus.CORRUPTED, ArtifactHealthReason.CONTENT_CORRUPTED
                )
            if not candidate.exists():
                return ArtifactHealthJudgement(
                    ModelArtifactStatus.MISSING,
                    ArtifactHealthReason.FILE_MISSING,
                )
            try:
                size, digest = _hash_regular_file(candidate)
            except ArtifactValidationError as error:
                # Unreadable storage is an execution failure, not evidence of
                # changed content. The job boundary persists only its safe code.
                if isinstance(error.__cause__, OSError):
                    raise error.__cause__ from None
                return ArtifactHealthJudgement(
                    ModelArtifactStatus.CORRUPTED,
                    ArtifactHealthReason.CONTENT_CORRUPTED,
                )
            if size != entry.size_bytes or digest != entry.sha256:
                return ArtifactHealthJudgement(
                    ModelArtifactStatus.CORRUPTED,
                    ArtifactHealthReason.CONTENT_CORRUPTED,
                )
    return ArtifactHealthJudgement(ModelArtifactStatus.READY, ArtifactHealthReason.HEALTHY)


class PublicationCommitHook(Protocol):
    """Fault-injection seam called inside the real final database transaction."""

    def __call__(self) -> None: ...


def _artifact_id_for_task(task_id: UUID) -> UUID:
    """Derive one stable UUIDv7 without conflating Task and Artifact identity."""

    # 存储格式标签，改名会让既有数据失效
    digest = hashlib.sha256(b"privatews:model-artifact:v1\0" + task_id.bytes).digest()
    timestamp = task_id.int >> 80 if task_id.version == 7 else int.from_bytes(digest[:6], "big")
    value = (timestamp & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= int.from_bytes(digest[6:16], "big") & ((1 << 76) - 1)
    value &= ~(0b11 << 62)
    value |= 0b10 << 62
    return UUID(int=value)


class DownloadArtifactPublisher:
    """Lease-fenced orchestration of Verifying, atomic Rename, and Ready commit."""

    __slots__ = (
        "_before_commit",
        "_storage",
        "_path_validator",
        "_layout",
        "_session_factory",
        "_system_calls",
        "_tokenizer_layout",
        "_verifier",
    )

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory,
        storage: StorageConfig,
        verifier: ArtifactVerifier | None = None,
        system_calls: ArtifactSystemCalls | None = None,
        before_commit: PublicationCommitHook | None = None,
        path_validator: PathValidator | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._layout = StagingLayout(storage.model_root)
        self._tokenizer_layout = TokenizerStagingLayout(storage.tokenizer_root)
        self._verifier = verifier or ArtifactVerifier()
        self._system_calls = system_calls
        self._before_commit = before_commit
        self._storage = storage
        self._path_validator = path_validator

    async def requires_resolution(self, task: DownloadResolutionWorkRecord) -> bool:
        """A Rename-complete retry can trust its Hash-authenticated Manifest."""

        if task.source_type is ModelOriginType.LOCAL_PATH:
            return False
        return await asyncio.to_thread(self._requires_resolution_sync, task)

    def _requires_resolution_sync(self, task: DownloadResolutionWorkRecord) -> bool:
        """Perform recovery hashing outside the Worker event loop."""

        if task.status is DownloadTaskStatus.VERIFYING:
            return True
        if task.status is not DownloadTaskStatus.PUBLISHING:
            return False
        if not self._destination(task).is_dir():
            return True
        artifact_id = _artifact_id_for_task(task.id)
        try:
            tokenizer_destination = self._tokenizer_layout.destination_path(artifact_id)
            if tokenizer_destination.is_dir():
                recover_tokenizer_bundle(tokenizer_destination)
                return False
            tokenizer_staging = self._tokenizer_layout.staging_path(artifact_id)
            if tokenizer_staging.is_dir():
                recover_tokenizer_bundle(tokenizer_staging)
                return False
        except (StagingPathRejected, TokenizerBundleRecoveryError):
            return True
        return True

    async def publish(
        self,
        context: JobHandlerContext,
        task: DownloadResolutionWorkRecord,
        resolved: ResolvedModel | None,
    ) -> UUID:
        if task.status not in {DownloadTaskStatus.VERIFYING, DownloadTaskStatus.PUBLISHING}:
            raise ArtifactPublicationError(renamed=False)
        if task.source_type is ModelOriginType.LOCAL_PATH:
            return await self._publish_external(context, task)
        if task.resolved_revision is None or task.staging_path is None:
            error = ArtifactPublicationError(renamed=False)
            await self._record_failure(context, task, error.reason)
            raise error
        try:
            staging = self._safe_staging(task)
            destination = self._destination(task)
        except ArtifactPublicationError as error:
            await self._record_failure(context, task, error.reason)
            raise
        artifact_id = _artifact_id_for_task(task.id)
        published: PublishedArtifact | None = None
        prepared_bundle: TokenizerBundleBuilt | None = None
        if task.status is DownloadTaskStatus.PUBLISHING and destination.is_dir():
            published = await asyncio.to_thread(recover_published_artifact, destination)

        if published is None or await self.requires_resolution(task):
            verification_root = destination if published is not None else staging
            verified = await self._verify_for_task(context, task, resolved, verification_root)
            prepared_bundle = await self._prepare_tokenizer_bundle(
                context,
                task,
                verification_root,
                verified,
                artifact_id,
            )

        if published is None:
            if task.status is DownloadTaskStatus.VERIFYING:
                await self._enter_publishing(context, task)
                task = replace(task, status=DownloadTaskStatus.PUBLISHING)
            try:
                await asyncio.to_thread(
                    _ensure_managed_parent,
                    destination,
                    self._layout.managed_root,
                )
                published = await asyncio.to_thread(
                    publish_verified_artifact,
                    staging,
                    destination,
                    verified,
                    system_calls=self._system_calls,
                    ensure_owned=context.ensure_lease,
                    attention_heads=verified.attention_heads,
                    kv_heads=verified.kv_heads,
                )
            except ArtifactPublicationError as error:
                if not error.renamed:
                    await self._record_failure(context, task, error.reason)
                raise

        published_bundle = await self._publish_tokenizer_bundle(
            context,
            artifact_id,
            prepared_bundle,
        )
        assert published is not None
        return await self._commit_ready(
            context,
            task,
            artifact_id,
            published,
            published_bundle,
        )

    async def _publish_external(
        self, context: JobHandlerContext, task: DownloadResolutionWorkRecord
    ) -> UUID:
        path = Path(task.repository_id)
        try:
            verified = await self._verify_external(context, path)
        except ArtifactValidationError as error:
            await self._record_failure(context, task, error.reason)
            raise
        context.ensure_lease()
        artifact_id = _artifact_id_for_task(task.id)
        # External has no filesystem publication step: Publishing, Ready and
        # Completed belong to one lease-fenced transaction.
        async with self._session_factory() as session, session.begin():
            if not await JobRepository(session).lock_owned_lease(
                context.job.id, owner=self._lease_owner(context)
            ):
                context.lease_lost.set()
                raise LeaseLostError()
            repository = DownloadTaskRepository(session)
            current = await repository.lock_resolution_work(task.id)
            if (
                current is None
                or current.attempt != task.attempt
                or current.status is not DownloadTaskStatus.VERIFYING
            ):
                raise ArtifactPublicationError(renamed=False)
            now = await database_utc_now(session)
            if not await repository.transition_to_publishing(
                task.id, expected_attempt=task.attempt, now=now
            ):
                raise ArtifactPublicationError(renamed=False)
            if not await repository.complete_publication(
                current,
                artifact_id=artifact_id,
                path=str(path),
                manifest_hash=verified.manifest_hash,
                manifest=verified.manifest,
                tokenizer_bundle_hash=None,
                tokenizer_bundle_path=None,
                attention_heads=verified.attention_heads,
                kv_heads=verified.kv_heads,
                size_bytes=verified.size_bytes,
                now=now,
            ):
                raise ArtifactPublicationError(renamed=False)
            if self._before_commit is not None:
                self._before_commit()
        return artifact_id

    async def _verify_external(self, context: JobHandlerContext, path: Path) -> VerifiedArtifact:
        def check_path() -> bool:
            policy = ExternalPathPolicy.for_configured_roots(self._storage.external_model_roots)
            return (
                judge_external_path(str(path), policy).accepted
                and path.resolve(strict=False) == path
            )

        if not await asyncio.to_thread(check_path):
            raise ArtifactValidationError(DownloadFailureReason.EXTERNAL_PATH_UNREADABLE)
        readable = (
            await self._path_validator.readable(str(path))
            if self._path_validator is not None
            else None
        )
        if readable is False:
            raise ArtifactValidationError(DownloadFailureReason.EXTERNAL_PATH_UNREADABLE)
        if readable is None:
            logging.getLogger(__name__).warning(
                "运行身份可读性未验证", extra={"task_id": str(context.job.target_object_id)}
            )
        context.ensure_lease()
        return await asyncio.to_thread(self._verifier.verify_external, path)

    async def _verify_for_task(
        self,
        context: JobHandlerContext,
        task: DownloadResolutionWorkRecord,
        resolved: ResolvedModel | None,
        root: Path,
    ) -> VerifiedArtifact:
        if resolved is None or (
            resolved.source is not task.source_type
            or resolved.repository_id != task.repository_id
            or resolved.resolved_revision != task.resolved_revision
        ):
            await self._record_failure(
                context,
                task,
                DownloadFailureReason.ARTIFACT_FILE_LIST_INVALID,
            )
            raise ArtifactValidationError(DownloadFailureReason.ARTIFACT_FILE_LIST_INVALID)
        try:
            return await asyncio.to_thread(self._verifier.verify, root, resolved)
        except ArtifactValidationError as error:
            await self._record_failure(context, task, error.reason)
            raise

    async def _prepare_tokenizer_bundle(
        self,
        context: JobHandlerContext,
        task: DownloadResolutionWorkRecord,
        source_root: Path,
        verified: VerifiedArtifact,
        artifact_id: UUID,
    ) -> TokenizerBundleBuilt:
        result = await asyncio.to_thread(
            build_managed_tokenizer_bundle,
            source_root,
            verified.manifest,
            layout=self._tokenizer_layout,
            model_root=self._layout.model_root,
            artifact_id=artifact_id,
            ensure_owned=context.ensure_lease,
        )
        if isinstance(result, TokenizerBundleBuilt):
            return result
        if isinstance(result, TokenizerBundleEmpty):
            reason = DownloadFailureReason.ARTIFACT_REQUIRED_FILES_INVALID
        else:
            assert isinstance(result, TokenizerBundleRejected)
            reason = (
                DownloadFailureReason.STAGING_FILESYSTEM_MISMATCH
                if result.reason is TokenizerBundleRejectionReason.FILESYSTEM_MISMATCH
                else DownloadFailureReason.ARTIFACT_PUBLISH_FAILED
            )
        await self._record_failure(context, task, reason)
        raise ArtifactValidationError(reason)

    async def _publish_tokenizer_bundle(
        self,
        context: JobHandlerContext,
        artifact_id: UUID,
        prepared: TokenizerBundleBuilt | None,
    ) -> PublishedArtifact:
        try:
            destination = self._tokenizer_layout.destination_path(artifact_id)
            if destination.is_dir():
                recovered = await asyncio.to_thread(recover_tokenizer_bundle, destination)
                return PublishedArtifact(
                    path=recovered.path,
                    manifest=recovered.manifest,
                    manifest_bytes=recovered.manifest_bytes,
                    manifest_hash=recovered.manifest_hash,
                    size_bytes=recovered.size_bytes,
                    already_published=True,
                    attention_heads=None,
                    kv_heads=None,
                )
            if prepared is None:
                staging = self._tokenizer_layout.staging_path(artifact_id)
                recovered = await asyncio.to_thread(
                    recover_tokenizer_bundle,
                    staging,
                )
                prepared_publication: _VerifiedPublication = recovered
            else:
                staging = prepared.staging_path
                prepared_publication = prepared
            return await asyncio.to_thread(
                publish_verified_artifact,
                staging,
                destination,
                prepared_publication,
                system_calls=self._system_calls,
                ensure_owned=context.ensure_lease,
            )
        except (
            ArtifactPublicationError,
            OSError,
            StagingPathRejected,
            TokenizerBundleRecoveryError,
        ) as error:
            raise ArtifactPublicationError(renamed=True) from error

    def _safe_staging(self, task: DownloadResolutionWorkRecord) -> Path:
        expected = self._layout.task_path(task.id)
        supplied = Path(task.staging_path or "")
        if not supplied.is_absolute() or supplied.resolve(strict=False) != expected:
            raise ArtifactPublicationError(renamed=False)
        return expected

    def _destination(self, task: DownloadResolutionWorkRecord) -> Path:
        return _managed_destination(self._layout, task)

    async def _enter_publishing(
        self,
        context: JobHandlerContext,
        task: DownloadResolutionWorkRecord,
    ) -> None:
        owner = self._lease_owner(context)
        context.ensure_lease()
        async with self._session_factory() as session, session.begin():
            if not await JobRepository(session).lock_owned_lease(context.job.id, owner=owner):
                context.lease_lost.set()
                raise LeaseLostError()
            repository = DownloadTaskRepository(session)
            current = await repository.lock_resolution_work(task.id)
            if current is None or current.attempt != task.attempt:
                raise ArtifactPublicationError(renamed=False)
            if not await repository.transition_to_publishing(
                task.id,
                expected_attempt=task.attempt,
                now=await database_utc_now(session),
            ):
                raise ArtifactPublicationError(renamed=False)

    async def _record_failure(
        self,
        context: JobHandlerContext,
        task: DownloadResolutionWorkRecord,
        reason: DownloadFailureReason,
    ) -> None:
        owner = self._lease_owner(context)
        context.ensure_lease()
        async with self._session_factory() as session, session.begin():
            if not await JobRepository(session).lock_owned_lease(context.job.id, owner=owner):
                context.lease_lost.set()
                raise LeaseLostError()
            changed = await DownloadTaskRepository(session).transition_staging_failure(
                task.id,
                expected_attempt=task.attempt,
                expected_status=task.status,
                status=DownloadTaskStatus.FAILED,
                reason=reason,
                now=await database_utc_now(session),
            )
            if not changed:
                raise ArtifactPublicationError(renamed=False)

    async def _commit_ready(
        self,
        context: JobHandlerContext,
        task: DownloadResolutionWorkRecord,
        artifact_id: UUID,
        published: PublishedArtifact,
        published_bundle: PublishedArtifact,
    ) -> UUID:
        owner = self._lease_owner(context)
        context.ensure_lease()
        async with self._session_factory() as session:
            try:
                if not await JobRepository(session).lock_owned_lease(context.job.id, owner=owner):
                    context.lease_lost.set()
                    raise LeaseLostError()
                repository = DownloadTaskRepository(session)
                current = await repository.lock_resolution_work(task.id)
                if (
                    current is None
                    or current.attempt != task.attempt
                    or current.status is not DownloadTaskStatus.PUBLISHING
                ):
                    raise ArtifactPublicationError(renamed=True)
                now = await database_utc_now(session)
                if not await repository.complete_publication(
                    current,
                    artifact_id=artifact_id,
                    path=str(published.path),
                    manifest_hash=published.manifest_hash,
                    manifest=published.manifest,
                    tokenizer_bundle_hash=published_bundle.manifest_hash,
                    tokenizer_bundle_path=str(published_bundle.path),
                    attention_heads=published.attention_heads,
                    kv_heads=published.kv_heads,
                    size_bytes=published.size_bytes,
                    now=now,
                ):
                    raise ArtifactPublicationError(renamed=True)
                if self._before_commit is not None:
                    self._before_commit()
                await session.commit()
            except BaseException:
                await session.rollback()
                raise
        return artifact_id

    @staticmethod
    def _lease_owner(context: JobHandlerContext) -> str:
        owner = context.job.lease_owner
        if owner is None:
            raise LeaseLostError()
        return owner


class ArtifactRevalidator:
    """Apply a read-only judgement; production manual jobs supply roots and a lease."""

    __slots__ = ("_session_factory",)

    def __init__(self, *, session_factory: AsyncSessionFactory) -> None:
        self._session_factory = session_factory

    @overload
    async def revalidate(
        self,
        artifact_id: UUID,
        *,
        full: bool,
        storage: StorageConfig | None = None,
        context: JobHandlerContext | None = None,
        launch_request_id: UUID | None = None,
        light_check: Literal[False] = False,
    ) -> ArtifactHealthJudgement: ...

    @overload
    async def revalidate(
        self,
        artifact_id: UUID,
        *,
        full: bool,
        storage: StorageConfig | None = None,
        context: JobHandlerContext | None = None,
        launch_request_id: UUID | None = None,
        light_check: Literal[True],
    ) -> ArtifactHealthJudgement | None: ...

    async def revalidate(
        self,
        artifact_id: UUID,
        *,
        full: bool,
        storage: StorageConfig | None = None,
        context: JobHandlerContext | None = None,
        launch_request_id: UUID | None = None,
        light_check: bool = False,
    ) -> ArtifactHealthJudgement | None:
        if light_check and (full or context is None or launch_request_id is not None):
            raise ValueError("light verification requires its own leased job")
        if launch_request_id is not None and context is None:
            raise ValueError("launch verification requires a leased job")
        async with self._session_factory() as session:
            record = await DownloadTaskRepository(session).artifact_for_inspection(artifact_id)
        if light_check and (record is None or record.status is not ModelArtifactStatus.READY):
            return None
        if record is None:
            raise LookupError("model artifact unavailable")
        if storage is not None:
            await asyncio.to_thread(self._check_root, record, storage)
        judgement = await asyncio.to_thread(self._judge_record, record, full=full)
        manifest = None
        if full and judgement.status is ModelArtifactStatus.READY:
            # Read only after full verification, and bind the second read to the
            # same manifest hash before persisting any file metadata.
            raw = (
                serialize_artifact_manifest(record.manifest)
                if record.storage_type is ArtifactStorageType.EXTERNAL
                and record.manifest is not None
                else await asyncio.to_thread(
                    (Path(record.path) / ARTIFACT_MANIFEST_FILENAME).read_bytes
                )
            )
            if artifact_manifest_hash(raw) != record.manifest_hash:
                raise ValueError("artifact manifest changed during verification")
            manifest = deserialize_artifact_manifest(raw)
            if not manifest.files:
                raise ValueError("artifact manifest is empty")
        async with self._session_factory() as session, session.begin():
            if context is not None:
                from holm_persistence.artifact_deletion_repository import (
                    ArtifactDeletionRepository,
                )

                context.ensure_lease()
                if context.job.lease_owner is None or not await JobRepository(
                    session
                ).lock_owned_lease(
                    context.job.id,
                    owner=context.job.lease_owner,
                ):
                    raise LeaseLostError()
                artifacts = ArtifactDeletionRepository(session)
                current = await artifacts.lock(artifact_id)
                deleting = await artifacts.pending(artifact_id)
                if light_check and (
                    current is None or current.status is not ModelArtifactStatus.READY or deleting
                ):
                    return None
                if current is None or deleting:
                    raise LookupError("model artifact unavailable")
            changed = await DownloadTaskRepository(session).apply_artifact_health(
                artifact_id,
                expected_manifest_hash=record.manifest_hash,
                status=judgement.status,
                now=await database_utc_now(session),
                light_check=light_check,
                manifest=manifest,
            )
            if not changed:
                raise RuntimeError("model artifact health update lost its identity")
            if launch_request_id is not None:
                from holm_domain.launch_verification import LaunchVerificationResult
                from holm_persistence.launch_verification_repository import (
                    LaunchVerificationRepository,
                )

                await LaunchVerificationRepository(session).complete(
                    launch_request_id,
                    LaunchVerificationResult(judgement.status.value),
                )
        return judgement

    @staticmethod
    def _check_root(record: ArtifactInspectionRecord, storage: StorageConfig) -> None:
        path = Path(record.path)
        roots = (
            (storage.model_root / "managed",)
            if record.storage_type is ArtifactStorageType.MANAGED
            else storage.external_model_roots
        )
        if (
            not path.is_absolute()
            or path.resolve(strict=False) != path
            or not any(
                root.is_absolute()
                and root.resolve(strict=False) == root
                and (path != root or record.storage_type is ArtifactStorageType.EXTERNAL)
                and path.is_relative_to(root)
                for root in roots
            )
        ):
            raise ValueError("artifact verification unsafe path")

    @staticmethod
    def _judge_record(
        record: ArtifactInspectionRecord,
        *,
        full: bool,
    ) -> ArtifactHealthJudgement:
        # The same read-only operation is used for Managed and External. It never
        # creates, removes, renames, chmods, or rewrites a source-owned path.
        return judge_artifact_health(
            Path(record.path),
            record.manifest_hash,
            full=full,
            stored_manifest=record.manifest
            if record.storage_type is ArtifactStorageType.EXTERNAL
            else None,
        )


__all__ = [
    "CACHEDIR_SIGNATURE",
    "ArtifactHealthJudgement",
    "ArtifactHealthReason",
    "ArtifactRevalidator",
    "ArtifactPublicationError",
    "ArtifactSystemCalls",
    "ArtifactValidationError",
    "ArtifactVerifier",
    "DownloadArtifactPublisher",
    "PublishedArtifact",
    "RuntimeCapability",
    "RuntimeCompatibilityStatus",
    "VerifiedArtifact",
    "judge_artifact_health",
    "judge_runtime_compatibility",
    "publish_verified_artifact",
    "recover_published_artifact",
]

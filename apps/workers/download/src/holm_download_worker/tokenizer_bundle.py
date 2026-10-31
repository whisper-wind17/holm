"""Fail-closed construction of a minimal, inert Tokenizer Bundle."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import ClassVar, Final
from uuid import UUID

from holm_common import (
    StagingFilesystemMismatch,
    StagingPathRejected,
    TokenizerStagingLayout,
)
from holm_domain import (
    ARTIFACT_MANIFEST_FILENAME,
    ArtifactManifest,
    ArtifactManifestEntry,
    ArtifactManifestError,
    artifact_manifest_hash,
    build_artifact_manifest,
    deserialize_artifact_manifest,
    serialize_artifact_manifest,
)

_COPY_CHUNK_BYTES: Final[int] = 1024 * 1024
_MAX_BUNDLE_MANIFEST_BYTES: Final[int] = 1024 * 1024

# This is a filename allowlist, not a suffix denylist.  Runtime-specific entries
# may intersect this set for certified runtimes; they may never add
# names that the platform-level policy does not already permit.
TOKENIZER_BUNDLE_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        "added_tokens.json",
        "config.json",
        "merges.txt",
        "sentencepiece.bpe.model",
        "special_tokens_map.json",
        "spiece.model",
        "tokenizer.json",
        "tokenizer.model",
        "tokenizer_config.json",
        "tokenizer.vocab",
        "vocab.json",
        "vocab.txt",
    }
)


class TokenizerBundleBuildStatus(StrEnum):
    BUILT = "built"
    NO_ELIGIBLE_FILES = "no_eligible_files"
    REJECTED = "rejected"


class TokenizerBundleRejectionReason(StrEnum):
    FILESYSTEM_MISMATCH = "filesystem_mismatch"
    UNSAFE_PATH = "unsafe_path"
    UNSAFE_FILE = "unsafe_file"
    CONTENT_CHANGED = "content_changed"
    IO_FAILURE = "io_failure"


@dataclass(frozen=True, slots=True)
class TokenizerBundleBuilt:
    """A non-empty Bundle staged beneath ``tokenizer_root``."""

    staging_path: Path
    destination_path: Path
    manifest: ArtifactManifest
    manifest_bytes: bytes
    manifest_hash: str
    size_bytes: int
    status: ClassVar[TokenizerBundleBuildStatus] = TokenizerBundleBuildStatus.BUILT

    def __post_init__(self) -> None:
        _validate_bundle_manifest(self.manifest)
        if self.size_bytes != sum(entry.size_bytes for entry in self.manifest.files):
            raise ValueError("Tokenizer Bundle size does not match its Manifest")
        if artifact_manifest_hash(self.manifest_bytes) != self.manifest_hash:
            raise ValueError("Tokenizer Bundle hash does not match its Manifest")


@dataclass(frozen=True, slots=True)
class TokenizerBundleEmpty:
    """The source contains no platform-allowlisted Tokenizer file."""

    status: ClassVar[TokenizerBundleBuildStatus] = TokenizerBundleBuildStatus.NO_ELIGIBLE_FILES


@dataclass(frozen=True, slots=True)
class TokenizerBundleRejected:
    """The Bundle could not be constructed without weakening a safety boundary."""

    reason: TokenizerBundleRejectionReason
    status: ClassVar[TokenizerBundleBuildStatus] = TokenizerBundleBuildStatus.REJECTED


type TokenizerBundleBuildResult = (
    TokenizerBundleBuilt | TokenizerBundleEmpty | TokenizerBundleRejected
)


@dataclass(frozen=True, slots=True)
class RecoveredTokenizerBundle:
    """A complete Bundle accepted by the stricter retry-time policy."""

    path: Path
    manifest: ArtifactManifest
    manifest_bytes: bytes
    manifest_hash: str
    size_bytes: int

    def __post_init__(self) -> None:
        _validate_bundle_manifest(self.manifest)
        if self.size_bytes != sum(entry.size_bytes for entry in self.manifest.files):
            raise ValueError("recovered Tokenizer Bundle size does not match its Manifest")
        if artifact_manifest_hash(self.manifest_bytes) != self.manifest_hash:
            raise ValueError("recovered Tokenizer Bundle hash does not match its Manifest")


class TokenizerBundleRecoveryError(RuntimeError):
    """A staged or published Bundle is not exact, inert, and allowlisted."""


class _UnsafeTokenizerFile(RuntimeError):
    def __init__(self, reason: TokenizerBundleRejectionReason) -> None:
        self.reason = reason
        super().__init__("Tokenizer Bundle source file rejected")


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("Tokenizer Bundle copy made no progress")
        view = view[written:]


def _copy_verified_file(
    source: Path,
    destination: Path,
    expected: ArtifactManifestEntry,
    ensure_owned: Callable[[], None],
) -> ArtifactManifestEntry:
    ensure_owned()
    try:
        source_descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise _UnsafeTokenizerFile(TokenizerBundleRejectionReason.UNSAFE_FILE) from error
    destination_descriptor: int | None = None
    digest = hashlib.sha256()
    copied = 0
    try:
        before = os.fstat(source_descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o111:
            raise _UnsafeTokenizerFile(TokenizerBundleRejectionReason.UNSAFE_FILE)
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o640,
        )
        while chunk := os.read(source_descriptor, _COPY_CHUNK_BYTES):
            ensure_owned()
            digest.update(chunk)
            copied += len(chunk)
            _write_all(destination_descriptor, chunk)
        ensure_owned()
        after = os.fstat(source_descriptor)
        if (before.st_dev, before.st_ino, before.st_size) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ):
            raise _UnsafeTokenizerFile(TokenizerBundleRejectionReason.CONTENT_CHANGED)
        if copied != expected.size_bytes or digest.hexdigest() != expected.sha256:
            raise _UnsafeTokenizerFile(TokenizerBundleRejectionReason.CONTENT_CHANGED)
        return ArtifactManifestEntry(expected.path, copied, digest.hexdigest())
    finally:
        if destination_descriptor is not None:
            os.close(destination_descriptor)
        os.close(source_descriptor)


def build_managed_tokenizer_bundle(
    source_root: Path,
    verified_manifest: ArtifactManifest,
    *,
    layout: TokenizerStagingLayout,
    model_root: Path,
    artifact_id: UUID,
    ensure_owned: Callable[[], None] = lambda: None,
) -> TokenizerBundleBuildResult:
    """Build one Managed Bundle, returning each non-success state explicitly."""

    try:
        ensure_owned()
        staging = layout.preflight(artifact_id, model_root=model_root)
        ensure_owned()
        destination = layout.destination_path(artifact_id)
    except StagingFilesystemMismatch:
        return TokenizerBundleRejected(TokenizerBundleRejectionReason.FILESYSTEM_MISMATCH)
    except (OSError, StagingPathRejected):
        return TokenizerBundleRejected(TokenizerBundleRejectionReason.UNSAFE_PATH)

    selected = tuple(
        entry for entry in verified_manifest.files if entry.path in TOKENIZER_BUNDLE_ALLOWLIST
    )
    if not selected:
        return TokenizerBundleEmpty()

    try:
        ensure_owned()
        layout.cleanup_staging(artifact_id)
        ensure_owned()
        staging = layout.create_staging_directory(artifact_id)
        copied = tuple(
            _copy_verified_file(
                source_root.joinpath(*entry.path.split("/")),
                staging.joinpath(*entry.path.split("/")),
                entry,
                ensure_owned,
            )
            for entry in selected
        )
        ensure_owned()
        manifest = build_artifact_manifest(copied)
        manifest_bytes = serialize_artifact_manifest(manifest)
        return TokenizerBundleBuilt(
            staging_path=staging,
            destination_path=destination,
            manifest=manifest,
            manifest_bytes=manifest_bytes,
            manifest_hash=artifact_manifest_hash(manifest_bytes),
            size_bytes=sum(entry.size_bytes for entry in copied),
        )
    except _UnsafeTokenizerFile as error:
        ensure_owned()
        _cleanup_rejected(layout, artifact_id)
        return TokenizerBundleRejected(error.reason)
    except (OSError, StagingPathRejected):
        ensure_owned()
        _cleanup_rejected(layout, artifact_id)
        return TokenizerBundleRejected(TokenizerBundleRejectionReason.IO_FAILURE)


def _validate_bundle_manifest(manifest: ArtifactManifest) -> None:
    paths = {entry.path for entry in manifest.files}
    if not paths:
        raise ValueError("a Tokenizer Bundle cannot be empty")
    if not paths <= TOKENIZER_BUNDLE_ALLOWLIST:
        raise ValueError("a Tokenizer Bundle contains a non-allowlisted file")


def _read_inert_file(path: Path, *, maximum_bytes: int | None = None) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise TokenizerBundleRecoveryError("Tokenizer Bundle file cannot be opened") from error
    chunks: list[bytes] = []
    total = 0
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o111:
            raise TokenizerBundleRecoveryError("Tokenizer Bundle file is not inert")
        while chunk := os.read(descriptor, _COPY_CHUNK_BYTES):
            total += len(chunk)
            if maximum_bytes is not None and total > maximum_bytes:
                raise TokenizerBundleRecoveryError("Tokenizer Bundle file is too large")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ):
            raise TokenizerBundleRecoveryError("Tokenizer Bundle file changed while reading")
    except OSError as error:
        raise TokenizerBundleRecoveryError("Tokenizer Bundle file cannot be read") from error
    finally:
        os.close(descriptor)
    return b"".join(chunks)


def _hash_inert_file(path: Path) -> tuple[int, str]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise TokenizerBundleRecoveryError("Tokenizer Bundle file cannot be opened") from error
    digest = hashlib.sha256()
    size = 0
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o111:
            raise TokenizerBundleRecoveryError("Tokenizer Bundle file is not inert")
        while chunk := os.read(descriptor, _COPY_CHUNK_BYTES):
            size += len(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ):
            raise TokenizerBundleRecoveryError("Tokenizer Bundle file changed while reading")
    except OSError as error:
        raise TokenizerBundleRecoveryError("Tokenizer Bundle file cannot be read") from error
    finally:
        os.close(descriptor)
    return size, digest.hexdigest()


def recover_tokenizer_bundle(path: Path) -> RecoveredTokenizerBundle:
    """Recover only an exact file set whose Manifest remains fail-closed."""

    try:
        root_metadata = path.lstat()
        if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_ISLNK(root_metadata.st_mode):
            raise TokenizerBundleRecoveryError("Tokenizer Bundle root is unsafe")
        manifest_bytes = _read_inert_file(
            path / ARTIFACT_MANIFEST_FILENAME,
            maximum_bytes=_MAX_BUNDLE_MANIFEST_BYTES,
        )
        manifest = deserialize_artifact_manifest(manifest_bytes)
        _validate_bundle_manifest(manifest)
        expected_paths = {ARTIFACT_MANIFEST_FILENAME, *(entry.path for entry in manifest.files)}
        before_names = {entry.name for entry in os.scandir(path)}
        if before_names != expected_paths:
            raise TokenizerBundleRecoveryError("Tokenizer Bundle file set is not exact")
        total_size = 0
        for entry in manifest.files:
            size, digest = _hash_inert_file(path / entry.path)
            if size != entry.size_bytes or digest != entry.sha256:
                raise TokenizerBundleRecoveryError("Tokenizer Bundle content does not match")
            total_size += size
        if {entry.name for entry in os.scandir(path)} != expected_paths:
            raise TokenizerBundleRecoveryError("Tokenizer Bundle changed while recovering")
        return RecoveredTokenizerBundle(
            path=path,
            manifest=manifest,
            manifest_bytes=manifest_bytes,
            manifest_hash=artifact_manifest_hash(manifest_bytes),
            size_bytes=total_size,
        )
    except (ArtifactManifestError, OSError, ValueError) as error:
        if isinstance(error, TokenizerBundleRecoveryError):
            raise
        raise TokenizerBundleRecoveryError("Tokenizer Bundle recovery rejected") from error


def _cleanup_rejected(layout: TokenizerStagingLayout, artifact_id: UUID) -> None:
    try:
        layout.cleanup_staging(artifact_id)
    except (OSError, StagingPathRejected):
        # The result remains a rejection.  A maintenance pass can quarantine a
        # staging directory that cannot be safely addressed by this process.
        return


__all__ = [
    "TOKENIZER_BUNDLE_ALLOWLIST",
    "RecoveredTokenizerBundle",
    "TokenizerBundleBuildResult",
    "TokenizerBundleBuildStatus",
    "TokenizerBundleBuilt",
    "TokenizerBundleEmpty",
    "TokenizerBundleRejected",
    "TokenizerBundleRejectionReason",
    "TokenizerBundleRecoveryError",
    "build_managed_tokenizer_bundle",
    "recover_tokenizer_bundle",
]

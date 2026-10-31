"""Canonical per-file evidence for one published Model Artifact."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

from holm_domain.model_resolution import Identified, RemoteFile

ARTIFACT_MANIFEST_FILENAME: Final[str] = "manifest.json"
ARTIFACT_MANIFEST_SCHEMA_VERSION: Final[int] = 1
_SHA256_HEX_LENGTH: Final[int] = 64
_HEX_DIGITS: Final[frozenset[str]] = frozenset("0123456789abcdef")


class ArtifactManifestError(ValueError):
    """Manifest bytes are unsafe, ambiguous, or not in canonical form."""


def canonical_artifact_path(value: str) -> str:
    """Validate the one relative POSIX spelling accepted by Artifact manifests."""

    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ArtifactManifestError("artifact manifest path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ArtifactManifestError("artifact manifest path is not a safe relative path")
    rendered = path.as_posix()
    if rendered != value:
        raise ArtifactManifestError("artifact manifest path is not canonical")
    return rendered


def _is_sha256(value: str) -> bool:
    return len(value) == _SHA256_HEX_LENGTH and all(character in _HEX_DIGITS for character in value)


@dataclass(frozen=True, order=True, slots=True)
class ArtifactManifestEntry:
    """One regular file, relative to the Artifact root."""

    path: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        canonical_artifact_path(self.path)
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            raise ArtifactManifestError("artifact manifest size is invalid")
        if not isinstance(self.sha256, str) or not _is_sha256(self.sha256):
            raise ArtifactManifestError("artifact manifest hash is invalid")


@dataclass(frozen=True, slots=True)
class ArtifactManifest:
    """A path-sorted, duplicate-free Manifest with deterministic bytes."""

    files: tuple[ArtifactManifestEntry, ...]
    schema_version: int = ARTIFACT_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != ARTIFACT_MANIFEST_SCHEMA_VERSION
        ):
            raise ArtifactManifestError("artifact manifest schema version is unsupported")
        paths = tuple(entry.path for entry in self.files)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ArtifactManifestError("artifact manifest files must be uniquely path-sorted")

    def as_remote_files(self) -> Identified[tuple[RemoteFile, ...]]:
        """Project persisted evidence onto the existing content-comparison contract."""

        return Identified(
            tuple(
                RemoteFile(
                    path=entry.path,
                    size_bytes=entry.size_bytes,
                    content_sha256=Identified(entry.sha256),
                )
                for entry in self.files
            )
        )


def build_artifact_manifest(entries: Iterable[ArtifactManifestEntry]) -> ArtifactManifest:
    """Sort once at the sole Manifest construction boundary."""

    return ArtifactManifest(tuple(sorted(entries, key=lambda entry: entry.path)))


def serialize_artifact_manifest(manifest: ArtifactManifest) -> bytes:
    """Return the only JSON representation used for disk bytes and hashing."""

    payload = {
        "schema_version": manifest.schema_version,
        "files": [
            {
                "path": entry.path,
                "size_bytes": entry.size_bytes,
                "sha256": entry.sha256,
            }
            for entry in manifest.files
        ],
    }
    return (
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def artifact_manifest_hash(manifest_bytes: bytes) -> str:
    """Hash the exact Manifest bytes; entry order is therefore observable."""

    return hashlib.sha256(manifest_bytes).hexdigest()


def deserialize_artifact_manifest(raw: bytes) -> ArtifactManifest:
    """Parse only canonical bytes so equivalent-but-different JSON cannot fork identity."""

    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ArtifactManifestError("artifact manifest JSON is invalid") from None
    if not isinstance(payload, dict) or set(payload) != {"files", "schema_version"}:
        raise ArtifactManifestError("artifact manifest object shape is invalid")
    if payload["schema_version"] != ARTIFACT_MANIFEST_SCHEMA_VERSION:
        raise ArtifactManifestError("artifact manifest schema version is unsupported")
    values = payload["files"]
    if not isinstance(values, list):
        raise ArtifactManifestError("artifact manifest files are invalid")
    entries: list[ArtifactManifestEntry] = []
    for value in values:
        if not isinstance(value, dict) or set(value) != {"path", "sha256", "size_bytes"}:
            raise ArtifactManifestError("artifact manifest entry shape is invalid")
        path = value["path"]
        digest = value["sha256"]
        size = value["size_bytes"]
        if not isinstance(path, str) or not isinstance(digest, str) or not isinstance(size, int):
            raise ArtifactManifestError("artifact manifest entry types are invalid")
        entries.append(ArtifactManifestEntry(path=path, size_bytes=size, sha256=digest))
    manifest = ArtifactManifest(tuple(entries))
    if serialize_artifact_manifest(manifest) != raw:
        raise ArtifactManifestError("artifact manifest bytes are not canonical")
    return manifest


__all__ = [
    "ARTIFACT_MANIFEST_FILENAME",
    "ARTIFACT_MANIFEST_SCHEMA_VERSION",
    "ArtifactManifest",
    "ArtifactManifestEntry",
    "ArtifactManifestError",
    "artifact_manifest_hash",
    "build_artifact_manifest",
    "canonical_artifact_path",
    "deserialize_artifact_manifest",
    "serialize_artifact_manifest",
]

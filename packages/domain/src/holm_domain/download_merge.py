"""Pure content comparison and distinguishable resolution-merge outcomes."""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from holm_domain.model_resolution import Identified, MetadataKnowledge, RemoteFile


class ComparisonUnavailableReason(StrEnum):
    ARTIFACT_FILE_MANIFEST_UNAVAILABLE = "artifact_file_manifest_unavailable"


@dataclass(frozen=True, slots=True)
class ArtifactFileManifestUnavailable:
    """The Artifact has no readable, hash-authenticated per-file evidence."""


@dataclass(frozen=True, slots=True)
class ContentsMatch:
    pass


@dataclass(frozen=True, slots=True)
class ContentsDiffer:
    pass


@dataclass(frozen=True, slots=True)
class ComparisonUnavailable:
    reason: ComparisonUnavailableReason


type ContentComparison = ContentsMatch | ContentsDiffer | ComparisonUnavailable


def _snapshot(files: MetadataKnowledge[tuple[RemoteFile, ...]]) -> dict[str, tuple[int, str]]:
    """Only complete, unambiguous snapshots constitute comparison evidence."""
    if not isinstance(files, Identified):
        raise ValueError("content comparison requires an identified file manifest")
    result: dict[str, tuple[int, str]] = {}
    for entry in files.value:
        if entry.path in result or not isinstance(entry.content_sha256, Identified):
            raise ValueError("content comparison requires unique files with content hashes")
        result[entry.path] = (entry.size_bytes, entry.content_sha256.value)
    return result


def compare_contents(
    source: MetadataKnowledge[tuple[RemoteFile, ...]],
    artifact: MetadataKnowledge[tuple[RemoteFile, ...]] | ArtifactFileManifestUnavailable,
) -> ContentComparison:
    # Missing evidence is not a negative comparison. Keep this first: today's
    # repository cannot supply a manifest, regardless of the source's metadata.
    if isinstance(artifact, ArtifactFileManifestUnavailable):
        return ComparisonUnavailable(ComparisonUnavailableReason.ARTIFACT_FILE_MANIFEST_UNAVAILABLE)
    if _snapshot(source) == _snapshot(artifact):
        return ContentsMatch()
    return ContentsDiffer()


@dataclass(frozen=True, slots=True)
class DownloadCompleted:
    task_id: UUID
    artifact_id: UUID


@dataclass(frozen=True, slots=True)
class DownloadMerged:
    """Subscriptions moved; no transient winner identity is promised on replay."""

    task_id: UUID


@dataclass(frozen=True, slots=True)
class DownloadStarted:
    task_id: UUID


type DownloadMergeResult = DownloadCompleted | DownloadMerged | DownloadStarted

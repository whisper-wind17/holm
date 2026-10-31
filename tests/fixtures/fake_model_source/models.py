"""Typed, metadata-only models for the Fake Model Source fixture.

The objects in this module are test data and injectable expectations for
``FX-MS-01`` through ``FX-MS-07``.  They do not access a network, create a
staging area, materialize model payloads, or create special filesystem objects.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Final

FIXTURE_REVIEW_STATUS: Final = "待真机复核"
GIB_BYTES: Final = 1024**3
MINIMUM_LARGE_REPOSITORY_BYTES: Final = 10 * GIB_BYTES

_COMMIT_PATTERN: Final = re.compile(r"[0-9a-f]{40}\Z")


class FixtureId(StrEnum):
    """TST 3.4 Fake Model Source fixture identifiers."""

    FX_MS_01 = "FX-MS-01"
    FX_MS_02 = "FX-MS-02"
    FX_MS_03 = "FX-MS-03"
    FX_MS_04 = "FX-MS-04"
    FX_MS_05 = "FX-MS-05"
    FX_MS_06 = "FX-MS-06"
    FX_MS_07 = "FX-MS-07"


class KnowledgeState(StrEnum):
    """Explicit state used whenever source metadata is unavailable."""

    UNRECOGNIZED = "unrecognized"


@dataclass(frozen=True, slots=True)
class KnownValue[T]:
    """A source metadata field whose value was identified."""

    value: T


@dataclass(frozen=True, slots=True)
class UnknownValue:
    """A source metadata field explicitly reported as not identified."""

    state: KnowledgeState = field(default=KnowledgeState.UNRECOGNIZED, init=False)


type Knowledge[T] = KnownValue[T] | UnknownValue


@dataclass(frozen=True, slots=True)
class ResolvedRevision:
    """One normalized request pinned to an immutable synthetic commit."""

    requested_revision: str
    resolved_commit: str

    def __post_init__(self) -> None:
        if not self.requested_revision:
            raise ValueError("requested revision must be non-empty after normalization")
        if _COMMIT_PATTERN.fullmatch(self.resolved_commit) is None:
            raise ValueError("resolved revision must be a canonical immutable commit")


class RevisionReferenceKind(StrEnum):
    """Mutable reference kinds resolved by the fixture."""

    BRANCH = "branch"
    TAG = "tag"


@dataclass(frozen=True, slots=True)
class RevisionScenario:
    """An isolated branch/tag/default resolver with no remote source access."""

    repository_id: str
    aliases: Mapping[str, str]
    reference_kinds: Mapping[str, RevisionReferenceKind]
    default_reference: str
    fixture_id: FixtureId = field(default=FixtureId.FX_MS_01, init=False)
    review_status: str = field(default=FIXTURE_REVIEW_STATUS, init=False)

    def __post_init__(self) -> None:
        if not self.repository_id:
            raise ValueError("repository ID must be non-empty")
        aliases = dict(self.aliases)
        if not aliases:
            raise ValueError("revision fixture requires at least one reference")
        if self.default_reference not in aliases:
            raise ValueError("default reference must exist in the alias mapping")
        if any(not reference for reference in aliases):
            raise ValueError("revision references must be non-empty")
        if any(_COMMIT_PATTERN.fullmatch(commit) is None for commit in aliases.values()):
            raise ValueError("every revision reference must resolve to an immutable commit")
        reference_kinds = dict(self.reference_kinds)
        if reference_kinds.keys() != aliases.keys():
            raise ValueError("every revision reference must declare whether it is a branch or tag")
        object.__setattr__(self, "aliases", MappingProxyType(aliases))
        object.__setattr__(self, "reference_kinds", MappingProxyType(reference_kinds))

    def resolve(self, requested_revision: str | None) -> ResolvedRevision:
        """Resolve a supplied reference, using ``@default`` only when omitted."""

        normalized = "@default" if requested_revision is None else requested_revision
        if not normalized:
            raise ValueError("an empty revision is not an omitted revision")
        lookup_reference = self.default_reference if normalized == "@default" else normalized
        return ResolvedRevision(
            requested_revision=normalized,
            resolved_commit=self.aliases[lookup_reference],
        )


class RemoteFileRole(StrEnum):
    """Roles needed to describe a synthetic sharded model repository."""

    CONFIG = "config"
    TOKENIZER = "tokenizer"
    SHARD = "shard"
    SHARD_INDEX = "shard_index"


@dataclass(frozen=True, slots=True)
class RemoteFileDescriptor:
    """Logical remote file metadata; no payload bytes are retained."""

    path: str
    logical_size_bytes: int
    role: RemoteFileRole

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError("remote file path must be non-empty")
        if self.logical_size_bytes < 0:
            raise ValueError("logical file size must be non-negative")


@dataclass(frozen=True, slots=True)
class ShardIndexDescriptor:
    """Metadata-only shard index with tensor names mapped to shard paths."""

    path: str
    shard_paths: tuple[str, ...]
    tensor_to_shard: Mapping[str, str]

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError("shard index path must be non-empty")
        if len(self.shard_paths) < 2 or len(set(self.shard_paths)) != len(self.shard_paths):
            raise ValueError("shard index requires multiple unique shard paths")
        tensor_to_shard = dict(self.tensor_to_shard)
        if not tensor_to_shard:
            raise ValueError("shard index requires at least one tensor mapping")
        if not set(tensor_to_shard.values()).issubset(self.shard_paths):
            raise ValueError("shard index mappings must reference declared shards")
        object.__setattr__(self, "tensor_to_shard", MappingProxyType(tensor_to_shard))


@dataclass(frozen=True, slots=True)
class SyntheticLargeRepository:
    """A >=10 GiB logical repository represented entirely by small metadata."""

    repository_id: str
    synthetic_logical_size_bytes: int
    shards: tuple[RemoteFileDescriptor, ...]
    shard_index: ShardIndexDescriptor
    reported_file_list: Knowledge[tuple[RemoteFileDescriptor, ...]]
    reported_total_size_bytes: Knowledge[int]
    fixture_id: FixtureId = field(default=FixtureId.FX_MS_02, init=False)
    review_status: str = field(default=FIXTURE_REVIEW_STATUS, init=False)
    materialized_payload_bytes: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.synthetic_logical_size_bytes < MINIMUM_LARGE_REPOSITORY_BYTES:
            raise ValueError("synthetic repository must describe at least 10 GiB")
        if len(self.shards) < 2 or any(
            shard.role is not RemoteFileRole.SHARD for shard in self.shards
        ):
            raise ValueError("synthetic repository requires multiple shard descriptors")
        shard_paths = tuple(shard.path for shard in self.shards)
        if shard_paths != self.shard_index.shard_paths:
            raise ValueError("shard descriptors and shard index must agree")
        if (
            sum(shard.logical_size_bytes for shard in self.shards)
            > self.synthetic_logical_size_bytes
        ):
            raise ValueError("shards cannot exceed the synthetic repository size")
        if isinstance(self.reported_file_list, KnownValue):
            paths = {item.path for item in self.reported_file_list.value}
            if self.shard_index.path not in paths or not set(shard_paths).issubset(paths):
                raise ValueError("known file list must contain the index and every shard")
        if (
            isinstance(self.reported_total_size_bytes, KnownValue)
            and self.reported_total_size_bytes.value != self.synthetic_logical_size_bytes
        ):
            raise ValueError("known total size must equal the synthetic logical size")


class InterruptionKind(StrEnum):
    """Interruptions required by the download resume fixture."""

    TRANSFER_INTERRUPTED = "transfer_interrupted"
    PROCESS_KILLED = "process_killed"
    NETWORK_DISCONNECTED = "network_disconnected"


class ReusableEntryKind(StrEnum):
    """Retained staging entry descriptions understood by an SDK resume path."""

    PARTIAL = "partial"
    CACHE = "cache"


@dataclass(frozen=True, slots=True)
class ReusableStagingEntry:
    """Description of retained progress; this is not an on-disk entry."""

    relative_path: str
    kind: ReusableEntryKind
    retained_bytes: int

    def __post_init__(self) -> None:
        if not self.relative_path or self.relative_path.startswith("/"):
            raise ValueError("reusable staging entry must use a non-empty relative path")
        if self.retained_bytes <= 0:
            raise ValueError("reusable staging entry must retain positive progress")


@dataclass(frozen=True, slots=True)
class OriginResumeRequest:
    """Expected ranged source request after reusing retained staging bytes."""

    path: str
    start_offset_bytes: int

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError("resume request path must be non-empty")
        if self.start_offset_bytes < 0:
            raise ValueError("resume request offset must be non-negative")


@dataclass(frozen=True, slots=True)
class ResumeScenario:
    """Expected resume inputs and origin requests after one interruption."""

    interruption: InterruptionKind
    completed_file_paths: tuple[str, ...]
    reusable_staging_entries: tuple[ReusableStagingEntry, ...]
    origin_requests_after_resume: tuple[OriginResumeRequest, ...]
    retransmitted_completed_file_paths: tuple[str, ...] = ()
    fixture_id: FixtureId = field(default=FixtureId.FX_MS_03, init=False)
    review_status: str = field(default=FIXTURE_REVIEW_STATUS, init=False)

    def __post_init__(self) -> None:
        if not self.completed_file_paths:
            raise ValueError("resume scenario requires at least one completed file")
        if {entry.kind for entry in self.reusable_staging_entries} != set(ReusableEntryKind):
            raise ValueError("resume scenario must describe reusable partial and cache entries")
        requested_paths = {request.path for request in self.origin_requests_after_resume}
        if set(self.completed_file_paths) & requested_paths:
            raise ValueError("completed files cannot be requested from the origin after resume")
        if self.retransmitted_completed_file_paths:
            raise ValueError("resume expectation cannot retransmit completed files")


class CredentialReference(StrEnum):
    """Opaque synthetic credential states; no credential material is stored."""

    NOT_SUPPLIED = "not_supplied"
    SYNTHETIC_AUTHORIZED = "synthetic_authorized_reference"
    SYNTHETIC_INVALID = "synthetic_invalid_reference"
    SYNTHETIC_EXPIRED = "synthetic_expired_reference"
    SYNTHETIC_UNAUTHORIZED = "synthetic_unauthorized_reference"


class CredentialCase(StrEnum):
    """The eight independently injectable source access cases from FX-MS-04."""

    PUBLIC = "public"
    PRIVATE_ACCESSIBLE = "private_accessible"
    INVALID_CREDENTIAL = "invalid_credential"
    EXPIRED_CREDENTIAL = "expired_credential"
    PERMISSION_DENIED = "permission_denied"
    REPOSITORY_NOT_FOUND = "repository_not_found"
    ANONYMOUS_INDISTINGUISHABLE = "anonymous_indistinguishable"
    RATE_LIMITED = "rate_limited"


class AccessOutcome(StrEnum):
    """Sanitized result category supplied to future adapter tests.

    By construction this is the set of outcomes that a ``ModelSourceValidationStatus``
    can carry, and ``TC-SRC-08`` holds it exactly equal to that enum, to the
    database CHECK and to the response Schema.  A conclusion the platform can
    reach but no verification status can express therefore has no member here --
    the same shape as ``VALIDATOR_UNAVAILABLE``/``VALIDATION_TIMEOUT``/
    ``VALIDATION_FAILED``, which are ``status=None`` results and are likewise
    absent from this enum.  See ``CredentialScenario.outcome``.
    """

    ACCESSIBLE = "accessible"
    INVALID_CREDENTIAL = "invalid_credential"
    PERMISSION_DENIED = "permission_denied"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"


@dataclass(frozen=True, slots=True)
class CredentialScenario:
    """Credential behavior expressed without a secret-like string.

    ``outcome`` is ``None`` when the case's conclusion carries **no** verification
    status: an explicit "no ``ModelSourceValidationStatus`` can express this",
    not a forgotten field and not a stand-in for "unknown".  Today one case is
    shaped that way -- ``ANONYMOUS_INDISTINGUISHABLE``, whose conclusion is
    "missing or invisible, the platform cannot tell" and whose result carries
    ``status=None`` with its own reason code and next action.  Minting an
    ``AccessOutcome`` member for it instead would break ``TC-SRC-08``'s
    four-way equality and force a database migration for a status the product
    does not have.
    """

    case: CredentialCase
    credential_reference: CredentialReference
    outcome: AccessOutcome | None
    fixture_id: FixtureId = field(default=FixtureId.FX_MS_04, init=False)
    review_status: str = field(default=FIXTURE_REVIEW_STATUS, init=False)


class ValidationFailureKind(StrEnum):
    """The four source validation failures required by FX-MS-05."""

    HASH_MISMATCH = "hash_mismatch"
    SHARD_MISSING = "shard_missing"
    SAFETENSORS_HEADER_CORRUPT = "safetensors_header_corrupt"
    CONFIG_OR_TOKENIZER_MISSING = "config_or_tokenizer_missing"


class SafetensorsHeaderState(StrEnum):
    """Descriptive Safetensors header state; no model bytes are needed."""

    NOT_APPLICABLE = "not_applicable"
    CORRUPT = "corrupt"


@dataclass(frozen=True, slots=True)
class ValidationFailureSample:
    """Metadata-only failure input for a future source validator."""

    kind: ValidationFailureKind
    affected_paths: tuple[str, ...]
    expected_sha256: str | None = None
    observed_sha256: str | None = None
    header_state: SafetensorsHeaderState = SafetensorsHeaderState.NOT_APPLICABLE
    fixture_id: FixtureId = field(default=FixtureId.FX_MS_05, init=False)
    review_status: str = field(default=FIXTURE_REVIEW_STATUS, init=False)

    def __post_init__(self) -> None:
        if not self.affected_paths:
            raise ValueError("validation failure sample requires at least one affected path")
        if self.kind is ValidationFailureKind.HASH_MISMATCH:
            if self.expected_sha256 is None or self.observed_sha256 is None:
                raise ValueError("hash mismatch requires expected and observed hashes")
            if self.expected_sha256 == self.observed_sha256:
                raise ValueError("hash mismatch hashes must differ")
        elif self.expected_sha256 is not None or self.observed_sha256 is not None:
            raise ValueError("hash values belong only to the hash mismatch sample")
        if self.kind is ValidationFailureKind.SAFETENSORS_HEADER_CORRUPT:
            if self.header_state is not SafetensorsHeaderState.CORRUPT:
                raise ValueError("Safetensors failure must describe a corrupt header")
        elif self.header_state is not SafetensorsHeaderState.NOT_APPLICABLE:
            raise ValueError("header state belongs only to the Safetensors sample")


class PathAttackKind(StrEnum):
    """Every descriptive path/repository attack input required by FX-MS-06."""

    PARENT_TRAVERSAL = "parent_traversal"
    ABSOLUTE_PATH = "absolute_path"
    ESCAPING_SYMBOLIC_LINK = "escaping_symbolic_link"
    DEVICE_FILE = "device_file"
    FIFO = "fifo"
    SOCKET = "socket"
    SETUID_FILE = "setuid_file"
    INVALID_REPOSITORY_ID = "invalid_repository_id"


class DescribedEntryKind(StrEnum):
    """Filesystem entry kinds represented only as enum values and strings."""

    REGULAR_FILE = "regular_file"
    SYMBOLIC_LINK = "symbolic_link"
    DEVICE = "device"
    FIFO = "fifo"
    SOCKET = "socket"


@dataclass(frozen=True, slots=True)
class PathAttackSample:
    """One described path/repository attack input (FX-MS-06).

    The sample itself is a description and holds no filesystem entry, which is
    what ``description_only`` states.  ``materialization.materialize_path_attack``
    projects a description onto real entries inside a caller-owned temporary
    directory when a test needs the attack to actually exist on disk; the
    description stays the only truth source for what gets created.
    """

    kind: PathAttackKind
    untrusted_value: str
    entry_kind: DescribedEntryKind | None
    symbolic_link_target: str | None = None
    mode_bits: int | None = None
    fixture_id: FixtureId = field(default=FixtureId.FX_MS_06, init=False)
    review_status: str = field(default=FIXTURE_REVIEW_STATUS, init=False)
    description_only: bool = field(default=True, init=False)

    def __post_init__(self) -> None:
        if not self.untrusted_value:
            raise ValueError("path attack description requires a non-empty value")
        if self.kind is PathAttackKind.INVALID_REPOSITORY_ID:
            if self.entry_kind is not None:
                raise ValueError("invalid repository ID is not a filesystem entry")
        elif self.entry_kind is None:
            raise ValueError("filesystem attack descriptions require an entry kind")
        if self.kind is PathAttackKind.ESCAPING_SYMBOLIC_LINK:
            if (
                self.entry_kind is not DescribedEntryKind.SYMBOLIC_LINK
                or self.symbolic_link_target is None
            ):
                raise ValueError("symbolic-link attack requires a described target")
        elif self.symbolic_link_target is not None:
            raise ValueError("only a symbolic-link attack may carry a target")
        if self.kind is PathAttackKind.SETUID_FILE:
            if self.entry_kind is not DescribedEntryKind.REGULAR_FILE or self.mode_bits is None:
                raise ValueError("Setuid attack requires regular-file mode metadata")
            if self.mode_bits & 0o4000 == 0:
                raise ValueError("Setuid attack mode must contain the Setuid bit")
        elif self.mode_bits is not None:
            raise ValueError("mode metadata belongs only to the Setuid sample")


class MetadataField(StrEnum):
    """Model metadata fields that may independently be unrecognized."""

    ARCHITECTURE = "architecture"
    MODALITY = "modality"
    PRECISION = "precision"
    CONTEXT = "context"


@dataclass(frozen=True, slots=True)
class ModelMetadata:
    """Source metadata whose unknowns are represented by ``UnknownValue``."""

    architecture: Knowledge[str]
    modality: Knowledge[str]
    precision: Knowledge[str]
    context: Knowledge[int]

    def __post_init__(self) -> None:
        for field_name, value in (
            (MetadataField.ARCHITECTURE, self.architecture),
            (MetadataField.MODALITY, self.modality),
            (MetadataField.PRECISION, self.precision),
        ):
            if isinstance(value, KnownValue) and not value.value:
                raise ValueError(f"known {field_name.value} metadata cannot be empty")
        if isinstance(self.context, KnownValue) and (
            isinstance(self.context.value, bool) or self.context.value <= 0
        ):
            raise ValueError("known context metadata must be a positive integer")

    def value_for(self, metadata_field: MetadataField) -> Knowledge[object]:
        """Return one typed field without synthesizing a fallback value."""

        if metadata_field is MetadataField.ARCHITECTURE:
            return self.architecture
        if metadata_field is MetadataField.MODALITY:
            return self.modality
        if metadata_field is MetadataField.PRECISION:
            return self.precision
        return self.context


@dataclass(frozen=True, slots=True)
class IncompleteMetadataScenario:
    """One model descriptor with exactly one explicitly unrecognized field."""

    unknown_field: MetadataField
    metadata: ModelMetadata
    fixture_id: FixtureId = field(default=FixtureId.FX_MS_07, init=False)
    review_status: str = field(default=FIXTURE_REVIEW_STATUS, init=False)

    def __post_init__(self) -> None:
        unknown_fields = tuple(
            metadata_field
            for metadata_field in MetadataField
            if isinstance(self.metadata.value_for(metadata_field), UnknownValue)
        )
        if unknown_fields != (self.unknown_field,):
            raise ValueError("incomplete metadata scenario must have exactly its declared unknown")

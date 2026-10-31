"""Framework-independent Resolve preview vocabulary.

This module is the single definition source for three things the rest of the
Resolve path derives from:

1. the ``ResolvedModel`` field set (``ResolvedModelField``),
2. the knowledge state a metadata field can be in (``MetadataKnowledgeState``),
3. the total mapping between that state and the nullable database column
   written during persistence (``KNOWLEDGE_STATE_PERSISTS_AS_NULL``).

An unidentified field is shown as "未识别" and never guessed. Persistence
keeps it ``null``. These representations agree about intent
but a bare JSON ``null`` cannot be told apart from an omitted field or from a
source that genuinely returned an empty value -- and that distinction is the
whole point. So every optionally-known field carries an explicit discriminant
(``Identified`` with a value, or ``Unrecognized``) and the NULL column stays a
persistence detail reached only through the mapping below.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from holm_domain.model_acquisition import ModelOriginType


class MetadataKnowledgeState(StrEnum):
    """Whether one metadata field was confirmed by the source or not."""

    IDENTIFIED = "identified"
    UNRECOGNIZED = "unrecognized"


@dataclass(frozen=True, slots=True)
class Identified[T]:
    """A metadata field the source confirmed, carrying that confirmed value."""

    value: T
    state: MetadataKnowledgeState = field(
        default=MetadataKnowledgeState.IDENTIFIED,
        init=False,
    )


@dataclass(frozen=True, slots=True)
class Unrecognized:
    """A metadata field explicitly reported as not identified.

    It deliberately has no ``value`` attribute at all: there is no place to put
    ``""``, ``0``, ``False`` or a source default even by accident.
    """

    state: MetadataKnowledgeState = field(
        default=MetadataKnowledgeState.UNRECOGNIZED,
        init=False,
    )


type MetadataKnowledge[T] = Identified[T] | Unrecognized


KNOWLEDGE_STATE_PERSISTS_AS_NULL: Final[Mapping[MetadataKnowledgeState, bool]] = MappingProxyType(
    {
        MetadataKnowledgeState.IDENTIFIED: False,
        MetadataKnowledgeState.UNRECOGNIZED: True,
    }
)
"""DTO state -> persistence NULL.  Total by construction: a state without an
entry raises ``KeyError`` in both directions below instead of falling into a
default branch.  Model persistence consumes this when it upserts ``core.models``."""


class KnowledgeMappingError(ValueError):
    """A knowledge value and its declared persistence shape disagree."""


def to_persisted_column[T](knowledge: MetadataKnowledge[T]) -> T | None:
    """Project one knowledge state onto the nullable persistence column."""

    persists_as_null = KNOWLEDGE_STATE_PERSISTS_AS_NULL[knowledge.state]
    if isinstance(knowledge, Identified):
        if persists_as_null:
            raise KnowledgeMappingError("an identified field cannot persist as NULL")
        return knowledge.value
    if not persists_as_null:
        raise KnowledgeMappingError("an unrecognized field can only persist as NULL")
    return None


def from_persisted_column[T](value: T | None) -> MetadataKnowledge[T]:
    """Read one nullable column back as an explicit knowledge state."""

    if value is None:
        return Unrecognized()
    return Identified(value)


class LocalAvailability(StrEnum):
    """Whether the same version exists locally, as three distinguishable answers.

    ``ABSENT`` is a conclusion the platform reached, not an omitted field: the
    wizard must be able to tell "no local copy" from "nobody looked".
    """

    ARTIFACT_READY = "artifact_ready"
    DOWNLOAD_ACTIVE = "download_active"
    ABSENT = "absent"


_HEX_DIGITS: Final[frozenset[str]] = frozenset("0123456789abcdef")


def _is_sha256(value: str) -> bool:
    """Whether a string is a lowercase hexadecimal SHA-256 digest."""

    return len(value) == 64 and all(character in _HEX_DIGITS for character in value)


class RevisionImmutability(StrEnum):
    """Whether a resolved Revision is an immutable reference.

    Immutability is a property of the *source*, not something the platform may
    assume.  Hugging Face resolves a Branch/Tag to a repository-level Commit;
    ModelScope has none -- its ``/revisions`` lists only branches and tags, its
    metadata ``Revision`` field is a branch name, and its file tree carries one
    last-commit sha *per file*.  So the answer is stated on every preview
    instead of inferred from the shape of the string, and a platform-minted
    "pseudo Commit" is never synthesized: such an identifier cannot be handed
    back to the source to get the same tree, so it looks like a reference
    without being usable as one.  For a mutable reference, reproducibility is
    carried by the file listing snapshot plus each file's content hash.
    """

    IMMUTABLE = "immutable"
    MUTABLE_REFERENCE = "mutable_reference"


@dataclass(frozen=True, slots=True)
class RemoteFile:
    """One entry of the remote file listing.

    ``content_sha256`` is the per-file content hash. It is a knowledge
    state rather than an optional string so that "the source did not state one"
    can never be read as an empty hash, and it defaults to ``Unrecognized`` for
    the one source that genuinely reports none: the Hugging Face adapter reads the source's
    dry-run plan entries, which carry a filename and a size and nothing else.
    """

    path: str
    size_bytes: int
    content_sha256: MetadataKnowledge[str] = Unrecognized()

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError("remote file path must be non-empty")
        if self.size_bytes < 0:
            raise ValueError("remote file size must be non-negative")
        if isinstance(self.content_sha256, Identified) and not _is_sha256(
            self.content_sha256.value
        ):
            # An identified hash that is not a hash would be worse than an
            # absent one: it reads as verifiable evidence and is not.
            raise ValueError("an identified content hash must be a SHA-256 digest")


class ResolvedModelField(StrEnum):
    """The complete ``ResolvedModel`` field set.

    ``requested_revision_norm`` is the normalized request key, carried so that the
    preview and the later pre-resolution de-duplication agree on one spelling.
    """

    SOURCE = "source"
    REPOSITORY_ID = "repository_id"
    REQUESTED_REVISION_NORM = "requested_revision_norm"
    RESOLVED_REVISION = "resolved_revision"
    REVISION_IMMUTABILITY = "revision_immutability"
    FILES = "files"
    TOTAL_SIZE_BYTES = "total_size_bytes"
    ARCHITECTURE = "architecture"
    MODALITIES = "modalities"
    PRECISION = "precision"
    QUANTIZATION = "quantization"
    CONTEXT_LENGTH = "context_length"
    CANDIDATE_RUNTIMES = "candidate_runtimes"
    LICENSE = "license"


KNOWLEDGE_BEARING_FIELDS: Final[frozenset[ResolvedModelField]] = frozenset(
    {
        ResolvedModelField.FILES,
        ResolvedModelField.TOTAL_SIZE_BYTES,
        ResolvedModelField.ARCHITECTURE,
        ResolvedModelField.MODALITIES,
        ResolvedModelField.PRECISION,
        ResolvedModelField.QUANTIZATION,
        ResolvedModelField.CONTEXT_LENGTH,
        ResolvedModelField.CANDIDATE_RUNTIMES,
        ResolvedModelField.LICENSE,
    }
)
"""Fields that may be unrecognized.  The complement is what a source has to
have confirmed before a preview can exist at all (see ``ResolvedModel``)."""

SOURCE_CONFIRMED_FIELDS: Final[frozenset[ResolvedModelField]] = (
    frozenset(ResolvedModelField) - KNOWLEDGE_BEARING_FIELDS
)


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    """One source-confirmed preview. There is no empty or default instance.

    Every field is required and the source-confirmed ones are validated, so
    "no adapter was available" cannot be expressed as a ``ResolvedModel``: a
    source Revision is not something the platform can invent. That is what
    makes the success branch unreachable without a real source answer rather
    than something a caller has to remember to check.

    ``revision_immutability`` has no default for the same reason. A default
    would let an Adapter stay silent and let the caller infer the answer from
    the shape of ``resolved_revision``, which is exactly the inference this explicit field
    forbids -- ModelScope's branch names and Hugging Face's Commits are
    both opaque strings.
    """

    source: ModelOriginType
    repository_id: str
    requested_revision_norm: str
    resolved_revision: str
    revision_immutability: RevisionImmutability
    files: MetadataKnowledge[tuple[RemoteFile, ...]]
    total_size_bytes: MetadataKnowledge[int]
    architecture: MetadataKnowledge[str]
    modalities: MetadataKnowledge[tuple[str, ...]]
    precision: MetadataKnowledge[str]
    quantization: MetadataKnowledge[str]
    context_length: MetadataKnowledge[int]
    candidate_runtimes: MetadataKnowledge[tuple[str, ...]]
    license: MetadataKnowledge[str]

    def __post_init__(self) -> None:
        if not self.repository_id:
            raise ValueError("a resolved preview requires a repository identity")
        for name, value in (
            ("requested_revision_norm", self.requested_revision_norm),
            ("resolved_revision", self.resolved_revision),
        ):
            if not value or any(character.isspace() for character in value):
                raise ValueError(f"{name} must be a non-empty reference without whitespace")
        if self.resolved_revision.startswith("@"):
            # "@" prefixes the reserved request sentinels (such as
            # "@default"); a source Revision is never one of them, whether the
            # source's references are immutable or not.
            raise ValueError("resolved_revision must be a source Revision")
        if not isinstance(self.revision_immutability, RevisionImmutability):
            raise ValueError("revision_immutability must be stated, not inferred")
        if isinstance(self.total_size_bytes, Identified) and self.total_size_bytes.value < 0:
            raise ValueError("an identified total size must be non-negative")
        if isinstance(self.context_length, Identified) and self.context_length.value <= 0:
            raise ValueError("an identified Context length must be positive")


def _assert_single_definition_source() -> None:
    """Fail at import if the field set drifts from its declared vocabulary."""

    declared = frozenset(ResolvedModelField)
    attributes = frozenset(item.name for item in fields(ResolvedModel))
    if attributes != frozenset(item.value for item in declared):
        raise RuntimeError("ResolvedModel fields drifted from ResolvedModelField")
    if not declared >= KNOWLEDGE_BEARING_FIELDS:
        raise RuntimeError("KNOWLEDGE_BEARING_FIELDS names an undeclared field")
    if frozenset(KNOWLEDGE_STATE_PERSISTS_AS_NULL) != frozenset(MetadataKnowledgeState):
        raise RuntimeError("the knowledge state -> NULL mapping is not total")


_assert_single_definition_source()


__all__ = [
    "KNOWLEDGE_BEARING_FIELDS",
    "KNOWLEDGE_STATE_PERSISTS_AS_NULL",
    "SOURCE_CONFIRMED_FIELDS",
    "Identified",
    "KnowledgeMappingError",
    "LocalAvailability",
    "MetadataKnowledge",
    "MetadataKnowledgeState",
    "RemoteFile",
    "ResolvedModel",
    "ResolvedModelField",
    "RevisionImmutability",
    "Unrecognized",
    "from_persisted_column",
    "to_persisted_column",
]

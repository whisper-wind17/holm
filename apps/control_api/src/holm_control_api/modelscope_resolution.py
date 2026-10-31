"""The ModelScope Resolve-preview Adapter.

An empty ``ModelScope`` entry in ``ModelResolutionRegistry`` means the
source could only answer ``validator_unavailable``.  This module fills it in
with a Hub API read -- "使用 Hub API 解析 Revision 和文件清单".
The download half (``snapshot_download``, filter rules, progress callbacks) is
owned by the download worker. No part of it is started here, so this Adapter
needs no
``modelscope`` SDK and adds no dependency.

It deliberately does **not** share an abstraction with the Hugging Face
Adapter.  The two sources disagree about three things the source
recordings settle, and a shared "generic Hub" would have to hide those
differences inside branches:

* **There is no repository-level immutable Commit.**  ``/revisions`` lists
  branches and tags only, the metadata ``Revision`` field *is* a branch name
  (``"master"``), and the file tree carries one last-commit sha per file (15
  files, 7 distinct values).  The resolved Revision is therefore the branch
  name, marked ``MUTABLE_REFERENCE``.  No repository-level identifier is
  synthesized from the per-file shas: such a string cannot be handed back to
  ModelScope to get the same tree, so it would look like a reference without
  being usable as one.  Reproducibility is carried by the file listing snapshot
  plus each file's ``Sha256``.
* **A non-existent Revision answers ``200`` with ``Files: null``.**  It is a
  failure, and the only signal the source gives.  See
  :func:`~holm_control_api.modelscope_source.parse_file_listing`.
* **"Not found" is observable without a credential.**  ModelScope answers 404
  with ``Code: 10010205001`` to an anonymous read, so this Adapter reaches a
  definite ``not_found`` where the Hugging Face Adapter can only reach "missing
  or invisible, and the platform cannot tell".  That asymmetry is the source's
  property, not a platform choice, and it is not copied in either direction.

Judgements are drawn from the numeric ``Code`` together with the HTTP status.
The source's Chinese ``Message`` is never read: the reply type this module
consumes has no field for it (the credential privacy boundary).  A
``Code`` no recording contains reaches an explicit unmapped conclusion rather
than a catch-all.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from typing import Any, Final, assert_never

from holm_domain import (
    Identified,
    MetadataKnowledge,
    ModelOriginType,
    ModelSourceValidationAction,
    ModelSourceValidationReason,
    ModelSourceValidationStatus,
    RemoteFile,
    ResolvedModel,
    RevisionImmutability,
    Unrecognized,
)
from holm_persistence import DEFAULT_REQUESTED_REVISION

from holm_control_api.model_resolution import (
    DEFAULT_RESOLUTION_TIMEOUT_SECONDS,
    ResolutionFailed,
    ResolutionOutcome,
    ResolutionRequest,
    ResolutionSucceeded,
)
from holm_control_api.model_source_validation import (
    ModelSourceValidationResult,
    suppress_hugging_face_http_debug_logs,
)
from holm_control_api.modelscope_source import (
    CONFIG_FILENAME,
    MODELSCOPE_OFFICIAL_ENDPOINT,
    FileListingAbsent,
    FileListingUnreadable,
    ModelScopeCode,
    ModelScopeReply,
    fetch_reply,
    fetch_repository_document,
    model_path,
    parse_file_listing,
    repository_files_path,
)

BLOCKING_WORKER_LIMIT: Final[int] = 4

_METADATA_REVISION_KEY: Final[str] = "Revision"
_ARCHITECTURES_KEY: Final[str] = "Architectures"
_LICENSE_KEY: Final[str] = "License"
_CONTEXT_LENGTH_KEY: Final[str] = "max_position_embeddings"
_RECURSIVE_LISTING: Final[Mapping[str, str]] = {"Recursive": "True"}


@dataclass(frozen=True, slots=True, repr=False)
class _SourceReading:
    """Everything one source round trip produced, before it becomes a preview."""

    branch: str
    metadata: Mapping[str, Any]
    files: tuple[RemoteFile, ...]
    configuration: Mapping[str, Any] | None

    def __repr__(self) -> str:
        return f"_SourceReading(branch={self.branch!r}, files={len(self.files)})"


@dataclass(frozen=True, slots=True)
class RepositoryNotFound:
    """A 404 with ``Code: 10010205001``, reachable with or without a credential."""


@dataclass(frozen=True, slots=True)
class RevisionNotFound:
    """A 200 whose ``Files`` is ``null``: the reference does not exist."""


@dataclass(frozen=True, slots=True)
class DefaultBranchNotStated:
    """The metadata answered but named no branch, so nothing can be resolved."""


@dataclass(frozen=True, slots=True)
class UnreadableFileListing:
    """A listing that is present but not in the recorded shape."""


@dataclass(frozen=True, slots=True)
class UnmappedSourceAnswer:
    """A status/``Code`` pair no source recording gives a meaning to.

    It exists so that an unrecognized answer is a stated conclusion rather than
    something a catch-all branch absorbs, and it carries neither the code nor
    the body: an operator-facing conclusion must not vary with a number this
    platform has decided it cannot interpret.
    """


type SourceFailure = (
    RepositoryNotFound
    | RevisionNotFound
    | DefaultBranchNotStated
    | UnreadableFileListing
    | UnmappedSourceAnswer
)
type _SourceReadOutcome = _SourceReading | SourceFailure


_NOT_FOUND_RESULT: Final[ModelSourceValidationResult] = ModelSourceValidationResult(
    status=ModelSourceValidationStatus.NOT_FOUND,
    reason_code=ModelSourceValidationReason.SOURCE_NOT_FOUND,
    next_action=ModelSourceValidationAction.CHECK_SOURCE_REFERENCE,
)
_UNRESOLVABLE_RESULT: Final[ModelSourceValidationResult] = ModelSourceValidationResult(
    status=None,
    reason_code=ModelSourceValidationReason.VALIDATION_FAILED,
    next_action=ModelSourceValidationAction.RETRY_LATER,
)
"""The conclusion for every answer this platform declines to interpret.

The shared vocabulary has no finer member, and minting one would create a second,
resolve-only error vocabulary.  What keeps
"unmapped" from being a catch-all is not the rendered pair but the branch: each
failure below is its own type and its own line in
:func:`render_source_failure`, so an answer nobody classified cannot arrive
here by falling through."""


class ModelScopeResolutionAdapter:
    """One bounded, side-effect-free metadata read against the ModelScope Hub."""

    __slots__ = ("_endpoint", "_executor", "_timeout_seconds")

    def __init__(
        self,
        *,
        endpoint: str = MODELSCOPE_OFFICIAL_ENDPOINT,
        executor: ThreadPoolExecutor | None = None,
        timeout_seconds: float = DEFAULT_RESOLUTION_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("resolution timeout must be positive")
        # Configures the shared httpx/httpcore loggers, which this source rides
        # on too; nothing about it is Hugging Face specific.  Without it a debug
        # handler would record the source's URLs and bodies.
        suppress_hugging_face_http_debug_logs()
        self._endpoint = endpoint
        self._executor = executor or ThreadPoolExecutor(
            max_workers=BLOCKING_WORKER_LIMIT,
            thread_name_prefix="ms-resolve",
        )
        self._timeout_seconds = timeout_seconds

    @property
    def endpoint(self) -> str:
        return self._endpoint

    async def resolve(
        self,
        request: ResolutionRequest,
        *,
        credential: bytes | None,
    ) -> ResolutionOutcome:
        """Run the blocking HTTP reads off the event loop and answer with a preview."""

        loop = asyncio.get_running_loop()
        outcome = await loop.run_in_executor(
            self._executor,
            partial(self._resolve_blocking, request, credential),
        )
        if isinstance(outcome, ResolutionFailed):
            return outcome
        return ResolutionSucceeded(outcome)

    # ----------------------------------------------------------- blocking half
    def _resolve_blocking(
        self,
        request: ResolutionRequest,
        credential: bytes | None,
    ) -> ResolvedModel | ResolutionFailed:
        reading = self._read_source(request, credential)
        if not isinstance(reading, _SourceReading):
            return render_source_failure(reading)
        files = reading.files
        return ResolvedModel(
            source=ModelOriginType.MODELSCOPE,
            repository_id=request.repository_id,
            requested_revision_norm=request.requested_revision_norm,
            # The branch name the source itself named, never the "@default"
            # request sentinel and never a synthesized repository-level sha.
            resolved_revision=reading.branch,
            revision_immutability=RevisionImmutability.MUTABLE_REFERENCE,
            files=Identified(files),
            total_size_bytes=Identified(sum(item.size_bytes for item in files)),
            architecture=_architecture(reading.metadata),
            # No ModelScope field states a modality; ``Tasks`` is a task list,
            # and the metadata contract forbids guessing a field the source did not
            # confirm.
            modalities=Unrecognized(),
            # The metadata's own ``Precision`` field is null in the recordings.
            # ``config.json``'s ``torch_dtype`` exists, but the Hugging Face
            # Adapter reports the safetensors dtype for the same checkpoint and
            # the two spell it differently, so naming a precision here would
            # make one model show two precisions depending on which source
            # previewed it.
            precision=Unrecognized(),
            quantization=Unrecognized(),
            context_length=_context_length(reading.configuration),
            # Candidate Runtimes come from the Runtime compatibility
            # configuration, and it declares none yet.
            candidate_runtimes=Unrecognized(),
            license=_license(reading.metadata),
        )

    def _read_source(
        self,
        request: ResolutionRequest,
        credential: bytes | None,
    ) -> _SourceReadOutcome:
        """Return only the facts the recorded status and ``Code`` distinguish."""

        metadata_reply = self._get(model_path(request.repository_id), None, credential)
        metadata = classify_reply(metadata_reply)
        if not isinstance(metadata, Mapping):
            return metadata
        branch = _requested_branch(request.requested_revision_norm, metadata)
        if not isinstance(branch, str):
            return branch
        listing_reply = self._get(
            repository_files_path(request.repository_id),
            {"Revision": branch, **_RECURSIVE_LISTING},
            credential,
        )
        listing_data = classify_reply(listing_reply)
        if not isinstance(listing_data, Mapping):
            return listing_data
        listing = parse_file_listing(listing_data)
        if isinstance(listing, FileListingAbsent):
            # 200 / Code 200 / Success true / Files null.  The Revision does not
            # exist; the request must not become "resolved with no files".
            return RevisionNotFound()
        if isinstance(listing, FileListingUnreadable):
            return UnreadableFileListing()
        configuration = fetch_repository_document(
            endpoint=self._endpoint,
            repository_id=request.repository_id,
            revision=branch,
            filename=CONFIG_FILENAME,
            credential=credential,
            timeout_seconds=self._timeout_seconds,
        )
        return _SourceReading(
            branch=branch,
            metadata=metadata,
            files=listing,
            configuration=configuration,
        )

    def _get(
        self,
        path: str,
        params: Mapping[str, str] | None,
        credential: bytes | None,
    ) -> ModelScopeReply:
        return fetch_reply(
            endpoint=self._endpoint,
            path=path,
            params=params,
            credential=credential,
            timeout_seconds=self._timeout_seconds,
        )


def classify_reply(reply: ModelScopeReply) -> Mapping[str, Any] | SourceFailure:
    """Classify one answer from the HTTP status and the numeric ``Code``.

    The two are read together on purpose: ``Code: 200`` appears inside a 200
    response that carries no tree, so neither number alone decides anything.
    """

    if reply.http_status == 200 and reply.code == ModelScopeCode.SUCCESS:
        if reply.data is None:
            return UnmappedSourceAnswer()
        return reply.data
    if reply.http_status == 404 and reply.code == ModelScopeCode.RECORD_NOT_FOUND:
        return RepositoryNotFound()
    # Every other pair -- including a ``Code`` the recordings never carried and
    # ``Code: 10010101003`` on a read path, which must never become "credential
    # invalid" (authentication and transport failure must remain distinct) -- is stated as unmapped.
    return UnmappedSourceAnswer()


def _requested_branch(
    requested_revision_norm: str,
    metadata: Mapping[str, Any],
) -> str | SourceFailure:
    """Resolve which reference to list, without ever leaking the sentinel.

    "未指定" normalizes to ``@default`` and the source's own
    metadata names the default branch.  Sending the sentinel to ModelScope
    would silently work -- an empty or unknown ``Revision`` falls back to the
    default branch there -- and the resolved Revision would then be a platform
    sentinel rather than the branch that was actually read.
    """

    if requested_revision_norm != DEFAULT_REQUESTED_REVISION:
        return requested_revision_norm
    branch = metadata.get(_METADATA_REVISION_KEY)
    if not isinstance(branch, str) or not branch or branch.startswith("@"):
        return DefaultBranchNotStated()
    return branch


def render_source_failure(failure: SourceFailure) -> ResolutionFailed:
    """Map the closed failure union onto the shared sanitized vocabulary."""

    if isinstance(failure, RepositoryNotFound):
        return ResolutionFailed(_NOT_FOUND_RESULT)
    if isinstance(failure, RevisionNotFound):
        # The reference the operator asked for is not on the source: the same
        # direction as a missing repository ("check the reference"), and never
        # a preview.
        return ResolutionFailed(_NOT_FOUND_RESULT)
    if isinstance(failure, DefaultBranchNotStated):
        return ResolutionFailed(_UNRESOLVABLE_RESULT)
    if isinstance(failure, UnreadableFileListing):
        return ResolutionFailed(_UNRESOLVABLE_RESULT)
    if isinstance(failure, UnmappedSourceAnswer):
        return ResolutionFailed(_UNRESOLVABLE_RESULT)
    assert_never(failure)


def _architecture(metadata: Mapping[str, Any]) -> MetadataKnowledge[str]:
    architectures = metadata.get(_ARCHITECTURES_KEY)
    if isinstance(architectures, list) and len(architectures) == 1:
        first = architectures[0]
        if isinstance(first, str) and first:
            return Identified(first)
    return Unrecognized()


def _license(metadata: Mapping[str, Any]) -> MetadataKnowledge[str]:
    value = metadata.get(_LICENSE_KEY)
    if isinstance(value, str) and value:
        return Identified(value)
    return Unrecognized()


def _context_length(configuration: Mapping[str, Any] | None) -> MetadataKnowledge[int]:
    if configuration is None:
        return Unrecognized()
    value = configuration.get(_CONTEXT_LENGTH_KEY)
    if _CONTEXT_LENGTH_KEY not in configuration:
        text_config = configuration.get("text_config")
        if isinstance(text_config, dict):
            value = text_config.get(_CONTEXT_LENGTH_KEY)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return Identified(value)
    return Unrecognized()


__all__ = [
    "BLOCKING_WORKER_LIMIT",
    "MODELSCOPE_OFFICIAL_ENDPOINT",
    "DefaultBranchNotStated",
    "ModelScopeResolutionAdapter",
    "RepositoryNotFound",
    "RevisionNotFound",
    "SourceFailure",
    "UnmappedSourceAnswer",
    "UnreadableFileListing",
    "classify_reply",
    "render_source_failure",
]

"""Resolve-preview orchestration: normalize, dispatch, and read-only lookup.

This service owns the four responsibilities behind the "读取信息" button and
nothing else:

* it normalizes the request through the functions -- the only definition
  source for a ``source_key`` and a requested Revision;
* it rejects an unusable Repository ID before any source is contacted;
* it dispatches one bounded attempt to the Adapter registry, which answers
  with an explicit non-answer when no implementation exists;
* it answers "本地是否已有相同版本" with a read-only query.

Nothing on any path writes: ``core.models``, ``core.model_artifacts``,
``core.download_tasks`` and ``core.download_subscriptions`` are untouched by a
preview.  There is deliberately
no "cache the preview" row: a query history is not a model catalog.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

import sqlalchemy as sa
from holm_domain import (
    CredentialType,
    DownloadTaskStatus,
    LocalAvailability,
    ModelArtifactStatus,
    ModelOriginType,
    ModelSourceType,
    ModelSourceValidationAction,
    ModelSourceValidationReason,
    ModelSourceValidationStatus,
)
from holm_persistence import (
    DEFAULT_REQUESTED_REVISION,
    DOWNLOAD_TASKS,
    MODEL_ARTIFACTS,
    AsyncSessionFactory,
    ModelSourceRepository,
    normalize_requested_revision,
    normalize_source_key,
)
from holm_security import CredentialDecryptionError, MasterKeyring
from sqlalchemy.ext.asyncio import AsyncSession

from holm_control_api.model_resolution import (
    ExternalPathAccepted,
    ExternalPathRequest,
    ModelResolutionRegistry,
    ResolutionFailed,
    ResolutionRequest,
    ResolutionSucceeded,
)
from holm_control_api.model_source_validation import ModelSourceValidationResult

_CREDENTIAL_TYPE: Final[str] = CredentialType.ACCESS_TOKEN.value

CREDENTIAL_SOURCES: Final[frozenset[ModelOriginType]] = frozenset(
    {ModelOriginType.HUGGING_FACE, ModelOriginType.MODELSCOPE}
)
"""Sources whose preview reads a Repository ID and may use a credential."""

EXTERNAL_PATH_SOURCES: Final[frozenset[ModelOriginType]] = frozenset({ModelOriginType.LOCAL_PATH})
"""Sources whose preview validates a server path instead of a Repository ID.

The partition decides which *request shape* the service builds, and that is all
it decides.  It is not a short circuit: a Local Path request is
dispatched to the registry like any other source, so an unregistered Local Path
still answers ``validator_unavailable`` and a registered one is genuinely
consulted."""

if frozenset(ModelOriginType) != CREDENTIAL_SOURCES | EXTERNAL_PATH_SOURCES or (
    CREDENTIAL_SOURCES & EXTERNAL_PATH_SOURCES
):  # pragma: no cover - import-time invariant
    raise RuntimeError("every ModelOriginType must belong to exactly one preview shape")

ACTIVE_DOWNLOAD_STATUSES: Final[tuple[str, ...]] = tuple(
    status.value
    for status in (
        DownloadTaskStatus.QUEUED,
        DownloadTaskStatus.RESOLVING,
        DownloadTaskStatus.DOWNLOADING,
        DownloadTaskStatus.PAUSED,
        DownloadTaskStatus.VERIFYING,
        DownloadTaskStatus.PUBLISHING,
    )
)

# Two non-empty segments of the conservative character set both official hubs
# accept.  A segment cannot start with a separator, so "a//b", "../b" and an
# absolute path are all rejected without inspecting the raw input again.
_REPOSITORY_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}"
)
_REVISION_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}")

_FORBIDDEN_PATH_CHARACTERS: Final[tuple[str, ...]] = ("\x00", "\n", "\r")
"""A NUL truncates the path at the C boundary; a newline forges a log record."""

_UNUSABLE_CREDENTIAL_RESULT: Final[ModelSourceValidationResult] = ModelSourceValidationResult(
    status=ModelSourceValidationStatus.INVALID_CREDENTIAL,
    reason_code=ModelSourceValidationReason.CREDENTIAL_INVALID,
    next_action=ModelSourceValidationAction.UPDATE_CREDENTIAL,
)


class ResolveInputRejected(ValueError):
    """The request cannot identify a model, so no source is contacted.

    The message is a fixed string.  It never carries the rejected value: an
    invalid Repository ID or path is attacker-influenced input and echoing a
    fragment of it back is how a path or an internal detail leaks out.
    """

    def __init__(self) -> None:
        super().__init__("resolve input rejected")


class ModelLayoutUnsupported(ResolveInputRejected):
    """A post-resolution rejection, handled safely for registration and download creation."""

    def __init__(self) -> None:
        ValueError.__init__(self, "model layout unsupported")


@dataclass(frozen=True, slots=True)
class ResolutionPreview:
    """The endpoint-facing conclusion: a confirmed preview, or an explicit no."""

    outcome: ResolutionSucceeded | ExternalPathAccepted | ResolutionFailed
    local_availability: LocalAvailability | None

    def __post_init__(self) -> None:
        if isinstance(self.outcome, ResolutionSucceeded) != (self.local_availability is not None):
            raise ValueError("only a confirmed preview carries a local availability answer")


@dataclass(frozen=True, slots=True, repr=False)
class _CredentialLookup:
    """A single-request credential read.  ``repr`` never renders the bytes."""

    credential: bytes | None
    unusable: bool

    def __repr__(self) -> str:
        return f"_CredentialLookup(credential=<redacted>, unusable={self.unusable})"


class ModelResolutionService:
    """Own one preview request end to end without persisting anything."""

    __slots__ = ("_keyring", "_registry", "_session_factory")

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory,
        keyring: MasterKeyring,
        registry: ModelResolutionRegistry | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._keyring = keyring
        self._registry = ModelResolutionRegistry() if registry is None else registry

    async def resolve(
        self,
        *,
        source: ModelOriginType,
        repository_id: str | None,
        external_path: str | None,
        revision: str | None,
    ) -> ResolutionPreview:
        source = ModelOriginType(source)
        if source in EXTERNAL_PATH_SOURCES:
            if not external_path or repository_id is not None or revision is not None:
                raise ResolveInputRejected()
            _reject_unusable_external_path(external_path)
            outcome = await self._registry.resolve_external_path(
                ExternalPathRequest(source=source, untrusted_path=external_path)
            )
            # An External Path has no Revision to compare, so the
            # "本地是否已有相同版本" has no question to answer here; the
            # directory *is* the local copy.  Registration is a separate operation.
            return ResolutionPreview(outcome=outcome, local_availability=None)
        if not repository_id or external_path is not None:
            raise ResolveInputRejected()

        normalized_repository_id = normalize_source_key(repository_id)
        requested_revision_norm = normalize_requested_revision(revision)
        _reject_unusable_identity(normalized_repository_id, requested_revision_norm)

        request = ResolutionRequest(
            source=source,
            repository_id=normalized_repository_id,
            requested_revision_norm=requested_revision_norm,
        )
        lookup = await self._credential(source)
        if lookup.unusable:
            return ResolutionPreview(
                outcome=ResolutionFailed(_UNUSABLE_CREDENTIAL_RESULT),
                local_availability=None,
            )
        outcome = await self._registry.resolve(request, credential=lookup.credential)
        if not isinstance(outcome, ResolutionSucceeded):
            return ResolutionPreview(outcome=outcome, local_availability=None)
        availability = await self._local_availability(
            source=source,
            repository_id=outcome.resolved.repository_id,
            resolved_revision=outcome.resolved.resolved_revision,
            requested_revision_norm=requested_revision_norm,
        )
        return ResolutionPreview(outcome=outcome, local_availability=availability)

    async def _credential(self, source: ModelOriginType) -> _CredentialLookup:
        """Decrypt the stored token for one request only; never store or log it."""

        source_type = ModelSourceType(source.value)
        async with self._session_factory() as session:
            record = await ModelSourceRepository(session).get_by_type(source_type)
        if record is None or record.credential is None:
            return _CredentialLookup(credential=None, unusable=False)
        if not self._keyring.can_decrypt(record.credential.key_id):
            return _CredentialLookup(credential=None, unusable=True)
        try:
            plaintext = self._keyring.decrypt(
                record.credential,
                source_id=record.id,
                credential_type=_CREDENTIAL_TYPE,
            )
        except CredentialDecryptionError:
            return _CredentialLookup(credential=None, unusable=True)
        return _CredentialLookup(credential=plaintext, unusable=False)

    async def _local_availability(
        self,
        *,
        source: ModelOriginType,
        repository_id: str,
        resolved_revision: str,
        requested_revision_norm: str,
    ) -> LocalAvailability:
        """Answer the local-availability question with SELECTs only."""

        async with self._session_factory() as session:
            if await _has_ready_artifact(
                session,
                source=source,
                repository_id=repository_id,
                resolved_revision=resolved_revision,
            ):
                return LocalAvailability.ARTIFACT_READY
            if await _has_active_download(
                session,
                source=source,
                repository_id=repository_id,
                resolved_revision=resolved_revision,
                requested_revision_norm=requested_revision_norm,
            ):
                return LocalAvailability.DOWNLOAD_ACTIVE
        return LocalAvailability.ABSENT


def _reject_unusable_external_path(external_path: str) -> None:
    """Refuse a path that cannot name a server directory, before any I/O.

    Only the judgements that need no filesystem live here, mirroring how an
    unusable Repository ID is refused before any source is contacted.  A path
    is deliberately *not* normalized: unlike a Repository ID it is an operating
    system identity, and folding its spelling would change which entry it
    names.  ``..`` is likewise not refused by spelling -- the path check resolves it
    with ``realpath`` and then compares, which is strictly stronger than a
    textual rule.
    """

    if not PurePosixPath(external_path).is_absolute():
        raise ResolveInputRejected()
    if any(character in external_path for character in _FORBIDDEN_PATH_CHARACTERS):
        raise ResolveInputRejected()
    if not external_path.isprintable():
        # A control character in a path is never a real directory name and is
        # how a terminal or a log line gets rewritten by the input.
        raise ResolveInputRejected()


def _reject_unusable_identity(repository_id: str, requested_revision_norm: str) -> None:
    if _REPOSITORY_ID_PATTERN.fullmatch(repository_id) is None:
        raise ResolveInputRejected()
    if requested_revision_norm == DEFAULT_REQUESTED_REVISION:
        return
    if _REVISION_PATTERN.fullmatch(requested_revision_norm) is None:
        raise ResolveInputRejected()
    if ".." in requested_revision_norm:
        raise ResolveInputRejected()


async def _has_ready_artifact(
    session: AsyncSession,
    *,
    source: ModelOriginType,
    repository_id: str,
    resolved_revision: str,
) -> bool:
    statement = (
        sa.select(MODEL_ARTIFACTS.c.id)
        .where(
            MODEL_ARTIFACTS.c.source_type == source.value,
            MODEL_ARTIFACTS.c.repository_id == repository_id,
            MODEL_ARTIFACTS.c.resolved_revision == resolved_revision,
            MODEL_ARTIFACTS.c.status == ModelArtifactStatus.READY.value,
        )
        .limit(1)
    )
    return (await session.execute(statement)).first() is not None


async def _has_active_download(
    session: AsyncSession,
    *,
    source: ModelOriginType,
    repository_id: str,
    resolved_revision: str,
    requested_revision_norm: str,
) -> bool:
    statement = (
        sa.select(DOWNLOAD_TASKS.c.id)
        .where(
            DOWNLOAD_TASKS.c.source_type == source.value,
            DOWNLOAD_TASKS.c.repository_id == repository_id,
            DOWNLOAD_TASKS.c.status.in_(ACTIVE_DOWNLOAD_STATUSES),
            sa.or_(
                DOWNLOAD_TASKS.c.resolved_revision == resolved_revision,
                sa.and_(
                    DOWNLOAD_TASKS.c.resolved_revision.is_(None),
                    DOWNLOAD_TASKS.c.requested_revision_norm == requested_revision_norm,
                ),
            ),
        )
        .limit(1)
    )
    return (await session.execute(statement)).first() is not None


__all__ = [
    "ACTIVE_DOWNLOAD_STATUSES",
    "CREDENTIAL_SOURCES",
    "EXTERNAL_PATH_SOURCES",
    "ModelResolutionService",
    "ResolutionPreview",
    "ResolveInputRejected",
]

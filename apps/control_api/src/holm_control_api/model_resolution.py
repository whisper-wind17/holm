"""Resolve-preview Adapter port and its fail-loud registry.

The point of this module is what it does when it finds nothing: it returns an
explicit "cannot resolve" conclusion drawn from the vocabulary, and it
has no way to express the opposite. An empty registry exercises this branch.

Two request shapes travel through the one registry to support
two genuinely different reads.  A *repository* source is asked to resolve an
identity and answers with a ``ResolvedModel``; that type has no empty or
default instance and validates an immutable Revision, so "no implementation ->
return a default -> the default happens to be blank metadata" cannot be
written here.  An *External Path* source is asked about a server path, where
there is no Revision to resolve and therefore no ``ResolvedModel`` to build.
Its strongest possible answer is ``ExternalPathAccepted``, which states what
the control plane actually verified and carries the Runtime-side question as an
explicit *unverified* state rather than as silence.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Literal, Protocol

from holm_domain import (
    ModelOriginType,
    ModelSourceValidationAction,
    ModelSourceValidationReason,
    ModelSourceValidationStatus,
    ResolvedModel,
)
from holm_persistence import DEFAULT_REQUESTED_REVISION

from holm_control_api.model_source_validation import ModelSourceValidationResult

DEFAULT_RESOLUTION_TIMEOUT_SECONDS: Final[float] = 30.0


@dataclass(frozen=True, slots=True)
class ResolutionRequest:
    """The already-normalized identity an Adapter is asked to resolve.

    The credential is not a member: it travels as a separate argument so that
    it can never reach a ``repr`` or an exception rendering of this value
    (the credential privacy boundary).
    """

    source: ModelOriginType
    repository_id: str
    requested_revision_norm: str


@dataclass(frozen=True, slots=True, repr=False)
class ExternalPathRequest:
    """The untrusted server path an External Path Adapter is asked to judge.

    ``repr`` is suppressed on purpose.  The value is attacker-influenced input
    and a dataclass ``repr`` would render it into any diagnostic that formats
    this object -- a forbidden disclosure of the directory structure.
    """

    source: ModelOriginType
    untrusted_path: str

    def __repr__(self) -> str:
        return f"ExternalPathRequest(source={self.source.value!r}, untrusted_path=<redacted>)"


class RuntimeReadability(StrEnum):
    """Whether the Runtime's own UID can read an External Root.

    ``UNVERIFIED`` is the only member, and that is the honest shape of this
    response rather than a placeholder. Runtime-side path validation requires
    the Host Agent; the read-only calls ``GetHostInfo``, ``ListGPUs`` and
    ``StreamLogs`` cannot validate a path. A second member requires a wired
    validation call. Until then the state is stated, never omitted and never
    folded into the accepted conclusion.
    """

    UNVERIFIED = "unverified"


class RuntimeReadabilityUnverifiedCause(StrEnum):
    """Why the Runtime-side answer is absent.  Not an error, a known gap."""

    RUNTIME_IDENTITY_CHECK_UNAVAILABLE = "runtime_identity_check_unavailable"


@dataclass(frozen=True, slots=True)
class ResolutionSucceeded:
    """A preview the source itself confirmed."""

    resolved: ResolvedModel


@dataclass(frozen=True, slots=True)
class ExternalPathAccepted:
    """The strongest conclusion the control plane can reach about a path.

    It deliberately carries **no path at all** -- not the input, not its
    ``realpath`` and not the allowed root that matched.  A caller learns that
    the path it submitted is usable, and nothing about the server's directory
    structure.

    ``control_plane_readable`` is stated rather than implied because the
    conclusion answers only half of the local-availability question: the process that ran the
    check could read the directory.  The other half travels beside it as
    ``runtime_readability``.
    """

    control_plane_readable: Literal[True]
    runtime_readability: RuntimeReadability
    runtime_readability_cause: RuntimeReadabilityUnverifiedCause

    def __post_init__(self) -> None:
        if self.control_plane_readable is not True:
            # An accepted conclusion that the control plane could *not* read is
            # not a conclusion; that path is a rejection.
            raise ValueError("an accepted External Path was readable by the control plane")


@dataclass(frozen=True, slots=True)
class ResolutionFailed:
    """An explicit non-answer, in the shared failure vocabulary.

    A non-answer always carries an operator direction and can never be the
    ``accessible`` status, so "unresolved" and "resolved" cannot converge on
    the same rendering by accident.
    """

    result: ModelSourceValidationResult

    def __post_init__(self) -> None:
        if self.result.next_action is None:
            raise ValueError("an unresolved conclusion must carry a next action")
        if self.result.status is ModelSourceValidationStatus.ACCESSIBLE:
            raise ValueError("an accessible source cannot be an unresolved conclusion")


type ResolutionOutcome = ResolutionSucceeded | ExternalPathAccepted | ResolutionFailed


class ModelResolutionAdapter(Protocol):
    """A repository source Adapter's single-attempt metadata read."""

    async def resolve(
        self,
        request: ResolutionRequest,
        *,
        credential: bytes | None,
    ) -> ResolutionOutcome: ...


class ExternalPathAdapter(Protocol):
    """An External Path source's single judgement of one server path.

    It takes no credential: Local Path resolution reads the local
    filesystem and contacts no source, so there is nothing to authenticate to
    and no place for a token to travel.
    """

    async def evaluate(self, request: ExternalPathRequest) -> ResolutionOutcome: ...


_UNAVAILABLE_RESULT: Final[ModelSourceValidationResult] = ModelSourceValidationResult(
    status=None,
    reason_code=ModelSourceValidationReason.VALIDATOR_UNAVAILABLE,
    next_action=ModelSourceValidationAction.INSTALL_VALIDATOR,
)
_TIMEOUT_RESULT: Final[ModelSourceValidationResult] = ModelSourceValidationResult(
    status=None,
    reason_code=ModelSourceValidationReason.VALIDATION_TIMEOUT,
    next_action=ModelSourceValidationAction.RETRY_LATER,
)
_FAILED_RESULT: Final[ModelSourceValidationResult] = ModelSourceValidationResult(
    status=None,
    reason_code=ModelSourceValidationReason.VALIDATION_FAILED,
    next_action=ModelSourceValidationAction.RETRY_LATER,
)


class ModelResolutionRegistry:
    """Dispatch one bounded attempt; an absent Adapter is never a preview."""

    __slots__ = ("_adapters", "_external_path_adapters", "_timeout_seconds")

    def __init__(
        self,
        adapters: Mapping[ModelOriginType, ModelResolutionAdapter] | None = None,
        *,
        external_path_adapters: Mapping[ModelOriginType, ExternalPathAdapter] | None = None,
        timeout_seconds: float = DEFAULT_RESOLUTION_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("resolution timeout must be positive")
        supplied = {} if adapters is None else dict(adapters)
        supplied_paths = {} if external_path_adapters is None else dict(external_path_adapters)
        if any(not isinstance(source, ModelOriginType) for source in (*supplied, *supplied_paths)):
            raise TypeError("resolution registry keys must be ModelOriginType values")
        if frozenset(supplied) & frozenset(supplied_paths):
            # The two mappings answer different questions.  A source in both
            # would be reachable through whichever shape the service happens to
            # build, which is how "registered" stops meaning "consulted".
            raise ValueError("a source cannot have both a repository and an External Path Adapter")
        self._adapters = MappingProxyType(supplied)
        self._external_path_adapters = MappingProxyType(supplied_paths)
        self._timeout_seconds = timeout_seconds

    @property
    def registered_sources(self) -> frozenset[ModelOriginType]:
        """Exactly the sources that have an Adapter, of either shape.

        Exposed so a test can assert the registration set itself rather than
        infer it from behaviour.  Every member is genuinely consulted: the
        service dispatches a repository source through :meth:`resolve` and an
        External Path source through :meth:`resolve_external_path`, and since
        it short-circuits neither.
        """

        return frozenset(self._adapters) | frozenset(self._external_path_adapters)

    async def resolve(
        self,
        request: ResolutionRequest,
        *,
        credential: bytes | None,
    ) -> ResolutionOutcome:
        adapter = self._adapters.get(ModelOriginType(request.source))
        if adapter is None:
            # An unregistered source lands here.  The answer is an
            # explicit non-answer rather than an empty preview.
            return ResolutionFailed(_UNAVAILABLE_RESULT)
        try:
            async with asyncio.timeout(self._timeout_seconds):
                outcome = await adapter.resolve(request, credential=credential)
        except TimeoutError:
            return ResolutionFailed(_TIMEOUT_RESULT)
        except Exception:
            # The Adapter's exception text can embed the repository, the source
            # response or the credential; none of it crosses this boundary.
            return ResolutionFailed(_FAILED_RESULT)
        return _answer_matches_request(outcome, request)

    async def resolve_external_path(self, request: ExternalPathRequest) -> ResolutionOutcome:
        """Judge one server path, or say explicitly that nothing can judge it."""

        adapter = self._external_path_adapters.get(ModelOriginType(request.source))
        if adapter is None:
            return ResolutionFailed(_UNAVAILABLE_RESULT)
        try:
            async with asyncio.timeout(self._timeout_seconds):
                return await adapter.evaluate(request)
        except TimeoutError:
            return ResolutionFailed(_TIMEOUT_RESULT)
        except Exception:
            # An OS error renders the path it failed on.  It stops here.
            return ResolutionFailed(_FAILED_RESULT)


def _answer_matches_request(
    outcome: ResolutionOutcome,
    request: ResolutionRequest,
) -> ResolutionOutcome:
    """Refuse a "success" that does not answer the identity that was asked."""

    if not isinstance(outcome, ResolutionSucceeded):
        return outcome
    resolved = outcome.resolved
    if (
        resolved.source is not request.source
        or resolved.repository_id != request.repository_id
        or resolved.requested_revision_norm != request.requested_revision_norm
        or resolved.resolved_revision == DEFAULT_REQUESTED_REVISION
    ):
        return ResolutionFailed(_FAILED_RESULT)
    return outcome


__all__ = [
    "DEFAULT_RESOLUTION_TIMEOUT_SECONDS",
    "ExternalPathAccepted",
    "ExternalPathAdapter",
    "ExternalPathRequest",
    "ModelResolutionAdapter",
    "ModelResolutionRegistry",
    "ResolutionFailed",
    "ResolutionOutcome",
    "ResolutionRequest",
    "ResolutionSucceeded",
    "RuntimeReadability",
    "RuntimeReadabilityUnverifiedCause",
]

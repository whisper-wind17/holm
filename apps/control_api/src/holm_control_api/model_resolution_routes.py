"""``POST /api/v1/models/resolve``: the side-effect-free model preview.

The request body is a *closed* field set: source, one identity (Repository ID
or server path) and an optional Revision.  There is no Endpoint, Base URL,
mirror, proxy or ``allow_patterns``/``ignore_patterns`` field, and unknown
fields are rejected rather than dropped -- the design keeps filter rules in the
Runtime compatibility configuration and the API forbids arbitrary URLs or
custom Endpoints.  The absence is a property of the schema, not a runtime
filter that a later field could slip past.

The response is a discriminated conclusion.  A confirmed preview and an
explicit "cannot resolve" are different shapes, and every optionally-known
metadata field carries its own identified/unrecognized discriminant so that
"未识别" can never be read as an empty string, a zero or a
missing key.  The reply follows the precedent for
``PUT /api/v1/system/model-sources``: the operation produced a conclusion, so
it answers 200 and the conclusion itself carries the stable reason code and
the operator's next action.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Final, Literal, Self

from fastapi import APIRouter
from holm_domain import (
    Capability,
    ErrorCode,
    Identified,
    LocalAvailability,
    MetadataKnowledge,
    MetadataKnowledgeState,
    ModelOriginType,
    ModelSourceValidationAction,
    ModelSourceValidationReason,
    ModelSourceValidationStatus,
    RemoteFile,
    ResolvedModel,
    RevisionImmutability,
)
from holm_domain.model_layout import DeployableLayout, deployable_layout
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from holm_control_api.auth_routes import json_response
from holm_control_api.authorization import capability_route, current_request_id
from holm_control_api.model_resolution import (
    ExternalPathAccepted,
    ResolutionSucceeded,
    RuntimeReadability,
    RuntimeReadabilityUnverifiedCause,
)
from holm_control_api.model_resolution_service import (
    ModelLayoutUnsupported,
    ModelResolutionService,
    ResolutionPreview,
    ResolveInputRejected,
)

MODELS_RESOLVE_PATH: Final[str] = "/api/v1/models/resolve"

_MAX_REPOSITORY_ID_LENGTH: Final[int] = 200
_MAX_EXTERNAL_PATH_LENGTH: Final[int] = 4096
_MAX_REVISION_LENGTH: Final[int] = 256


class ResolveConclusion(StrEnum):
    """The response discriminant: which conclusion the platform reached.

    ``EXTERNAL_PATH_ACCEPTED`` is its own value rather than a ``resolved``
    with empty metadata.  A Local Path has no Revision and no source-confirmed
    metadata to report, so reusing ``resolved`` would require inventing a
    ``ResolvedModel`` and falsely reporting an unimplemented preview.
    """

    RESOLVED = "resolved"
    EXTERNAL_PATH_ACCEPTED = "external_path_accepted"
    UNRESOLVED = "unresolved"


class ResolveRequest(BaseModel):
    """Closed request Schema: no address-like field and no Glob field exists."""

    model_config = ConfigDict(extra="forbid")

    source: ModelOriginType
    repository_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=_MAX_REPOSITORY_ID_LENGTH,
    )
    external_path: str | None = Field(
        default=None,
        min_length=1,
        max_length=_MAX_EXTERNAL_PATH_LENGTH,
    )
    # An omitted Revision is "未指定" and normalizes to "@default".
    # An empty string is not an omission: it is malformed input and
    # ``min_length`` rejects it before any normalization runs.
    revision: str | None = Field(default=None, min_length=1, max_length=_MAX_REVISION_LENGTH)


class IdentifiedValue[T](BaseModel):
    """A metadata field the source confirmed, rendered with its value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal[MetadataKnowledgeState.IDENTIFIED] = MetadataKnowledgeState.IDENTIFIED
    value: T


class UnrecognizedValue(BaseModel):
    """A metadata field explicitly reported as "未识别".

    It has no ``value`` key at all, so the rendering cannot degrade into
    ``null``, ``""``, ``0``, ``false`` or a source default.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal[MetadataKnowledgeState.UNRECOGNIZED] = MetadataKnowledgeState.UNRECOGNIZED


class RemoteFileResponse(BaseModel):
    """One entry of the remote file listing.

    ``content_sha256`` is the per-file content hash and carries its own
    knowledge state: for a mutable-reference source it is what makes the
    preview reproducible at all, and for a source that states none the field
    says "unrecognized" rather than rendering an empty hash.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    size_bytes: int
    content_sha256: IdentifiedValue[str] | UnrecognizedValue

    @classmethod
    def from_domain(cls, value: RemoteFile) -> Self:
        return cls(
            path=value.path,
            size_bytes=value.size_bytes,
            content_sha256=_render(IdentifiedValue[str], value.content_sha256),
        )


def _render[T](
    model: type[IdentifiedValue[T]],
    knowledge: MetadataKnowledge[T],
) -> IdentifiedValue[T] | UnrecognizedValue:
    if isinstance(knowledge, Identified):
        return model(value=knowledge.value)
    return UnrecognizedValue()


def _render_files(
    knowledge: MetadataKnowledge[tuple[RemoteFile, ...]],
) -> IdentifiedValue[tuple[RemoteFileResponse, ...]] | UnrecognizedValue:
    if isinstance(knowledge, Identified):
        return IdentifiedValue[tuple[RemoteFileResponse, ...]](
            value=tuple(RemoteFileResponse.from_domain(item) for item in knowledge.value)
        )
    return UnrecognizedValue()


class ResolvedModelResponse(BaseModel):
    """Resolved model metadata, with a knowledge state on every optional field."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: ModelOriginType
    repository_id: str
    requested_revision_norm: str
    resolved_revision: str
    # Stated on every preview, never inferred from the shape of the string
    # above: a branch name and a Commit are both opaque here.
    revision_immutability: RevisionImmutability
    files: IdentifiedValue[tuple[RemoteFileResponse, ...]] | UnrecognizedValue
    total_size_bytes: IdentifiedValue[int] | UnrecognizedValue
    architecture: IdentifiedValue[str] | UnrecognizedValue
    modalities: IdentifiedValue[tuple[str, ...]] | UnrecognizedValue
    precision: IdentifiedValue[str] | UnrecognizedValue
    quantization: IdentifiedValue[str] | UnrecognizedValue
    context_length: IdentifiedValue[int] | UnrecognizedValue
    candidate_runtimes: IdentifiedValue[tuple[str, ...]] | UnrecognizedValue
    license: IdentifiedValue[str] | UnrecognizedValue

    @classmethod
    def from_domain(cls, resolved: ResolvedModel) -> Self:
        return cls(
            source=resolved.source,
            repository_id=resolved.repository_id,
            requested_revision_norm=resolved.requested_revision_norm,
            resolved_revision=resolved.resolved_revision,
            revision_immutability=resolved.revision_immutability,
            files=_render_files(resolved.files),
            total_size_bytes=_render(IdentifiedValue[int], resolved.total_size_bytes),
            architecture=_render(IdentifiedValue[str], resolved.architecture),
            modalities=_render(IdentifiedValue[tuple[str, ...]], resolved.modalities),
            precision=_render(IdentifiedValue[str], resolved.precision),
            quantization=_render(IdentifiedValue[str], resolved.quantization),
            context_length=_render(IdentifiedValue[int], resolved.context_length),
            candidate_runtimes=_render(
                IdentifiedValue[tuple[str, ...]],
                resolved.candidate_runtimes,
            ),
            license=_render(IdentifiedValue[str], resolved.license),
        )


class ResolvedPreviewResponse(BaseModel):
    """A preview the source confirmed, plus the local-copy answer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: Literal[ResolveConclusion.RESOLVED] = ResolveConclusion.RESOLVED
    resolved: ResolvedModelResponse
    local_availability: LocalAvailability
    deployable_layout: DeployableLayout


class ExternalPathAcceptedResponse(BaseModel):
    """A server path the control plane judged usable, and what it did not judge.

    The body carries no path: not the submitted value, not its ``realpath``
    and not the allowed root that matched.

    ``runtime_readability`` is always present. Runtime-side path validation
    requires the Host Agent and is unavailable through the read-only
    ``GetHostInfo``, ``ListGPUs`` and ``StreamLogs`` methods, so the field
    states "unverified" and names why.  A client that renders this
    as "readable" is contradicting the field, not filling a gap.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: Literal[ResolveConclusion.EXTERNAL_PATH_ACCEPTED] = (
        ResolveConclusion.EXTERNAL_PATH_ACCEPTED
    )
    control_plane_readable: Literal[True] = True
    runtime_readability: RuntimeReadability
    runtime_readability_cause: RuntimeReadabilityUnverifiedCause


class UnresolvedResponse(BaseModel):
    """An explicit unresolved result with source validation status and reason."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: Literal[ResolveConclusion.UNRESOLVED] = ResolveConclusion.UNRESOLVED
    status: ModelSourceValidationStatus | None
    reason_code: ModelSourceValidationReason
    next_action: ModelSourceValidationAction


type PreviewResponse = ResolvedPreviewResponse | ExternalPathAcceptedResponse | UnresolvedResponse


def render_preview(preview: ResolutionPreview) -> PreviewResponse:
    """Project one conclusion onto its own response shape."""

    if isinstance(preview.outcome, ResolutionSucceeded):
        if preview.local_availability is None:  # pragma: no cover - forbidden by construction
            raise RuntimeError("a confirmed preview must carry a local availability answer")
        return ResolvedPreviewResponse(
            resolved=ResolvedModelResponse.from_domain(preview.outcome.resolved),
            local_availability=preview.local_availability,
            deployable_layout=deployable_layout(
                (entry.path for entry in preview.outcome.resolved.files.value)
                if isinstance(preview.outcome.resolved.files, Identified)
                else None
            ),
        )
    if isinstance(preview.outcome, ExternalPathAccepted):
        return ExternalPathAcceptedResponse(
            runtime_readability=preview.outcome.runtime_readability,
            runtime_readability_cause=preview.outcome.runtime_readability_cause,
        )
    result = preview.outcome.result
    if result.next_action is None:  # pragma: no cover - forbidden by ResolutionFailed
        raise RuntimeError("an unresolved conclusion must carry a direction")
    return UnresolvedResponse(
        status=result.status,
        reason_code=result.reason_code,
        next_action=result.next_action,
    )


async def handle_resolve_input_rejected(
    request: Request,
    exception: Exception,
) -> JSONResponse:
    """Reject an unusable identity without echoing any fragment of it back."""

    if not isinstance(exception, ResolveInputRejected):
        raise exception
    request_id = str(current_request_id(request))
    unsupported = isinstance(exception, ModelLayoutUnsupported)
    return json_response(
        status_code=422 if unsupported else 400,
        request_id=request_id,
        content={
            "error": {
                "code": (
                    ErrorCode.MODEL_LAYOUT_UNSUPPORTED if unsupported else ErrorCode.INVALID_REQUEST
                ).value,
                "message": (
                    "该模型类型暂不支持，未加载到模型目录"
                    if unsupported
                    else "模型标识不正确，本次操作未执行，请检查后重试"
                ),
                "details": {},
                "request_id": request_id,
            }
        },
    )


def create_model_resolution_router(service: ModelResolutionService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/models", tags=["models"])

    @router.post("/resolve")
    @capability_route(Capability.MANAGE_LOCAL_MODELS)
    async def resolve_model(request: Request, body: ResolveRequest) -> JSONResponse:
        preview = await service.resolve(
            source=body.source,
            repository_id=body.repository_id,
            external_path=body.external_path,
            revision=body.revision,
        )
        payload: dict[str, Any] = render_preview(preview).model_dump(mode="json")
        return json_response(content=payload, request_id=str(current_request_id(request)))

    return router


__all__ = [
    "MODELS_RESOLVE_PATH",
    "ExternalPathAcceptedResponse",
    "IdentifiedValue",
    "PreviewResponse",
    "RemoteFileResponse",
    "ResolveConclusion",
    "ResolveRequest",
    "ResolvedModelResponse",
    "ResolvedPreviewResponse",
    "UnrecognizedValue",
    "UnresolvedResponse",
    "create_model_resolution_router",
    "handle_resolve_input_rejected",
    "render_preview",
]

"""``POST /api/v1/models``: explicit, identity-only catalog registration."""

from __future__ import annotations

from typing import Any, Final
from uuid import UUID

from fastapi import APIRouter, Header
from holm_domain import (
    Capability,
    Identified,
    MetadataKnowledge,
    MetadataStatus,
    ModelOriginType,
    from_persisted_column,
)
from pydantic import BaseModel, ConfigDict
from starlette.requests import Request
from starlette.responses import JSONResponse

from holm_control_api.auth_routes import json_response
from holm_control_api.authorization import (
    capability_route,
    current_authenticated_session,
    current_request_id,
)
from holm_control_api.model_registration_service import (
    IdempotencyConflict,
    ModelRegistrationService,
    ModelRegistrationSuccess,
)
from holm_control_api.model_resolution_routes import (
    IdentifiedValue,
    ResolveRequest,
    UnrecognizedValue,
    UnresolvedResponse,
)

MODELS_PATH: Final[str] = "/api/v1/models"
IDEMPOTENCY_KEY_HEADER: Final[str] = "Idempotency-Key"


class ModelRegistrationRequest(ResolveRequest):
    """The Resolve identity shape, with no client-supplied metadata fields."""


def _render_knowledge[T](
    knowledge: MetadataKnowledge[T],
) -> IdentifiedValue[T] | UnrecognizedValue:
    if isinstance(knowledge, Identified):
        return IdentifiedValue[T](value=knowledge.value)
    return UnrecognizedValue()


class RegisteredModelResponse(BaseModel):
    """Safe Model metadata projection; source keys and External Paths are absent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    source: ModelOriginType
    display_name: str
    architecture: IdentifiedValue[str] | UnrecognizedValue
    modalities: IdentifiedValue[tuple[str, ...]] | UnrecognizedValue
    precision: IdentifiedValue[str] | UnrecognizedValue
    quantization: IdentifiedValue[str] | UnrecognizedValue
    context_length: IdentifiedValue[int] | UnrecognizedValue
    candidate_runtimes: IdentifiedValue[tuple[str, ...]] | UnrecognizedValue
    estimated_size_bytes: IdentifiedValue[int] | UnrecognizedValue
    license: IdentifiedValue[str] | UnrecognizedValue
    metadata_status: MetadataStatus
    replayed: bool

    @classmethod
    def from_success(cls, value: ModelRegistrationSuccess) -> RegisteredModelResponse:
        model = value.model
        return cls(
            id=model.id,
            source=model.source_type,
            display_name=model.display_name,
            architecture=_render_knowledge(from_persisted_column(model.architecture)),
            modalities=_render_knowledge(from_persisted_column(model.modalities)),
            precision=_render_knowledge(from_persisted_column(model.precision)),
            quantization=_render_knowledge(from_persisted_column(model.quantization)),
            context_length=_render_knowledge(from_persisted_column(model.context_length)),
            candidate_runtimes=_render_knowledge(from_persisted_column(model.supported_runtimes)),
            estimated_size_bytes=_render_knowledge(
                from_persisted_column(model.estimated_size_bytes)
            ),
            license=_render_knowledge(from_persisted_column(model.license)),
            metadata_status=model.metadata_status,
            replayed=value.replayed,
        )


class RegistrationUnresolvedResponse(UnresolvedResponse):
    """The existing source-validation conclusion, not a second vocabulary."""


async def handle_idempotency_conflict(
    request: Request,
    exception: Exception,
) -> JSONResponse:
    """Return a stable 409 without the key, hashes, payload, or competing object."""

    if not isinstance(exception, IdempotencyConflict):
        raise exception
    request_id = str(current_request_id(request))
    return json_response(
        status_code=409,
        request_id=request_id,
        content={
            "error": {
                "code": "idempotency_conflict",
                "message": "幂等键已用于其他请求，本次操作未执行，请更换后重试",
                "details": {},
                "request_id": request_id,
            }
        },
    )


def create_model_registration_router(service: ModelRegistrationService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/models", tags=["models"])

    @router.post("")
    @capability_route(Capability.MANAGE_LOCAL_MODELS)
    async def register_model(
        request: Request,
        body: ModelRegistrationRequest,
        idempotency_key: str = Header(
            min_length=1,
            max_length=256,
            alias=IDEMPOTENCY_KEY_HEADER,
        ),
    ) -> JSONResponse:
        authenticated = current_authenticated_session(request)
        result = await service.register(
            actor_id=authenticated.user.id,
            idempotency_key=idempotency_key,
            source=body.source,
            repository_id=body.repository_id,
            external_path=body.external_path,
            revision=body.revision,
        )
        if isinstance(result, ModelRegistrationSuccess):
            payload: dict[str, Any] = RegisteredModelResponse.from_success(result).model_dump(
                mode="json"
            )
        else:
            source_result = result.result
            if source_result.next_action is None:
                raise RuntimeError("an unresolved registration must carry a direction")
            payload = RegistrationUnresolvedResponse(
                status=source_result.status,
                reason_code=source_result.reason_code,
                next_action=source_result.next_action,
            ).model_dump(mode="json")
        return json_response(content=payload, request_id=str(current_request_id(request)))

    return router


__all__ = [
    "IDEMPOTENCY_KEY_HEADER",
    "MODELS_PATH",
    "ModelRegistrationRequest",
    "RegisteredModelResponse",
    "RegistrationUnresolvedResponse",
    "create_model_registration_router",
    "handle_idempotency_conflict",
]

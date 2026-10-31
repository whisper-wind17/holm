"""``GET`` and ``DELETE /api/v1/models`` catalog surfaces."""

from __future__ import annotations

from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends
from holm_contracts import CursorPage, CursorPageParams
from holm_domain import (
    Capability,
    LocalAvailability,
    ModelOriginType,
    from_persisted_column,
)
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from holm_control_api.auth_routes import json_response
from holm_control_api.authorization import (
    capability_route,
    current_authenticated_session,
    current_request_id,
)
from holm_control_api.model_catalog_service import (
    ModelCatalogCursorInvalid,
    ModelCatalogItem,
    ModelCatalogNotFound,
    ModelCatalogReferenced,
    ModelCatalogService,
)
from holm_control_api.model_registration_routes import _render_knowledge
from holm_control_api.model_resolution_routes import IdentifiedValue, UnrecognizedValue

MODELS_PATH: Final[str] = "/api/v1/models"
REVISION_UNAVAILABLE: Final[str] = "—"


class ModelReferenceCountsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifacts: Annotated[int, Field(ge=0)]
    download_tasks: Annotated[int, Field(ge=0)]
    model_instances: Annotated[int, Field(ge=0)] | None


class ModelCatalogItemResponse(BaseModel):
    """Path- and credential-free projection required by the Model catalog."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    source: ModelOriginType
    display_name: str
    repository_id: str | None
    architecture: IdentifiedValue[str] | UnrecognizedValue
    modalities: IdentifiedValue[tuple[str, ...]] | UnrecognizedValue
    estimated_size_bytes: IdentifiedValue[int] | UnrecognizedValue
    revision: str
    local_availability: LocalAvailability
    references: ModelReferenceCountsResponse

    @classmethod
    def from_item(cls, item: ModelCatalogItem) -> ModelCatalogItemResponse:
        model = item.model
        return cls(
            id=model.id,
            source=model.source_type,
            display_name=model.display_name,
            repository_id=model.repository_id,
            architecture=_render_knowledge(from_persisted_column(model.architecture)),
            modalities=_render_knowledge(from_persisted_column(model.modalities)),
            estimated_size_bytes=_render_knowledge(
                from_persisted_column(model.estimated_size_bytes)
            ),
            revision=(
                REVISION_UNAVAILABLE
                if model.source_type is ModelOriginType.LOCAL_PATH
                else item.resolved_revision or REVISION_UNAVAILABLE
            ),
            local_availability=item.local_availability,
            references=ModelReferenceCountsResponse(
                artifacts=item.references.artifacts,
                download_tasks=item.references.download_tasks,
                model_instances=item.references.model_instances,
            ),
        )


def _catalog_page_params(request: Request) -> CursorPageParams:
    """Decode URL strings, then delegate every pagination rule to the contract."""

    pairs = request.query_params.multi_items()
    if len({name for name, _ in pairs}) != len(pairs):
        raise ModelCatalogCursorInvalid()
    raw: dict[str, object] = dict(pairs)
    page_size = raw.get("page_size")
    if page_size is not None:
        if not isinstance(page_size, str) or not page_size.isascii() or not page_size.isdigit():
            raise ModelCatalogCursorInvalid()
        raw["page_size"] = int(page_size)
    try:
        parsed = CursorPageParams.model_validate(raw)
    except ValueError:
        parsed = None
    if parsed is None:
        # Raise outside the ``except`` block: query values must not survive in
        # ``__context__`` for exception-chain walkers.
        raise ModelCatalogCursorInvalid()
    return parsed


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
) -> JSONResponse:
    request_id = str(current_request_id(request))
    return json_response(
        status_code=status_code,
        request_id=request_id,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": {},
                "request_id": request_id,
            }
        },
    )


async def handle_model_catalog_cursor_invalid(
    request: Request,
    exception: Exception,
) -> JSONResponse:
    if not isinstance(exception, ModelCatalogCursorInvalid):
        raise exception
    return _error_response(
        request,
        status_code=400,
        code="invalid_request",
        message="分页游标无效，模型目录未读取，请刷新后重试",
    )


async def handle_model_catalog_not_found(
    request: Request,
    exception: Exception,
) -> JSONResponse:
    if not isinstance(exception, ModelCatalogNotFound):
        raise exception
    return _error_response(
        request,
        status_code=404,
        code="model_not_found",
        message="模型不存在，无法清理，请刷新模型目录",
    )


async def handle_model_catalog_referenced(
    request: Request,
    exception: Exception,
) -> JSONResponse:
    if not isinstance(exception, ModelCatalogReferenced):
        raise exception
    return _error_response(
        request,
        status_code=409,
        code="model_referenced",
        message="模型仍被本地模型、下载任务或模型实例引用，未执行清理，请先解除引用",
    )


def create_model_catalog_router(service: ModelCatalogService) -> APIRouter:
    router = APIRouter(prefix=MODELS_PATH, tags=["models"])

    @router.get("")
    @capability_route(Capability.MANAGE_LOCAL_MODELS)
    async def list_models(
        request: Request,
        params: Annotated[CursorPageParams, Depends(_catalog_page_params)],
    ) -> JSONResponse:
        page = await service.list(params)
        response = CursorPage[ModelCatalogItemResponse](
            items=tuple(ModelCatalogItemResponse.from_item(item) for item in page.items),
            next_cursor=page.next_cursor,
        )
        return json_response(
            content=response.model_dump(mode="json"),
            request_id=str(current_request_id(request)),
        )

    @router.delete("/{model_id}", status_code=204)
    @capability_route(Capability.MANAGE_LOCAL_MODELS)
    async def delete_model(request: Request, model_id: UUID) -> Response:
        authenticated = current_authenticated_session(request)
        await service.delete(
            model_id,
            actor_id=authenticated.user.id,
        )
        return Response(status_code=204)

    return router


__all__ = [
    "MODELS_PATH",
    "REVISION_UNAVAILABLE",
    "ModelCatalogItemResponse",
    "ModelReferenceCountsResponse",
    "create_model_catalog_router",
    "handle_model_catalog_cursor_invalid",
    "handle_model_catalog_not_found",
    "handle_model_catalog_referenced",
]

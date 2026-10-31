"""``GET /api/v1/downloads`` and ``GET /api/v1/artifacts``.

The two lists shown by the Local models page.  Both are read-only.

Every value the platform cannot supply today is ``null`` rather than a rendered
dash.  ``model_catalog_routes.REVISION_UNAVAILABLE`` renders ``"—"`` for one
catalog column, and that string is deliberately not reused here: the API needs
"collection failed", "genuinely zero" and "no longer applicable" to stay
distinguishable, and a value already flattened into display text cannot carry
that distinction to the client.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from holm_contracts import CursorPage, CursorPageParams
from holm_domain import (
    ArtifactStorageType,
    Capability,
    DownloadFailureStage,
    DownloadTaskStatus,
    JobStatus,
    ModelArtifactStatus,
    ModelInstanceStatus,
    ModelOriginType,
)
from holm_persistence import ModelArtifactFilters
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from holm_control_api.artifact_deletion_service import ArtifactDeleteRejected
from holm_control_api.artifact_verification_service import ArtifactVerifyRejected
from holm_control_api.auth_routes import json_response
from holm_control_api.authorization import (
    capability_route,
    current_authenticated_session,
    current_request_id,
)
from holm_control_api.download_command_routes import handle_download_command_error
from holm_control_api.download_command_service import (
    DownloadCommand,
    DownloadCommandNotAllowed,
    DownloadCommandNotFound,
)
from holm_control_api.model_acquisition_read_service import (
    DownloadTaskItem,
    ModelAcquisitionCursorInvalid,
    ModelAcquisitionReadService,
    ModelArtifactItem,
    ModelArtifactSummary,
)

DOWNLOADS_PATH: Final[str] = "/api/v1/downloads"
ARTIFACTS_PATH: Final[str] = "/api/v1/artifacts"


class ModelAcquisitionQueryInvalid(ValueError):
    """A query string this endpoint cannot honour; the values are never echoed."""


class DownloadPageParams(CursorPageParams):
    status: tuple[DownloadTaskStatus, ...] = ()


class ArtifactPageParams(CursorPageParams):
    """Four model filters layered onto the shared pagination contract.

    Subclassing keeps ``cursor`` and ``page_size`` defined exactly once.  The
    enum-typed fields make an unrecognized filter value a rejection rather than
    a silently ignored word, which is the same default-deny rule the Allowlists
    elsewhere in this service follow.
    """

    search: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    source: ModelOriginType | None = None
    status: ModelArtifactStatus | None = None
    storage_type: ArtifactStorageType | None = None

    def as_filters(self) -> ModelArtifactFilters:
        return ModelArtifactFilters(
            search=self.search,
            source=self.source,
            status=self.status,
            storage_type=self.storage_type,
        )


class DownloadTaskItemResponse(BaseModel):
    """One Download Task row.

    ``revision`` carries only a resolved, immutable Revision.  The persisted
    ``requested_revision_norm`` is never projected: it exists from ``Queued``
    onwards but means nothing until resolution, and when the administrator named
    no Revision its value is the internal sentinel ``@default``.

    There is no speed and no ETA field.  ``core.download_tasks`` holds a single
    observation per task, and a rate needs two; ``downloaded_bytes`` over elapsed
    time would be wrong for any task that was ever paused or resumed from
    staging, and wrong in a way that looks authoritative.  The client differences two
    polls instead.  For the same reason a percentage is absent: with
    ``total_bytes`` unknown there is no honest one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    model_id: UUID
    display_name: str
    source: ModelOriginType
    repository_id: str
    revision: str | None
    status: DownloadTaskStatus
    attempt: Annotated[int, Field(ge=1)]
    downloaded_bytes: Annotated[int, Field(ge=0)]
    total_bytes: Annotated[int, Field(ge=0)] | None
    staging_path: str | None
    error_code: str | None
    failure_stage: DownloadFailureStage | None
    available_commands: tuple[DownloadCommand, ...]
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    @classmethod
    def from_item(cls, item: DownloadTaskItem) -> DownloadTaskItemResponse:
        record = item.record
        return cls(
            id=record.id,
            model_id=record.model_id,
            display_name=record.display_name,
            source=record.source_type,
            repository_id=record.repository_id,
            revision=None
            if record.source_type is ModelOriginType.LOCAL_PATH
            else record.resolved_revision,
            status=record.status,
            attempt=record.attempt,
            downloaded_bytes=record.downloaded_bytes,
            total_bytes=record.total_bytes,
            staging_path=record.staging_path,
            error_code=record.error_code,
            failure_stage=item.failure_stage,
            available_commands=item.available_commands,
            created_at=record.created_at,
            updated_at=record.updated_at,
            completed_at=record.completed_at,
        )


class ModelArtifactItemResponse(BaseModel):
    """One local Model row.

    ``referencing_instances`` is always ``null`` today and must not become
    ``0``: the platform does not report the count here, rather than knowing
    it to be zero.

    ``last_verified_at`` is the latest completed manual or launch verification time,
    including a completed unhealthy judgement. Before any verification completes it
    remains the publication time; enqueueing and execution failures never advance it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    model_id: UUID
    display_name: str
    source: ModelOriginType
    repository_id: str
    resolved_revision: str | None
    status: ModelArtifactStatus
    storage_type: ArtifactStorageType
    size_bytes: Annotated[int, Field(ge=0)]
    path: str
    referencing_instances: Annotated[int, Field(ge=0)] | None
    last_verified_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_item(cls, item: ModelArtifactItem) -> ModelArtifactItemResponse:
        record = item.record
        return cls(
            id=record.id,
            model_id=record.model_id,
            display_name=record.display_name,
            source=record.source_type,
            repository_id=record.repository_id,
            resolved_revision=None
            if record.source_type is ModelOriginType.LOCAL_PATH
            else record.resolved_revision,
            status=record.status,
            storage_type=record.storage_type,
            size_bytes=record.size_bytes,
            path=record.path,
            referencing_instances=item.referencing_instances,
            last_verified_at=item.last_verified_at or record.created_at,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class DownloadStatusCountsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    counts: dict[DownloadTaskStatus, Annotated[int, Field(ge=0)]]


class ArtifactModelMetadataResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    architecture: str | None
    modalities: tuple[str, ...] | None
    precision: str | None
    quantization: str | None
    context_length: int | None
    license: str | None


class ArtifactFileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str
    size_bytes: Annotated[int, Field(ge=0)]
    sha256: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]


class ArtifactDownloadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    status: DownloadTaskStatus
    created_at: datetime
    completed_at: datetime | None


class ArtifactInstanceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    name: str
    status: ModelInstanceStatus


class ModelArtifactDetailResponse(ModelArtifactItemResponse):
    model: ArtifactModelMetadataResponse
    files: Annotated[list[ArtifactFileResponse], Field(min_length=1)] | None
    download_tasks: list[ArtifactDownloadResponse]
    instances: list[ArtifactInstanceResponse]


class ArtifactVerifyResponse(BaseModel):
    """Accepted work is queued, not a completed integrity judgement."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    queued: Literal[True] = True


class ArtifactVerificationStatusResponse(BaseModel):
    """Persisted job progress is separate from Artifact health."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    status: JobStatus | None
    job_id: UUID | None
    error_code: Literal["artifact_verify_unsafe_path", "artifact_verify_failed"] | None


class ModelArtifactSummaryResponse(BaseModel):
    """The three storage figures above the Local models table.

    ``managed_bytes`` counts Managed Artifacts only.  External files belong to
    the administrator, the platform does not occupy them, and they may sit on a
    different filesystem than the one ``storage_available_bytes`` measures --
    adding the two kinds together would produce a number that reconciles with
    nothing.  Each External row still reports its own ``size_bytes``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    managed_bytes: Annotated[int, Field(ge=0)]
    storage_available_bytes: Annotated[int, Field(ge=0)] | None
    active_download_tasks: Annotated[int, Field(ge=0)]

    @classmethod
    def from_summary(cls, summary: ModelArtifactSummary) -> ModelArtifactSummaryResponse:
        return cls(
            managed_bytes=summary.managed_bytes,
            storage_available_bytes=summary.storage_available_bytes,
            active_download_tasks=summary.active_download_tasks,
        )


class ModelArtifactPageResponse(CursorPage[ModelArtifactItemResponse]):
    """The shared page contract plus the page-level storage summary."""

    summary: ModelArtifactSummaryResponse


def _page_params[ParamsT: CursorPageParams](request: Request, model: type[ParamsT]) -> ParamsT:
    """Decode URL strings, then delegate every rule to the parameter contract."""

    pairs = request.query_params.multi_items()
    repeated_status = model is DownloadPageParams
    unique_pairs = [
        (name, value) for name, value in pairs if not (repeated_status and name == "status")
    ]
    if len({name for name, _ in unique_pairs}) != len(unique_pairs):
        raise ModelAcquisitionQueryInvalid()
    raw: dict[str, object] = dict(unique_pairs)
    if repeated_status:
        raw["status"] = request.query_params.getlist("status")
    page_size = raw.get("page_size")
    if page_size is not None:
        if not isinstance(page_size, str) or not page_size.isascii() or not page_size.isdigit():
            raise ModelAcquisitionQueryInvalid()
        raw["page_size"] = int(page_size)
    parsed: ParamsT | None = None
    try:
        parsed = model.model_validate(raw)
    except ValueError:
        parsed = None
    if parsed is None:
        # Raised outside the ``except`` block: rejected query values must not
        # survive in ``__context__`` for an exception-chain walker to recover.
        raise ModelAcquisitionQueryInvalid()
    return parsed


def _download_page_params(request: Request) -> DownloadPageParams:
    return _page_params(request, DownloadPageParams)


def _artifact_page_params(request: Request) -> ArtifactPageParams:
    return _page_params(request, ArtifactPageParams)


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


async def handle_model_acquisition_cursor_invalid(
    request: Request,
    exception: Exception,
) -> JSONResponse:
    if not isinstance(exception, ModelAcquisitionCursorInvalid):
        raise exception
    return _error_response(
        request,
        status_code=400,
        code="invalid_request",
        message="分页游标无效，列表未读取，请返回第一页后重试",
    )


async def handle_model_acquisition_query_invalid(
    request: Request,
    exception: Exception,
) -> JSONResponse:
    if not isinstance(exception, ModelAcquisitionQueryInvalid):
        raise exception
    return _error_response(
        request,
        status_code=400,
        code="invalid_request",
        message="筛选或分页条件无效，列表未读取，请修正后重试",
    )


def create_download_listing_router(service: ModelAcquisitionReadService) -> APIRouter:
    router = APIRouter(prefix=DOWNLOADS_PATH, tags=["downloads"])

    @router.get("")
    @capability_route(Capability.MANAGE_LOCAL_MODELS)
    async def list_downloads(
        request: Request,
        params: Annotated[DownloadPageParams, Depends(_download_page_params)],
    ) -> JSONResponse:
        page = await service.list_download_tasks(params, statuses=params.status)
        response = CursorPage[DownloadTaskItemResponse](
            items=tuple(DownloadTaskItemResponse.from_item(item) for item in page.items),
            next_cursor=page.next_cursor,
        )
        return json_response(
            content=response.model_dump(mode="json"),
            request_id=str(current_request_id(request)),
        )

    @router.get("/status-counts")
    @capability_route(Capability.MANAGE_LOCAL_MODELS)
    async def status_counts(request: Request) -> JSONResponse:
        response = DownloadStatusCountsResponse(counts=await service.download_status_counts())
        return json_response(
            content=response.model_dump(mode="json"), request_id=str(current_request_id(request))
        )

    @router.delete("/{id}")
    @capability_route(Capability.MANAGE_LOCAL_MODELS)
    async def delete_download(
        request: Request, id: UUID, cleanup_staging: bool = False
    ) -> Response:
        try:
            await service.delete_download_task(
                task_id=id,
                actor_id=current_authenticated_session(request).user.id,
                request_id=current_request_id(request),
                cleanup_staging=cleanup_staging,
            )
        except (DownloadCommandNotFound, DownloadCommandNotAllowed) as error:
            return await handle_download_command_error(request, error)
        return Response(status_code=204)

    return router


def create_artifact_listing_router(service: ModelAcquisitionReadService) -> APIRouter:
    router = APIRouter(prefix=ARTIFACTS_PATH, tags=["artifacts"])

    @router.get("")
    @capability_route(Capability.MANAGE_LOCAL_MODELS)
    async def list_artifacts(
        request: Request,
        params: Annotated[ArtifactPageParams, Depends(_artifact_page_params)],
    ) -> JSONResponse:
        page = await service.list_artifacts(params, filters=params.as_filters())
        response = ModelArtifactPageResponse(
            items=tuple(ModelArtifactItemResponse.from_item(item) for item in page.items),
            next_cursor=page.next_cursor,
            summary=ModelArtifactSummaryResponse.from_summary(page.summary),
        )
        return json_response(
            content=response.model_dump(mode="json"),
            request_id=str(current_request_id(request)),
        )

    @router.get("/{id}")
    @capability_route(Capability.MANAGE_LOCAL_MODELS)
    async def artifact_detail(request: Request, id: UUID) -> JSONResponse:
        detail = await service.artifact_detail(id)
        if detail is None:
            return _error_response(
                request,
                status_code=404,
                code="artifact_not_found",
                message="本地模型不存在，详情未读取，请刷新列表后重试",
            )
        item, fields = detail
        response = ModelArtifactDetailResponse.model_validate(
            {
                **ModelArtifactItemResponse.from_item(item).model_dump(),
                **fields,
            }
        )
        return json_response(
            content=response.model_dump(mode="json"), request_id=str(current_request_id(request))
        )

    @router.post("/{id}:verify")
    @capability_route(Capability.MANAGE_LOCAL_MODELS)
    async def verify_artifact(request: Request, id: UUID) -> JSONResponse:
        try:
            await service.verify_artifact(id)
        except ArtifactVerifyRejected as error:
            return _error_response(
                request,
                status_code=404 if error.code == "artifact_not_found" else 409,
                code=error.code,
                message="本地模型未开始校验，请刷新列表并等待当前操作结束后重试",
            )
        return json_response(
            status_code=202,
            content=ArtifactVerifyResponse(id=id).model_dump(mode="json"),
            request_id=str(current_request_id(request)),
        )

    @router.get("/{id}/verification")
    @capability_route(Capability.MANAGE_LOCAL_MODELS)
    async def artifact_verification_status(request: Request, id: UUID) -> JSONResponse:
        try:
            status, error, job_id = await service.artifact_verification_status(id)
        except ArtifactVerifyRejected as rejected:
            return _error_response(
                request,
                status_code=404,
                code=rejected.code,
                message="本地模型不存在，请刷新列表后重试",
            )
        return json_response(
            content=ArtifactVerificationStatusResponse.model_validate(
                {"status": status, "error_code": error, "job_id": job_id}
            ).model_dump(mode="json"),
            request_id=str(current_request_id(request)),
        )

    @router.delete("/{id}")
    @capability_route(Capability.MANAGE_LOCAL_MODELS)
    async def delete_artifact(request: Request, id: UUID) -> JSONResponse:
        try:
            queued = await service.delete_artifact(
                artifact_id=id,
                actor_id=current_authenticated_session(request).user.id,
                request_id=current_request_id(request),
            )
        except ArtifactDeleteRejected as error:
            status = 404 if error.code == "artifact_not_found" else 409
            return json_response(
                status_code=status,
                request_id=str(current_request_id(request)),
                content={
                    "error": {
                        "code": error.code,
                        "message": (
                            "本地模型正在校验，删除未执行，请等待校验完成后重试"
                            if error.code == "artifact_verify_in_progress"
                            else "本地模型未删除，请刷新列表或先删除引用它的模型实例后重试"
                        ),
                        "details": {},
                        "request_id": str(current_request_id(request)),
                    }
                },
            )
        return json_response(
            status_code=202 if queued else 200,
            content={"id": str(id), "queued": queued},
            request_id=str(current_request_id(request)),
        )

    @router.get("/{id}/deletion")
    @capability_route(Capability.MANAGE_LOCAL_MODELS)
    async def artifact_deletion_status(request: Request, id: UUID) -> JSONResponse:
        status = await service.artifact_deletion_status(id)
        if status is None:
            return json_response(
                status_code=404,
                request_id=str(current_request_id(request)),
                content={
                    "error": {
                        "code": "artifact_delete_not_found",
                        "message": "删除作业不存在，请刷新本地模型列表后重试",
                        "details": {},
                        "request_id": str(current_request_id(request)),
                    }
                },
            )
        return json_response(
            content={"status": status[0], "error_code": status[1]},
            request_id=str(current_request_id(request)),
        )

    return router


__all__ = [
    "ARTIFACTS_PATH",
    "DOWNLOADS_PATH",
    "ArtifactPageParams",
    "DownloadTaskItemResponse",
    "ModelAcquisitionQueryInvalid",
    "ModelArtifactItemResponse",
    "ModelArtifactPageResponse",
    "ModelArtifactSummaryResponse",
    "create_artifact_listing_router",
    "create_download_listing_router",
    "handle_model_acquisition_cursor_invalid",
    "handle_model_acquisition_query_invalid",
]

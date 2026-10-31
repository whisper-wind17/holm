"""Administrator-only Request Record HTTP contract."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Self
from uuid import UUID

from fastapi import APIRouter, Query
from holm_domain import Capability, ErrorCode, RequestFinishReason, RequestRecordState
from holm_persistence import RequestRecordListItem
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from holm_control_api.auth_routes import json_response
from holm_control_api.authorization import capability_route, current_request_id
from holm_control_api.model_catalog_routes import _error_response
from holm_control_api.request_record_service import (
    RequestRecordQuery,
    RequestRecordQueryInvalid,
    RequestRecordQueryService,
)

REQUESTS_PATH: Final[str] = "/api/v1/requests"


class RequestRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    received_at: datetime
    completed_at: datetime | None
    user_id: UUID | None
    username: str | None
    api_key_id: UUID | None
    api_key_name: str | None
    api_key_display_prefix: str | None
    api_key_last4: str | None
    service_id: UUID | None
    public_model_name: str | None
    instance_id: UUID | None
    stream: bool | None
    status: RequestRecordState
    http_status: Annotated[int, Field(ge=100, le=599)] | None
    error_code: ErrorCode | None
    input_tokens: Annotated[int, Field(ge=0)] | None
    output_tokens: Annotated[int, Field(ge=0)] | None
    ttft_ms: Annotated[int, Field(ge=0)] | None
    duration_ms: Annotated[int, Field(ge=0)] | None
    finish_reason: RequestFinishReason | None

    @classmethod
    def from_record(cls, record: RequestRecordListItem) -> Self:
        return cls(**{field: getattr(record, field) for field in cls.model_fields})


class RequestRecordPageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[RequestRecordResponse, ...]
    next_cursor: str | None


class RequestRecordErrorGroupResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    status: RequestRecordState
    error_code: ErrorCode | None
    count: Annotated[int, Field(ge=1)]
    first_at: datetime
    last_at: datetime
    latest_request_id: UUID


class RequestRecordErrorGroupsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[RequestRecordErrorGroupResponse, ...]


async def handle_request_record_query_invalid(
    request: Request,
    exception: Exception,
) -> JSONResponse:
    if not isinstance(exception, RequestRecordQueryInvalid):
        raise exception
    return _error_response(
        request,
        status_code=400,
        code="invalid_request",
        message="调用记录筛选条件无效，列表无法更新，请检查后重试",
    )


def create_request_record_router(service: RequestRecordQueryService) -> APIRouter:
    router = APIRouter(prefix=REQUESTS_PATH, tags=["requests"])

    @router.get("")
    @capability_route(Capability.VIEW_REQUEST_RECORDS)
    async def list_request_records(
        request: Request,
        service_id: UUID | None = None,
        user_id: UUID | None = None,
        api_key_id: UUID | None = None,
        status: RequestRecordState | None = None,
        error_code: Annotated[str | None, Query(max_length=64)] = None,
        request_id: UUID | None = None,
        from_time: Annotated[datetime | None, Query(alias="from")] = None,
        to_time: Annotated[datetime | None, Query(alias="to")] = None,
        cursor: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> JSONResponse:
        page = await service.list(
            RequestRecordQuery(
                service_id=service_id,
                user_id=user_id,
                api_key_id=api_key_id,
                status=status,
                error_code=error_code,
                request_id=request_id,
                from_time=from_time,
                to_time=to_time,
                cursor=cursor,
                limit=limit,
            )
        )
        response = RequestRecordPageResponse(
            items=tuple(RequestRecordResponse.from_record(item) for item in page.items),
            next_cursor=page.next_cursor,
        )
        return json_response(
            request_id=str(current_request_id(request)),
            content=response.model_dump(mode="json"),
        )

    @router.get("/error-groups")
    @capability_route(Capability.VIEW_REQUEST_RECORDS)
    async def request_error_groups(
        request: Request,
        service_id: UUID | None = None,
        from_time: Annotated[datetime | None, Query(alias="from")] = None,
        to_time: Annotated[datetime | None, Query(alias="to")] = None,
    ) -> JSONResponse:
        groups = await service.error_groups(
            from_time=from_time, to_time=to_time, service_id=service_id
        )
        response = RequestRecordErrorGroupsResponse(
            items=tuple(RequestRecordErrorGroupResponse.model_validate(item) for item in groups)
        )
        return json_response(
            request_id=str(current_request_id(request)),
            content=response.model_dump(mode="json"),
        )

    return router


__all__ = [
    "REQUESTS_PATH",
    "create_request_record_router",
    "handle_request_record_query_invalid",
]

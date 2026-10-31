"""Role-scoped Usage HTTP contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Self
from uuid import UUID

from fastapi import APIRouter, Query
from holm_domain import ApiKeyStatus, Capability
from holm_persistence import (
    UsageBucketWidth,
    UsageGroup,
    UsageGroupBy,
    UsageTotals,
    UsageTrendPoint,
)
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from holm_control_api.auth_routes import json_response
from holm_control_api.authorization import (
    capability_route,
    current_authenticated_session,
    current_request_id,
)
from holm_control_api.model_catalog_routes import _error_response
from holm_control_api.usage_service import (
    UsageForbiddenReason,
    UsageQuery,
    UsageQueryForbidden,
    UsageQueryInvalid,
    UsageQueryService,
    UsageReport,
    UsageSummarySnapshot,
)

USAGE_PATH: Final[str] = "/api/v1/usage"
USAGE_SUMMARY_PATH: Final[str] = "/api/v1/usage/summary"


class _ClosedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UsageTotalsResponse(_ClosedResponse):
    requests: Annotated[int, Field(ge=0)]
    input_tokens: Annotated[int, Field(ge=0)] | None
    output_tokens: Annotated[int, Field(ge=0)] | None
    total_tokens: Annotated[int, Field(ge=0)] | None
    error_requests: Annotated[int, Field(ge=0)]
    partial_token_requests: Annotated[int, Field(ge=0)]
    error_rate: Annotated[float, Field(ge=0, le=1)] | None

    @classmethod
    def from_totals(cls, totals: UsageTotals) -> Self:
        return cls(
            requests=totals.requests,
            input_tokens=totals.input_tokens,
            output_tokens=totals.output_tokens,
            total_tokens=totals.total_tokens,
            error_requests=totals.error_requests,
            partial_token_requests=totals.partial_token_requests,
            error_rate=totals.error_rate,
        )


class UsageGroupResponse(UsageTotalsResponse):
    group_id: UUID | None
    display_name: str
    status: ApiKeyStatus | None
    is_deleted: bool
    api_key_name: str | None
    owner_username: str | None
    owner_is_deleted: bool | None

    @classmethod
    def from_group(cls, group: UsageGroup) -> Self:
        return cls(
            group_id=group.group_id,
            display_name=group.display_name,
            status=group.status,
            is_deleted=group.is_deleted,
            api_key_name=group.api_key_name,
            owner_username=group.owner_username,
            owner_is_deleted=group.owner_is_deleted,
            **UsageTotalsResponse.from_totals(group.totals).model_dump(),
        )


class UsageTrendPointResponse(_ClosedResponse):
    bucket_start: datetime
    requests: Annotated[int, Field(ge=0)]
    input_tokens: Annotated[int, Field(ge=0)] | None
    output_tokens: Annotated[int, Field(ge=0)] | None
    total_tokens: Annotated[int, Field(ge=0)] | None

    @classmethod
    def from_point(cls, point: UsageTrendPoint) -> Self:
        return cls(
            bucket_start=point.bucket_start,
            requests=point.requests,
            input_tokens=point.input_tokens,
            output_tokens=point.output_tokens,
            total_tokens=point.total_tokens,
        )


class UsageReportResponse(_ClosedResponse):
    from_time: datetime = Field(serialization_alias="from")
    to_time: datetime = Field(serialization_alias="to")
    tz: str
    bucket_width: UsageBucketWidth
    summary: UsageTotalsResponse
    details: tuple[UsageGroupResponse, ...]
    trend: tuple[UsageTrendPointResponse, ...]

    @classmethod
    def from_report(cls, report: UsageReport) -> Self:
        return cls(
            from_time=report.from_time,
            to_time=report.to_time,
            tz=report.time_zone,
            bucket_width=report.bucket_width,
            summary=UsageTotalsResponse.from_totals(report.summary),
            details=tuple(UsageGroupResponse.from_group(group) for group in report.details),
            trend=tuple(UsageTrendPointResponse.from_point(point) for point in report.trend),
        )


class UsageSummaryResponse(_ClosedResponse):
    today_active_users: Annotated[int, Field(ge=0)]
    today: UsageTotalsResponse
    this_month: UsageTotalsResponse
    data_updated_at: datetime | None

    @classmethod
    def from_snapshot(cls, snapshot: UsageSummarySnapshot) -> Self:
        return cls(
            today_active_users=snapshot.today_active_users,
            today=UsageTotalsResponse.from_totals(snapshot.today),
            this_month=UsageTotalsResponse.from_totals(snapshot.this_month),
            data_updated_at=snapshot.data_updated_at,
        )


async def handle_usage_error(request: Request, exception: Exception) -> JSONResponse:
    if isinstance(exception, UsageQueryInvalid):
        return _error_response(
            request,
            status_code=400,
            code="invalid_request",
            message="用量筛选条件无效，数据无法显示，请检查日期和时区后重试",
        )
    if isinstance(exception, UsageQueryForbidden):
        if exception.reason is UsageForbiddenReason.SERVICE:
            return _error_response(
                request,
                status_code=403,
                code="service_not_authorized",
                message="未获得所选 AI 服务的权限，用量无法显示，请联系管理员授权",
            )
        return _error_response(
            request,
            status_code=403,
            code="permission_denied",
            message="没有查看所选用量的权限，数据无法显示，请调整筛选条件",
        )
    raise exception


def create_usage_router(service: UsageQueryService) -> APIRouter:
    router = APIRouter(tags=["usage"])

    @router.get(USAGE_SUMMARY_PATH)
    @capability_route(Capability.VIEW_USAGE)
    async def usage_summary(
        request: Request,
        tz: Annotated[str, Query(min_length=1, max_length=128)] = "UTC",
    ) -> JSONResponse:
        authenticated = current_authenticated_session(request)
        response = UsageSummaryResponse.from_snapshot(
            await service.summary(
                time_zone=tz,
                actor_id=authenticated.user.id,
                actor_role=authenticated.user.role,
            )
        )
        return json_response(
            request_id=str(current_request_id(request)),
            content=response.model_dump(mode="json", by_alias=True),
        )

    @router.get(USAGE_PATH)
    @capability_route(Capability.VIEW_USAGE)
    async def usage_report(
        request: Request,
        from_time: Annotated[datetime, Query(alias="from")],
        to_time: Annotated[datetime, Query(alias="to")],
        tz: Annotated[str, Query(min_length=1, max_length=128)] = "UTC",
        service_id: UUID | None = None,
        user_id: UUID | None = None,
        api_key_id: UUID | None = None,
        group_by: UsageGroupBy = UsageGroupBy.SERVICE,
    ) -> JSONResponse:
        authenticated = current_authenticated_session(request)
        response = UsageReportResponse.from_report(
            await service.report(
                UsageQuery(
                    from_time=from_time,
                    to_time=to_time,
                    time_zone=tz,
                    service_id=service_id,
                    user_id=user_id,
                    api_key_id=api_key_id,
                    group_by=group_by,
                ),
                actor_id=authenticated.user.id,
                actor_role=authenticated.user.role,
            )
        )
        return json_response(
            request_id=str(current_request_id(request)),
            content=response.model_dump(mode="json", by_alias=True),
        )

    return router


__all__ = [
    "USAGE_PATH",
    "USAGE_SUMMARY_PATH",
    "UsageGroupResponse",
    "UsageReportResponse",
    "UsageSummaryResponse",
    "UsageTotalsResponse",
    "UsageTrendPointResponse",
    "create_usage_router",
    "handle_usage_error",
]

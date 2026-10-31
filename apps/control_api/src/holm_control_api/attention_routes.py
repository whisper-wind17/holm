"""Administrator-only Overview "needs attention" query API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final

from fastapi import APIRouter
from holm_domain import Capability
from pydantic import BaseModel, ConfigDict, Field

from holm_control_api.attention_service import (
    AttentionKind,
    AttentionService,
    AttentionSeverity,
)
from holm_control_api.authorization import capability_route

ATTENTION_PATH: Final = "/api/v1/overview/attention"


class _ClosedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AttentionItemResponse(_ClosedResponse):
    kind: AttentionKind
    severity: AttentionSeverity
    target_id: str | None
    target_name: str | None
    count: Annotated[int, Field(ge=1)]
    latest_at: datetime | None
    detail: str | None


class AttentionListResponse(_ClosedResponse):
    items: tuple[AttentionItemResponse, ...]


def create_attention_router(service: AttentionService) -> APIRouter:
    router = APIRouter(tags=["overview"])

    @router.get(ATTENTION_PATH, response_model=AttentionListResponse)
    @capability_route(Capability.VIEW_INFRASTRUCTURE)
    async def read_attention() -> AttentionListResponse:
        return AttentionListResponse(
            items=tuple(
                AttentionItemResponse(
                    kind=item.kind,
                    severity=item.severity,
                    target_id=item.target_id,
                    target_name=item.target_name,
                    count=item.count,
                    latest_at=item.latest_at,
                    detail=item.detail,
                )
                for item in await service.items()
            )
        )

    return router


__all__ = [
    "ATTENTION_PATH",
    "AttentionItemResponse",
    "AttentionListResponse",
    "create_attention_router",
]

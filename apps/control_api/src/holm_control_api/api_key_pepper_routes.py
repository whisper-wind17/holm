"""Administrator-only API Key Pepper metadata endpoint."""

from __future__ import annotations

from typing import Final, Self

from fastapi import APIRouter, Response
from holm_domain import Capability
from holm_security import (
    ApiKeyPepperKeyringAssessment,
    ApiKeyPepperRemovalAssessment,
    ApiKeyPepperRemovalReason,
)
from pydantic import BaseModel, ConfigDict

from holm_control_api.api_key_pepper_service import ApiKeyPepperService
from holm_control_api.authorization import capability_route
from holm_control_api.http_constants import CACHE_CONTROL_VALUE

API_KEY_PEPPERS_PATH: Final[str] = "/api/v1/system/api-key-peppers"


class ApiKeyPepperResponse(BaseModel):
    """Safe Pepper metadata; key material has no representation in this schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pepper_key_id: str
    is_current: bool
    active_reference_count: int
    can_remove: bool
    reason_code: ApiKeyPepperRemovalReason | None

    @classmethod
    def from_assessment(cls, value: ApiKeyPepperRemovalAssessment) -> Self:
        return cls(
            pepper_key_id=value.pepper_key_id,
            is_current=value.is_current,
            active_reference_count=value.active_reference_count,
            can_remove=value.can_remove,
            reason_code=value.reason,
        )


class ApiKeyPepperListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[ApiKeyPepperResponse, ...]

    @classmethod
    def from_assessment(cls, value: ApiKeyPepperKeyringAssessment) -> Self:
        return cls(
            items=tuple(ApiKeyPepperResponse.from_assessment(item) for item in value.peppers)
        )


def create_api_key_pepper_router(service: ApiKeyPepperService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/system", tags=["system"])

    @router.get("/api-key-peppers", response_model=ApiKeyPepperListResponse)
    @capability_route(Capability.VIEW_SYSTEM)
    async def read_api_key_peppers(response: Response) -> ApiKeyPepperListResponse:
        response.headers["Cache-Control"] = CACHE_CONTROL_VALUE
        return ApiKeyPepperListResponse.from_assessment(await service.inspect())

    return router


__all__ = [
    "API_KEY_PEPPERS_PATH",
    "ApiKeyPepperListResponse",
    "ApiKeyPepperResponse",
    "create_api_key_pepper_router",
]

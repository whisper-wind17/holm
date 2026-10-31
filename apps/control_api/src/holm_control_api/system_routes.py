"""Administrator-only System endpoints."""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter
from holm_domain import Capability

from holm_control_api.authorization import capability_route
from holm_control_api.system_status import (
    SystemComponentsResponse,
    SystemServerResponse,
    SystemStatusService,
)

SYSTEM_SERVER_PATH: Final[str] = "/api/v1/system/server"
SYSTEM_COMPONENTS_PATH: Final[str] = "/api/v1/system/components"


def create_system_router(service: SystemStatusService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/system", tags=["system"])

    @router.get("/server", response_model=SystemServerResponse)
    @capability_route(Capability.VIEW_SYSTEM)
    async def read_server() -> SystemServerResponse:
        return await service.read_server()

    @router.get("/components", response_model=SystemComponentsResponse)
    @capability_route(Capability.VIEW_SYSTEM)
    async def read_components() -> SystemComponentsResponse:
        return await service.read_components()

    return router


__all__ = [
    "SYSTEM_COMPONENTS_PATH",
    "SYSTEM_SERVER_PATH",
    "create_system_router",
]

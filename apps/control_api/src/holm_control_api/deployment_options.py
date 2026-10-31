"""Safe, model-specific deployment policy projection for administrators."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Query
from holm_contracts.agent.v1 import agent_pb2
from holm_domain import Capability
from holm_persistence import AsyncSessionFactory, ModelRecord, ModelRepository
from holm_runtimes import RuntimeCompatibility, RuntimeCompatibilityCatalog
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from holm_control_api.agent_client import AgentSnapshot, HostAgentClient
from holm_control_api.auth_routes import json_response
from holm_control_api.authorization import capability_route, current_request_id
from holm_control_api.model_catalog_routes import _error_response
from holm_control_api.model_resolution_routes import IdentifiedValue, UnrecognizedValue

RUNTIMES_PATH: Final[str] = "/api/v1/runtimes"


class DeploymentModelNotFound(LookupError):
    """The requested Model definition does not exist."""


class DeploymentGpuInventoryUnavailable(RuntimeError):
    """The node GPU inventory cannot be used to resolve an automatic limit."""


@dataclass(frozen=True, slots=True)
class DeploymentRuntimeOption:
    """The public subset of one Runtime's model-specific launch policy."""

    name: str
    version: str
    context_lengths: tuple[int, ...]
    default_context_length: int | None
    certified_gpu_counts: tuple[int, ...]
    max_gpus_per_instance: int


@dataclass(frozen=True, slots=True)
class ModelDeploymentOptions:
    model_id: UUID
    model_context_limit: int | None
    runtimes: tuple[DeploymentRuntimeOption, ...]


def _derive_runtime_option(
    runtime: RuntimeCompatibility,
    *,
    model_context_limit: int | None,
    discovered_gpu_count: int,
) -> DeploymentRuntimeOption:
    allowed = runtime.context_policy.allowed_steps
    if model_context_limit is not None:
        allowed = tuple(step for step in allowed if step <= model_context_limit)
    if not allowed:
        default = None
    elif model_context_limit is not None and runtime.context_policy.default > model_context_limit:
        default = allowed[-1]
    else:
        default = runtime.context_policy.default
    return DeploymentRuntimeOption(
        name=runtime.name,
        version=runtime.version,
        context_lengths=allowed,
        default_context_length=default,
        certified_gpu_counts=tuple(sorted(runtime.gpu_policy.certified_gpu_counts)),
        max_gpus_per_instance=(
            discovered_gpu_count
            if runtime.gpu_policy.max_gpus_per_instance == "auto"
            else min(runtime.gpu_policy.max_gpus_per_instance, discovered_gpu_count)
        ),
    )


def derive_model_deployment_options(
    model: ModelRecord,
    catalog: RuntimeCompatibilityCatalog,
    *,
    discovered_gpu_count: int,
) -> ModelDeploymentOptions:
    """Derive the complete browser policy without exposing launch internals."""

    supported = None if model.supported_runtimes is None else frozenset(model.supported_runtimes)
    runtimes = tuple(
        _derive_runtime_option(
            runtime,
            model_context_limit=model.context_length,
            discovered_gpu_count=discovered_gpu_count,
        )
        for runtime in catalog.runtimes.values()
        if supported is None or runtime.name in supported
    )
    return ModelDeploymentOptions(
        model_id=model.id,
        model_context_limit=model.context_length,
        runtimes=runtimes,
    )


class DeploymentOptionsService:
    """Read Model metadata and return its catalog-derived deployment policy."""

    __slots__ = ("_agent", "_catalog", "_session_factory")

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory,
        catalog: RuntimeCompatibilityCatalog,
        agent: HostAgentClient,
    ) -> None:
        self._session_factory = session_factory
        self._catalog = catalog
        self._agent = agent

    async def get(self, model_id: UUID) -> ModelDeploymentOptions:
        async with self._session_factory() as session:
            model = await ModelRepository(session).get(model_id)
        if model is None:
            raise DeploymentModelNotFound()
        observation = await self._agent.observe()
        if (
            not isinstance(observation, AgentSnapshot)
            or observation.gpu_inventory.discovery_state != agent_pb2.GPU_DISCOVERY_STATE_READY
            or not observation.gpu_inventory.devices
        ):
            raise DeploymentGpuInventoryUnavailable()
        return derive_model_deployment_options(
            model, self._catalog, discovered_gpu_count=len(observation.gpu_inventory.devices)
        )


class DeploymentRuntimeOptionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    version: str
    context_lengths: tuple[Annotated[int, Field(gt=0)], ...]
    default_context_length: Annotated[int, Field(gt=0)] | None
    certified_gpu_counts: tuple[Annotated[int, Field(gt=0)], ...]
    max_gpus_per_instance: Annotated[int, Field(gt=0)]

    @classmethod
    def from_option(cls, option: DeploymentRuntimeOption) -> DeploymentRuntimeOptionResponse:
        return cls(
            name=option.name,
            version=option.version,
            context_lengths=option.context_lengths,
            default_context_length=option.default_context_length,
            certified_gpu_counts=option.certified_gpu_counts,
            max_gpus_per_instance=option.max_gpus_per_instance,
        )


class ModelDeploymentOptionsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model_context_limit: IdentifiedValue[int] | UnrecognizedValue
    runtimes: tuple[DeploymentRuntimeOptionResponse, ...]

    @classmethod
    def from_options(cls, options: ModelDeploymentOptions) -> ModelDeploymentOptionsResponse:
        context_limit: IdentifiedValue[int] | UnrecognizedValue
        if options.model_context_limit is None:
            context_limit = UnrecognizedValue()
        else:
            context_limit = IdentifiedValue[int](value=options.model_context_limit)
        return cls(
            model_context_limit=context_limit,
            runtimes=tuple(
                DeploymentRuntimeOptionResponse.from_option(option) for option in options.runtimes
            ),
        )


async def handle_deployment_model_not_found(
    request: Request,
    exception: Exception,
) -> JSONResponse:
    if not isinstance(exception, DeploymentModelNotFound):
        raise exception
    return _error_response(
        request,
        status_code=404,
        code="model_not_found",
        message="模型不存在，部署选项未读取，请刷新模型目录",
    )


def create_deployment_options_router(service: DeploymentOptionsService) -> APIRouter:
    router = APIRouter(prefix=RUNTIMES_PATH, tags=["runtimes"])

    @router.get("")
    @capability_route(Capability.MANAGE_MODEL_INSTANCES)
    async def get_deployment_options(
        request: Request,
        model_id: Annotated[UUID, Query()],
    ) -> JSONResponse:
        try:
            options = await service.get(model_id)
        except DeploymentGpuInventoryUnavailable:
            return _error_response(
                request,
                status_code=503,
                code="gpu_inventory_unavailable",
                message="设备清单暂不可用，部署选项未读取，请稍后重试",
            )
        return json_response(
            request_id=str(current_request_id(request)),
            content=ModelDeploymentOptionsResponse.from_options(options).model_dump(mode="json"),
        )

    return router


__all__ = [
    "RUNTIMES_PATH",
    "DeploymentModelNotFound",
    "DeploymentOptionsService",
    "DeploymentRuntimeOption",
    "ModelDeploymentOptions",
    "ModelDeploymentOptionsResponse",
    "create_deployment_options_router",
    "derive_model_deployment_options",
    "handle_deployment_model_not_found",
]

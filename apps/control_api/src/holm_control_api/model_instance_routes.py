"""Administrator API for Model Instances and GPU reservations."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Query
from holm_domain import Capability, ModelInstanceStatus, RuntimeType
from holm_domain.instance_names import InvalidInstanceName, normalize_instance_name
from holm_domain.model_instances import ModelInstanceEventType
from holm_persistence import ModelInstanceRecord
from holm_persistence.model_instance_repository import ModelInstanceNameConflict
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.config import JsonDict
from pydantic.json_schema import SkipJsonSchema
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from holm_control_api.auth_routes import json_response
from holm_control_api.authorization import (
    capability_route,
    current_authenticated_session,
    current_request_id,
)
from holm_control_api.model_catalog_routes import _error_response
from holm_control_api.model_instance_service import (
    CreateModelInstance,
    ModelInstanceArtifactNotReady,
    ModelInstanceEventPage,
    ModelInstanceGpuInventoryUnavailable,
    ModelInstanceInvalidConfiguration,
    ModelInstanceInvalidState,
    ModelInstanceLogCursorInvalid,
    ModelInstanceLogPage,
    ModelInstanceLogsUnavailable,
    ModelInstanceNotFound,
    ModelInstanceReferenced,
    ModelInstanceService,
)

INSTANCES_PATH: Final[str] = "/api/v1/instances"


InstanceName = Annotated[
    str,
    Field(
        min_length=2,
        max_length=64,
        description=(
            "Administrator display name; trim surrounding whitespace; "
            "exact, system-wide unique value; no NUL."
        ),
    ),
]


def _omit_name_default(schema: JsonDict) -> None:
    # None is the internal omission sentinel, not an accepted JSON value.
    schema.pop("default", None)


class CreateModelInstanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: InstanceName | SkipJsonSchema[None] = Field(
        default=None, json_schema_extra=_omit_name_default
    )
    artifact_id: UUID

    runtime_type: RuntimeType = RuntimeType.VLLM
    gpu_uuids: Annotated[tuple[str, ...], Field(min_length=1)]
    tensor_parallel_size: Annotated[int, Field(gt=0)]
    context_length: Annotated[int, Field(gt=0)]
    gpu_memory_utilization: Annotated[float, Field(gt=0, le=1)]

    @field_validator("name", mode="before")
    @classmethod
    def validate_name(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("instance name must be a string")
        return normalize_instance_name(value)

    @model_validator(mode="after")
    def validate_gpu_selection(self) -> CreateModelInstanceRequest:
        if self.tensor_parallel_size != len(self.gpu_uuids):
            raise ValueError("tensor_parallel_size must match GPU count")
        if len(set(self.gpu_uuids)) != len(self.gpu_uuids):
            raise ValueError("GPU UUIDs must be unique")
        if any(not value or value != value.strip() for value in self.gpu_uuids):
            raise ValueError("GPU UUIDs must be non-empty normalized strings")
        return self

    def to_service_value(self) -> CreateModelInstance:
        return CreateModelInstance(
            name=self.name,
            artifact_id=self.artifact_id,
            runtime_type=self.runtime_type,
            gpu_uuids=self.gpu_uuids,
            tensor_parallel_size=self.tensor_parallel_size,
            context_length=self.context_length,
            gpu_memory_utilization=self.gpu_memory_utilization,
        )


class SelectModelInstanceGpusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    gpu_uuids: Annotated[tuple[str, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_gpu_selection(self) -> SelectModelInstanceGpusRequest:
        if len(set(self.gpu_uuids)) != len(self.gpu_uuids):
            raise ValueError("GPU UUIDs must be unique")
        if any(not value or value != value.strip() for value in self.gpu_uuids):
            raise ValueError("GPU UUIDs must be non-empty normalized strings")
        return self


class ModelInstanceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    artifact_id: UUID
    runtime_type: RuntimeType
    gpu_uuids: tuple[str, ...]
    tensor_parallel_size: int
    context_length: int
    gpu_memory_utilization: float
    internal_model_name: str
    name: InstanceName
    status: ModelInstanceStatus
    error_code: str | None
    internal_endpoint: str | None
    config_version: int
    restart_count: Annotated[int, Field(ge=0)]
    updated_at: datetime

    @classmethod
    def from_record(cls, record: ModelInstanceRecord) -> ModelInstanceResponse:
        return cls(
            id=record.id,
            artifact_id=record.artifact_id,
            runtime_type=record.runtime_type,
            gpu_uuids=record.expected_gpu_uuids,
            tensor_parallel_size=record.tensor_parallel_size,
            context_length=record.context_length,
            gpu_memory_utilization=record.gpu_memory_utilization,
            internal_model_name=record.internal_model_name,
            name=record.name,
            status=record.status,
            error_code=record.error_code,
            internal_endpoint=record.internal_endpoint,
            config_version=record.config_version,
            restart_count=record.restart_count,
            updated_at=record.updated_at,
        )


class ModelInstanceLogEntryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: Annotated[int, Field(ge=0)]
    text: str
    truncated: bool
    original_character_count: Annotated[int, Field(ge=0)]


class ModelInstanceLogPageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entries: tuple[ModelInstanceLogEntryResponse, ...]
    next_cursor: str | None

    @classmethod
    def from_page(cls, page: ModelInstanceLogPage) -> ModelInstanceLogPageResponse:
        return cls(
            entries=tuple(
                ModelInstanceLogEntryResponse(
                    sequence=entry.sequence,
                    text=entry.text,
                    truncated=entry.truncated,
                    original_character_count=entry.original_character_count,
                )
                for entry in page.entries
            ),
            next_cursor=page.next_cursor,
        )


class ModelInstanceEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    event_type: ModelInstanceEventType
    from_status: ModelInstanceStatus | None
    to_status: ModelInstanceStatus | None
    occurred_at: datetime
    actor: Literal["admin", "controller", "system"]
    detail: dict[str, str | int | bool | None]


class ModelInstanceEventPageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[ModelInstanceEventResponse, ...]
    next_cursor: str | None

    @classmethod
    def from_page(cls, page: ModelInstanceEventPage) -> ModelInstanceEventPageResponse:
        return cls(
            items=tuple(
                ModelInstanceEventResponse(
                    id=event.id,
                    event_type=event.event_type,
                    from_status=event.from_status,
                    to_status=event.to_status,
                    occurred_at=event.occurred_at,
                    actor=event.actor,
                    detail={},
                )
                for event in page.items
            ),
            next_cursor=page.next_cursor,
        )


async def handle_model_instance_error(request: Request, exception: Exception) -> JSONResponse:
    if isinstance(exception, InvalidInstanceName):
        return _error_response(
            request,
            status_code=422,
            code="invalid_instance_name",
            message="实例名称无效，模型实例未创建，请输入2～64个字符的名称",
        )
    if isinstance(exception, ModelInstanceNameConflict):
        return _error_response(
            request,
            status_code=409,
            code="instance_name_conflict",
            message="实例名称已被使用，模型实例未创建，请修改名称后重试",
        )
    if isinstance(exception, ModelInstanceNotFound):
        return _error_response(
            request,
            status_code=404,
            code="instance_not_found",
            message="模型实例不存在，操作未执行，请刷新实例列表",
        )
    if isinstance(exception, ModelInstanceArtifactNotReady):
        return _error_response(
            request,
            status_code=409,
            code="artifact_not_ready",
            message="本地模型尚未就绪，模型实例未创建，请先完成本地模型准备",
        )
    if isinstance(exception, ModelInstanceInvalidConfiguration):
        messages = {
            "context_length_not_allowed": (
                "上下文长度不在可用档位中，模型实例未创建，请选择可用档位"
            ),
            "gpu_count_exceeds_maximum": (
                "所选设备数量超过单实例上限，模型实例未创建，请减少所选设备"
            ),
            "gpu_count_mismatch": "并行数量与所选设备数量不一致，模型实例未创建，请重新选择设备",
            "invalid_tensor_parallel_size": (
                "模型结构无法按所选并行数量切分，模型实例未创建，请调整并行数量"
            ),
            "runtime_not_supported": "所选运行环境不可用，模型实例未创建，请重新选择运行环境",
        }
        code = exception.code
        return _error_response(
            request,
            status_code=422 if code in messages else 400,
            code=code if code in messages else "invalid_request",
            message=messages.get(
                code,
                "模型实例配置无效，操作未执行，请检查设备与并行数量",
            ),
        )
    if isinstance(exception, ModelInstanceInvalidState):
        return _error_response(
            request,
            status_code=409,
            code="invalid_instance_state",
            message="模型实例当前状态不允许此操作，请刷新状态后重试",
        )
    if isinstance(exception, ModelInstanceGpuInventoryUnavailable):
        return _error_response(
            request,
            status_code=503,
            code="gpu_inventory_unavailable",
            message="GPU 信息暂不可用，模型实例操作未执行，请稍后重试",
        )
    if isinstance(exception, ModelInstanceReferenced):
        return _error_response(
            request,
            status_code=409,
            code="instance_referenced",
            message="模型实例仍被 AI 服务使用，无法删除，请先更换服务绑定",
        )
    if isinstance(exception, ModelInstanceLogCursorInvalid):
        return _error_response(
            request,
            status_code=400,
            code="invalid_log_cursor",
            message="日志游标无效，当前页面未加载，请从第一页重新查看",
        )
    if isinstance(exception, ModelInstanceLogsUnavailable):
        return _error_response(
            request,
            status_code=503,
            code="instance_logs_unavailable",
            message="模型实例日志暂不可用，当前页面未加载，请稍后重试",
        )
    raise exception


def _response(
    request: Request, record: ModelInstanceRecord, *, status_code: int = 200
) -> JSONResponse:
    return json_response(
        status_code=status_code,
        request_id=str(current_request_id(request)),
        content=ModelInstanceResponse.from_record(record).model_dump(mode="json"),
    )


def create_model_instance_router(service: ModelInstanceService) -> APIRouter:
    router = APIRouter(prefix=INSTANCES_PATH, tags=["instances"])

    @router.get("")
    @capability_route(Capability.MANAGE_MODEL_INSTANCES)
    async def list_instances(request: Request) -> JSONResponse:
        records = await service.list()
        return json_response(
            request_id=str(current_request_id(request)),
            content={
                "items": [
                    ModelInstanceResponse.from_record(record).model_dump(mode="json")
                    for record in records
                ]
            },
        )

    @router.get("/{instance_id}/logs")
    @capability_route(Capability.MANAGE_MODEL_INSTANCES)
    async def get_instance_logs(
        request: Request,
        instance_id: UUID,
        cursor: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
        page_size: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> JSONResponse:
        page = await service.logs(instance_id, cursor=cursor, page_size=page_size)
        return json_response(
            request_id=str(current_request_id(request)),
            content=ModelInstanceLogPageResponse.from_page(page).model_dump(mode="json"),
        )

    @router.get("/{instance_id}/events")
    @capability_route(Capability.MANAGE_MODEL_INSTANCES)
    async def get_instance_events(
        request: Request,
        instance_id: UUID,
        cursor: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> JSONResponse:
        page = await service.events(instance_id, cursor=cursor, page_size=page_size)
        return json_response(
            request_id=str(current_request_id(request)),
            content=ModelInstanceEventPageResponse.from_page(page).model_dump(mode="json"),
        )

    @router.get("/{instance_id}")
    @capability_route(Capability.MANAGE_MODEL_INSTANCES)
    async def get_instance(request: Request, instance_id: UUID) -> JSONResponse:
        return _response(request, await service.get(instance_id))

    @router.post("", status_code=201)
    @capability_route(Capability.MANAGE_MODEL_INSTANCES)
    async def create_instance(
        request: Request,
        body: CreateModelInstanceRequest,
    ) -> JSONResponse:
        return _response(request, await service.create(body.to_service_value()), status_code=201)

    @router.post("/{instance_id}:stop")
    @capability_route(Capability.MANAGE_MODEL_INSTANCES)
    async def stop_instance(request: Request, instance_id: UUID) -> JSONResponse:
        return _response(request, await service.stop(instance_id))

    @router.post("/{instance_id}:start")
    @capability_route(Capability.MANAGE_MODEL_INSTANCES)
    async def start_instance(request: Request, instance_id: UUID) -> JSONResponse:
        return _response(request, await service.start(instance_id))

    @router.post("/{instance_id}:restart")
    @capability_route(Capability.MANAGE_MODEL_INSTANCES)
    async def restart_instance(request: Request, instance_id: UUID) -> JSONResponse:
        return _response(request, await service.restart(instance_id))

    @router.post("/{instance_id}:retry")
    @capability_route(Capability.MANAGE_MODEL_INSTANCES)
    async def retry_instance(request: Request, instance_id: UUID) -> JSONResponse:
        return _response(request, await service.retry(instance_id))

    @router.post("/{instance_id}:select-gpus")
    @capability_route(Capability.MANAGE_MODEL_INSTANCES)
    async def select_instance_gpus(
        request: Request,
        instance_id: UUID,
        body: SelectModelInstanceGpusRequest,
    ) -> JSONResponse:
        return _response(request, await service.select_gpus(instance_id, body.gpu_uuids))

    @router.post("/{instance_id}:cancel", status_code=204)
    @capability_route(Capability.MANAGE_MODEL_INSTANCES)
    async def cancel_instance(request: Request, instance_id: UUID) -> Response:
        await service.cancel(
            instance_id,
            actor_id=current_authenticated_session(request).user.id,
            request_id=current_request_id(request),
        )
        return Response(status_code=204)

    @router.delete("/{instance_id}", status_code=204)
    @capability_route(Capability.MANAGE_MODEL_INSTANCES)
    async def delete_instance(
        request: Request, instance_id: UUID, delete_bound_services: bool = False
    ) -> Response:
        await service.delete(
            instance_id,
            delete_bound_services=delete_bound_services,
            actor_id=current_authenticated_session(request).user.id,
            request_id=current_request_id(request),
        )
        return Response(status_code=204)

    return router


__all__ = [
    "INSTANCES_PATH",
    "CreateModelInstanceRequest",
    "SelectModelInstanceGpusRequest",
    "ModelInstanceEventPageResponse",
    "ModelInstanceEventResponse",
    "ModelInstanceLogEntryResponse",
    "ModelInstanceLogPageResponse",
    "ModelInstanceResponse",
    "create_model_instance_router",
    "handle_model_instance_error",
]

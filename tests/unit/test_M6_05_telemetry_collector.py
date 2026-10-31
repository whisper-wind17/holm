"""M6-05 collector extraction, Target lifecycle, and production composition tests."""

from __future__ import annotations

import ast
import asyncio
from dataclasses import fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from fixtures.asgi_client import NO_CONFIGURED_CREDENTIAL_ROWS, AsgiTestClient
from holm_contracts import VersionPayload
from holm_contracts.agent.v1 import agent_pb2
from holm_control_api import DependencyCheck, ReadinessReason
from holm_control_api import application as application_module
from holm_control_api.agent_client import HostAgentClient
from holm_control_api.ai_service_service import AiServiceManagementService
from holm_control_api.model_instance_service import ModelInstanceService
from holm_control_api.telemetry_service import TelemetryService
from holm_domain import ModelInstanceStatus, RuntimeType
from holm_persistence import AiServiceRecord, AsyncSessionFactory, ModelInstanceRecord
from holm_telemetry import (
    LEGACY_VLLM_METRIC_MAPPING,
    RingBuffer,
    RuntimeMetricMapping,
    TelemetryCollector,
    TelemetryStatus,
)

NOW = datetime(2026, 9, 21, tzinfo=UTC)
INSTANCE_ID = UUID("00000000-0000-7000-8000-000000006005")
ARTIFACT_ID = UUID("00000000-0000-7000-8000-000000006006")


def _test_metric_mapping_resolver(_runtime_type: str) -> RuntimeMetricMapping:
    return LEGACY_VLLM_METRIC_MAPPING


class _GpuSource:
    async def observe_gpus(self) -> agent_pb2.ListGPUsResponse:
        return agent_pb2.ListGPUsResponse(discovery_state=agent_pb2.GPU_DISCOVERY_STATE_READY)


class _Instances:
    def __init__(self, record: ModelInstanceRecord) -> None:
        self.record = record

    async def list(self) -> tuple[ModelInstanceRecord, ...]:
        return (self.record,)


class _Services:
    async def list(self) -> tuple[AiServiceRecord, ...]:
        return ()


def _instance(status: ModelInstanceStatus) -> ModelInstanceRecord:
    return ModelInstanceRecord(
        name="FX-M3-10B instance",
        id=INSTANCE_ID,
        artifact_id=ARTIFACT_ID,
        runtime_type=RuntimeType.VLLM,
        expected_gpu_uuids=("GPU-M6-05",),
        tensor_parallel_size=1,
        context_length=4096,
        gpu_memory_utilization=0.9,
        internal_model_name="mi_m605",
        status=status,
        error_code=None,
        internal_endpoint="127.0.0.1:8605",
        config_version=1,
        created_at=NOW,
        updated_at=NOW,
    )


def test_M6_05_target_lifecycle_stops_sampling_and_preserves_existing_series() -> None:
    instances = _Instances(_instance(ModelInstanceStatus.RUNNING))
    sampled_endpoints: list[str] = []

    async def read_metrics(endpoint: str) -> bytes:
        sampled_endpoints.append(endpoint)
        return b"vllm:num_requests_running 1\nvllm:num_requests_waiting 0\n"

    collector = TelemetryCollector(
        gpus=_GpuSource(),
        instances=instances,
        ai_services=_Services(),
        metrics_reader=read_metrics,
        metric_mapping_resolver=_test_metric_mapping_resolver,
        interval_seconds=30,
        jitter_seconds=0,
    )

    async def scenario() -> tuple[int, int, TelemetryStatus]:
        await collector.collect_once()
        first_count = len(collector.store.instance(INSTANCE_ID, now_monotonic=0).series)
        instances.record = replace(instances.record, status=ModelInstanceStatus.STOPPED)
        await collector.collect_once()
        stopped_count = len(collector.store.instance(INSTANCE_ID, now_monotonic=0).series)
        instances.record = replace(instances.record, status=ModelInstanceStatus.RUNNING)
        await collector.collect_once()
        resumed = collector.store.instance(INSTANCE_ID, now_monotonic=0)
        return first_count, stopped_count, resumed.telemetry_status

    first_count, stopped_count, resumed_status = asyncio.run(scenario())

    assert sampled_endpoints == ["127.0.0.1:8605", "127.0.0.1:8605"]
    assert (first_count, stopped_count) == (1, 1)
    assert len(collector.store.instance(INSTANCE_ID, now_monotonic=0).series) == 2
    assert resumed_status is TelemetryStatus.AVAILABLE


def test_M6_05_missing_samples_remain_unknown_instead_of_zero() -> None:
    collector = TelemetryCollector(
        gpus=_GpuSource(),
        instances=_Instances(_instance(ModelInstanceStatus.STOPPED)),
        ai_services=_Services(),
        metric_mapping_resolver=_test_metric_mapping_resolver,
    )

    snapshot = collector.store.instance(INSTANCE_ID, now_monotonic=0)

    assert snapshot.telemetry_status is TelemetryStatus.UNAVAILABLE
    assert all(getattr(snapshot.current, field.name) is None for field in fields(snapshot.current))


def test_M6_05_package_ring_buffer_retains_exactly_720_points() -> None:
    series = RingBuffer[int]()
    for value in range(721):
        series.append(value)

    assert len(series) == 720
    assert series.values() == tuple(range(1, 721))


def test_M6_05_package_has_no_fastapi_or_control_api_imports() -> None:
    source_root = Path(__file__).resolve().parents[2] / "packages" / "telemetry" / "src"
    imported_roots: set[str] = set()
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", 1)[0])

    assert "fastapi" not in imported_roots
    assert "holm_control_api" not in imported_roots


async def _readiness_ok() -> ReadinessReason:
    return ReadinessReason.OK


class _LifecycleTelemetry:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1


def test_M6_05_application_assembles_and_starts_production_collector(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    lifecycle = _LifecycleTelemetry()
    assembled: dict[str, object] = {}

    def assemble(**kwargs):  # type: ignore[no-untyped-def]
        assembled.update(kwargs)
        return cast(TelemetryService, lifecycle)

    monkeypatch.setattr(application_module, "TelemetryService", assemble)
    version = VersionPayload(
        build_version="1.0.0+m605",
        commit="6" * 40,
        schema_version=1,
        runtime_compat_hash="5" * 64,
    )
    agent = cast(HostAgentClient, object())
    instances = cast(ModelInstanceService, object())
    services = cast(AiServiceManagementService, object())
    session_factory = cast(AsyncSessionFactory, object())
    app = application_module.create_app(
        version_payload=version,
        readiness_checks=[DependencyCheck(name="database", probe=_readiness_ok)],
        credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
        model_instances=instances,
        ai_services=services,
        telemetry_agent=agent,
        telemetry_session_factory=session_factory,
    )

    async def scenario() -> None:
        async with AsgiTestClient(app).lifespan():
            assert lifecycle.started == 1

    asyncio.run(scenario())

    assert lifecycle.stopped == 1
    assert assembled["agent"] is agent
    assert assembled["instances"] is instances
    assert assembled["ai_services"] is services
    assert assembled["session_factory"] is session_factory
    assert assembled["build_version"] == version.build_version


def test_M6_05_application_rejects_partial_production_assembly() -> None:
    version = VersionPayload(
        build_version="1.0.0+m605",
        commit="6" * 40,
        schema_version=1,
        runtime_compat_hash="5" * 64,
    )

    with pytest.raises(ValueError, match="requires Agent, database, instances, and services"):
        application_module.create_app(
            version_payload=version,
            readiness_checks=[DependencyCheck(name="database", probe=_readiness_ok)],
            credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
            telemetry_agent=cast(HostAgentClient, object()),
        )

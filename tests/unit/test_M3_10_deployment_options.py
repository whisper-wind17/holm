"""M3-10 deployment-policy derivation, authorization and early rejection."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, cast
from uuid import UUID

import pytest
from fixtures.asgi_client import NO_CONFIGURED_CREDENTIAL_ROWS, AsgiTestClient
from holm_contracts import VersionPayload
from holm_control_api import (
    AuthenticatedSession,
    AuthenticationService,
    DependencyCheck,
    ReadinessReason,
    create_app,
)
from holm_control_api.agent_client import HostAgentClient
from holm_control_api.deployment_options import (
    DeploymentOptionsService,
    ModelDeploymentOptions,
    derive_model_deployment_options,
)
from holm_control_api.model_instance_service import (
    CreateModelInstance,
    ModelInstanceInvalidConfiguration,
    ModelInstanceService,
)
from holm_domain import MetadataStatus, ModelOriginType, RuntimeType, UserRole, UserStatus
from holm_persistence import (
    AsyncSessionFactory,
    AuditEventDraft,
    AuditEventRecord,
    ModelRecord,
    SessionRecord,
    UserRecord,
)
from holm_runtimes import RuntimeCompatibilityCatalog, load_runtime_compatibility
from sqlalchemy.ext.asyncio import AsyncSession

NOW: Final = datetime(2026, 9, 21, tzinfo=UTC)
MODEL_ID: Final = UUID("30000000-0000-4000-8000-000000000010")
ARTIFACT_ID: Final = UUID("30000000-0000-4000-8000-000000000011")
USER_ID: Final = UUID("30000000-0000-4000-8000-000000000012")


def _catalog() -> RuntimeCompatibilityCatalog:
    return load_runtime_compatibility(Path("runtime-compat.yaml"))


def test_FIX_08_deployment_options_requires_agent_at_construction() -> None:
    with pytest.raises(TypeError, match="agent"):
        DeploymentOptionsService(
            session_factory=cast(AsyncSessionFactory, None),
            catalog=_catalog(),
        )


def _model(context_length: int | None) -> ModelRecord:
    return ModelRecord(
        id=MODEL_ID,
        source_id=None,
        source_type=ModelOriginType.MODELSCOPE,
        source_key="m3-10/model",
        display_name="M3-10 model",
        repository_id="m3-10/model",
        external_path=None,
        architecture="Qwen2ForCausalLM",
        modalities=("text",),
        precision=None,
        quantization=None,
        context_length=context_length,
        supported_runtimes=("vllm",),
        estimated_size_bytes=1024,
        license=None,
        metadata_status=MetadataStatus.CONFIRMED,
        created_at=NOW,
        updated_at=NOW,
    )


def test_M3_10_TC_UI_15_derives_context_steps_default_and_unknown_from_catalog() -> None:
    bounded = derive_model_deployment_options(_model(8192), _catalog(), discovered_gpu_count=8)
    assert bounded.runtimes[0].max_gpus_per_instance == 8
    assert bounded.runtimes[0].certified_gpu_counts == (1, 2, 3, 4, 5, 6, 7, 8)
    assert bounded.runtimes[0].context_lengths == (2048, 4096, 8192)
    assert bounded.runtimes[0].default_context_length == 4096

    lowered_default = derive_model_deployment_options(
        _model(3000), _catalog(), discovered_gpu_count=8
    )
    assert lowered_default.runtimes[0].context_lengths == (2048,)
    assert lowered_default.runtimes[0].default_context_length == 2048

    unknown = derive_model_deployment_options(_model(None), _catalog(), discovered_gpu_count=8)
    runtime = _catalog().runtime("vllm")
    assert runtime is not None
    assert unknown.model_context_limit is None
    assert unknown.runtimes[0].context_lengths == runtime.context_policy.allowed_steps


def test_FIX_08_TC_GPU_03_auto_limit_uses_discovered_count_without_certification_gate() -> None:
    options = derive_model_deployment_options(_model(None), _catalog(), discovered_gpu_count=3)
    assert options.runtimes[0].max_gpus_per_instance == 3
    assert options.runtimes[0].certified_gpu_counts == (1, 2, 3, 4, 5, 6, 7, 8)


def test_M3_10_catalog_step_change_changes_derived_options_positive_control(tmp_path: Path) -> None:
    payload = json.loads(Path("runtime-compat.yaml").read_text(encoding="utf-8"))
    payload["runtimes"][0]["context_policy"]["allowed_steps"][0] = 1024
    changed_path = tmp_path / "runtime-compat.json"
    changed_path.write_text(json.dumps(payload), encoding="utf-8")

    changed = derive_model_deployment_options(
        _model(8192), load_runtime_compatibility(changed_path), discovered_gpu_count=8
    )

    assert changed.runtimes[0].context_lengths == (1024, 4096, 8192)


async def _readiness_ok() -> ReadinessReason:
    return ReadinessReason.OK


class _AuditWriter:
    async def add(self, session: AsyncSession, event: AuditEventDraft) -> AuditEventRecord:
        raise AssertionError("authorization test has no business transaction")

    async def write(self, event: AuditEventDraft) -> AuditEventRecord:
        return cast(AuditEventRecord, object())


class _Authenticator:
    def __init__(self, role: UserRole) -> None:
        user = UserRecord(
            id=USER_ID,
            username_norm=f"m3-10-{role.value}",
            display_name="M3-10 subject",
            role=role,
            status=UserStatus.ACTIVE,
            must_change_password=False,
            created_at=NOW,
            updated_at=NOW,
            deleted_at=None,
            password_hash="$argon2id$not-returned",
        )
        self.authenticated = AuthenticatedSession(
            user=user,
            session=SessionRecord(
                id=UUID("30000000-0000-4000-8000-000000000013"),
                user_id=user.id,
                session_hash="a" * 64,
                csrf_hash="b" * 64,
                created_at=NOW,
                last_seen_at=NOW,
                expires_at=NOW + timedelta(hours=1),
                revoked_at=None,
            ),
        )
        self.audit_writer = _AuditWriter()

    async def authenticate_session(
        self,
        *,
        session_token: str | None,
        csrf_token: str | None = None,
        require_csrf: bool = False,
    ) -> AuthenticatedSession:
        return self.authenticated


class _DeploymentOptions:
    def __init__(self) -> None:
        self.calls = 0

    async def get(self, model_id: UUID) -> ModelDeploymentOptions:
        self.calls += 1
        return derive_model_deployment_options(_model(8192), _catalog(), discovered_gpu_count=8)


def _get_options(role: UserRole) -> tuple[int, bytes, int]:
    service = _DeploymentOptions()
    app = create_app(
        version_payload=VersionPayload(
            build_version="1.0.0+gm310",
            commit="1" * 40,
            schema_version=1,
            runtime_compat_hash="2" * 64,
        ),
        readiness_checks=[DependencyCheck(name="database", probe=_readiness_ok)],
        credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
        authentication=cast(AuthenticationService, _Authenticator(role)),
        deployment_options=cast(DeploymentOptionsService, service),
    )

    async def scenario() -> tuple[int, bytes]:
        client = AsgiTestClient(app)
        async with client.lifespan():
            response = await client.get(f"/api/v1/runtimes?model_id={MODEL_ID}")
            return response.status, response.body

    status, body = asyncio.run(scenario())
    return status, body, service.calls


def test_M3_10_runtime_policy_response_is_closed_and_redacts_launch_details() -> None:
    status, body, calls = _get_options(UserRole.ADMIN)
    payload = json.loads(body)

    assert status == 200
    assert calls == 1
    assert set(payload) == {"model_context_limit", "runtimes"}
    assert set(payload["runtimes"][0]) == {
        "certified_gpu_counts",
        "context_lengths",
        "default_context_length",
        "max_gpus_per_instance",
        "name",
        "version",
    }
    assert payload["model_context_limit"] == {"state": "identified", "value": 8192}
    assert b"/workspace/" not in body
    assert b"--tensor-parallel-size" not in body
    assert b"executable" not in body
    assert b"arguments" not in body


def test_M3_10_runtime_policy_rejects_normal_users_before_model_lookup() -> None:
    status, body, calls = _get_options(UserRole.USER)
    assert status == 403
    assert calls == 0
    assert str(MODEL_ID).encode() not in body


class _AgentMustNotRun:
    async def observe(self) -> object:
        raise AssertionError("policy rejection must precede GPU inventory")


def test_M3_10_control_plane_rejects_catalog_policy_before_runtime_start() -> None:
    service = ModelInstanceService(
        session_factory=cast(AsyncSessionFactory, None),
        agent=cast(HostAgentClient, _AgentMustNotRun()),
        runtime_catalog=_catalog(),
    )

    async def rejected(context_length: int, gpu_count: int) -> str:
        value = CreateModelInstance(
            artifact_id=ARTIFACT_ID,
            runtime_type=RuntimeType.VLLM,
            gpu_uuids=tuple(f"GPU-m3-10-{index}" for index in range(gpu_count)),
            tensor_parallel_size=gpu_count,
            context_length=context_length,
            gpu_memory_utilization=0.9,
        )
        try:
            await service.create(value)
        except ModelInstanceInvalidConfiguration as error:
            return error.code
        raise AssertionError("invalid launch policy was accepted")

    assert asyncio.run(rejected(12345, 1)) == "context_length_not_allowed"


def test_M3_10_create_api_returns_specific_policy_code_before_runtime_start() -> None:
    service = ModelInstanceService(
        session_factory=cast(AsyncSessionFactory, None),
        agent=cast(HostAgentClient, _AgentMustNotRun()),
        runtime_catalog=_catalog(),
    )
    app = create_app(
        version_payload=VersionPayload(
            build_version="1.0.0+gm310early",
            commit="1" * 40,
            schema_version=1,
            runtime_compat_hash="2" * 64,
        ),
        readiness_checks=[DependencyCheck(name="database", probe=_readiness_ok)],
        credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
        authentication=cast(AuthenticationService, _Authenticator(UserRole.ADMIN)),
        model_instances=service,
    )

    async def scenario() -> tuple[int, dict[str, object]]:
        client = AsgiTestClient(app)
        async with client.lifespan():
            response = await client.request(
                "POST",
                "/api/v1/instances",
                json_body={
                    "artifact_id": str(ARTIFACT_ID),
                    "runtime_type": "vllm",
                    "gpu_uuids": ["GPU-m3-10-0"],
                    "tensor_parallel_size": 1,
                    "context_length": 12345,
                    "gpu_memory_utilization": 0.9,
                },
            )
            return response.status, cast(dict[str, object], response.json())

    status, payload = asyncio.run(scenario())
    assert status == 422
    assert cast(dict[str, object], payload["error"])["code"] == ("context_length_not_allowed")

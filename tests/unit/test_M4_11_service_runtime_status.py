"""M4-11: one canonical four-state AI Service runtime-status mapping."""

from __future__ import annotations

from pathlib import Path

import pytest
from holm_domain import (
    AiServiceRuntimeStatus,
    ModelInstanceStatus,
    derive_ai_service_runtime_status,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("enabled", "instance_status", "expected"),
    (
        (False, ModelInstanceStatus.RUNNING, AiServiceRuntimeStatus.DISABLED),
        (True, ModelInstanceStatus.RUNNING, AiServiceRuntimeStatus.AVAILABLE),
        (True, ModelInstanceStatus.STARTING, AiServiceRuntimeStatus.DEGRADED),
        (True, ModelInstanceStatus.WAITING_FOR_RESOURCE, AiServiceRuntimeStatus.DEGRADED),
        (True, ModelInstanceStatus.RESTARTING, AiServiceRuntimeStatus.DEGRADED),
        (True, ModelInstanceStatus.UNHEALTHY, AiServiceRuntimeStatus.UNAVAILABLE),
        (True, ModelInstanceStatus.STOPPING, AiServiceRuntimeStatus.UNAVAILABLE),
        (True, ModelInstanceStatus.STOPPED, AiServiceRuntimeStatus.UNAVAILABLE),
        (True, ModelInstanceStatus.FAILED, AiServiceRuntimeStatus.UNAVAILABLE),
        (True, None, AiServiceRuntimeStatus.UNAVAILABLE),
    ),
)
def test_M4_11_tdd_12_9_four_state_mapping(
    enabled: bool,
    instance_status: ModelInstanceStatus | None,
    expected: AiServiceRuntimeStatus,
) -> None:
    assert (
        derive_ai_service_runtime_status(
            enabled=enabled,
            primary_instance_status=instance_status,
        )
        is expected
    )


def _assert_delegates_to_canonical_mapping(source: str) -> None:
    assert "derive_ai_service_runtime_status(" in source


def test_M4_11_three_backend_read_models_delegate_to_the_canonical_mapping() -> None:
    paths = (
        "apps/control_api/src/holm_control_api/ai_service_service.py",
        # M6-05 把遥测采集体从 apps/control_api/.../telemetry_service.py 搬到了
        # packages/telemetry；服务计数这个调用点跟着走，守卫跟到新位置，判定不变。
        "packages/telemetry/src/holm_telemetry/collector.py",
        "packages/persistence/src/holm_persistence/user_management_repository.py",
    )
    for path in paths:
        _assert_delegates_to_canonical_mapping((REPOSITORY_ROOT / path).read_text())


def test_M4_11_single_source_guard_fails_for_an_inline_status_comparison() -> None:
    with pytest.raises(AssertionError):
        _assert_delegates_to_canonical_mapping("available = status == 'Running'")

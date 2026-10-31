"""Canonical AI Service runtime-status derivation from its bound Model Instance."""

from __future__ import annotations

from enum import StrEnum

from holm_domain.model_instances import ModelInstanceStatus


class AiServiceRuntimeStatus(StrEnum):
    """Administrator-facing four-state AI Service status."""

    DISABLED = "Disabled"
    AVAILABLE = "Available"
    DEGRADED = "Degraded"
    UNAVAILABLE = "Unavailable"


def derive_ai_service_runtime_status(
    *,
    enabled: bool,
    primary_instance_status: ModelInstanceStatus | None,
) -> AiServiceRuntimeStatus:
    """Derive service status without telemetry or Gateway instantaneous signals."""

    if not enabled:
        return AiServiceRuntimeStatus.DISABLED
    if primary_instance_status is ModelInstanceStatus.RUNNING:
        return AiServiceRuntimeStatus.AVAILABLE
    if primary_instance_status in {
        ModelInstanceStatus.WAITING_FOR_RESOURCE,
        ModelInstanceStatus.STARTING,
        ModelInstanceStatus.RESTARTING,
    }:
        return AiServiceRuntimeStatus.DEGRADED
    if primary_instance_status is None or primary_instance_status in {
        ModelInstanceStatus.UNHEALTHY,
        ModelInstanceStatus.STOPPING,
        ModelInstanceStatus.STOPPED,
        ModelInstanceStatus.FAILED,
    }:
        return AiServiceRuntimeStatus.UNAVAILABLE
    raise AssertionError(f"unhandled Model Instance status: {primary_instance_status!r}")


__all__ = ["AiServiceRuntimeStatus", "derive_ai_service_runtime_status"]

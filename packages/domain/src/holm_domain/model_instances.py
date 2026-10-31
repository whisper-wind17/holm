"""Framework-independent vocabulary for Model Instance GPU reservations."""

from enum import StrEnum


class RuntimeType(StrEnum):
    """Runtime types implemented by the prototype instance data plane."""

    VLLM = "vllm"
    SGLANG = "sglang"


class ModelInstanceStatus(StrEnum):
    """Supported Model Instance lifecycle states."""

    WAITING_FOR_RESOURCE = "WaitingForResource"
    STARTING = "Starting"
    RUNNING = "Running"
    RESTARTING = "Restarting"
    UNHEALTHY = "Unhealthy"
    STOPPING = "Stopping"
    STOPPED = "Stopped"
    FAILED = "Failed"


class ModelInstanceEventType(StrEnum):
    """Allowlisted structured events exposed by the instance event feed."""

    STATUS_CHANGED = "StatusChanged"
    RESTART_REQUESTED = "RestartRequested"
    RETRY_REQUESTED = "RetryRequested"
    DEPLOYMENT_CANCELLED = "DeploymentCancelled"
    GPU_SELECTION_CHANGED = "GpuSelectionChanged"
    WAITING_FOR_GPU = "WaitingForGpu"


class GpuAllocationStatus(StrEnum):
    """Lifecycle of one GPU reservation owned by a Model Instance."""

    RESERVED = "Reserved"
    ALLOCATED = "Allocated"
    RELEASED = "Released"


__all__ = [
    "GpuAllocationStatus",
    "ModelInstanceEventType",
    "ModelInstanceStatus",
    "RuntimeType",
]

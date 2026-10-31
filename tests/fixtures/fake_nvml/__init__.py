"""Standard-library Fake NVML fixture for M0-06C.

All constructed device values and expectations are ``待真机复核`` and must be
reconciled with certified hardware behavior during the G1 test window.
"""

from .models import (
    FIXTURE_REVIEW_STATUS,
    DeploymentExpectation,
    ExpectedInstanceState,
    FakeNvml,
    FakeNvmlScenario,
    FixtureId,
    GpuDevice,
    GpuHealth,
    InitializationFailure,
    InstanceTransitionExpectation,
    NvmlSnapshot,
    NvmlTopologyLink,
    QueryFailure,
)
from .scenarios import scenario_for

__all__ = [
    "FIXTURE_REVIEW_STATUS",
    "DeploymentExpectation",
    "ExpectedInstanceState",
    "FakeNvml",
    "FakeNvmlScenario",
    "FixtureId",
    "GpuDevice",
    "GpuHealth",
    "InitializationFailure",
    "InstanceTransitionExpectation",
    "NvmlSnapshot",
    "NvmlTopologyLink",
    "QueryFailure",
    "scenario_for",
]

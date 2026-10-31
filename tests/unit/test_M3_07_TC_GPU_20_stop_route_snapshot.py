"""M3-07 / TC-GPU-20: Stopping instances are removed from inference routing."""

from __future__ import annotations

import asyncio
from uuid import UUID

from holm_domain import ModelInstanceStatus
from holm_gateway.routing import GatewayRoute, InferenceRouteDirectory


class _RouteDirectory:
    def __init__(self, route: GatewayRoute) -> None:
        self._route = route

    async def find(self, public_model_name: str) -> GatewayRoute | None:
        assert public_model_name == self._route.public_model_name
        return self._route

    async def list_enabled(self) -> tuple[GatewayRoute, ...]:
        return (self._route,)


def test_M3_07_TC_GPU_20_stopping_instance_is_not_ready_in_route_snapshot() -> None:
    route = GatewayRoute(
        service_id=UUID("45000000-0000-4000-8000-000000000307"),
        instance_id=UUID("43000000-0000-4000-8000-000000000307"),
        public_model_name="m3-07-model",
        enabled=True,
        instance_status=ModelInstanceStatus.STOPPING.value,
        runtime_name="vllm",
        internal_endpoint="127.0.0.1:8401",
        internal_model_name="mi_m3070000000",
    )

    selected = asyncio.run(InferenceRouteDirectory(_RouteDirectory(route)).find("m3-07-model"))

    assert selected is not None
    assert selected.ready is False

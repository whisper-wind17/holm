"""FX-UI-26: reuse PROTO-01 real PostgreSQL/API and PROTO-03 fake Runtime."""

from __future__ import annotations

import asyncio
from uuid import UUID

import sqlalchemy as sa
from conftest import EphemeralPostgres
from holm_domain import ModelInstanceStatus
from holm_instance_controller import PostgresControllerStore
from test_PROTO_01_model_instances import (
    ADMIN_USERNAME,
    GPU_0,
    MEMBER_USERNAME,
    READY_ARTIFACT_ID,
    _create_accounts,
    _create_body,
    _environment,
    _login,
    _migrated_instance_database,  # noqa: F401 -- reuse the autouse PG fixture
)
from test_PROTO_03_instance_controller_postgres import _controller, _RuntimeAgent


def test_UI_26_TC_GPU_09_restart_count_stop_permissions_and_allocation_release(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as environment:
            await _create_accounts(environment)
            admin = await _login(environment, ADMIN_USERNAME)
            member = await _login(environment, MEMBER_USERNAME)
            created = await environment.client.request(
                "POST",
                "/api/v1/instances",
                cookies=admin,
                json_body=_create_body(READY_ARTIFACT_ID, [GPU_0], 1),
            )
            assert created.status == 201
            assert created.json()["restart_count"] == 0
            instance_id = UUID(created.json()["id"])
            path = f"/api/v1/instances/{instance_id}"
            store = PostgresControllerStore(environment.session_factory)
            assert await store.mark_running(instance_id)
            restarted = await environment.client.request("POST", f"{path}:restart", cookies=admin)
            assert restarted.status == 200
            assert restarted.json()["status"] == "Restarting"
            assert restarted.json()["restart_count"] == 0
            for attempt in (1, 2):
                assert await store.record_restart_attempt(instance_id) == attempt
                detail = await environment.client.get(path, cookies=admin)
                listing = await environment.client.get("/api/v1/instances", cookies=admin)
                assert detail.status == listing.status == 200
                assert detail.json()["restart_count"] == attempt
                assert detail.json()["status"] == "Restarting"
                assert listing.json()["items"][0]["restart_count"] == attempt

            async def route_events() -> int:
                async with environment.session_factory() as session:
                    return int(
                        await session.scalar(
                            sa.text(
                                "SELECT count(*) FROM core.domain_events "
                                "WHERE aggregate_id = :id AND event = 'instance.route_changed'"
                            ),
                            {"id": instance_id},
                        )
                    )

            before = await route_events()
            for cookies, expected in ((None, 401), (member, 403)):
                denied = await environment.client.request("POST", f"{path}:stop", cookies=cookies)
                assert denied.status == expected
                assert await store.status(instance_id) is ModelInstanceStatus.RESTARTING
                assert await route_events() == before
            stopped = await environment.client.request("POST", f"{path}:stop", cookies=admin)
            assert stopped.status == 200
            assert stopped.json()["status"] == "Stopping"
            assert stopped.json()["restart_count"] == 2
            assert stopped.json()["internal_endpoint"] is None
            assert await route_events() == before + 1
            assert await store.record_restart_attempt(instance_id) is None
            assert not await store.mark_restarted_running(instance_id, endpoint="127.0.0.1:8123")
            assert not await store.mark_failed(
                instance_id,
                expected_status=ModelInstanceStatus.RESTARTING,
                error_code="runtime_restart_failed",
            )
            item = await store.claim(ModelInstanceStatus.STOPPING)
            assert item is not None
            agent = _RuntimeAgent()
            await _controller(store, agent).process_stopping(item)
            assert agent.stop_calls == [str(instance_id)]
            assert await store.status(instance_id) is ModelInstanceStatus.STOPPED
            async with environment.session_factory() as session:
                allocations = (
                    (
                        await session.execute(
                            sa.text(
                                "SELECT status, released_at FROM core.gpu_allocations "
                                "WHERE instance_id = :id"
                            ),
                            {"id": instance_id},
                        )
                    )
                    .mappings()
                    .all()
                )
            assert len(allocations) == 1
            assert allocations[0]["status"] == "Released"
            assert allocations[0]["released_at"] is not None
            detail = await environment.client.get(path, cookies=admin)
            assert detail.json()["restart_count"] == 2
            invalid = await environment.client.request("POST", f"{path}:stop", cookies=admin)
            assert invalid.status == 409
            assert invalid.json()["error"]["code"] == "invalid_instance_state"
            started = await environment.client.request("POST", f"{path}:start", cookies=admin)
            assert started.status == 200
            assert started.json()["restart_count"] == 0
            assert await store.mark_failed(
                instance_id,
                expected_status=ModelInstanceStatus.STARTING,
                error_code="runtime_start_failed",
            )
            invalid = await environment.client.request("POST", f"{path}:stop", cookies=admin)
            assert invalid.status == 409
            assert invalid.json()["error"]["code"] == "invalid_instance_state"

    asyncio.run(scenario())

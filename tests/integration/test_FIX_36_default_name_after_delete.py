"""FIX-36: FX-FIX-36 reuses PROTO-05 real-PG accounts and instances."""

from __future__ import annotations

import asyncio
from uuid import UUID

from conftest import EphemeralPostgres
from holm_domain import ModelInstanceStatus
from test_PROTO_05_services_api_keys import (
    ADMIN_USERNAME,
    INSTANCE_1,
    _create_accounts,
    _environment,
    _login,
    _migrated_database,  # noqa: F401 - imported pytest fixture
    _set_instance_status,
)


def test_FIX_36_default_name_reuses_deleted_and_preserves_active_fallback(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as environment:
            await _create_accounts(environment)
            admin = await _login(environment, ADMIN_USERNAME)
            await _set_instance_status(environment, INSTANCE_1, ModelInstanceStatus.RUNNING)
            public_model_name = "fix36-model"
            payload = {
                "public_model_name": public_model_name,
                "primary_instance_id": str(INSTANCE_1),
            }
            first = await environment.client.request(
                "POST", "/api/v1/services", cookies=admin, json_body=payload
            )
            assert first.status == 201
            assert first.json()["name"] == public_model_name
            await _set_instance_status(environment, INSTANCE_1, ModelInstanceStatus.STOPPED)
            deleted = await environment.client.request(
                "DELETE", f"/api/v1/services/{first.json()['id']}", cookies=admin
            )
            assert deleted.status == 204
            await _set_instance_status(environment, INSTANCE_1, ModelInstanceStatus.RUNNING)
            recreated = await environment.client.request(
                "POST", "/api/v1/services", cookies=admin, json_body=payload
            )
            assert recreated.status == 201
            assert recreated.json()["name"] == public_model_name
            assert recreated.json()["id"] != first.json()["id"]

            occupied_name = "fix36-occupied"
            occupant = await environment.client.request(
                "POST",
                "/api/v1/services",
                cookies=admin,
                json_body={**payload, "public_model_name": "fix36-other", "name": occupied_name},
            )
            assert occupant.status == 201
            assert occupant.json()["name"] == occupied_name
            fallback = await environment.client.request(
                "POST",
                "/api/v1/services",
                cookies=admin,
                json_body={**payload, "public_model_name": occupied_name},
            )
            assert fallback.status == 201
            assert fallback.json()["name"] == (
                occupied_name[:7] + "-" + UUID(fallback.json()["id"]).hex
            )

    asyncio.run(scenario())

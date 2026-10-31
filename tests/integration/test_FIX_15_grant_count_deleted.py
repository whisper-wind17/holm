"""FX-FIX-15: reuse PROTO-05 real PostgreSQL accounts, services and ASGI fixtures."""

from __future__ import annotations

import asyncio

import sqlalchemy as sa
from conftest import EphemeralPostgres
from holm_persistence.ai_services import AI_SERVICES, USER_SERVICE_PERMISSIONS
from holm_persistence.user_management_repository import UserManagementRepository
from migration_test_support import upgrade_head
from test_PROTO_05_services_api_keys import (
    ADMIN_USERNAME,
    INSTANCE_1,
    _create_accounts,
    _create_service,
    _environment,
    _login,
    _seed_instances,
)


def test_FIX_15_grant_counts_exclude_deleted_but_include_disabled_services(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    upgrade_head(ephemeral_postgres.engine)
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(
            sa.text("TRUNCATE core.ai_services, core.models, core.users, core.audit_events CASCADE")
        )
    _seed_instances(ephemeral_postgres.engine)

    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as env:
            ids = await _create_accounts(env)
            admin = await _login(env, ADMIN_USERNAME)
            user_id = ids[ADMIN_USERNAME]
            services = [
                await _create_service(env, admin, name=name, instance_id=INSTANCE_1)
                for name in ("fix15-active", "fix15-disabled", "fix15-deleted")
            ]
            grant = await env.client.request(
                "PUT",
                f"/api/v1/users/{user_id}/permissions",
                cookies=admin,
                json_body={"service_ids": [str(identity) for identity in services]},
            )
            assert grant.status == 200
            # Preserve all grants while reproducing the persisted soft-deletion state.
            async with env.session_factory() as session, session.begin():
                await session.execute(
                    sa.update(AI_SERVICES)
                    .where(AI_SERVICES.c.id == services[1])
                    .values(enabled=False)
                )
                await session.execute(
                    sa.update(AI_SERVICES)
                    .where(AI_SERVICES.c.id == services[2])
                    .values(enabled=False, deleted_at=sa.func.now())
                )

            listed = await env.client.get("/api/v1/users", cookies=admin)
            detail = await env.client.get(f"/api/v1/users/{user_id}", cookies=admin)
            profile = await env.client.get("/api/v1/me/profile", cookies=admin)
            personal = await env.client.get("/api/v1/me/services", cookies=admin)
            assert listed.status == detail.status == profile.status == personal.status == 200
            overview = next(item for item in listed.json()["items"] if item["id"] == str(user_id))
            assert overview["authorized_service_count"] == 2
            assert detail.json()["authorized_service_count"] == 2
            assert profile.json()["authorized_service_count"] == 2
            expected_ids = {str(identity) for identity in services[:2]}
            assert {item["id"] for item in detail.json()["authorized_services"]} == expected_ids
            assert {item["id"] for item in personal.json()["items"]} == expected_ids
            async with env.session_factory() as session:
                authorized = await UserManagementRepository(session).authorized_services(user_id)
                assert len(authorized) == 2
                assert {item.id for item in authorized} == set(services[:2])
                grants = (
                    await session.execute(
                        sa.select(USER_SERVICE_PERMISSIONS.c.service_id).where(
                            USER_SERVICE_PERMISSIONS.c.user_id == user_id
                        )
                    )
                ).scalars()
                assert set(grants) == set(services)

    asyncio.run(scenario())

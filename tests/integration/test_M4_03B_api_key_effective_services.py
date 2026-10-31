"""M4-03B: FX-M4-03B uses PROTO-05 accounts/services and isolated real PG.

Scope and permission snapshots are read-only assertions; service/backend fixture
updates represent administrator configuration and transient backend conditions.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from holm_control_api.api_key_service import ApiKeyManagementService
from holm_domain import ModelInstanceStatus, UserRole
from holm_persistence import (
    AI_SERVICE_BACKENDS,
    AI_SERVICES,
    API_KEY_SERVICE_SCOPES,
    API_KEYS,
    USER_SERVICE_PERMISSIONS,
    ApiKeyRepository,
    UserManagementRepository,
    create_session_factory,
)
from holm_persistence.role_grants import CONTROL
from migration_test_support import upgrade_head
from sqlalchemy.ext.asyncio import create_async_engine
from test_PROTO_05_services_api_keys import (
    ADMIN_USERNAME,
    INSTANCE_1,
    INSTANCE_2,
    MEMBER_USERNAME,
    _async_url,
    _create_accounts,
    _create_service,
    _environment,
    _login,
    _seed_instances,
    _set_instance_status,
)

PATH = "/api/v1/me/api-keys"
SAFE_KEY_FIELDS = {
    "id",
    "name",
    "display_prefix",
    "last4",
    "status",
    "expires_at",
    "last_used_at",
    "created_at",
    "revoked_at",
    "service_ids",
    "effective_services",
}


@pytest.fixture(autouse=True)
def _database(ephemeral_postgres: EphemeralPostgres) -> None:
    upgrade_head(ephemeral_postgres.engine)
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(
            sa.text(
                "TRUNCATE core.ai_services, core.models, core.users, "
                "core.audit_events, core.domain_events CASCADE"
            )
        )
    _seed_instances(ephemeral_postgres.engine)


def test_M4_03B_TC_GW_22_32_TC_SEC_08_12_permission_intersection_is_read_only(
    ephemeral_postgres: EphemeralPostgres,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as env:
            ids = await _create_accounts(env)
            admin = await _login(env, ADMIN_USERNAME)
            member = await _login(env, MEMBER_USERNAME)
            a = await _create_service(env, admin, name="public-alpha", instance_id=INSTANCE_1)
            b = await _create_service(env, admin, name="public-beta", instance_id=INSTANCE_2)
            async with env.session_factory() as session, session.begin():
                await UserManagementRepository(session).replace_for_user(
                    ids[MEMBER_USERNAME],
                    (a, b),
                    actor_id=ids[ADMIN_USERNAME],
                    now=datetime.now(UTC),
                )
            async with env.session_factory() as session, session.begin():
                await UserManagementRepository(session).replace_for_user(
                    ids[ADMIN_USERNAME], (a, b), actor_id=ids[ADMIN_USERNAME], now=datetime.now(UTC)
                )
            plaintexts: list[str] = []
            for cookies in (member, admin):
                for name, scope in (("all-grants", []), ("fixed-alpha", [str(a)])):
                    response = await env.client.request(
                        "POST",
                        PATH,
                        cookies=cookies,
                        json_body={"name": name, "service_ids": scope},
                    )
                    assert response.status == 201
                    plaintexts.append(response.json()["api_key"])

            async def assert_ranges(expected_all: list[UUID], expected_fixed: list[UUID]) -> None:
                # GET must not change keys, scopes, or permissions.
                async with env.session_factory() as session:
                    before = {
                        table.name: (await session.execute(sa.select(table))).all()
                        for table in (API_KEYS, API_KEY_SERVICE_SCOPES, USER_SERVICE_PERMISSIONS)
                    }
                for cookies, wanted in (
                    (member, {"all-grants": expected_all, "fixed-alpha": expected_fixed}),
                    (admin, {"all-grants": [a, b], "fixed-alpha": [a]}),
                ):
                    response = await env.client.get(PATH, cookies=cookies)
                    assert response.status == 200
                    items = response.json()["items"]
                    assert len(items) == 2
                    for item in items:
                        assert set(item) == SAFE_KEY_FIELDS
                        assert item["status"] == "Active"
                        assert item["service_ids"] == (
                            [] if item["name"] == "all-grants" else [str(a)]
                        )
                        assert item["effective_services"] == [
                            {
                                "id": str(service_id),
                                "name": "public-alpha" if service_id == a else "public-beta",
                                "public_model_name": "public-alpha"
                                if service_id == a
                                else "public-beta",
                            }
                            for service_id in wanted[item["name"]]
                        ]
                    for plaintext in plaintexts:
                        assert plaintext not in response.body.decode()
                async with env.session_factory() as session:
                    after = {
                        table.name: (await session.execute(sa.select(table))).all()
                        for table in (API_KEYS, API_KEY_SERVICE_SCOPES, USER_SERVICE_PERMISSIONS)
                    }
                assert after == before

            await assert_ranges([a, b], [a])
            async with env.session_factory() as session, session.begin():
                await UserManagementRepository(session).replace_for_service(
                    a, (ids[ADMIN_USERNAME],), actor_id=ids[ADMIN_USERNAME], now=datetime.now(UTC)
                )
            await assert_ranges([b], [])
            async with env.session_factory() as session, session.begin():
                await UserManagementRepository(session).replace_for_user(
                    ids[MEMBER_USERNAME], (), actor_id=ids[ADMIN_USERNAME], now=datetime.now(UTC)
                )
            await assert_ranges([], [])
            async with env.session_factory() as session, session.begin():
                await UserManagementRepository(session).replace_for_user(
                    ids[MEMBER_USERNAME],
                    (a, b),
                    actor_id=ids[ADMIN_USERNAME],
                    now=datetime.now(UTC),
                )
            await assert_ranges([a, b], [a])
            member_keys = (await env.client.get(PATH, cookies=member)).json()["items"]
            admin_keys = (await env.client.get(PATH, cookies=admin)).json()["items"]
            assert {item["id"] for item in member_keys}.isdisjoint(
                item["id"] for item in admin_keys
            )
            for plaintext in plaintexts:
                assert plaintext not in caplog.text

    asyncio.run(scenario())


def test_M4_03B_TC_GW_32_backend_independence_disabled_deleted_and_intrinsic_status(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as env:
            ids = await _create_accounts(env)
            admin = await _login(env, ADMIN_USERNAME)
            service_id = await _create_service(
                env, admin, name="public-stable", instance_id=INSTANCE_1
            )
            async with env.session_factory() as session, session.begin():
                await UserManagementRepository(session).replace_for_user(
                    ids[ADMIN_USERNAME],
                    (service_id,),
                    actor_id=ids[ADMIN_USERNAME],
                    now=datetime.now(UTC),
                )
            keys: dict[str, UUID] = {}
            for name in ("active", "expired", "revoked"):
                response = await env.client.request(
                    "POST", PATH, cookies=admin, json_body={"name": name}
                )
                assert response.status == 201
                keys[name] = UUID(response.json()["id"])
            async with env.session_factory() as session, session.begin():
                await session.execute(
                    sa.update(API_KEYS)
                    .where(API_KEYS.c.id == keys["expired"])
                    .values(
                        created_at=datetime.now(UTC) - timedelta(days=2),
                        expires_at=datetime.now(UTC) - timedelta(days=1),
                    )
                )
            assert (
                await env.client.request("POST", f"{PATH}/{keys['revoked']}:revoke", cookies=admin)
            ).status == 200

            async def check(visible: bool) -> None:
                response = await env.client.get(PATH, cookies=admin)
                assert response.status == 200
                for item in response.json()["items"]:
                    assert (
                        item["status"]
                        == {"active": "Active", "expired": "Expired", "revoked": "Revoked"}[
                            item["name"]
                        ]
                    )
                    assert item["effective_services"] == (
                        [
                            {
                                "id": str(service_id),
                                "name": "public-stable",
                                "public_model_name": "public-stable",
                            }
                        ]
                        if visible
                        else []
                    )

            for status in (
                ModelInstanceStatus.STARTING,
                ModelInstanceStatus.RESTARTING,
                ModelInstanceStatus.FAILED,
            ):
                await _set_instance_status(env, INSTANCE_1, status)
                await check(True)
            async with env.session_factory() as session, session.begin():
                await session.execute(
                    sa.update(AI_SERVICE_BACKENDS).values(health_gate_passed=False)
                )
            await check(True)
            async with env.session_factory() as session, session.begin():
                await session.execute(
                    sa.update(AI_SERVICE_BACKENDS).values(retired_at=datetime.now(UTC))
                )
            await check(True)
            async with env.session_factory() as session, session.begin():
                await session.execute(sa.update(AI_SERVICES).values(enabled=False))
            await check(False)
            async with env.session_factory() as session, session.begin():
                await session.execute(sa.update(AI_SERVICES).values(enabled=True))
            await check(True)
            async with env.session_factory() as session, session.begin():
                await session.execute(sa.update(AI_SERVICES).values(deleted_at=datetime.now(UTC)))
            await check(False)
            # An empty range does not block revocation or restore hidden history.
            assert (
                await env.client.request("POST", f"{PATH}/{keys['active']}:revoke", cookies=admin)
            ).status == 200
            assert (
                await env.client.request("DELETE", f"{PATH}/{keys['revoked']}", cookies=admin)
            ).status == 200
            items = (await env.client.get(PATH, cookies=admin)).json()["items"]
            assert {item["id"] for item in items} == {str(keys["active"]), str(keys["expired"])}
            async with env.session_factory() as session:
                assert len(await ApiKeyRepository(session).list_owned(ids[ADMIN_USERNAME])) == 3

    asyncio.run(scenario())


def test_M4_03B_TC_SEC_11_control_role_batch_query_and_session_denial(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as env:
            ids = await _create_accounts(env)
            admin = await _login(env, ADMIN_USERNAME)
            member = await _login(env, MEMBER_USERNAME)
            service_id = await _create_service(
                env, admin, name="control-visible", instance_id=INSTANCE_1
            )
            for name in ("no-grants-a", "no-grants-b", "no-grants-c"):
                assert (
                    await env.client.request("POST", PATH, cookies=member, json_body={"name": name})
                ).status == 201
            engine = create_async_engine(
                _async_url(ephemeral_postgres),
                connect_args={"options": f"-c role={CONTROL} -c timezone=UTC"},
            )
            try:
                factory = create_session_factory(engine)
                async with factory() as session:
                    assert (
                        await session.execute(sa.text("SELECT current_user"))
                    ).scalar_one() == CONTROL
                queries: list[str] = []

                def count_queries(
                    _conn: object,
                    _cursor: object,
                    statement: str,
                    _parameters: object,
                    _context: object,
                    _many: bool,
                ) -> None:
                    queries.append(statement)

                sa.event.listen(engine.sync_engine, "before_cursor_execute", count_queries)
                service = ApiKeyManagementService(session_factory=factory, keyring=env.keyring)
                result = await service.list_with_effective_services(
                    ids[MEMBER_USERNAME], user_role=UserRole.USER
                )
                assert len(result) == 3 and all(item.effective_services == () for item in result)
                assert (
                    len(queries) == 3
                )  # keys, all scopes, safe enabled services; independent of key count
                sa.event.remove(engine.sync_engine, "before_cursor_execute", count_queries)
                async with env.session_factory() as session, session.begin():
                    await UserManagementRepository(session).replace_for_user(
                        ids[MEMBER_USERNAME],
                        (service_id,),
                        actor_id=ids[ADMIN_USERNAME],
                        now=datetime.now(UTC),
                    )
                result = await service.list_with_effective_services(
                    ids[MEMBER_USERNAME], user_role=UserRole.USER
                )
                assert all(
                    [entry.id for entry in item.effective_services] == [service_id]
                    for item in result
                )
            finally:
                await engine.dispose()
            assert (await env.client.get(PATH)).status == 401
            disabled = await env.client.request(
                "POST", f"/api/v1/users/{ids[MEMBER_USERNAME]}:disable", cookies=admin
            )
            assert disabled.status == 200
            response = await env.client.get(PATH, cookies=member)
            assert response.status == 401
            assert "no-grants" not in response.body.decode()

    asyncio.run(scenario())

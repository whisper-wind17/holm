"""M4-17 / FX-CALL-LIMITS reuses FX-PROTO-05 and FX-M4-04 real PG seeds."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import datetime
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
import test_M4_04_snapshot_integration as snapshot_fixture
import test_PROTO_05_services_api_keys as api_fixture
from conftest import EphemeralPostgres
from holm_persistence import PostgresApiKeyValidationLookup
from holm_security import parse_api_key
from migration_test_support import upgrade_head


def test_M4_17_TC_GW_39_both_put_paths_preserve_limits_and_audit(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    upgrade_head(ephemeral_postgres.engine)
    api_fixture._seed_instances(ephemeral_postgres.engine)

    async def scenario() -> None:
        f = api_fixture
        async with f._environment(ephemeral_postgres) as env:
            ids = await f._create_accounts(env)
            admin = await f._login(env, f.ADMIN_USERNAME)
            member = await f._login(env, f.MEMBER_USERNAME)
            user = ids[f.MEMBER_USERNAME]
            service = await f._create_service(env, admin, name="m417", instance_id=f.INSTANCE_1)
            paths = [
                (f"/api/v1/users/{user}/permissions", "service_ids", service),
                (f"/api/v1/services/{service}/permissions", "user_ids", user),
            ]
            expected = {"max_concurrent_requests": 8, "max_requests_per_minute": None}

            async def assert_read(value: dict[str, int | None]) -> None:
                detail = await env.client.request("GET", f"/api/v1/users/{user}", cookies=admin)
                grant = detail.json()["authorized_services"][0]
                assert {key: grant[key] for key in value} == value
                permissions = await env.client.request(
                    "GET", f"/api/v1/services/{service}/permissions", cookies=admin
                )
                assert permissions.json()["limits"][str(user)] == value

            for path, ids_field, peer in paths:
                response = await env.client.request(
                    "PUT", path, cookies=admin, json_body={ids_field: [str(peer)]}
                )
                assert response.status == 200
                await assert_read(expected)
                expected = {"max_concurrent_requests": 2, "max_requests_per_minute": 3}
                response = await env.client.request(
                    "PUT",
                    path,
                    cookies=admin,
                    json_body={ids_field: [str(peer)], "limits": {str(peer): expected}},
                )
                assert response.status == 200
                await assert_read(expected)
                # Regression assertion: DELETE-all/INSERT-all resets these values.
                response = await env.client.request(
                    "PUT", path, cookies=admin, json_body={ids_field: [str(peer)]}
                )
                assert response.status == 200
                await assert_read(expected)
                response = await env.client.request(
                    "PUT",
                    path,
                    cookies=admin,
                    json_body={
                        ids_field: [str(peer)],
                        "limits": {str(peer): {"max_concurrent_requests": None}},
                    },
                )
                assert response.status == 200
                expected["max_concurrent_requests"] = None
                await assert_read(expected)
                for bad in [0, 65, -1, True, "2"]:
                    response = await env.client.request(
                        "PUT",
                        path,
                        cookies=admin,
                        json_body={
                            ids_field: [str(peer)],
                            "limits": {str(peer): {"max_concurrent_requests": bad}},
                        },
                    )
                    assert response.status == 400
                for bad in [0, 601]:
                    response = await env.client.request(
                        "PUT",
                        path,
                        cookies=admin,
                        json_body={
                            ids_field: [str(peer)],
                            "limits": {str(peer): {"max_requests_per_minute": bad}},
                        },
                    )
                    assert response.status == 400
                response = await env.client.request(
                    "PUT",
                    path,
                    cookies=admin,
                    json_body={ids_field: [str(peer)], "limits": {str(uuid4()): {}}},
                )
                assert response.status == 400
                forbidden = await env.client.request(
                    "PUT", path, cookies=member, json_body={ids_field: [str(peer)]}
                )
                assert forbidden.status == 403
            with ephemeral_postgres.engine.connect() as connection:
                details = (
                    connection.execute(
                        sa.text(
                            "SELECT details FROM core.audit_events WHERE action = "
                            "'permission.change' ORDER BY created_at"
                        )
                    )
                    .scalars()
                    .all()
                )
            limit_changes = [
                detail for detail in details if "limits" in detail.get("changed_fields", [])
            ]
            assert limit_changes
            assert any(
                json.loads(item)[1] == [2, 3]
                for detail in limit_changes
                for item in detail["limits_after"]
            )
            assert all(
                "limits_before" in detail and len(detail["limits_before"]) <= 32
                for detail in limit_changes
            )

    asyncio.run(scenario())


def test_M4_17_TC_GW_38_39_snapshot_and_direct_lookup_propagate_within_two_seconds(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    f = snapshot_fixture
    f._seed(ephemeral_postgres)

    async def scenario() -> None:
        async with f._environment(ephemeral_postgres, listen=False) as env:
            await env.user_service.replace_user_permissions(
                f.MEMBER_ID,
                (env.service_id,),
                actor_id=f.ADMIN_ID,
                request_id=uuid4(),
                limits={
                    env.service_id: {"max_concurrent_requests": 1, "max_requests_per_minute": 1}
                },
            )
            await f._wait_for(env, status=429, code="rate_limit_exceeded")
            response = await f._call(env)
            assert response.headers["retry-after"].isdigit()
            assert response.json()["error"]["request_id"] == response.headers["x-request-id"]
            assert response.json()["error"]["details"] == {"limit_type": "per_minute", "limit": 1}
            parsed = parse_api_key(env.plaintext_key)
            direct = await PostgresApiKeyValidationLookup(env.factory)(parsed.lookup_id)
            assert direct is not None
            assert direct.service_limits[env.service_id] == (1, 1)
            assert env.manager.snapshot.users_by_id[f.MEMBER_ID].service_limits[env.service_id] == (
                1,
                1,
            )
            await env.user_service.replace_service_permissions(
                env.service_id,
                (f.MEMBER_ID,),
                actor_id=f.ADMIN_ID,
                request_id=uuid4(),
                limits={
                    f.MEMBER_ID: {"max_concurrent_requests": None, "max_requests_per_minute": None}
                },
            )
            await f._wait_for(env, status=200, code=None)

    asyncio.run(scenario())


@pytest.mark.parametrize("side", ["user", "service"])
def test_M4_17_TC_GW_39_preservation_assertion_detects_delete_insert(
    ephemeral_postgres: EphemeralPostgres,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
) -> None:
    from holm_persistence import UserManagementRepository
    from holm_persistence.ai_services import USER_SERVICE_PERMISSIONS

    f = snapshot_fixture
    f._seed(ephemeral_postgres)

    async def scenario() -> None:
        async with f._environment(ephemeral_postgres, listen=False) as env:
            await env.user_service.replace_user_permissions(
                f.MEMBER_ID,
                (env.service_id,),
                actor_id=f.ADMIN_ID,
                request_id=uuid4(),
                limits={
                    env.service_id: {"max_concurrent_requests": 2, "max_requests_per_minute": 3}
                },
            )
            method = UserManagementRepository._replace_permissions

            async def delete_insert(
                self: UserManagementRepository,
                owner_column: str,
                owner_id: UUID,
                peer_column: str,
                peers: tuple[UUID, ...],
                *,
                actor_id: UUID,
                now: datetime,
                limits: Mapping[UUID, Mapping[str, int | None]],
            ) -> None:
                await self._session.execute(
                    sa.delete(USER_SERVICE_PERMISSIONS).where(
                        USER_SERVICE_PERMISSIONS.c[owner_column] == owner_id
                    )
                )
                await method(
                    self,
                    owner_column,
                    owner_id,
                    peer_column,
                    peers,
                    actor_id=actor_id,
                    now=now,
                    limits=limits,
                )

            monkeypatch.setattr(UserManagementRepository, "_replace_permissions", delete_insert)
            if side == "user":
                await env.user_service.replace_user_permissions(
                    f.MEMBER_ID, (env.service_id,), actor_id=f.ADMIN_ID, request_id=uuid4()
                )
            else:
                await env.user_service.replace_service_permissions(
                    env.service_id, (f.MEMBER_ID,), actor_id=f.ADMIN_ID, request_id=uuid4()
                )
            actual = (await env.user_service.service_permission_limits(env.service_id))[f.MEMBER_ID]
            assert actual == (8, None)  # Positive control reached the intended regression.

            def assert_preserved(value: tuple[int | None, int | None]) -> None:
                assert value == (2, 3)

            with pytest.raises(AssertionError):
                assert_preserved(actual)  # The same preservation assertion must fail.

    asyncio.run(scenario())


def test_M4_18_mixed_permission_activity_counts_only_retained_grants(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    """FX-M4-18 uses PROTO-05 accounts and two real service grants."""
    upgrade_head(ephemeral_postgres.engine)
    api_fixture._seed_instances(ephemeral_postgres.engine)

    async def scenario() -> None:
        f = api_fixture
        async with f._environment(ephemeral_postgres) as env:
            ids = await f._create_accounts(env)
            admin = await f._login(env, f.ADMIN_USERNAME)
            user_id = str(ids[f.MEMBER_USERNAME])
            first = str(
                await f._create_service(env, admin, name="m418-first", instance_id=f.INSTANCE_1)
            )
            second = str(
                await f._create_service(env, admin, name="m418-second", instance_id=f.INSTANCE_2)
            )
            path = f"/api/v1/users/{user_id}/permissions"
            for body in (
                {"service_ids": [first]},
                {
                    "service_ids": [first, second],
                    "limits": {
                        first: {"max_concurrent_requests": 2},
                        second: {"max_concurrent_requests": 4},
                    },
                },
                {"service_ids": [second], "limits": {second: {"max_requests_per_minute": 30}}},
            ):
                assert (
                    await env.client.request("PUT", path, cookies=admin, json_body=body)
                ).status == 200
            response = await env.client.get(
                f"/api/v1/users/{user_id}/activity?action=permission.change", cookies=admin
            )
            assert response.status == 200
            events = response.json()["items"]
            assert len(events) == 3
            assert events[0]["limit_changed_service_ids"] == [second]
            assert events[0]["removed_service_ids"] == [first]
            assert events[1]["limit_changed_service_ids"] == [first]
            assert events[1]["added_service_ids"] == [second]
            assert events[2]["limit_changed_service_ids"] == []

    asyncio.run(scenario())

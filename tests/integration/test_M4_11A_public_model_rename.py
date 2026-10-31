"""FX-M4-11A: reuse PROTO-05 accounts and M4-04 real PG/Snapshot fixtures."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import UUID

import httpx
import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from holm_control_api.ai_service_routes import RenameAiServiceRequest
from holm_control_api.ai_service_service import AiServiceVersionConflict, UpdateAiService
from holm_persistence import AsyncSessionFactory
from migration_test_support import upgrade_head
from test_M4_04_snapshot_integration import (
    ADMIN_ID,
    INSTANCE_A,
    INSTANCE_B,
    MEMBER_ID,
    _seed,
)
from test_M4_04_snapshot_integration import (
    _environment as snapshot_environment,
)
from test_PROTO_05_services_api_keys import (
    ADMIN_USERNAME,
    INSTANCE_1,
    INSTANCE_2,
    MEMBER_USERNAME,
    _create_accounts,
    _create_service,
    _environment,
    _login,
    _seed_instances,
)


@pytest.fixture
def database(ephemeral_postgres: EphemeralPostgres) -> EphemeralPostgres:
    upgrade_head(ephemeral_postgres.engine)
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(
            sa.text(
                "TRUNCATE core.ai_services, core.models, core.users, core.audit_events, "
                "core.domain_events CASCADE"
            )
        )
    _seed_instances(ephemeral_postgres.engine)
    return ephemeral_postgres


def body(name: str, version: int = 1) -> dict[str, object]:
    return {
        "public_model_name": name,
        "confirm_public_model_name": name,
        "expected_route_version": version,
    }


async def state(factory: AsyncSessionFactory) -> dict[str, tuple[str, ...]]:
    # Explicit metadata tables detect accidental reassignment or history rewrites.
    result: dict[str, tuple[str, ...]] = {}
    async with factory() as session:
        for table in (
            "ai_services",
            "ai_service_backends",
            "user_service_permissions",
            "api_key_service_scopes",
            "model_instances",
            "gpu_allocations",
            "request_records",
            "audit_events",
            "domain_events",
        ):
            rows = await session.execute(
                sa.text(
                    f"SELECT row_to_json(t)::text FROM core.{table} t ORDER BY row_to_json(t)::text"
                )
            )
            result[table] = tuple(rows.scalars())
        for table in ("request_records", "usage_15m"):
            rows = await session.execute(
                sa.text(
                    f"SELECT row_to_json(t)::text FROM usage.{table} t ORDER BY "
                    "row_to_json(t)::text"
                )
            )
            result[f"usage.{table}"] = tuple(rows.scalars())
    return result


def test_M4_11A_TC_SEC_11_12_http_confirmation_version_and_atomic_rejections(
    database: EphemeralPostgres,
) -> None:
    async def scenario() -> None:
        async with _environment(database) as env:
            await _create_accounts(env)
            admin = await _login(env, ADMIN_USERNAME)
            member = await _login(env, MEMBER_USERNAME)
            service_id = await _create_service(env, admin, name="old-name", instance_id=INSTANCE_1)
            await _create_service(env, admin, name="Legacy.NAME", instance_id=INSTANCE_2)
            path = f"/api/v1/services/{service_id}:rename"
            # Disabled and Stopped are intentionally valid rename states.
            disabled = await env.client.request(
                "PATCH",
                f"/api/v1/services/{service_id}",
                cookies=admin,
                json_body={"enabled": False},
            )
            assert disabled.status == 200
            assert disabled.json()["primary_instance_status"] == "Stopped"
            before = await state(env.session_factory)
            invalid = [
                body("x"),
                body("UPPER"),
                body("a/b"),
                body("a" * 65),
                body("name "),
                body("name\n"),
                {**body("new-name"), "confirm_public_model_name": "wrong"},
                {"public_model_name": "new-name", "expected_route_version": 1},
                body("new-name", 0),
            ]
            for payload in invalid:
                response = await env.client.request("POST", path, cookies=admin, json_body=payload)
                assert response.status == 400
            for payload, code in (
                (body("legacy.name"), "service_name_conflict"),
                (body("new-name", 2), "version_conflict"),
            ):
                response = await env.client.request("POST", path, cookies=admin, json_body=payload)
                assert response.status == 409
                assert response.json()["error"]["code"] == code
                assert "request_id" in response.json()["error"]
                assert "uq_ai_services" not in response.body.decode()
            assert await state(env.session_factory) == before
            for cookies, status in ((member, 403), (None, 401), (dict(admin), 403)):
                response = await env.client.request(
                    "POST", path, cookies=cookies, json_body=body("new-name")
                )
                assert response.status == status
            missing = await env.client.request(
                "POST",
                f"/api/v1/services/{UUID(int=17)}:rename",
                cookies=admin,
                json_body=body("new-name"),
            )
            assert missing.status == 404
            # Auth failures may write their own security audit, but no service mutation/event.
            after = await state(env.session_factory)
            assert {k: v for k, v in after.items() if k != "audit_events"} == {
                k: v for k, v in before.items() if k != "audit_events"
            }
            before = after
            changed = await env.client.request(
                "POST", path, cookies=admin, json_body=body("new-name")
            )
            assert changed.status == 200
            assert changed.json() == {
                **disabled.json(),
                "public_model_name": "new-name",
                "route_version": 2,
            }
            after = await state(env.session_factory)
            for key in before.keys() - {"ai_services", "audit_events", "domain_events"}:
                assert after[key] == before[key]
            assert len(after["audit_events"]) == len(before["audit_events"]) + 1
            assert len(after["domain_events"]) == len(before["domain_events"]) + 1
            event = json.loads(
                next(iter(set(after["domain_events"]) - set(before["domain_events"])))
            )
            audit = json.loads(next(iter(set(after["audit_events"]) - set(before["audit_events"]))))
            assert (event["event"], event["aggregate_id"], event["payload"]) == (
                "ai_service.changed",
                str(service_id),
                {},
            )
            assert (audit["action"], audit["object_id"], audit["details"]) == (
                "ai_service.change",
                str(service_id),
                {},
            )
            same = await env.client.request(
                "POST", path, cookies=admin, json_body=body("new-name", 2)
            )
            assert same.status == 200
            assert await state(env.session_factory) == after
            stale = await env.client.request(
                "POST", path, cookies=admin, json_body=body("third-name")
            )
            assert stale.status == 409
            assert await state(env.session_factory) == after

    asyncio.run(scenario())


def test_M4_11A_TC_GW_03_snapshot_refresh_preserves_identity_and_history(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _seed(ephemeral_postgres)
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(sa.text("TRUNCATE usage.request_records, usage.usage_15m"))

    async def scenario() -> None:
        async with snapshot_environment(ephemeral_postgres) as env:
            async with env.factory() as session, session.begin():
                await session.execute(
                    sa.text(
                        "INSERT INTO core.request_records (request_id, received_at, completed_at, "
                        "user_id, api_key_id, service_id, instance_id, stream, status, "
                        "http_status, "
                        "input_tokens, output_tokens, duration_ms) VALUES (:rid, "
                        "clock_timestamp(), "
                        "clock_timestamp(), :uid, :kid, :sid, :iid, false, 'completed', 200, 5, 7, "
                        "1)"
                    ),
                    {
                        "rid": UUID(int=411),
                        "uid": MEMBER_ID,
                        "kid": env.key_id,
                        "sid": env.service_id,
                        "iid": INSTANCE_A,
                    },
                )
                await session.execute(
                    sa.text(
                        "CREATE TABLE IF NOT EXISTS usage.request_records_202609 PARTITION OF "
                        "usage.request_records FOR VALUES FROM ('2026-09-01T00:00:00+00') TO "
                        "('2026-10-01T00:00:00+00')"
                    )
                )
                await session.execute(
                    sa.text(
                        "INSERT INTO usage.request_records (received_at, request_id, event_seq, "
                        "record_state, service_id, user_id, api_key_id, input_tokens, "
                        "output_tokens, updated_at) VALUES ('2026-09-30T00:00:00+00', :rid, 3, "
                        "'completed', :sid, :uid, :kid, 5, 7, clock_timestamp())"
                    ),
                    {
                        "rid": UUID(int=411),
                        "sid": env.service_id,
                        "uid": MEMBER_ID,
                        "kid": env.key_id,
                    },
                )
                await session.execute(
                    sa.text(
                        "INSERT INTO usage.usage_15m (bucket_start, user_id, api_key_id, "
                        "service_id, requests, input_tokens, output_tokens, error_requests, "
                        "aborted_requests, partial_token_requests, updated_at) VALUES "
                        "('2026-09-30T00:00:00+00', :uid, :kid, :sid, 1, 5, 7, 0, 0, 0, "
                        "clock_timestamp())"
                    ),
                    {"sid": env.service_id, "uid": MEMBER_ID, "kid": env.key_id},
                )
            before = await state(env.factory)
            old_route = await env.manager.find("m4-04-model")
            assert old_route is not None
            changed = await env.ai_service.rename(
                env.service_id,
                public_model_name="renamed-model",
                expected_route_version=1,
                actor_id=ADMIN_ID,
                request_id=UUID(int=412),
            )
            assert changed.id == env.service_id
            assert changed.primary_instance_id == INSTANCE_A
            # Real production manager refresh atomically swaps the immutable snapshot.
            await env.manager.refresh()
            assert await env.manager.find("m4-04-model") is None
            new_route = await env.manager.find("renamed-model")
            assert new_route is not None
            assert new_route.service_id == old_route.service_id
            assert new_route.instance_id == old_route.instance_id
            assert old_route.public_model_name == "m4-04-model"
            assert old_route.route_version == 1
            assert new_route.route_version == 2
            after = await state(env.factory)
            for key in before.keys() - {"ai_services", "audit_events", "domain_events"}:
                assert after[key] == before[key]
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=env.gateway), base_url="http://gateway"
            ) as client:
                headers = {"Authorization": f"Bearer {env.plaintext_key}"}
                models = await client.get("/v1/models", headers=headers)
                assert [item["id"] for item in models.json()["data"]] == ["renamed-model"]
                old = await client.post(
                    "/v1/chat/completions",
                    headers=headers,
                    json={"model": "m4-04-model", "messages": []},
                )
                assert old.status_code == 404
                assert old.json()["error"]["code"] == "model_not_found"
                new = await client.post(
                    "/v1/chat/completions",
                    headers=headers,
                    json={"model": "renamed-model", "messages": []},
                )
                assert new.status_code == 200
                assert env.upstream_hosts == ["runtime-a.internal"]
                await env.user_service.replace_user_permissions(
                    MEMBER_ID, (), actor_id=ADMIN_ID, request_id=UUID(int=413)
                )
                await env.manager.refresh()
                denied = await client.post(
                    "/v1/chat/completions",
                    headers=headers,
                    json={"model": "renamed-model", "messages": []},
                )
                assert denied.status_code == 403
                assert denied.json()["error"]["code"] == "service_not_authorized"
            # A rebind after the editor opened also invalidates its expected version.
            await env.ai_service.update(
                env.service_id,
                UpdateAiService(primary_instance_id=INSTANCE_B),
                actor_id=ADMIN_ID,
                request_id=UUID(int=414),
            )
            with pytest.raises(AiServiceVersionConflict):
                await env.ai_service.rename(
                    env.service_id,
                    public_model_name="other-name",
                    expected_route_version=2,
                    actor_id=ADMIN_ID,
                    request_id=UUID(int=415),
                )

    asyncio.run(scenario())


def test_M4_11A_request_contract_matches_frontend_snapshot() -> None:
    snapshot = (
        Path(__file__).resolve().parents[1]
        / "contract/snapshots/m4_11a_rename_ai_service_request.json"
    )
    assert RenameAiServiceRequest.model_json_schema() == json.loads(snapshot.read_text())

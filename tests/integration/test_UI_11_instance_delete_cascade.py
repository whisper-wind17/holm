"""UI-11 / TC-INV-08: FX-UI-11 reuses PROTO-05 real-PG accounts and instances."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from holm_domain import DomainEventType, ModelInstanceStatus, RequestRecordStatus
from holm_gateway.application import (  # type: ignore[import-untyped]
    create_app as create_gateway_app,
)
from holm_gateway.authentication import ApiKeyAuthenticator  # type: ignore[import-untyped]
from holm_gateway.route_snapshot import (  # type: ignore[import-untyped]
    PostgresRouteSnapshotSource,
    RouteSnapshotManager,
    SnapshotApiKeyValidationLookup,
)
from holm_persistence import (
    LEDGER_REQUEST_RECORDS,
    PostgresApiKeyValidationLookup,
    UsageRepository,
    ensure_request_record_partitions,
)
from holm_persistence.usage_repository import UsageFilters, UsageGroupBy
from holm_security import validate_api_key
from sqlalchemy.ext.asyncio import AsyncEngine
from test_PROTO_05_services_api_keys import (
    ADMIN_USERNAME,
    INSTANCE_1,
    INSTANCE_2,
    MEMBER_USERNAME,
    VLLM_CAPABILITIES,
    _create_accounts,
    _create_service,
    _environment,
    _login,
    _migrated_database,  # noqa: F401 - imported pytest fixture
    _set_instance_status,
)
from test_PROTO_13_usage import _record


@pytest.mark.parametrize("status", [ModelInstanceStatus.STOPPED, ModelInstanceStatus.FAILED])
def test_UI_11_TC_INV_08_atomic_delete_preserves_scopes_and_invalidates_routes(
    ephemeral_postgres: EphemeralPostgres, status: ModelInstanceStatus
) -> None:
    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as environment:
            accounts = await _create_accounts(environment)
            admin = await _login(environment, ADMIN_USERNAME)
            member = await _login(environment, MEMBER_USERNAME)
            service_a = await _create_service(
                environment, admin, name="ui11-first", instance_id=INSTANCE_1
            )
            service_c = await _create_service(
                environment, admin, name="ui11-also-bound", instance_id=INSTANCE_1
            )
            service_b = await _create_service(
                environment, admin, name="ui11-second", instance_id=INSTANCE_2
            )
            grant = await environment.client.request(
                "PUT",
                f"/api/v1/users/{accounts[MEMBER_USERNAME]}/permissions",
                cookies=admin,
                json_body={"service_ids": [str(service_a), str(service_b)]},
            )
            assert grant.status == 200
            keys = []
            for scopes in ([str(service_a)], []):
                response = await environment.client.request(
                    "POST",
                    "/api/v1/me/api-keys",
                    cookies=member,
                    json_body={"name": "FX-UI-11", "service_ids": scopes},
                )
                assert response.status == 201
                keys.append(str(response.json()["api_key"]))
            lookup = PostgresApiKeyValidationLookup(environment.session_factory)
            limited = await validate_api_key(keys[0], lookup=lookup, keyring=environment.keyring)
            # Both service authorization and restricted scope exist before deletion.
            assert limited.authorized_service_ids == frozenset({service_a, service_b})
            assert limited.scoped_service_ids == frozenset({service_a})
            # FX-UI-11 historical usage uses the existing PROTO-13 metadata-only fixture.
            at = datetime.now(UTC)
            record = _record(
                at=at,
                user_id=accounts[MEMBER_USERNAME],
                api_key_id=limited.api_key_id,
                service_id=service_a,
                status=RequestRecordStatus.COMPLETED,
                input_tokens=10,
                output_tokens=5,
            )
            record["instance_id"] = INSTANCE_1
            async with environment.session_factory() as session:
                assert isinstance(session.bind, AsyncEngine)
                await ensure_request_record_partitions(session.bind, (at,))
                await session.execute(sa.insert(LEDGER_REQUEST_RECORDS), [record])
                await session.commit()
            path = f"/api/v1/instances/{INSTANCE_1}"
            cascade_path = path + "?delete_bound_services=true"
            denied = await environment.client.request("DELETE", cascade_path, cookies=member)
            assert denied.status == 403
            await _set_instance_status(environment, INSTANCE_1, ModelInstanceStatus.RUNNING)
            invalid = await environment.client.request("DELETE", cascade_path, cookies=admin)
            assert invalid.status == 409
            await _set_instance_status(environment, INSTANCE_1, status)
            legacy = await environment.client.request("DELETE", path, cookies=admin)
            assert legacy.status == 409
            async with environment.session_factory() as session:
                assert (
                    await session.scalar(
                        sa.text(
                            "SELECT count(*) FROM core.ai_services WHERE deleted_at IS NOT NULL"
                        )
                    )
                    == 0
                )
                assert (
                    await session.scalar(
                        sa.text(
                            "SELECT count(*) FROM core.ai_service_backends "
                            "WHERE retired_at IS NOT NULL"
                        )
                    )
                    == 0
                )
            await _set_instance_status(environment, INSTANCE_2, ModelInstanceStatus.RUNNING)
            async with environment.session_factory() as session, session.begin():
                await session.execute(
                    sa.text(
                        "UPDATE core.model_instances SET internal_endpoint='fake-upstream:8000' "
                        "WHERE id=:id"
                    ),
                    {"id": INSTANCE_2},
                )
            manager = RouteSnapshotManager(
                PostgresRouteSnapshotSource(environment.session_factory), listener=None
            )
            await manager.refresh()
            previous_version = manager.snapshot.version
            assert await manager.find("ui11-first") is not None
            response = await environment.client.request("DELETE", cascade_path, cookies=admin)
            assert response.status == 204
            # No forced refresh: missing invalidation must leave this assertion red.
            await manager.check_once()
            assert manager.snapshot.version > previous_version
            assert await manager.find("ui11-first") is None
            assert await manager.find("ui11-also-bound") is None
            assert await manager.find("ui11-second") is not None
            async with environment.session_factory() as session:
                assert (
                    await session.scalar(
                        sa.text("SELECT count(*) FROM core.model_instances WHERE id=:id"),
                        {"id": INSTANCE_1},
                    )
                    == 0
                )
                for service_id in (service_a, service_c):
                    row = (
                        await session.execute(
                            sa.text(
                                "SELECT deleted_at, route_version FROM core.ai_services "
                                "WHERE id=:id"
                            ),
                            {"id": service_id},
                        )
                    ).one()
                    assert row.deleted_at is not None and row.route_version == 2
                    assert (
                        await session.scalar(
                            sa.text(
                                "SELECT count(*) FROM core.ai_service_backends "
                                "WHERE service_id=:id AND retired_at IS NOT NULL "
                                "AND instance_id IS NULL"
                            ),
                            {"id": service_id},
                        )
                        == 1
                    )
                    assert (
                        await session.scalar(
                            sa.text(
                                "SELECT count(*) FROM core.audit_events WHERE object_id=:id "
                                "AND action='ai_service.change'"
                            ),
                            {"id": service_id},
                        )
                        == 2
                    )
                    assert (
                        await session.scalar(
                            sa.text(
                                "SELECT count(*) FROM core.domain_events WHERE aggregate_id=:id "
                                "AND event=:event AND id>:previous"
                            ),
                            {
                                "id": service_id,
                                "event": DomainEventType.AI_SERVICE_CHANGED.value,
                                "previous": previous_version,
                            },
                        )
                        == 1
                    )
                assert (
                    await session.scalar(
                        sa.text(
                            "SELECT count(*) FROM core.api_key_service_scopes WHERE service_id=:id"
                        ),
                        {"id": service_a},
                    )
                    == 1
                )
                assert (
                    await session.scalar(
                        sa.text(
                            "SELECT count(*) FROM core.user_service_permissions "
                            "WHERE service_id=:id"
                        ),
                        {"id": service_a},
                    )
                    == 1
                )
                historical = (
                    (
                        await session.execute(
                            sa.select(LEDGER_REQUEST_RECORDS).where(
                                LEDGER_REQUEST_RECORDS.c.request_id == record["request_id"]
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                assert historical["service_id"] == service_a
                assert historical["input_tokens"] == 10
                groups = await UsageRepository(session).group(
                    UsageFilters(service_id=service_a), group_by=UsageGroupBy.SERVICE
                )
                assert len(groups) == 1
                assert groups[0].display_name == "ui11-first"
                assert groups[0].totals.requests == 1
                assert groups[0].totals.total_tokens == 15
            for service_id in (service_a, service_c):
                detail = await environment.client.get(
                    f"/api/v1/services/{service_id}", cookies=admin
                )
                assert detail.status == 404
            listing = await environment.client.get("/api/v1/services", cookies=admin)
            assert {UUID(item["id"]) for item in listing.json()["items"]} == {service_b}

            async def upstream(request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    200,
                    content=b"data: "
                    + json.dumps(
                        {
                            "id": "chatcmpl-ui11",
                            "object": "chat.completion.chunk",
                            "model": "ui11-second",
                            "choices": [{"delta": {"role": "assistant", "content": "ok"}}],
                        }
                    ).encode()
                    + b"\n\ndata: [DONE]\n\n",
                )

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(upstream)
            ) as upstream_client:
                for validation_lookup in (lookup, SnapshotApiKeyValidationLookup(manager)):
                    validated = await validate_api_key(
                        keys[0], lookup=validation_lookup, keyring=environment.keyring
                    )
                    assert validated.scoped_service_ids == frozenset({service_a})
                    assert service_b in validated.authorized_service_ids
                    gateway = create_gateway_app(
                        authenticator=ApiKeyAuthenticator(
                            lookup=validation_lookup, keyring=environment.keyring
                        ),
                        routes=manager,
                        upstream_client=upstream_client,
                        runtime_capabilities={"vllm": VLLM_CAPABILITIES},
                    )
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=gateway), base_url="http://gateway"
                    ) as client:
                        for key, model, expected, code in (
                            (keys[0], "ui11-first", 404, "model_not_found"),
                            (keys[0], "ui11-second", 403, "service_not_authorized"),
                            (keys[1], "ui11-second", 200, None),
                        ):
                            result = await client.post(
                                "/v1/chat/completions",
                                headers={"Authorization": f"Bearer {key}"},
                                json={
                                    "model": model,
                                    "messages": [{"role": "user", "content": "hi"}],
                                },
                            )
                            assert result.status_code == expected, result.text
                            if code is not None:
                                assert result.json()["error"]["code"] == code
            recreated = await environment.client.request(
                "POST",
                "/api/v1/services",
                cookies=admin,
                json_body={
                    "name": "ui11-first",
                    "public_model_name": "ui11-first",
                    "primary_instance_id": str(INSTANCE_2),
                },
            )
            assert recreated.status == 201
            duplicate = await environment.client.request(
                "POST",
                "/api/v1/services",
                cookies=admin,
                json_body={
                    "name": "ui11-first",
                    "public_model_name": "ui11-unused",
                    "primary_instance_id": str(INSTANCE_2),
                },
            )
            assert duplicate.status == 409
            assert duplicate.json()["error"]["code"] == "service_display_name_conflict"

    asyncio.run(scenario())

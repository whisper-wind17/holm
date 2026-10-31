"""UI-13: FX-UI-13 uses PROTO-05 accounts/instances and M5-08 ledger metadata on real PG."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock, patch
from uuid import UUID

import sqlalchemy as sa
from conftest import EphemeralPostgres
from fastapi import FastAPI
from holm_control_api import usage_service as usage_module
from holm_control_api.attention_routes import create_attention_router
from holm_control_api.attention_service import AttentionService
from holm_control_api.usage_routes import UsageSummaryResponse
from holm_control_api.usage_service import UsageQueryService
from holm_domain import ModelInstanceStatus, RequestRecordState, UserRole
from holm_ledger import EventType
from holm_persistence import (
    AI_SERVICE_BACKENDS,
    AI_SERVICES,
    LEDGER_REQUEST_RECORDS,
    MODEL_INSTANCES,
    USAGE_15M,
    UsageRepository,
    ensure_request_record_partitions,
)
from holm_persistence.model_domain import DOWNLOAD_TASKS
from holm_persistence.model_instance_events import MODEL_INSTANCE_EVENTS
from sqlalchemy.ext.asyncio import AsyncEngine
from test_M5_08_usage_buckets import USER_ID, _record
from test_PROTO_05_services_api_keys import (
    ADMIN_USERNAME,
    INSTANCE_1,
    INSTANCE_2,
    MEMBER_USERNAME,
    MODEL_ID,
    _create_accounts,
    _create_service,
    _environment,
    _login,
    _migrated_database,  # noqa: F401 - imported fixture
    _set_instance_status,
)


def test_UI_13_attention_real_sources_closed_response_and_permissions(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as env:
            await _create_accounts(env)
            admin = await _login(env, ADMIN_USERNAME)
            member = await _login(env, MEMBER_USERNAME)
            enabled = await _create_service(env, admin, name="ui13-enabled", instance_id=INSTANCE_1)
            disabled = await _create_service(
                env, admin, name="ui13-disabled", instance_id=INSTANCE_2
            )
            await _set_instance_status(env, INSTANCE_1, ModelInstanceStatus.FAILED)
            await _set_instance_status(env, INSTANCE_2, ModelInstanceStatus.UNHEALTHY)
            now = datetime.now(UTC)
            download_id = UUID(int=1313)
            async with env.session_factory() as session:
                assert isinstance(session.bind, AsyncEngine)
                await ensure_request_record_partitions(session.bind, (now,))
                await session.execute(
                    sa.update(AI_SERVICES).where(AI_SERVICES.c.id == disabled).values(enabled=False)
                )
                await session.execute(
                    sa.insert(MODEL_INSTANCE_EVENTS).values(
                        id=UUID(int=1314),
                        instance_id=INSTANCE_1,
                        event_type="StatusChanged",
                        from_status="Starting",
                        to_status="Failed",
                        occurred_at=now,
                        actor="controller",
                        detail={},
                    )
                )
                await session.execute(
                    sa.insert(DOWNLOAD_TASKS).values(
                        id=download_id,
                        model_id=MODEL_ID,
                        source_type="HuggingFace",
                        repository_id="FX-UI-13/model",
                        requested_revision_norm="main",
                        status="Failed",
                        attempt=1,
                        downloaded_bytes=0,
                        error_code="download_process_failed",
                        staging_path="/FX-UI-13-private-path",
                        created_at=now,
                        updated_at=now,
                    )
                )
                await session.execute(
                    sa.insert(LEDGER_REQUEST_RECORDS).values(
                        **_record(
                            1313,
                            at=now - timedelta(seconds=2),
                            state=RequestRecordState.UNKNOWN,
                            last_confirmed=EventType.IN_PROGRESS,
                        )
                    )
                )
                await session.commit()
            attention = AttentionService(session_factory=env.session_factory)
            # Reuse production authorization middleware from the fixture application.
            cast(FastAPI, env.client._app).include_router(create_attention_router(attention))
            response = await env.client.request("GET", "/api/v1/overview/attention", cookies=admin)
            assert response.status == 200
            items = response.json()["items"]
            assert [item["kind"] for item in items] == [
                "instance_failed",
                "service_unavailable",
                "instance_unhealthy",
                "download_failed",
                "unknown_requests",
            ]
            assert all(
                set(item)
                == {"kind", "severity", "target_id", "target_name", "count", "latest_at", "detail"}
                for item in items
            )
            assert items[0]["target_id"] == str(INSTANCE_1)
            assert items[1]["target_id"] == str(enabled)
            assert items[1]["detail"] == "Failed"
            assert items[3]["target_name"] == "PROTO-05 Model"
            assert items[3]["detail"] == "Downloading"
            assert items[4]["count"] == 1
            assert all(item["target_id"] != str(disabled) for item in items)
            assert "/FX-UI-13-private-path" not in str(items)
            denied = await env.client.request("GET", "/api/v1/overview/attention", cookies=member)
            assert denied.status == 403
            async with env.session_factory() as session:
                await session.execute(
                    sa.delete(AI_SERVICE_BACKENDS).where(
                        AI_SERVICE_BACKENDS.c.service_id == enabled
                    )
                )
                await session.execute(
                    sa.delete(MODEL_INSTANCES).where(MODEL_INSTANCES.c.id == INSTANCE_1)
                )
                await session.commit()
            after = await attention.items()
            assert not any(item.target_id == str(INSTANCE_1) for item in after)
            assert (
                next(item for item in after if item.target_id == str(enabled)).detail
                == "no_backend"
            )
            async with env.session_factory() as session:
                await session.execute(
                    sa.update(AI_SERVICES).where(AI_SERVICES.c.id == enabled).values(deleted_at=now)
                )
                await session.execute(
                    sa.delete(LEDGER_REQUEST_RECORDS).where(
                        LEDGER_REQUEST_RECORDS.c.request_id
                        == UUID("58000000-0000-4000-8001-000000001313")
                    )
                )
                await session.commit()
            assert not any(item.target_id == str(enabled) for item in await attention.items())

    asyncio.run(scenario())


def test_UI_13_active_users_union_closed_buckets_live_edges_and_scope(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as env:
            now = datetime(2026, 10, 10, 12, 7, tzinfo=UTC)
            other = UUID(int=1315)
            records = [
                _record(
                    1320, at=now.replace(hour=11, minute=50), state=RequestRecordState.COMPLETED
                ),
                _record(1321, at=now.replace(minute=2), state=RequestRecordState.COMPLETED),
                {
                    **_record(1322, at=now.replace(minute=3), state=RequestRecordState.COMPLETED),
                    "user_id": other,
                },
                _record(
                    1323,
                    at=now.replace(minute=4),
                    state=RequestRecordState.COMPLETED,
                    dimensions=False,
                ),
                {
                    **_record(1324, at=now - timedelta(days=1), state=RequestRecordState.COMPLETED),
                    "user_id": UUID(int=1316),
                },
                {
                    **_record(1325, at=now.replace(minute=5), state=RequestRecordState.REJECTED),
                    "user_id": UUID(int=1317),
                },
            ]
            async with env.session_factory() as session:
                assert isinstance(session.bind, AsyncEngine)
                await ensure_request_record_partitions(session.bind, (now,))
                await session.execute(sa.delete(USAGE_15M))
                await session.execute(sa.delete(LEDGER_REQUEST_RECORDS))
                await session.execute(sa.insert(LEDGER_REQUEST_RECORDS), records)
                await UsageRepository(session).recompute_recent_buckets(now=now)
                # Prove closed data comes from materialized buckets, not a full-day record scan.
                await session.execute(
                    sa.delete(LEDGER_REQUEST_RECORDS).where(
                        LEDGER_REQUEST_RECORDS.c.received_at < now.replace(minute=0)
                    )
                )
                await session.commit()
            service = UsageQueryService(session_factory=env.session_factory)
            with patch.object(usage_module, "database_utc_now", new=AsyncMock(return_value=now)):
                admin = await service.summary(
                    time_zone="UTC", actor_id=USER_ID, actor_role=UserRole.ADMIN
                )
                own = await service.summary(
                    time_zone="UTC", actor_id=USER_ID, actor_role=UserRole.USER
                )
                absent = await service.summary(
                    time_zone="UTC", actor_id=UUID(int=1399), actor_role=UserRole.USER
                )
            assert admin.today.requests == 3
            assert admin.today_active_users == 2
            assert own.today.requests == 2
            assert own.today_active_users == 1
            assert absent.today_active_users == 0
            assert UsageSummaryResponse.from_snapshot(admin).model_dump()["today_active_users"] == 2
            async with env.session_factory() as session:
                await session.execute(
                    sa.delete(LEDGER_REQUEST_RECORDS).where(
                        LEDGER_REQUEST_RECORDS.c.user_id == USER_ID
                    )
                )
                await session.commit()
            with patch.object(usage_module, "database_utc_now", new=AsyncMock(return_value=now)):
                closed_only = await service.summary(
                    time_zone="UTC", actor_id=USER_ID, actor_role=UserRole.ADMIN
                )
            assert closed_only.today.requests == 2
            assert closed_only.today_active_users == 2
            async with env.session_factory() as session:
                await session.execute(sa.delete(USAGE_15M))
                await session.execute(sa.delete(LEDGER_REQUEST_RECORDS))
                await session.commit()

    asyncio.run(scenario())

"""UI-42 / TC-UI-17: real PostgreSQL usage group metadata."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres, temporary_postgres
from holm_control_api import usage_service as usage_service_module
from holm_control_api.usage_service import UsageQueryService
from holm_domain import UserRole
from holm_persistence import create_database_engine, create_session_factory, database_utc_now
from migration_test_support import upgrade_head
from test_PROTO_13_usage import (
    USAGE_CLOCK_OFFSET,
    _async_url,
    _seed_model_instance,
    _seed_usage,
)


@pytest.fixture
def usage_database() -> Iterator[EphemeralPostgres]:
    with temporary_postgres() as database:
        upgrade_head(database.engine)
        yield database


def test_UI_42_TC_UI_17_group_metadata_and_own_scope(
    usage_database: EphemeralPostgres,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # FX-UI-42 reuses FX-PROTO-13's real ledger records and two key owners.
    from holm_control_api.usage_routes import UsageGroupResponse
    from holm_control_api.usage_service import UsageQuery, UsageReport
    from holm_domain import UserStatus
    from holm_persistence import AI_SERVICES, API_KEYS, USERS, UsageGroupBy

    _seed_model_instance(usage_database.engine)

    async def scenario() -> None:
        engine = create_database_engine(_async_url(usage_database))
        factory = create_session_factory(engine)
        try:
            admin_id, user_id, service_id, key_id, boundary = await _seed_usage(factory, engine)

            async def fixed_database_now(session: object) -> datetime:
                del session
                return boundary + USAGE_CLOCK_OFFSET

            monkeypatch.setattr(usage_service_module, "database_utc_now", fixed_database_now)
            usage = UsageQueryService(session_factory=factory)

            async def report(group_by: UsageGroupBy, *, own: bool = False) -> UsageReport:
                return await usage.report(
                    UsageQuery(
                        from_time=boundary - timedelta(days=1),
                        to_time=boundary + timedelta(days=2),
                        group_by=group_by,
                    ),
                    actor_id=user_id if own else admin_id,
                    actor_role=UserRole.USER if own else UserRole.ADMIN,
                )

            own = await report(UsageGroupBy.API_KEY, own=True)
            assert len(own.details) == 1
            assert own.details[0].group_id == key_id
            assert own.details[0].api_key_name == "user-key"
            assert own.details[0].owner_username == "proto-13-user"
            assert own.details[0].owner_is_deleted is False
            before = {group: await report(group) for group in UsageGroupBy}
            for value in before.values():
                assert all(not row.is_deleted for row in value.details)

            async with factory() as session:
                now = await database_utc_now(session)
                await session.execute(
                    sa.update(AI_SERVICES)
                    .where(AI_SERVICES.c.id == service_id)
                    .values(deleted_at=now)
                )
                await session.execute(
                    sa.update(USERS)
                    .where(USERS.c.id == user_id)
                    .values(deleted_at=now, status=UserStatus.DELETED.value)
                )
                await session.execute(
                    sa.update(API_KEYS).where(API_KEYS.c.id == key_id).values(deleted_at=now)
                )
                await session.commit()

            for group_by in UsageGroupBy:
                after = await report(group_by)
                assert after.summary == before[group_by].summary
                assert after.trend == before[group_by].trend
                previous = {row.group_id: row for row in before[group_by].details}
                for row in after.details:
                    assert row.display_name == previous[row.group_id].display_name
                    assert row.totals == previous[row.group_id].totals
                    assert row.status == previous[row.group_id].status
                    assert row.is_deleted is (row.group_id in {service_id, user_id, key_id})
                    payload = UsageGroupResponse.from_group(row).model_dump()
                    assert set(payload) == {
                        "group_id",
                        "display_name",
                        "status",
                        "is_deleted",
                        "api_key_name",
                        "owner_username",
                        "owner_is_deleted",
                        "requests",
                        "input_tokens",
                        "output_tokens",
                        "total_tokens",
                        "error_requests",
                        "partial_token_requests",
                        "error_rate",
                    }
                    if group_by is UsageGroupBy.API_KEY:
                        member = row.group_id == key_id
                        assert payload["api_key_name"] == ("user-key" if member else "admin-key")
                        assert payload["owner_username"] == (
                            "proto-13-user" if member else "proto-13-admin"
                        )
                        assert payload["owner_is_deleted"] is member
                    else:
                        assert payload["api_key_name"] is None
                        assert payload["owner_username"] is None
                        assert payload["owner_is_deleted"] is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())

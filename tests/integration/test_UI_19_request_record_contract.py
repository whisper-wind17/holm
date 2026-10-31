"""UI-19 real PostgreSQL queries; FX-UI-19 derives identities from FX-PROTO-12.

Projection rows are metadata-only synthetic fixtures for the PDD 9.6 window.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from urllib.parse import urlencode
from uuid import UUID

import sqlalchemy as sa
from conftest import EphemeralPostgres
from fixtures.asgi_client import NO_CONFIGURED_CREDENTIAL_ROWS, AsgiTestClient
from holm_common import utc_now
from holm_contracts import VersionPayload
from holm_control_api.application import create_app
from holm_control_api.authentication import AuthenticationService
from holm_control_api.readiness import DependencyCheck
from holm_control_api.request_record_service import RequestRecordQueryService
from holm_domain import ErrorCode, RequestRecordState
from holm_persistence import (
    LEDGER_REQUEST_RECORDS,
    create_database_engine,
    create_session_factory,
    ensure_request_record_partitions,
)
from holm_persistence.ai_services import API_KEYS
from holm_security import PasswordHasher
from migration_test_support import upgrade_head
from test_PROTO_12_request_records import (
    ADMIN_USERNAME,
    CHEAP,
    USER_USERNAME,
    _async_url,
    _login,
    _readiness_ok,
    _seed_identities,
    _seed_model_instance,
)


def test_UI_19_request_filters_and_full_window_error_groups(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    upgrade_head(ephemeral_postgres.engine)
    _seed_model_instance(ephemeral_postgres.engine)

    async def scenario() -> None:
        engine = create_database_engine(_async_url(ephemeral_postgres))
        factory = create_session_factory(engine)
        admin_id, _, service_id, key_id, *_ = await _seed_identities(factory)
        start = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=1)
        await ensure_request_record_partitions(engine, (start - timedelta(days=1), end))
        rows: list[dict[str, object]] = []

        def add(
            number: int,
            at: datetime,
            state: RequestRecordState,
            code: ErrorCode | None = None,
            *,
            other_service: bool = False,
            with_key: bool = True,
        ) -> None:
            rows.append(
                {
                    "request_id": UUID(int=number),
                    "received_at": at,
                    "event_seq": 2,
                    "record_state": state.value,
                    "error_code": None if code is None else code.value,
                    "service_id": UUID(int=999) if other_service else service_id,
                    "user_id": admin_id,
                    "api_key_id": key_id if with_key else None,
                    "updated_at": at,
                }
            )

        # 300 exceeds the list's maximum page size. Last two timestamps tie.
        for number in range(1, 301):
            add(
                number,
                start + timedelta(seconds=min(number - 1, 298)),
                RequestRecordState.FAILED,
                ErrorCode.BACKEND_UNAVAILABLE,
            )
        add(
            301,
            start + timedelta(minutes=10),
            RequestRecordState.REJECTED,
            ErrorCode.INVALID_API_KEY,
        )
        add(302, end, RequestRecordState.UNKNOWN, with_key=False)
        for number, state in enumerate(
            (
                RequestRecordState.COMPLETED,
                RequestRecordState.ABORTED,
                RequestRecordState.RECEIVED,
                RequestRecordState.IN_PROGRESS,
            ),
            start=303,
        ):
            add(number, end, state)
        add(
            310,
            start - timedelta(seconds=1),
            RequestRecordState.FAILED,
            ErrorCode.BACKEND_UNAVAILABLE,
        )
        add(
            311,
            end + timedelta(seconds=1),
            RequestRecordState.FAILED,
            ErrorCode.BACKEND_UNAVAILABLE,
        )
        add(
            312, start, RequestRecordState.FAILED, ErrorCode.BACKEND_UNAVAILABLE, other_service=True
        )
        async with factory() as session:
            await session.execute(sa.insert(LEDGER_REQUEST_RECORDS), rows)
            await session.commit()
        client = AsgiTestClient(
            create_app(
                version_payload=VersionPayload(
                    build_version="0.1.0+ui19",
                    commit="1" * 40,
                    schema_version=1,
                    runtime_compat_hash="2" * 64,
                ),
                readiness_checks=[DependencyCheck(name="database", probe=_readiness_ok)],
                credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
                authentication=AuthenticationService(
                    session_factory=factory,
                    password_hasher=PasswordHasher(CHEAP),
                    minimum_login_failure_seconds=0,
                ),
                request_records=RequestRecordQueryService(session_factory=factory),
            )
        )
        admin = await _login(client, ADMIN_USERNAME)
        user = await _login(client, USER_USERNAME)
        params = {"service_id": str(service_id), "from": start.isoformat(), "to": end.isoformat()}
        path = "/api/v1/requests/error-groups"
        response = await client.request("GET", path + "?" + urlencode(params), cookies=admin)
        assert response.status == 200
        groups = response.json()["items"]
        assert [group["status"] for group in groups] == ["unknown", "rejected", "failed"]
        assert [group["count"] for group in groups] == [1, 1, 300]
        assert groups[0]["error_code"] is None
        failed = groups[2]
        assert failed["error_code"] == ErrorCode.BACKEND_UNAVAILABLE.value
        assert datetime.fromisoformat(failed["first_at"]) == start
        assert datetime.fromisoformat(failed["last_at"]) == start + timedelta(seconds=298)
        assert failed["latest_request_id"] == str(UUID(int=300))
        for group in groups:
            assert set(group) == {
                "status",
                "error_code",
                "count",
                "first_at",
                "last_at",
                "latest_request_id",
            }
        assert set(response.json()) == {"items"}

        for query in (
            {},
            {"from": "bad-UI-19-body-sentinel"},
            {"from": start.replace(tzinfo=None).isoformat()},
            {"from": end.isoformat(), "to": start.isoformat()},
            {"from": start.isoformat(), "service_id": "invalid"},
        ):
            invalid = await client.request("GET", path + "?" + urlencode(query), cookies=admin)
            assert invalid.status == 400
            assert invalid.json()["error"]["code"] == "invalid_request"
            assert "bad-UI-19-body-sentinel" not in str(invalid.json())
        denied = await client.request("GET", path + "?" + urlencode(params), cookies=user)
        assert denied.status == 403
        assert denied.json()["error"]["code"] == "permission_denied"
        unauthenticated = await client.request("GET", path + "?" + urlencode(params))
        assert unauthenticated.status == 401
        empty = await client.request(
            "GET",
            path + "?" + urlencode({**params, "service_id": str(UUID(int=998))}),
            cookies=admin,
        )
        assert empty.status == 200
        assert empty.json() == {"items": []}
        all_services = await client.request(
            "GET",
            path + "?" + urlencode({"from": start.isoformat(), "to": end.isoformat()}),
            cookies=admin,
        )
        assert all_services.json()["items"][2]["count"] == 301
        no_end = await client.request(
            "GET",
            path + "?" + urlencode({"from": start.isoformat(), "service_id": str(service_id)}),
            cookies=admin,
        )
        assert next(g for g in no_end.json()["items"] if g["status"] == "failed")["count"] == 301

        list_params = {
            **params,
            "api_key_id": str(key_id),
            "status": "failed",
            "error_code": "backend_unavailable",
            "request_id": str(UUID(int=1)),
            "user_id": str(admin_id),
        }
        listed = await client.request(
            "GET", "/api/v1/requests?" + urlencode(list_params), cookies=admin
        )
        assert listed.status == 200
        assert len(listed.json()["items"]) == 1
        assert listed.json()["items"][0]["api_key_name"] == "admin-key"
        for overrides in ({"api_key_id": str(UUID(int=998))}, {"status": "rejected"}):
            miss = await client.request(
                "GET", "/api/v1/requests?" + urlencode({**list_params, **overrides}), cookies=admin
            )
            assert miss.status == 200
            assert miss.json()["items"] == []
        invalid_key = await client.request(
            "GET", "/api/v1/requests?api_key_id=invalid", cookies=admin
        )
        assert invalid_key.status == 400
        assert invalid_key.json()["error"]["code"] == "invalid_request"
        no_key = await client.request(
            "GET",
            "/api/v1/requests?" + urlencode({"request_id": str(UUID(int=302))}),
            cookies=admin,
        )
        assert no_key.json()["items"][0]["api_key_name"] is None
        async with factory() as session:
            await session.execute(sa.delete(API_KEYS).where(API_KEYS.c.id == key_id))
            await session.commit()
        deleted_key = await client.request(
            "GET", "/api/v1/requests?" + urlencode(list_params), cookies=admin
        )
        assert deleted_key.status == 200
        assert deleted_key.json()["items"][0]["api_key_name"] is None
        assert deleted_key.json()["items"][0]["api_key_id"] == str(key_id)

        rows.clear()
        # Same code in another status must remain a separate group; six total.
        for number, code in enumerate(
            (ErrorCode.INVALID_API_KEY, ErrorCode.SERVICE_DISABLED, ErrorCode.INFERENCE_TIMEOUT),
            start=400,
        ):
            add(
                number,
                start + timedelta(minutes=20 + number - 400),
                RequestRecordState.FAILED,
                code,
            )
        async with factory() as session:
            await session.execute(sa.insert(LEDGER_REQUEST_RECORDS), rows)
            await session.commit()
        limited = await client.request("GET", path + "?" + urlencode(params), cookies=admin)
        assert limited.status == 200
        limited_groups = limited.json()["items"]
        assert len(limited_groups) == 5
        assert [g["latest_request_id"] for g in limited_groups] == [
            str(UUID(int=n)) for n in (302, 402, 401, 400, 301)
        ]
        assert [(g["status"], g["error_code"]) for g in limited_groups[-2:]] == [
            ("failed", "invalid_api_key"),
            ("rejected", "invalid_api_key"),
        ]
        await engine.dispose()

    asyncio.run(scenario())

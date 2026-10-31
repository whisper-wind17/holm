"""M4-03A / TC-SEC-08/20: FX-M4-03A reuses PROTO-05 accounts and real PG.

FX-M4-03A usage rows follow PROTO-13's accepted ledger projection fixture;
no external services or real credentials are used.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from conftest import EphemeralPostgres, temporary_postgres
from holm_control_api.api_key_service import ApiKeyManagementService, CreatePersonalApiKey
from holm_control_api.usage_routes import UsageGroupResponse
from holm_control_api.usage_service import UsageQuery, UsageQueryForbidden, UsageQueryService
from holm_domain import ApiKeyStatus, RequestRecordStatus, UserRole
from holm_persistence import (
    API_KEYS,
    LEDGER_REQUEST_RECORDS,
    ApiKeyRepository,
    AuditWriter,
    PostgresApiKeyValidationLookup,
    create_database_engine,
    create_session_factory,
    ensure_request_record_partitions,
)
from holm_persistence.role_grants import CONTROL
from holm_persistence.usage_repository import UsageGroupBy
from holm_security import ApiKeyValidationError, issue_api_key, validate_api_key
from migration_test_support import ALEMBIC_CONFIG_PATH, head_revision, upgrade_head
from sqlalchemy.ext.asyncio import create_async_engine
from test_PROTO_05_services_api_keys import (
    ADMIN_USERNAME,
    INSTANCE_1,
    MEMBER_USERNAME,
    _async_url,
    _create_accounts,
    _create_service,
    _environment,
    _login,
    _seed_instances,
)
from test_PROTO_13_usage import _record

PATH = "/api/v1/me/api-keys"


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
        connection.execute(sa.text("TRUNCATE usage.request_records"))
    _seed_instances(ephemeral_postgres.engine)


def test_M4_03A_TC_SEC_08_20_lifecycle_retains_history_and_secret(
    ephemeral_postgres: EphemeralPostgres,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as env:
            ids = await _create_accounts(env)
            member = await _login(env, MEMBER_USERNAME)
            admin = await _login(env, ADMIN_USERNAME)
            service_id = await _create_service(
                env, admin, name="m403a-history", instance_id=INSTANCE_1
            )
            created = await env.client.request(
                "POST", PATH, cookies=member, json_body={"name": "original-key"}
            )
            assert created.status == 201
            key_id = UUID(created.json()["id"])
            plaintext = created.json()["api_key"]
            async with env.session_factory() as session:
                before = await ApiKeyRepository(session).get_owned(
                    key_id, user_id=ids[MEMBER_USERNAME]
                )
            assert before is not None
            lookup = PostgresApiKeyValidationLookup(env.session_factory)
            await validate_api_key(plaintext, lookup=lookup, keyring=env.keyring)
            renamed = await env.client.request(
                "PATCH", f"{PATH}/{key_id}", cookies=member, json_body={"name": "renamed-key"}
            )
            assert renamed.status == 200
            assert renamed.json()["name"] == "renamed-key"
            await validate_api_key(plaintext, lookup=lookup, keyring=env.keyring)
            async with env.session_factory() as session:
                after = await ApiKeyRepository(session).get_owned(
                    key_id, user_id=ids[MEMBER_USERNAME]
                )
            assert after == replace(before, name="renamed-key")
            rejected = await env.client.request("DELETE", f"{PATH}/{key_id}", cookies=member)
            assert rejected.status == 409
            assert rejected.json()["error"]["code"] == "api_key_active"

            now = datetime.now(UTC)
            engine = create_database_engine(_async_url(ephemeral_postgres))
            try:
                await ensure_request_record_partitions(engine, (now,))
            finally:
                await engine.dispose()
            async with env.session_factory() as session, session.begin():
                row = _record(
                    at=now,
                    user_id=ids[MEMBER_USERNAME],
                    api_key_id=key_id,
                    service_id=service_id,
                    status=RequestRecordStatus.COMPLETED,
                    input_tokens=13,
                    output_tokens=7,
                )
                row["instance_id"] = INSTANCE_1
                await session.execute(sa.insert(LEDGER_REQUEST_RECORDS), row)
            usage = UsageQueryService(session_factory=env.session_factory)
            query = UsageQuery(
                from_time=now - timedelta(hours=1),
                to_time=now + timedelta(hours=1),
                api_key_id=key_id,
                group_by=UsageGroupBy.API_KEY,
            )
            report_before = await usage.report(
                query, actor_id=ids[MEMBER_USERNAME], actor_role=UserRole.USER
            )
            revoked = await env.client.request("POST", f"{PATH}/{key_id}:revoke", cookies=member)
            assert revoked.status == 200
            for _ in range(2):
                deleted = await env.client.request("DELETE", f"{PATH}/{key_id}", cookies=member)
                assert deleted.status == 200
                assert deleted.json()["status"] == "Revoked"
                assert plaintext not in deleted.body.decode()
            assert (await env.client.get(PATH, cookies=member)).json()["items"] == []
            assert (
                await env.client.request(
                    "PATCH",
                    f"{PATH}/{key_id}",
                    cookies=member,
                    json_body={"name": "cannot-restore"},
                )
            ).status == 404
            with pytest.raises(ApiKeyValidationError, match="invalid_api_key"):
                await validate_api_key(plaintext, lookup=lookup, keyring=env.keyring)
            report = await usage.report(
                query, actor_id=ids[MEMBER_USERNAME], actor_role=UserRole.USER
            )
            assert report.summary == report_before.summary
            assert (
                report.summary.requests,
                report.summary.input_tokens,
                report.summary.output_tokens,
                report.summary.total_tokens,
            ) == (1, 13, 7, 20)
            assert report.details[0].status is ApiKeyStatus.REVOKED
            assert report.details[0].is_deleted
            assert (
                UsageGroupResponse.from_group(report.details[0]).model_dump()["is_deleted"] is True
            )
            with pytest.raises(UsageQueryForbidden):
                await usage.report(query, actor_id=ids[ADMIN_USERNAME], actor_role=UserRole.USER)
            async with env.session_factory() as session:
                retained = await ApiKeyRepository(session).get_owned(
                    key_id, user_id=ids[MEMBER_USERNAME]
                )
                assert retained is not None and retained.deleted_at is not None
                assert retained.secret_hmac == before.secret_hmac
                assert len(await ApiKeyRepository(session).list_owned(ids[MEMBER_USERNAME])) == 1
                audits = (
                    (
                        await session.execute(
                            sa.text(
                                "SELECT actor_id, object_id, request_id, action, details "
                                "FROM core.audit_events WHERE object_id=:id ORDER BY created_at"
                            ),
                            {"id": key_id},
                        )
                    )
                    .mappings()
                    .all()
                )
                assert len(audits) == 5  # create, rename, revoke, delete, idempotent delete
                assert all(
                    a["actor_id"] == ids[MEMBER_USERNAME]
                    and a["request_id"]
                    and a["action"] == "api_key.change"
                    and a["details"] == {"operation": operation}
                    for a, operation in zip(
                        audits, ("create", "rename", "revoke", "delete", "delete"), strict=True
                    )
                )
                stored = str((await session.execute(sa.select(API_KEYS))).mappings().all()) + str(
                    audits
                )
                assert plaintext not in stored
                assert plaintext.partition("_")[2].partition("_")[2] not in stored
                assert "renamed-key" not in str(audits)
            assert plaintext not in caplog.text

    asyncio.run(scenario())


def test_M4_03A_TC_SEC_08_idor_csrf_auth_and_input_guards(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as env:
            await _create_accounts(env)
            member = await _login(env, MEMBER_USERNAME)
            admin = await _login(env, ADMIN_USERNAME)
            created = await env.client.request(
                "POST", PATH, cookies=member, json_body={"name": "valid-name"}
            )
            key_id = created.json()["id"]
            admin_key = await env.client.request(
                "POST", PATH, cookies=admin, json_body={"name": "admin-key"}
            )
            for method in ("PATCH", "DELETE"):
                body = {"name": "new-name"} if method == "PATCH" else None
                foreign = await env.client.request(
                    method, f"{PATH}/{admin_key.json()['id']}", cookies=member, json_body=body
                )
                assert foreign.status == 404
                assert foreign.json()["error"]["code"] == "api_key_not_found"
                assert (
                    await env.client.request(method, f"{PATH}/{key_id}", json_body=body)
                ).status == 401
                assert (
                    await env.client.request(
                        method, f"{PATH}/{key_id}", cookies=dict(member), json_body=body
                    )
                ).status == 403
                for target in (key_id, str(uuid4())):
                    result = await env.client.request(
                        method, f"{PATH}/{target}", cookies=admin, json_body=body
                    )
                    assert result.status == 404
                    assert result.json()["error"]["code"] == "api_key_not_found"
            for name in ("", "x", " " * 2, " a", "a ", "a" * 41):
                for method, path in (("POST", PATH), ("PATCH", f"{PATH}/{key_id}")):
                    result = await env.client.request(
                        method, path, cookies=member, json_body={"name": name}
                    )
                    assert result.status == 400
                    assert result.json()["error"]["code"] == "invalid_request"
            extra_fields: tuple[dict[str, object], ...] = (
                {"expires_at": None},
                {"service_ids": []},
                {"status": "Active"},
                {"deleted_at": None},
                {"api_key": "secret-input-sentinel"},
            )
            for extra in extra_fields:
                result = await env.client.request(
                    "PATCH",
                    f"{PATH}/{key_id}",
                    cookies=member,
                    json_body={"name": "valid-name", **extra},
                )
                assert result.status == 400
                assert result.json()["error"]["code"] == "invalid_request"
                assert "secret-input-sentinel" not in result.body.decode()
            for name in ("ab", "字" * 40):
                assert (
                    await env.client.request(
                        "PATCH", f"{PATH}/{key_id}", cookies=member, json_body={"name": name}
                    )
                ).status == 200
            assert (await env.client.get(PATH, cookies=member)).json()["items"][0][
                "status"
            ] == "Active"
            async with env.session_factory() as session, session.begin():
                await session.execute(
                    sa.text(
                        "UPDATE core.users SET must_change_password=true WHERE username_norm=:name"
                    ),
                    {"name": MEMBER_USERNAME},
                )
            for method in ("PATCH", "DELETE"):
                denied = await env.client.request(
                    method,
                    f"{PATH}/{key_id}",
                    cookies=member,
                    json_body={"name": "new-name"} if method == "PATCH" else None,
                )
                assert denied.status == 403
                assert denied.json()["error"]["code"] == "password_change_required"

    asyncio.run(scenario())


def test_M4_03A_expired_active_storage_hides_and_user_delete_still_revokes(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as env:
            ids = await _create_accounts(env)
            member = await _login(env, MEMBER_USERNAME)
            created = await env.client.request(
                "POST", PATH, cookies=member, json_body={"name": "expired-key"}
            )
            key_id = UUID(created.json()["id"])
            async with env.session_factory() as session, session.begin():
                await session.execute(
                    sa.text(
                        "UPDATE core.api_keys SET created_at=clock_timestamp()-interval '2 days', "
                        "expires_at=clock_timestamp()-interval '1 day' WHERE id=:id"
                    ),
                    {"id": key_id},
                )
            renamed = await env.client.request(
                "PATCH", f"{PATH}/{key_id}", cookies=member, json_body={"name": "expired-renamed"}
            )
            assert renamed.status == 200 and renamed.json()["status"] == "Expired"
            deleted = await env.client.request("DELETE", f"{PATH}/{key_id}", cookies=member)
            assert deleted.status == 200 and deleted.json()["status"] == "Expired"
            async with env.session_factory() as session:
                record = await ApiKeyRepository(session).get_owned(
                    key_id, user_id=ids[MEMBER_USERNAME]
                )
                assert (
                    record is not None
                    and record.status is ApiKeyStatus.ACTIVE
                    and record.revoked_at is None
                )
            with pytest.raises(ApiKeyValidationError, match="api_key_expired"):
                await validate_api_key(
                    created.json()["api_key"],
                    lookup=PostgresApiKeyValidationLookup(env.session_factory),
                    keyring=env.keyring,
                )
            await env.users.delete_user(
                ids[MEMBER_USERNAME], actor_id=ids[ADMIN_USERNAME], request_id=uuid4()
            )
            async with env.session_factory() as session:
                record = await ApiKeyRepository(session).get_owned(
                    key_id, user_id=ids[MEMBER_USERNAME]
                )
                assert (
                    record is not None
                    and record.status is ApiKeyStatus.REVOKED
                    and record.deleted_at is not None
                )
                events = (
                    await session.execute(
                        sa.text("SELECT count(*) FROM core.domain_events WHERE aggregate_id=:id"),
                        {"id": key_id},
                    )
                ).scalar_one()
                assert (
                    events == 2
                )  # issuance and user-deletion invalidation; hiding is not revocation

    asyncio.run(scenario())


def test_M4_03A_control_role_and_audit_failure_rollback(
    ephemeral_postgres: EphemeralPostgres, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as env:
            ids = await _create_accounts(env)
            engine = create_async_engine(
                _async_url(ephemeral_postgres),
                connect_args={"options": f"-c role={CONTROL} -c timezone=UTC"},
            )
            try:
                factory = create_session_factory(engine)
                service = ApiKeyManagementService(session_factory=factory, keyring=env.keyring)
                async with factory() as session:
                    assert (
                        await session.execute(sa.text("SELECT current_user"))
                    ).scalar_one() == CONTROL
                created = await service.create(
                    CreatePersonalApiKey(name="control-key", expires_at=None, service_ids=()),
                    user_id=ids[MEMBER_USERNAME],
                    user_role=UserRole.USER,
                    request_id=uuid4(),
                )
                await service.rename(
                    created.record.id,
                    name="control-renamed",
                    user_id=ids[MEMBER_USERNAME],
                    request_id=uuid4(),
                )
                await service.revoke(
                    created.record.id, user_id=ids[MEMBER_USERNAME], request_id=uuid4()
                )

                async def fail_audit(*args: object, **kwargs: object) -> None:
                    raise RuntimeError("FX-M4-03A audit unavailable")

                with monkeypatch.context() as patch:
                    patch.setattr(AuditWriter, "add", fail_audit)
                    with pytest.raises(RuntimeError, match="audit unavailable"):
                        await service.rename(
                            created.record.id,
                            name="rollback-name",
                            user_id=ids[MEMBER_USERNAME],
                            request_id=uuid4(),
                        )
                    with pytest.raises(RuntimeError, match="audit unavailable"):
                        await service.delete(
                            created.record.id, user_id=ids[MEMBER_USERNAME], request_id=uuid4()
                        )
                listed = await service.list(ids[MEMBER_USERNAME])
                assert (
                    len(listed) == 1
                    and listed[0].name == "control-renamed"
                    and listed[0].deleted_at is None
                )
                deleted = await service.delete(
                    created.record.id, user_id=ids[MEMBER_USERNAME], request_id=uuid4()
                )
                assert deleted.deleted_at is not None
                assert await service.list(ids[MEMBER_USERNAME]) == ()
            finally:
                await engine.dispose()

    asyncio.run(scenario())


def test_M4_03A_migration_preserves_legacy_names_and_key_material() -> None:
    with temporary_postgres() as database:
        with database.engine.connect() as connection:
            config = Config(str(ALEMBIC_CONFIG_PATH))
            config.attributes["connection"] = connection
            command.upgrade(config, "20260928_0031")

        async def scenario() -> None:
            async with _environment(database) as env:
                ids = await _create_accounts(env)
                issued = [issue_api_key(env.keyring), issue_api_key(env.keyring)]
                async with env.session_factory() as session, session.begin():
                    for name, key in zip(("x", "宽" * 128), issued, strict=True):
                        await session.execute(
                            sa.text(
                                "INSERT INTO core.api_keys (id,user_id,name,lookup_id,"
                                "display_prefix,"
                                "last4,pepper_key_id,secret_hmac,status,created_at) "
                                "VALUES (:id,:uid,:name,:lookup,'pwsk_',:last4,:pepper,:digest,"
                                "'Active',clock_timestamp())"
                            ),
                            {
                                "id": uuid4(),
                                "uid": ids[MEMBER_USERNAME],
                                "name": name,
                                "lookup": key.lookup_id,
                                "last4": key.last4,
                                "pepper": key.pepper_key_id,
                                "digest": key.secret_hmac,
                            },
                        )
                upgrade_head(database.engine)
                with database.engine.connect() as connection:
                    assert (
                        connection.scalar(sa.text("SELECT version_num FROM public.alembic_version"))
                        == head_revision()
                    )
                member = await _login(env, MEMBER_USERNAME)
                listed = await env.client.get(PATH, cookies=member)
                assert listed.status == 200
                assert sorted(len(k["name"]) for k in listed.json()["items"]) == [1, 128]
                async with env.session_factory() as session:
                    records = await ApiKeyRepository(session).list_owned(ids[MEMBER_USERNAME])
                    assert all(k.deleted_at is None for k in records)
                    assert {k.secret_hmac for k in records} == {k.secret_hmac for k in issued}
                for key in issued:
                    await validate_api_key(
                        key.plaintext,
                        lookup=PostgresApiKeyValidationLookup(env.session_factory),
                        keyring=env.keyring,
                    )

        asyncio.run(scenario())

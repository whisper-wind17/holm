"""TC-DL-01: real PostgreSQL creation, retry convergence, and seven fault controls.

FX-MS-01 creation fixture: reuse the registration race adapter's deterministic
immutable metadata. This tests orchestration, not live Hub certification.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from fixtures.asgi_client import NO_CONFIGURED_CREDENTIAL_ROWS, AsgiTestClient, SessionCookies
from fixtures.source_mutation import load_mutated_module
from holm_contracts import VersionPayload
from holm_control_api import download_creation_service as creation_module
from holm_control_api.application import create_app
from holm_control_api.authentication import AuthenticationService
from holm_control_api.download_creation_service import (
    DownloadCreationService,
    DownloadCreationSuccess,
)
from holm_control_api.external_path_resolution import (
    ExternalPathPolicy,
    ExternalPathResolutionAdapter,
)
from holm_control_api.model_registration_service import ModelRegistrationService
from holm_control_api.model_resolution import ModelResolutionRegistry, ResolutionFailed
from holm_control_api.model_resolution_service import ModelResolutionService
from holm_control_api.readiness import DependencyCheck, ReadinessReason
from holm_domain import DownloadTaskStatus, ModelOriginType, UserRole
from holm_persistence import (
    AsyncSessionFactory,
    AuditRepository,
    IdempotencyRepository,
    UserRepository,
    create_database_engine,
    create_session_factory,
    database_utc_now,
)
from holm_persistence.download_task_repository import DownloadTaskRepository
from holm_security import PasswordHasher
from test_TC_MDL_01_10_11_13_model_catalog_api import CHEAP, PASSWORD, _cookie, _truncate
from test_TC_MDL_05_07_model_registration_concurrency import (
    HMAC_SECRET,
    KEYRING,
    _prepare,
    _RaceAdapter,
)

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_SOURCE = ROOT / "packages/persistence/src/holm_persistence/download_task_repository.py"
SERVICE_SOURCE = ROOT / "apps/control_api/src/holm_control_api/download_creation_service.py"
ALLOWLIST_SOURCE = ROOT / "packages/security/src/holm_security/audit_details.py"
SOURCE = ModelOriginType.HUGGING_FACE


@pytest.fixture(autouse=True)
def empty_database(ephemeral_postgres: EphemeralPostgres) -> Iterator[None]:
    _prepare(ephemeral_postgres.engine)
    _truncate(ephemeral_postgres.engine)
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(sa.text("TRUNCATE core.jobs, core.domain_events RESTART IDENTITY"))
    yield
    _truncate(ephemeral_postgres.engine)
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(sa.text("TRUNCATE core.jobs, core.domain_events RESTART IDENTITY"))


@dataclass
class Environment:
    factory: AsyncSessionFactory
    registration: ModelRegistrationService
    service: DownloadCreationService
    client: AsgiTestClient
    actor_id: UUID
    second_actor_id: UUID


async def _ready() -> ReadinessReason:
    return ReadinessReason.OK


@asynccontextmanager
async def environment(
    fixture: EphemeralPostgres, external_root: Path = Path("/tmp")
) -> AsyncIterator[Environment]:
    engine = create_database_engine(fixture.engine.url, connect_timeout_seconds=5)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            now = await database_utc_now(session)
            hasher = PasswordHasher(CHEAP)
            password_hash = await asyncio.to_thread(hasher.hash, PASSWORD)
            users = UserRepository(session)
            admin = await users.create(
                username="download-admin",
                display_name="Download Admin",
                role=UserRole.ADMIN,
                password_hash=password_hash,
                now=now,
                must_change_password=False,
            )
            await users.create(
                username="download-member",
                display_name="Download Member",
                role=UserRole.USER,
                password_hash=password_hash,
                now=now,
                must_change_password=False,
            )
            second_admin = await users.create(
                username="download-admin-two",
                display_name="Second Admin",
                role=UserRole.ADMIN,
                password_hash=password_hash,
                now=now,
                must_change_password=False,
            )
            await session.commit()
        resolution = ModelResolutionService(
            session_factory=factory,
            keyring=KEYRING,
            registry=ModelResolutionRegistry(
                {SOURCE: _RaceAdapter(), ModelOriginType.MODELSCOPE: _RaceAdapter()},
                external_path_adapters={
                    ModelOriginType.LOCAL_PATH: ExternalPathResolutionAdapter(
                        policy=ExternalPathPolicy.for_configured_roots((external_root,))
                    )
                },
            ),
        )
        registration = ModelRegistrationService(
            session_factory=factory, resolution=resolution, idempotency_hmac_secret=HMAC_SECRET
        )
        service = DownloadCreationService(
            session_factory=factory, registration=registration, idempotency_hmac_secret=HMAC_SECRET
        )
        app = create_app(
            version_payload=VersionPayload(
                build_version="2.2.0+dl080001",
                commit="c" * 40,
                schema_version=1,
                runtime_compat_hash="d" * 64,
            ),
            readiness_checks=[DependencyCheck(name="database", probe=_ready)],
            credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
            authentication=AuthenticationService(
                session_factory=factory, password_hasher=hasher, minimum_login_failure_seconds=0
            ),
            model_registration=registration,
            download_creation=service,
        )
        yield Environment(
            factory, registration, service, AsgiTestClient(app), admin.id, second_admin.id
        )
    finally:
        await engine.dispose()


async def create(
    env: Environment,
    *,
    key: str = "download-key",
    source: ModelOriginType = SOURCE,
    repository: str = "Owner/Model",
    revision: str | None = None,
    actor_id: UUID | None = None,
) -> DownloadCreationSuccess:
    result = await env.service.create(
        actor_id=actor_id or env.actor_id,
        request_id=uuid4(),
        idempotency_key=key,
        source=source,
        repository_id=repository,
        external_path=None,
        revision=revision,
    )
    assert isinstance(result, DownloadCreationSuccess)
    return result


def rows(fixture: EphemeralPostgres, table: str) -> list[dict[str, Any]]:
    assert table in {
        "models",
        "download_tasks",
        "download_subscriptions",
        "idempotency_records",
        "audit_events",
        "jobs",
    }
    with fixture.engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                sa.text(f"SELECT * FROM core.{table} ORDER BY id")
            ).mappings()
        ]


def assert_created(fixture: EphemeralPostgres) -> None:
    tasks = rows(fixture, "download_tasks")
    assert len(tasks) == 1
    task = tasks[0]
    assert task["status"] == "Queued"
    assert task["resolved_revision"] is None
    assert task["requested_revision_norm"] == "@default"
    assert task["repository_id"] == "Owner/Model"
    assert task["attempt"] == 1 and task["downloaded_bytes"] == 0
    assert all(
        task[name] is None
        for name in (
            "artifact_id",
            "lease_owner",
            "lease_until",
            "staging_path",
            "error_code",
            "completed_at",
            "total_bytes",
        )
    )
    assert len(rows(fixture, "models")) == 1
    jobs = rows(fixture, "jobs")
    assert len(jobs) == 1
    assert jobs[0]["job_type"] == "download.resolve"
    assert jobs[0]["payload"] == {}
    assert jobs[0]["target_object_id"] == task["id"]
    assert jobs[0]["target_object_version"] == task["attempt"] == 1
    assert jobs[0]["status"] == "Queued"
    assert {row["operation"] for row in rows(fixture, "idempotency_records")} == {
        "model.register",
        "download.create",
    }
    events = [row for row in rows(fixture, "audit_events") if row["action"] == "download.change"]
    assert events
    for event in events:
        assert event["object_type"] == "download_task" and event["object_id"] == task["id"]
        assert event["result"] == "succeeded" and event["actor_type"] == "user"
        assert set(event["details"]) == {"operation", "reason_code", "source_type"}
        assert event["details"]["source_type"] == SOURCE.value
        assert event["details"]["operation"] in {"created", "joined_existing"}


async def creation_scenario(env: Environment, fixture: EphemeralPostgres) -> None:
    registered = await env.registration.register(
        actor_id=env.actor_id,
        idempotency_key="download-key",
        source=SOURCE,
        repository_id=" Owner/Model ",
        external_path=None,
        revision=None,
    )
    assert not isinstance(registered, ResolutionFailed)
    first = await create(env)
    assert first.task.model_id == registered.model.id
    assert first.disposition == "created" and not first.replayed
    subscriptions = rows(fixture, "download_subscriptions")
    replay = await create(env, repository=" Owner/Model ")
    assert replay.task == first.task and replay.replayed
    assert replay.disposition == "joined_existing"
    joined = await create(env, key="another-key")
    assert joined.task == first.task and not joined.replayed
    assert joined.disposition == "joined_existing"
    assert rows(fixture, "download_subscriptions") == subscriptions
    assert subscriptions[0]["subscriber_id"] == env.actor_id
    assert subscriptions[0]["actor_id"] == env.actor_id
    assert subscriptions[0]["subscriber_type"] == "Standalone"
    assert len(rows(fixture, "audit_events")) == 2
    assert_created(fixture)


def test_TC_DL_01_creation_registration_replay_subscription_and_audit(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def run() -> None:
        async with environment(ephemeral_postgres) as env:
            await creation_scenario(env, ephemeral_postgres)

    asyncio.run(run())


async def retry_scenario(
    env: Environment, fixture: EphemeralPostgres, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = DownloadTaskRepository.create_or_join

    async def fail_before_task(self: DownloadTaskRepository, **kwargs: Any) -> Any:
        raise RuntimeError("injected before task")

    with monkeypatch.context() as patch:
        patch.setattr(DownloadTaskRepository, "create_or_join", fail_before_task)
        with pytest.raises(RuntimeError, match="injected before task"):
            await create(env)
    assert DownloadTaskRepository.create_or_join is original
    assert len(rows(fixture, "models")) == 1
    assert [r["operation"] for r in rows(fixture, "idempotency_records")] == ["model.register"]
    assert (
        rows(fixture, "download_tasks")
        == rows(fixture, "download_subscriptions")
        == rows(fixture, "audit_events")
        == []
    )
    first = await create(env)
    replay = await create(env)
    assert first.task == replay.task and replay.replayed
    assert len(rows(fixture, "download_tasks")) == 1
    assert_created(fixture)


def test_TC_DL_01_two_transaction_failure_same_key_retry_converges(
    ephemeral_postgres: EphemeralPostgres, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        async with environment(ephemeral_postgres) as env:
            await retry_scenario(env, ephemeral_postgres, monkeypatch)

    asyncio.run(run())


@pytest.mark.parametrize("failure_point", ["subscribe", "claim", "audit"])
def test_TC_DL_01_download_transaction_is_atomic(
    ephemeral_postgres: EphemeralPostgres, monkeypatch: pytest.MonkeyPatch, failure_point: str
) -> None:
    async def run() -> None:
        async with environment(ephemeral_postgres) as env:
            target: Any = {
                "subscribe": DownloadTaskRepository,
                "claim": IdempotencyRepository,
                "audit": AuditRepository,
            }[failure_point]
            method = {"subscribe": "subscribe", "claim": "claim", "audit": "add"}[failure_point]
            original = getattr(target, method)

            async def fail(self: Any, *args: Any, **kwargs: Any) -> Any:
                if failure_point == "claim" and kwargs["operation"] == "model.register":
                    return await original(self, *args, **kwargs)
                raise RuntimeError("injected download transaction failure")

            with monkeypatch.context() as patch:
                patch.setattr(target, method, fail)
                with pytest.raises(RuntimeError, match="injected download transaction"):
                    await create(env)
            assert rows(ephemeral_postgres, "download_tasks") == []
            assert rows(ephemeral_postgres, "download_subscriptions") == []
            assert rows(ephemeral_postgres, "audit_events") == []
            assert [r["operation"] for r in rows(ephemeral_postgres, "idempotency_records")] == [
                "model.register"
            ]
            await create(env)
            assert_created(ephemeral_postgres)

    asyncio.run(run())


async def race_scenario(
    env: Environment,
    fixture: EphemeralPostgres,
    monkeypatch: pytest.MonkeyPatch,
    repository_class: Any = DownloadTaskRepository,
    *,
    same_key: bool = False,
) -> None:
    gate = asyncio.Barrier(2)
    pids: set[int] = set()
    original = repository_class.create_or_join

    async def together(self: Any, **kwargs: Any) -> Any:
        pids.add(
            int((await self._session.execute(sa.text("SELECT pg_backend_pid()"))).scalar_one())
        )
        await asyncio.wait_for(gate.wait(), 5)
        return await original(self, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(repository_class, "create_or_join", together)
        patch.setattr(creation_module, "DownloadTaskRepository", repository_class)
        results = await asyncio.wait_for(
            asyncio.gather(
                create(env, key="race-one"),
                create(
                    env,
                    key="race-one" if same_key else "race-two",
                    actor_id=env.actor_id if same_key else env.second_actor_id,
                    repository=" Owner/Model ",
                ),
                return_exceptions=True,
            ),
            15,
        )
    assert len(pids) == 2, "race did not use two simultaneous PostgreSQL connections"
    assert all(isinstance(result, DownloadCreationSuccess) for result in results), results
    successes = [result for result in results if isinstance(result, DownloadCreationSuccess)]
    assert len({result.task.id for result in successes}) == 1
    assert sorted(result.disposition for result in successes) == ["created", "joined_existing"]
    assert len(rows(fixture, "download_subscriptions")) == (1 if same_key else 2)
    assert len(rows(fixture, "audit_events")) == (1 if same_key else 2)
    assert sum(result.replayed for result in successes) == (1 if same_key else 0)
    assert_created(fixture)


@pytest.mark.parametrize("same_key", [False, True])
def test_TC_DL_01_real_multiconnection_race(
    ephemeral_postgres: EphemeralPostgres, monkeypatch: pytest.MonkeyPatch, same_key: bool
) -> None:
    async def run() -> None:
        async with environment(ephemeral_postgres) as env:
            await race_scenario(env, ephemeral_postgres, monkeypatch, same_key=same_key)

    asyncio.run(run())


@pytest.mark.parametrize(
    "source,revision", [(SOURCE, "release-v1"), (ModelOriginType.MODELSCOPE, None)]
)
def test_TC_DL_01_distinct_source_or_revision_creates_distinct_task(
    ephemeral_postgres: EphemeralPostgres, source: ModelOriginType, revision: str | None
) -> None:
    async def run() -> None:
        async with environment(ephemeral_postgres) as env:
            first = await create(env)
            second = await create(env, key="distinct", source=source, revision=revision)
            assert first.task.id != second.task.id
            assert len(rows(ephemeral_postgres, "download_tasks")) == 2
            assert all(
                row["resolved_revision"] is None
                for row in rows(ephemeral_postgres, "download_tasks")
            )

    asyncio.run(run())


async def external_scenario(env: Environment) -> None:
    result = await env.service.create(
        actor_id=env.actor_id,
        request_id=uuid4(),
        idempotency_key="external",
        source=ModelOriginType.LOCAL_PATH,
        repository_id=None,
        external_path="/private/TC-DL-01-path-sentinel",
        revision=None,
    )
    assert isinstance(result, ResolutionFailed)
    assert result.result.reason_code.value == "source_not_found"


def test_TC_DL_01_external_source_rejected_before_registration(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def run() -> None:
        async with environment(ephemeral_postgres) as env:
            await external_scenario(env)
            assert (
                rows(ephemeral_postgres, "models")
                == rows(ephemeral_postgres, "idempotency_records")
                == []
            )

    asyncio.run(run())


def test_TC_DL_01_http_contract_authentication_validation_and_external_refusal(
    ephemeral_postgres: EphemeralPostgres, caplog: pytest.LogCaptureFixture
) -> None:
    async def run() -> None:
        async with environment(ephemeral_postgres) as env:
            body = {"source": SOURCE.value, "repository_id": "Owner/Model"}
            unauthorized = await env.client.request("POST", "/api/v1/downloads", json_body=body)
            assert unauthorized.status == 401
            for username, expected in (("download-member", 403), ("download-admin", 200)):
                login = await env.client.request(
                    "POST",
                    "/api/v1/auth/login",
                    json_body={"username": username, "password": PASSWORD},
                )
                assert login.status == 200
                cookies = SessionCookies(
                    {"__Host-pws_session": _cookie(login, "__Host-pws_session")},
                    csrf_token=str(login.json()["csrf_token"]),
                )
                response = await env.client.request(
                    "POST",
                    "/api/v1/downloads",
                    json_body=body,
                    cookies=cookies,
                    headers={"Idempotency-Key": "api-key"},
                )
                assert response.status == expected, response.text
                assert response.server_error is None
            assert set(response.json()) == {"id", "model_id", "status", "replayed", "disposition"}
            assert response.json()["status"] == "Queued"
            assert response.json()["disposition"] == "created"
            assert "owner/model" not in response.text.lower()
            assert dict(response.headers)["x-request-id"]
            changed = await env.client.request(
                "POST",
                "/api/v1/downloads",
                json_body={**body, "repository_id": "Owner/Other"},
                cookies=cookies,
                headers={"Idempotency-Key": "api-key"},
            )
            assert (
                changed.status == 409 and changed.json()["error"]["code"] == "idempotency_conflict"
            )
            for headers in ({}, {"Idempotency-Key": ""}, {"Idempotency-Key": "x" * 257}):
                invalid = await env.client.request(
                    "POST", "/api/v1/downloads", json_body=body, cookies=cookies, headers=headers
                )
                assert invalid.status == 400
            invalid_body = await env.client.request(
                "POST",
                "/api/v1/downloads",
                json_body={**body, "source_key": "TC-DL-01-private-field"},
                cookies=cookies,
                headers={"Idempotency-Key": "invalid-body"},
            )
            assert invalid_body.status == 400
            assert "TC-DL-01-private-field" not in invalid_body.text
            external = await env.client.request(
                "POST",
                "/api/v1/downloads",
                json_body={
                    "source": "Local Path",
                    "external_path": "/private/TC-DL-01-path-sentinel",
                },
                cookies=cookies,
                headers={"Idempotency-Key": "external-api"},
            )
            assert external.status == 200
            assert external.json()["reason_code"] == "source_not_found"
            assert external.server_error is None
            assert "TC-DL-01-path-sentinel" not in external.text + caplog.text
            assert_created(ephemeral_postgres)

    asyncio.run(run())


# Mutations replace production modules, not the assertions or returned test data.
@pytest.mark.parametrize(
    "mutation", ["resolved_revision", "operation", "audit", "subscription", "retry", "external"]
)
def test_TC_DL_01_positive_controls(
    ephemeral_postgres: EphemeralPostgres,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
) -> None:
    async def run() -> None:
        async with environment(ephemeral_postgres) as env:
            if mutation == "resolved_revision":
                mutated = load_mutated_module(
                    REPOSITORY_SOURCE,
                    [("resolved_revision=None", 'resolved_revision="7" * 40')],
                    module_name="_dl_resolved",
                    destination=tmp_path / "resolved.py",
                )
                monkeypatch.setattr(
                    creation_module, "DownloadTaskRepository", mutated.DownloadTaskRepository
                )
            elif mutation == "subscription":
                mutated = load_mutated_module(
                    REPOSITORY_SOURCE,
                    [("subscriber_id=actor_id,", "subscriber_id=new_uuid7(),")],
                    module_name="_dl_subscription",
                    destination=tmp_path / "subscription.py",
                )
                monkeypatch.setattr(
                    creation_module, "DownloadTaskRepository", mutated.DownloadTaskRepository
                )
            elif mutation == "audit":
                allowlist = load_mutated_module(
                    ALLOWLIST_SOURCE,
                    [
                        (
                            '"download.change": frozenset({"operation", "reason_code", '
                            '"source_type"})',
                            '"download.change": frozenset({"operation", "reason_code", '
                            '"source_type", "repository_id"})',
                        )
                    ],
                    module_name="_dl_allowlist",
                    destination=tmp_path / "allowlist.py",
                )
                mutated = load_mutated_module(
                    SERVICE_SOURCE,
                    [
                        (
                            '"source_type": model.source_type.value,',
                            '"source_type": model.source_type.value, '
                            '"repository_id": model.repository_id,',
                        )
                    ],
                    module_name="_dl_audit",
                    destination=tmp_path / "audit.py",
                )
                monkeypatch.setattr(mutated, "build_audit_details", allowlist.build_audit_details)
                monkeypatch.setattr(mutated, "DownloadCreationSuccess", DownloadCreationSuccess)
                env.service = mutated.DownloadCreationService(
                    session_factory=env.factory,
                    registration=env.registration,
                    idempotency_hmac_secret=HMAC_SECRET,
                )
            else:
                replacements = {
                    "operation": [
                        (
                            'CREATE_DOWNLOAD_OPERATION: Final[str] = "download.create"',
                            'CREATE_DOWNLOAD_OPERATION: Final[str] = "model.register"',
                        )
                    ],
                    "retry": [("if not claim.claimed:", "if False:")],
                    "external": [
                        (
                            "if source is ModelOriginType.LOCAL_PATH:",
                            "if source is ModelOriginType.LOCAL_PATH and False:",
                        ),
                        (
                            "        registered = await self._registration.register(",
                            "        source = ModelOriginType.HUGGING_FACE\n"
                            "        repository_id = 'Owner/Model'\n"
                            "        external_path = None\n"
                            "        registered = await self._registration.register(",
                        ),
                    ],
                }[mutation]
                mutated = load_mutated_module(
                    SERVICE_SOURCE,
                    replacements,
                    module_name=f"_dl_{mutation}",
                    destination=tmp_path / f"{mutation}.py",
                )
                monkeypatch.setattr(mutated, "DownloadCreationSuccess", DownloadCreationSuccess)
                env.service = mutated.DownloadCreationService(
                    session_factory=env.factory,
                    registration=env.registration,
                    idempotency_hmac_secret=HMAC_SECRET,
                )
            if mutation == "external":
                with pytest.raises(AssertionError):
                    await external_scenario(env)
                assert len(rows(ephemeral_postgres, "download_tasks")) == 1
            elif mutation == "operation":
                with pytest.raises(RuntimeError, match="download replay object unavailable"):
                    await creation_scenario(env, ephemeral_postgres)
            elif mutation == "retry":
                # Move the first task out of the pre-resolution index so the
                # client-key replay is the only protection against a second task.
                first = await create(env)
                with ephemeral_postgres.engine.begin() as connection:
                    connection.execute(sa.text("UPDATE core.download_tasks SET status='Failed'"))
                with pytest.raises(AssertionError):
                    replay = await create(env)
                    assert replay.task.id == first.task.id and replay.replayed
                assert len(rows(ephemeral_postgres, "download_tasks")) == 2
            else:
                with pytest.raises(AssertionError):
                    await creation_scenario(env, ephemeral_postgres)
            print(f"TC-DL-01 positive control {mutation}: broken production behavior detected")

    asyncio.run(run())


def test_TC_DL_01_replay_after_task_leaves_partial_index(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def run() -> None:
        async with environment(ephemeral_postgres) as env:
            first = await create(env)
            with ephemeral_postgres.engine.begin() as connection:
                connection.execute(sa.text("UPDATE core.download_tasks SET status='Failed'"))
            replay = await create(env)
            assert replay.task.id == first.task.id and replay.replayed
            assert replay.task.status is DownloadTaskStatus.FAILED
            assert len(rows(ephemeral_postgres, "download_tasks")) == 1
            assert len(rows(ephemeral_postgres, "download_subscriptions")) == 1
            assert len(rows(ephemeral_postgres, "audit_events")) == 1

    asyncio.run(run())


def test_TC_DL_01_positive_control_preselect_race_fails(
    ephemeral_postgres: EphemeralPostgres, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Synchronize after both SELECTs: both transactions observed an absent row.
    # The mutation still uses the real unique index; its loser must fail loudly.
    mutant_body = """        found = (await self._session.execute(sa.select(DOWNLOAD_TASKS).where(
            DOWNLOAD_TASKS.c.source_type == source_type.value,
            DOWNLOAD_TASKS.c.repository_id == repository_id,
            DOWNLOAD_TASKS.c.requested_revision_norm == requested_revision_norm,
            DOWNLOAD_TASKS.c.status.in_(('Queued', 'Resolving')),
        ))).mappings().one_or_none()
        await _after_select()
        if found is not None:
            return DownloadTaskCreation(_task(found), created=False)
        row = (await self._session.execute(sa.insert(DOWNLOAD_TASKS).values(
            id=new_uuid7(), model_id=model_id, source_type=source_type.value,
            repository_id=repository_id, requested_revision_norm=requested_revision_norm,
            status='Queued', attempt=1, downloaded_bytes=0,
            created_at=now, updated_at=now,
        ).returning(*DOWNLOAD_TASKS.c))).mappings().one()
        return DownloadTaskCreation(_task(row), created=True)

"""
    text = REPOSITORY_SOURCE.read_text()
    start = text.index("        statement = (\n            sa.insert(DOWNLOAD_TASKS)")
    end = text.index("    async def subscribe(", start)
    mutated = load_mutated_module(
        REPOSITORY_SOURCE,
        [(text[start:end], mutant_body)],
        module_name="_dl_preselect",
        destination=tmp_path / "preselect.py",
    )

    async def run() -> None:
        gate = asyncio.Barrier(2)
        reached = 0

        async def after_select() -> None:
            nonlocal reached
            reached += 1
            await asyncio.wait_for(gate.wait(), 5)

        monkeypatch.setattr(mutated, "_after_select", after_select, raising=False)
        async with environment(ephemeral_postgres) as env:
            with pytest.raises(AssertionError):
                await race_scenario(
                    env, ephemeral_postgres, monkeypatch, mutated.DownloadTaskRepository
                )
            assert reached == 2
            assert len(rows(ephemeral_postgres, "download_tasks")) == 1
            print(
                "TC-DL-01 positive control preselect: two empty reads, "
                "unique-conflict loser detected"
            )

    asyncio.run(run())


def test_TC_DL_01_resolution_failure_is_explicit_and_writes_nothing(
    ephemeral_postgres: EphemeralPostgres,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fail_adapter(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("TC-DL-01-source-response-sentinel")

    monkeypatch.setattr(_RaceAdapter, "resolve", fail_adapter)

    async def run() -> None:
        async with environment(ephemeral_postgres) as env:
            login = await env.client.request(
                "POST",
                "/api/v1/auth/login",
                json_body={"username": "download-admin", "password": PASSWORD},
            )
            response = await env.client.request(
                "POST",
                "/api/v1/downloads",
                json_body={"source": SOURCE.value, "repository_id": "Owner/Model"},
                cookies=SessionCookies(
                    {"__Host-pws_session": _cookie(login, "__Host-pws_session")},
                    csrf_token=str(login.json()["csrf_token"]),
                ),
                headers={"Idempotency-Key": "failed-source"},
            )
            assert response.status == 200 and response.server_error is None
            assert response.json() == {
                "outcome": "unresolved",
                "status": None,
                "reason_code": "validation_failed",
                "next_action": "retry_later",
            }
            assert "TC-DL-01-source-response-sentinel" not in response.text + caplog.text
            for table in (
                "models",
                "download_tasks",
                "download_subscriptions",
                "idempotency_records",
            ):
                assert rows(ephemeral_postgres, table) == []
            assert not any(
                row["action"] == "download.change"
                for row in rows(ephemeral_postgres, "audit_events")
            )

    asyncio.run(run())


def test_TC_DL_01_unrelated_integrity_failures_are_sanitized_not_joined(
    ephemeral_postgres: EphemeralPostgres, monkeypatch: pytest.MonkeyPatch
) -> None:
    from holm_persistence import download_task_repository as repository_module

    async def run() -> None:
        async with environment(ephemeral_postgres) as env:
            first = await create(env)
            monkeypatch.setattr(repository_module, "new_uuid7", lambda: first.task.id)
            with pytest.raises(RuntimeError, match="download task creation failed") as caught:
                await create(env, key="pk-conflict", revision="different-revision")
            assert caught.value.__context__ is None and caught.value.__cause__ is None
            assert len(rows(ephemeral_postgres, "download_tasks")) == 1
            async with env.factory() as session:
                now = await database_utc_now(session)
                with pytest.raises(
                    RuntimeError, match="download subscription creation failed"
                ) as error:
                    await DownloadTaskRepository(session).subscribe(
                        task_id=uuid4(), actor_id=env.actor_id, now=now
                    )
                assert error.value.__context__ is None and error.value.__cause__ is None
            assert len(rows(ephemeral_postgres, "download_subscriptions")) == 1

    asyncio.run(run())

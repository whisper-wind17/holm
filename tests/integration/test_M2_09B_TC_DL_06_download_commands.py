"""TC-DL-06: command matrix, idempotency, audit and lease-owned termination."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Final, cast
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from fastapi.routing import APIRoute
from fixtures.asgi_client import NO_CONFIGURED_CREDENTIAL_ROWS, AsgiTestClient, SessionCookies
from fixtures.source_mutation import load_mutated_module
from holm_contracts import VersionPayload
from holm_control_api.application import create_app
from holm_control_api.authentication import AuthenticationService
from holm_control_api.download_command_routes import create_download_command_router
from holm_control_api.download_command_service import (
    DOWNLOAD_COMMAND_MATRIX,
    STAGING_CLEANUP_REQUESTED_REASON,
    DownloadAlreadyInProgress,
    DownloadCommand,
    DownloadCommandErrorCode,
    DownloadCommandNotAllowed,
    DownloadCommandNotFound,
    DownloadCommandResult,
    DownloadCommandService,
)
from holm_control_api.readiness import DependencyCheck, ReadinessReason
from holm_domain import (
    DOWNLOAD_RESOLUTION_JOB_TYPE,
    DownloadTaskStatus,
    JobStatus,
    MetadataStatus,
    ModelOriginType,
    UserRole,
)
from holm_domain.model_acquisition import DownloadCancellationReason
from holm_download_worker import DownloadResolutionJobHandler, DownloadWorker
from holm_download_worker.resolution_subprocess import (
    ResolutionSubprocessRequest,
    ResolutionSubprocessResult,
)
from holm_persistence import (
    JobDraft,
    JobRepository,
    UserRepository,
    create_database_engine,
    create_session_factory,
    database_utc_now,
)
from holm_persistence.download_task_repository import (
    DownloadCommandTaskRecord,
    DownloadTaskRepository,
)
from holm_persistence.model_domain import DOWNLOAD_SUBSCRIPTIONS, DOWNLOAD_TASKS, MODELS
from holm_persistence.model_sources import MODEL_SOURCES
from holm_security import PasswordHasher
from migration_test_support import upgrade_head
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from test_TC_MDL_01_10_11_13_model_catalog_api import CHEAP, PASSWORD, _cookie

SOURCE: Final[ModelOriginType] = ModelOriginType.HUGGING_FACE
ROOT: Final[Path] = Path(__file__).resolve().parents[2]
COMMAND_SERVICE_SOURCE: Final[Path] = (
    ROOT / "apps/control_api/src/holm_control_api/download_command_service.py"
)
DOWNLOAD_REPOSITORY_SOURCE: Final[Path] = (
    ROOT / "packages/persistence/src/holm_persistence/download_task_repository.py"
)
EXPECTED_MATRIX: Final[dict[DownloadTaskStatus, frozenset[DownloadCommand]]] = {
    DownloadTaskStatus.QUEUED: frozenset({DownloadCommand.CANCEL}),
    DownloadTaskStatus.RESOLVING: frozenset({DownloadCommand.CANCEL}),
    DownloadTaskStatus.DOWNLOADING: frozenset({DownloadCommand.PAUSE, DownloadCommand.CANCEL}),
    DownloadTaskStatus.PAUSED: frozenset({DownloadCommand.RESUME, DownloadCommand.CANCEL}),
    DownloadTaskStatus.VERIFYING: frozenset({DownloadCommand.CANCEL}),
    DownloadTaskStatus.PUBLISHING: frozenset(),
    DownloadTaskStatus.COMPLETED: frozenset(),
    DownloadTaskStatus.FAILED: frozenset({DownloadCommand.RESUME, DownloadCommand.RETRY}),
    DownloadTaskStatus.CANCELLED: frozenset(),
}
EXPECTED_COMMANDS: Final[tuple[DownloadCommand, ...]] = (
    DownloadCommand.PAUSE,
    DownloadCommand.RESUME,
    DownloadCommand.CANCEL,
    DownloadCommand.RETRY,
)


@pytest.fixture(autouse=True)
def _migrated_empty_database(ephemeral_postgres: EphemeralPostgres) -> Iterator[None]:
    upgrade_head(ephemeral_postgres.engine)
    with ephemeral_postgres.engine.begin() as connection:
        connection.exec_driver_sql(
            "TRUNCATE core.audit_events, core.jobs, core.download_subscriptions, "
            "core.download_tasks, core.model_artifacts, core.models, core.model_sources, "
            "core.sessions, core.users CASCADE"
        )
    yield


def _seed_task(
    fixture: EphemeralPostgres,
    status: DownloadTaskStatus,
    *,
    resolved: bool = False,
    staging: bool = False,
) -> UUID:
    source_id = uuid4()
    model_id = uuid4()
    task_id = uuid4()
    repository_id = f"Owner/TC-DL-06-{task_id}"
    with fixture.engine.begin() as connection:
        now = connection.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        existing_source_id = connection.execute(
            sa.select(MODEL_SOURCES.c.id).where(MODEL_SOURCES.c.type == SOURCE.value)
        ).scalar_one_or_none()
        if existing_source_id is None:
            connection.execute(
                sa.insert(MODEL_SOURCES).values(
                    id=source_id,
                    type=SOURCE.value,
                    credential_key_id=None,
                    credential_nonce=None,
                    credential_ciphertext=None,
                    credential_tag=None,
                    verification_status=None,
                    verified_at=None,
                    verification_reason_code=None,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            source_id = existing_source_id
        connection.execute(
            sa.insert(MODELS).values(
                id=model_id,
                source_id=source_id,
                source_type=SOURCE.value,
                source_key=repository_id,
                display_name="TC-DL-06",
                repository_id=repository_id,
                external_path=None,
                architecture=None,
                modalities=None,
                precision=None,
                quantization=None,
                context_length=None,
                supported_runtimes=None,
                estimated_size_bytes=None,
                license=None,
                metadata_status=MetadataStatus.UNCONFIRMED.value,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            sa.insert(DOWNLOAD_TASKS).values(
                id=task_id,
                model_id=model_id,
                artifact_id=None,
                source_type=SOURCE.value,
                repository_id=repository_id,
                requested_revision_norm="main",
                resolved_revision="a" * 40 if resolved else None,
                status=status.value,
                attempt=1,
                downloaded_bytes=7,
                total_bytes=10,
                lease_owner=None,
                lease_until=None,
                staging_path="opaque-staging-token" if staging else None,
                error_code="prior_failure" if status is DownloadTaskStatus.FAILED else None,
                created_at=now,
                updated_at=now,
                completed_at=now if status is DownloadTaskStatus.COMPLETED else None,
            )
        )
    return task_id


def _seed_pre_resolution_command_conflict(
    fixture: EphemeralPostgres,
) -> tuple[UUID, UUID]:
    failed_id = _seed_task(fixture, DownloadTaskStatus.FAILED)
    queued_id = uuid4()
    with fixture.engine.begin() as connection:
        identity = (
            connection.execute(
                sa.select(
                    DOWNLOAD_TASKS.c.model_id,
                    DOWNLOAD_TASKS.c.source_type,
                    DOWNLOAD_TASKS.c.repository_id,
                    DOWNLOAD_TASKS.c.requested_revision_norm,
                ).where(DOWNLOAD_TASKS.c.id == failed_id)
            )
            .mappings()
            .one()
        )
        now = connection.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        connection.execute(
            sa.insert(DOWNLOAD_TASKS).values(
                id=queued_id,
                model_id=identity["model_id"],
                artifact_id=None,
                source_type=identity["source_type"],
                repository_id=identity["repository_id"],
                requested_revision_norm=identity["requested_revision_norm"],
                resolved_revision=None,
                status=DownloadTaskStatus.QUEUED.value,
                attempt=1,
                downloaded_bytes=0,
                total_bytes=None,
                lease_owner=None,
                lease_until=None,
                staging_path=None,
                error_code=None,
                created_at=now,
                updated_at=now,
                completed_at=None,
            )
        )
    return failed_id, queued_id


def _task_row(fixture: EphemeralPostgres, task_id: UUID) -> dict[str, object]:
    with fixture.engine.connect() as connection:
        return dict(
            connection.execute(
                sa.text(
                    "SELECT status, attempt, resolved_revision, downloaded_bytes, total_bytes, "
                    "staging_path, error_code FROM core.download_tasks WHERE id = :id"
                ),
                {"id": task_id},
            )
            .mappings()
            .one()
        )


def _command_audits(fixture: EphemeralPostgres, task_id: UUID) -> list[dict[str, object]]:
    with fixture.engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                sa.text(
                    "SELECT actor_id, request_id, details FROM core.audit_events "
                    "WHERE action = 'download.change' AND object_id = :id ORDER BY id"
                ),
                {"id": task_id},
            ).mappings()
        ]


async def _service(fixture: EphemeralPostgres) -> tuple[AsyncEngine, DownloadCommandService]:
    engine = create_database_engine(fixture.engine.url)
    return engine, DownloadCommandService(session_factory=create_session_factory(engine))


def _expected_target(
    status: DownloadTaskStatus,
    command: DownloadCommand,
    *,
    resolved: bool,
) -> DownloadTaskStatus:
    if command is DownloadCommand.CANCEL:
        return DownloadTaskStatus.CANCELLED
    if command is DownloadCommand.PAUSE:
        return DownloadTaskStatus.PAUSED
    if command is DownloadCommand.RETRY:
        return DownloadTaskStatus.RESOLVING
    if status is DownloadTaskStatus.PAUSED or resolved:
        return DownloadTaskStatus.DOWNLOADING
    return DownloadTaskStatus.RESOLVING


def test_TC_DL_06_all_nine_matrix_rows_and_every_disallowed_pair(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    assert tuple(DownloadCommand) == EXPECTED_COMMANDS
    expected_matrix: dict[tuple[DownloadTaskStatus, str | None], frozenset[DownloadCommand]] = {
        **{(status, None): commands for status, commands in EXPECTED_MATRIX.items()},
        (DownloadTaskStatus.FAILED, "model_layout_unsupported"): frozenset(),
        (DownloadTaskStatus.FAILED, "artifact_required_files_invalid"): frozenset(),
    }
    assert dict(DOWNLOAD_COMMAND_MATRIX) == expected_matrix

    async def exercise() -> None:
        engine, service = await _service(ephemeral_postgres)
        try:
            for status, allowed in EXPECTED_MATRIX.items():
                for command in DownloadCommand:
                    resolved = status in {
                        DownloadTaskStatus.DOWNLOADING,
                        DownloadTaskStatus.PAUSED,
                        DownloadTaskStatus.VERIFYING,
                        DownloadTaskStatus.PUBLISHING,
                        DownloadTaskStatus.COMPLETED,
                    }
                    task_id = _seed_task(ephemeral_postgres, status, resolved=resolved)
                    before = _task_row(ephemeral_postgres, task_id)
                    if command in allowed:
                        result = await service.execute(
                            task_id=task_id,
                            command=command,
                            actor_id=uuid4(),
                            request_id=uuid4(),
                        )
                        assert result.task.status is _expected_target(
                            status, command, resolved=resolved
                        )
                    else:
                        with pytest.raises(DownloadCommandNotAllowed):
                            await service.execute(
                                task_id=task_id,
                                command=command,
                                actor_id=uuid4(),
                                request_id=uuid4(),
                            )
                        assert _task_row(ephemeral_postgres, task_id) == before
                        assert _command_audits(ephemeral_postgres, task_id) == []
        finally:
            await engine.dispose()

    asyncio.run(exercise())
    print("TC-DL-06 matrix: 9 rows and all 36 state-command pairs enforced")


@pytest.mark.parametrize(
    ("command", "status", "resolved"),
    [
        (DownloadCommand.PAUSE, DownloadTaskStatus.DOWNLOADING, True),
        (DownloadCommand.RESUME, DownloadTaskStatus.PAUSED, True),
        (DownloadCommand.CANCEL, DownloadTaskStatus.VERIFYING, True),
        (DownloadCommand.RETRY, DownloadTaskStatus.FAILED, False),
    ],
)
def test_TC_DL_06_repeated_commands_do_not_change_observable_result(
    ephemeral_postgres: EphemeralPostgres,
    command: DownloadCommand,
    status: DownloadTaskStatus,
    resolved: bool,
) -> None:
    async def exercise() -> None:
        engine, service = await _service(ephemeral_postgres)
        try:
            task_id = _seed_task(ephemeral_postgres, status, resolved=resolved, staging=True)
            await service.execute(
                task_id=task_id,
                command=command,
                actor_id=uuid4(),
                request_id=uuid4(),
            )
            first = _task_row(ephemeral_postgres, task_id)
            first_audits = _command_audits(ephemeral_postgres, task_id)
            with pytest.raises(DownloadCommandNotAllowed):
                await service.execute(
                    task_id=task_id,
                    command=command,
                    actor_id=uuid4(),
                    request_id=uuid4(),
                )
            assert _task_row(ephemeral_postgres, task_id) == first
            assert _command_audits(ephemeral_postgres, task_id) == first_audits
            if command is DownloadCommand.RETRY:
                assert first["attempt"] == 2
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_TC_DL_06_concurrent_retry_advances_one_attempt_and_writes_one_audit(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def exercise() -> None:
        engine, service = await _service(ephemeral_postgres)
        try:
            task_id = _seed_task(ephemeral_postgres, DownloadTaskStatus.FAILED, staging=True)

            async def retry() -> str:
                try:
                    await service.execute(
                        task_id=task_id,
                        command=DownloadCommand.RETRY,
                        actor_id=uuid4(),
                        request_id=uuid4(),
                    )
                except DownloadCommandNotAllowed:
                    return "rejected"
                return "applied"

            assert sorted(await asyncio.gather(retry(), retry())) == ["applied", "rejected"]
            row = _task_row(ephemeral_postgres, task_id)
            assert row["status"] == DownloadTaskStatus.RESOLVING.value
            assert row["attempt"] == 2
            assert len(_command_audits(ephemeral_postgres, task_id)) == 1
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_TC_DL_06_retry_and_cancel_record_cleanup_contract_without_deleting_staging(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def exercise() -> None:
        engine, service = await _service(ephemeral_postgres)
        try:
            for command, status in (
                (DownloadCommand.CANCEL, DownloadTaskStatus.DOWNLOADING),
                (DownloadCommand.RETRY, DownloadTaskStatus.FAILED),
            ):
                task_id = _seed_task(ephemeral_postgres, status, staging=True)
                result = await service.execute(
                    task_id=task_id,
                    command=command,
                    actor_id=uuid4(),
                    request_id=uuid4(),
                )
                assert result.staging_cleanup_requested is True
                assert result.task.staging_path == "opaque-staging-token"
                assert _command_audits(ephemeral_postgres, task_id)[0]["details"] == {
                    "operation": command.value,
                    "reason_code": STAGING_CLEANUP_REQUESTED_REASON,
                    "source_type": SOURCE.value,
                }
                if command is DownloadCommand.RETRY:
                    with ephemeral_postgres.engine.connect() as connection:
                        payload = connection.execute(
                            sa.text("SELECT payload FROM core.jobs WHERE target_object_id = :id"),
                            {"id": task_id},
                        ).scalar_one()
                    assert payload == {"staging_cleanup_requested": True}
        finally:
            await engine.dispose()

    asyncio.run(exercise())
    print("TC-DL-06 cleanup contract: Cancel/Retry recorded; staging deletion deferred")


def test_TC_DL_06_zero_subscriptions_never_silently_cancel(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    assert tuple(DownloadCancellationReason) == (
        DownloadCancellationReason.DUPLICATE_RESOLVED_REVISION,
    )

    async def exercise() -> None:
        engine, service = await _service(ephemeral_postgres)
        try:
            task_id = _seed_task(
                ephemeral_postgres,
                DownloadTaskStatus.DOWNLOADING,
                resolved=True,
            )
            result = await service.execute(
                task_id=task_id,
                command=DownloadCommand.PAUSE,
                actor_id=uuid4(),
                request_id=uuid4(),
            )
            assert result.task.status is DownloadTaskStatus.PAUSED
            with ephemeral_postgres.engine.connect() as connection:
                count = connection.execute(
                    sa.text("SELECT count(*) FROM core.download_subscriptions WHERE task_id = :id"),
                    {"id": task_id},
                ).scalar_one()
            assert count == 0
        finally:
            await engine.dispose()

    asyncio.run(exercise())


class _BlockingResolver:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def resolve(self, request: ResolutionSubprocessRequest) -> ResolutionSubprocessResult:
        del request
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError("blocking resolver returned unexpectedly")


def test_TC_DL_06_lease_owner_observes_cancel_and_stops_active_work(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def exercise() -> None:
        engine = create_database_engine(ephemeral_postgres.engine.url)
        try:
            factory = create_session_factory(engine)
            task_id = _seed_task(ephemeral_postgres, DownloadTaskStatus.RESOLVING)
            async with factory() as session, session.begin():
                await JobRepository(session).enqueue(
                    JobDraft(
                        job_type=DOWNLOAD_RESOLUTION_JOB_TYPE,
                        payload={},
                        target_object_id=task_id,
                        target_object_version=1,
                    )
                )
            resolver = _BlockingResolver()
            worker = DownloadWorker(
                session_factory=factory,
                handler=DownloadResolutionJobHandler(
                    session_factory=factory,
                    resolver=resolver,
                    command_poll_seconds=0.05,
                ),
                owner="TC-DL-06-worker",
                build_version="m2-09b-test",
                lease_seconds=1,
                heartbeat_seconds=0.2,
            )
            running = asyncio.create_task(worker.run_once())
            await asyncio.wait_for(resolver.started.wait(), 2)
            result = await DownloadCommandService(session_factory=factory).execute(
                task_id=task_id,
                command=DownloadCommand.CANCEL,
                actor_id=uuid4(),
                request_id=uuid4(),
            )
            assert result.task.status is DownloadTaskStatus.CANCELLED
            assert not resolver.cancelled.is_set()
            completed = await asyncio.wait_for(running, 2)
            assert completed is not None and completed.completed is True
            assert resolver.cancelled.is_set()
            with ephemeral_postgres.engine.connect() as connection:
                job_status = connection.execute(
                    sa.text(
                        "SELECT status FROM core.jobs "
                        "WHERE target_object_id = :id AND job_type = :job_type"
                    ),
                    {"id": task_id, "job_type": DOWNLOAD_RESOLUTION_JOB_TYPE},
                ).scalar_one()
            assert job_status == JobStatus.SUCCEEDED.value
        finally:
            await engine.dispose()

    asyncio.run(exercise())
    print("TC-DL-06 termination: HTTP-side state write observed by lease owner")


async def _ready() -> ReadinessReason:
    return ReadinessReason.OK


def test_TC_DL_06_four_distinct_http_endpoints_actor_and_stable_errors(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def exercise() -> None:
        engine = create_database_engine(ephemeral_postgres.engine.url)
        try:
            factory = create_session_factory(engine)
            hasher = PasswordHasher(CHEAP)
            async with factory() as session, session.begin():
                actor = await UserRepository(session).create(
                    username="command-admin",
                    display_name="Command Admin",
                    role=UserRole.ADMIN,
                    password_hash=await asyncio.to_thread(hasher.hash, PASSWORD),
                    now=await database_utc_now(session),
                    must_change_password=False,
                )
            service = DownloadCommandService(session_factory=factory)
            app = create_app(
                version_payload=VersionPayload(
                    build_version="2.2.0+m209b",
                    commit="9" * 40,
                    schema_version=1,
                    runtime_compat_hash="8" * 64,
                ),
                readiness_checks=[DependencyCheck(name="database", probe=_ready)],
                credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
                authentication=AuthenticationService(
                    session_factory=factory,
                    password_hasher=hasher,
                    minimum_login_failure_seconds=0,
                ),
                download_commands=service,
            )
            client = AsgiTestClient(app)
            login = await client.request(
                "POST",
                "/api/v1/auth/login",
                json_body={"username": "command-admin", "password": PASSWORD},
            )
            cookie = SessionCookies(
                {"__Host-pws_session": _cookie(login, "__Host-pws_session")},
                csrf_token=str(login.json()["csrf_token"]),
            )
            unauthorized = await client.request("POST", f"/api/v1/downloads/{uuid4()}/pause")
            assert unauthorized.status == 401
            missing = await client.request(
                "POST", f"/api/v1/downloads/{uuid4()}/pause", cookies=cookie
            )
            assert missing.status == 404
            assert missing.json()["error"]["code"] == DownloadCommandErrorCode.NOT_FOUND.value

            scenarios = (
                ("pause", DownloadTaskStatus.DOWNLOADING),
                ("resume", DownloadTaskStatus.PAUSED),
                ("cancel", DownloadTaskStatus.QUEUED),
                ("retry", DownloadTaskStatus.FAILED),
            )
            for command, status in scenarios:
                task_id = _seed_task(
                    ephemeral_postgres,
                    status,
                    resolved=status in {DownloadTaskStatus.DOWNLOADING, DownloadTaskStatus.PAUSED},
                )
                response = await client.request(
                    "POST", f"/api/v1/downloads/{task_id}/{command}", cookies=cookie
                )
                assert response.status == 200, response.text
                assert response.json()["command"] == command
                audit = _command_audits(ephemeral_postgres, task_id)
                assert len(audit) == 1 and audit[0]["actor_id"] == actor.id
                assert str(audit[0]["request_id"]) == dict(response.headers)["x-request-id"]
                replay = await client.request(
                    "POST", f"/api/v1/downloads/{task_id}/{command}", cookies=cookie
                )
                assert replay.status == 409
                assert replay.json()["error"]["code"] == (
                    DownloadCommandErrorCode.NOT_ALLOWED.value
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())
    print("TC-DL-06 HTTP: four endpoints distinct, authenticated and actor-audited")


@pytest.mark.parametrize("command", [DownloadCommand.RETRY, DownloadCommand.RESUME])
def test_TC_DL_06_pre_resolution_identity_conflict_is_stable_and_atomic(
    ephemeral_postgres: EphemeralPostgres,
    command: DownloadCommand,
) -> None:
    async def exercise() -> None:
        engine = create_database_engine(ephemeral_postgres.engine.url)
        try:
            factory = create_session_factory(engine)
            hasher = PasswordHasher(CHEAP)
            async with factory() as session, session.begin():
                await UserRepository(session).create(
                    username="conflict-admin",
                    display_name="Conflict Admin",
                    role=UserRole.ADMIN,
                    password_hash=await asyncio.to_thread(hasher.hash, PASSWORD),
                    now=await database_utc_now(session),
                    must_change_password=False,
                )
            app = create_app(
                version_payload=VersionPayload(
                    build_version="2.2.0+m209b-rework1",
                    commit="7" * 40,
                    schema_version=1,
                    runtime_compat_hash="6" * 64,
                ),
                readiness_checks=[DependencyCheck(name="database", probe=_ready)],
                credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
                authentication=AuthenticationService(
                    session_factory=factory,
                    password_hasher=hasher,
                    minimum_login_failure_seconds=0,
                ),
                download_commands=DownloadCommandService(session_factory=factory),
            )
            client = AsgiTestClient(app)
            login = await client.request(
                "POST",
                "/api/v1/auth/login",
                json_body={"username": "conflict-admin", "password": PASSWORD},
            )
            cookie = SessionCookies(
                {"__Host-pws_session": _cookie(login, "__Host-pws_session")},
                csrf_token=str(login.json()["csrf_token"]),
            )
            failed_id, queued_id = _seed_pre_resolution_command_conflict(ephemeral_postgres)
            before = {
                failed_id: _task_row(ephemeral_postgres, failed_id),
                queued_id: _task_row(ephemeral_postgres, queued_id),
            }

            response = await client.request(
                "POST",
                f"/api/v1/downloads/{failed_id}/{command.value}",
                cookies=cookie,
            )

            assert response.status == 409, response.text
            assert response.json()["error"]["code"] == (
                DownloadCommandErrorCode.ALREADY_IN_PROGRESS.value
            )
            assert _task_row(ephemeral_postgres, failed_id) == before[failed_id]
            assert _task_row(ephemeral_postgres, queued_id) == before[queued_id]
            assert _command_audits(ephemeral_postgres, failed_id) == []
            assert _command_audits(ephemeral_postgres, queued_id) == []
            with ephemeral_postgres.engine.connect() as connection:
                job_count = connection.execute(
                    sa.text("SELECT count(*) FROM core.jobs WHERE job_type = :job_type"),
                    {"job_type": DOWNLOAD_RESOLUTION_JOB_TYPE},
                ).scalar_one()
            assert job_count == 0
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_TC_DL_06_positive_control_unhandled_pre_resolution_conflict_fails_loudly(
    ephemeral_postgres: EphemeralPostgres,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        engine, service = await _service(ephemeral_postgres)
        try:
            marker = """        except IntegrityError as error:
            conflict = _unique(error, "uq_pre_resolution_download_request")
        if conflict:
            raise DownloadCommandIdentityConflict()
"""
            mutated = load_mutated_module(
                DOWNLOAD_REPOSITORY_SOURCE,
                [(marker, "        except IntegrityError:\n            raise\n")],
                module_name="_m2_09b_unhandled_command_identity_conflict",
                destination=tmp_path / "unhandled_command_identity_conflict.py",
            )
            monkeypatch.setattr(
                "holm_control_api.download_command_service.DownloadTaskRepository",
                mutated.DownloadTaskRepository,
            )
            failed_id, _ = _seed_pre_resolution_command_conflict(ephemeral_postgres)

            async def assert_stable_conflict() -> None:
                with pytest.raises(DownloadAlreadyInProgress):
                    await service.execute(
                        task_id=failed_id,
                        command=DownloadCommand.RETRY,
                        actor_id=uuid4(),
                        request_id=uuid4(),
                    )

            with pytest.raises(IntegrityError):
                await assert_stable_conflict()
        finally:
            await engine.dispose()

    asyncio.run(exercise())
    print("TC-DL-06 positive control: unhandled identity conflict raised IntegrityError")


def test_TC_DL_06_positive_control_second_retry_attempt_and_audit_are_detected(
    ephemeral_postgres: EphemeralPostgres,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        engine, service = await _service(ephemeral_postgres)
        try:
            task_id = _seed_task(ephemeral_postgres, DownloadTaskStatus.FAILED)
            await service.execute(
                task_id=task_id,
                command=DownloadCommand.RETRY,
                actor_id=uuid4(),
                request_id=uuid4(),
            )
            first = _task_row(ephemeral_postgres, task_id)
            mutant = dict(DOWNLOAD_COMMAND_MATRIX)
            mutant[(DownloadTaskStatus.RESOLVING, None)] = frozenset(
                {DownloadCommand.CANCEL, DownloadCommand.RETRY}
            )
            monkeypatch.setattr(
                "holm_control_api.download_command_service.DOWNLOAD_COMMAND_MATRIX",
                mutant,
            )
            await service.execute(
                task_id=task_id,
                command=DownloadCommand.RETRY,
                actor_id=uuid4(),
                request_id=uuid4(),
            )
            with pytest.raises(AssertionError):
                assert _task_row(ephemeral_postgres, task_id) == first
            with pytest.raises(AssertionError):
                assert len(_command_audits(ephemeral_postgres, task_id)) == 1
        finally:
            await engine.dispose()

    asyncio.run(exercise())
    print("TC-DL-06 positive control 1: repeated Retry mutant advanced attempt and audit")


def test_TC_DL_06_positive_control_publishing_cancel_mutant_is_detected(
    ephemeral_postgres: EphemeralPostgres,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        engine, service = await _service(ephemeral_postgres)
        try:
            mutant = dict(DOWNLOAD_COMMAND_MATRIX)
            mutant[(DownloadTaskStatus.PUBLISHING, None)] = frozenset({DownloadCommand.CANCEL})
            monkeypatch.setattr(
                "holm_control_api.download_command_service.DOWNLOAD_COMMAND_MATRIX",
                mutant,
            )
            task_id = _seed_task(
                ephemeral_postgres,
                DownloadTaskStatus.PUBLISHING,
                resolved=True,
            )
            result = await service.execute(
                task_id=task_id,
                command=DownloadCommand.CANCEL,
                actor_id=uuid4(),
                request_id=uuid4(),
            )
            with pytest.raises(AssertionError):
                assert result.task.status is DownloadTaskStatus.PUBLISHING
        finally:
            await engine.dispose()

    asyncio.run(exercise())
    print("TC-DL-06 positive control 2: Publishing-Cancel mutant changed state")


def test_TC_DL_06_positive_control_silent_disallowed_success_is_detected(
    ephemeral_postgres: EphemeralPostgres,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        engine, service = await _service(ephemeral_postgres)
        original = DownloadCommandService.execute
        try:
            task_id = _seed_task(ephemeral_postgres, DownloadTaskStatus.COMPLETED)

            async def silent_execute(
                self: DownloadCommandService,
                *,
                task_id: UUID,
                command: DownloadCommand,
                actor_id: UUID,
                request_id: UUID,
            ) -> DownloadCommandResult:
                try:
                    return await original(
                        self,
                        task_id=task_id,
                        command=command,
                        actor_id=actor_id,
                        request_id=request_id,
                    )
                except DownloadCommandNotAllowed:
                    async with create_session_factory(engine)() as session, session.begin():
                        task = await DownloadTaskRepository(session).lock_command_task(task_id)
                        assert task is not None
                    return DownloadCommandResult(task, command, False)

            monkeypatch.setattr(DownloadCommandService, "execute", silent_execute)

            async def assert_rejected() -> None:
                try:
                    await service.execute(
                        task_id=task_id,
                        command=DownloadCommand.CANCEL,
                        actor_id=uuid4(),
                        request_id=uuid4(),
                    )
                except DownloadCommandNotAllowed:
                    return
                raise AssertionError("disallowed command returned success")

            with pytest.raises(AssertionError, match="returned success"):
                await assert_rejected()
        finally:
            await engine.dispose()

    asyncio.run(exercise())
    print("TC-DL-06 positive control 3: silent disallowed success failed loudly")


def test_TC_DL_06_positive_control_merged_resume_retry_route_is_detected() -> None:
    router = create_download_command_router(cast(DownloadCommandService, object()))
    paths = {
        (route.path, frozenset(route.methods or ()))
        for route in router.routes
        if isinstance(route, APIRoute)
    }
    expected = {
        (f"/api/v1/downloads/{{id}}/{command.value}", frozenset({"POST"}))
        for command in EXPECTED_COMMANDS
    }
    assert paths == expected
    router.routes[:] = [
        route
        for route in router.routes
        if not isinstance(route, APIRoute) or not route.path.endswith("/retry")
    ]
    mutated_paths = {
        (route.path, frozenset(route.methods or ()))
        for route in router.routes
        if isinstance(route, APIRoute)
    }
    with pytest.raises(AssertionError):
        assert mutated_paths == expected
    print("TC-DL-06 positive control 4: merged Resume/Retry route lost matrix coverage")


def test_TC_DL_06_positive_control_retry_without_cleanup_contract_is_detected(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        engine = create_database_engine(ephemeral_postgres.engine.url)
        try:
            source = COMMAND_SERVICE_SOURCE.read_text(encoding="utf-8")
            marker = (
                "cleanup_requested = command in {DownloadCommand.CANCEL, DownloadCommand.RETRY}"
            )
            assert marker in source
            mutated = load_mutated_module(
                COMMAND_SERVICE_SOURCE,
                [(marker, "cleanup_requested = command is DownloadCommand.CANCEL")],
                module_name="_m2_09b_retry_reuses_staging",
                destination=tmp_path / "retry_reuses_staging.py",
            )
            task_id = _seed_task(ephemeral_postgres, DownloadTaskStatus.FAILED, staging=True)
            result = await mutated.DownloadCommandService(
                session_factory=create_session_factory(engine)
            ).execute(
                task_id=task_id,
                command=mutated.DownloadCommand.RETRY,
                actor_id=uuid4(),
                request_id=uuid4(),
            )
            with pytest.raises(AssertionError):
                assert result.staging_cleanup_requested is True
        finally:
            await engine.dispose()

    asyncio.run(exercise())
    print("TC-DL-06 positive control 5: Retry-reuses-staging mutant failed")


def test_TC_DL_06_positive_control_zero_subscriber_auto_cancel_is_detected(
    ephemeral_postgres: EphemeralPostgres,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        engine, service = await _service(ephemeral_postgres)
        original = DownloadTaskRepository.apply_command_transition
        try:

            async def force_cancel(
                self: DownloadTaskRepository,
                task_id: UUID,
                *,
                expected_status: DownloadTaskStatus,
                status: DownloadTaskStatus,
                attempt: int,
                reset_progress: bool,
                now: datetime,
            ) -> DownloadCommandTaskRecord:
                subscriptions = (
                    await self._session.execute(  # noqa: SLF001 - deliberate repository mutant
                        sa.select(sa.func.count())
                        .select_from(DOWNLOAD_SUBSCRIPTIONS)
                        .where(DOWNLOAD_SUBSCRIPTIONS.c.task_id == task_id)
                    )
                ).scalar_one()
                mutated_status = DownloadTaskStatus.CANCELLED if subscriptions == 0 else status
                return await original(
                    self,
                    task_id,
                    expected_status=expected_status,
                    status=mutated_status,
                    attempt=attempt,
                    reset_progress=reset_progress,
                    now=now,
                )

            monkeypatch.setattr(
                DownloadTaskRepository,
                "apply_command_transition",
                force_cancel,
            )
            zero_id = _seed_task(
                ephemeral_postgres,
                DownloadTaskStatus.DOWNLOADING,
                resolved=True,
            )
            mutated = await service.execute(
                task_id=zero_id,
                command=DownloadCommand.PAUSE,
                actor_id=uuid4(),
                request_id=uuid4(),
            )
            with pytest.raises(AssertionError):
                assert mutated.task.status is DownloadTaskStatus.PAUSED
        finally:
            await engine.dispose()

    asyncio.run(exercise())
    print("TC-DL-06 positive control 6: zero-subscriber auto-cancel mutant failed")


def test_TC_DL_06_not_found_is_stable_and_side_effect_free(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def exercise() -> None:
        engine, service = await _service(ephemeral_postgres)
        try:
            with pytest.raises(DownloadCommandNotFound):
                await service.execute(
                    task_id=uuid4(),
                    command=DownloadCommand.CANCEL,
                    actor_id=uuid4(),
                    request_id=uuid4(),
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())

"""M2-12A: the two read-only acquisition lists over real PostgreSQL.

These checks need real rows: a
row-level snapshot to prove nothing was written, an ``xmin``-visible rewrite as
the control, real ``storage_type`` mixtures for the storage summary, real
keyset ordering across pages, and a persisted error code the vocabulary does
not contain.

Inherited judgements: ``TC-RES-02``/``TC-INV-05`` (a preview writes nothing),
``TC-UI-08`` (a failed collection is not ``0``), ``TC-UI-11`` (an empty system
and a filtered-out result are different answers) and ``TC-DL-17`` (a sentinel
planted in a path must reach none of the four sinks).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Final, Protocol
from uuid import UUID

import holm_control_api.model_acquisition_read_service as read_service_module
import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from fixtures.asgi_client import (
    NO_CONFIGURED_CREDENTIAL_ROWS,
    AsgiResponse,
    AsgiTestClient,
)
from fixtures.model_domain_snapshot import (
    assert_no_model_domain_writes,
    model_domain_snapshot,
)
from fixtures.source_mutation import load_mutated_module
from holm_contracts import VersionPayload
from holm_control_api.application import create_app
from holm_control_api.authentication import AuthenticationService
from holm_control_api.download_command_service import (
    DOWNLOAD_COMMAND_MATRIX,
    DownloadCommand,
)
from holm_control_api.http_constants import SESSION_COOKIE_NAME
from holm_control_api.model_acquisition_read_routes import (
    ARTIFACTS_PATH,
    DOWNLOADS_PATH,
)
from holm_control_api.model_acquisition_read_service import (
    ARTIFACTS_CURSOR_KIND,
    ModelAcquisitionReadService,
    ModelRootAvailability,
    available_commands_for,
    encode_cursor,
)
from holm_control_api.model_resolution_service import ACTIVE_DOWNLOAD_STATUSES
from holm_control_api.readiness import DependencyCheck, ReadinessReason
from holm_domain import (
    ArtifactStorageType,
    AuditAction,
    AuditActorType,
    AuditObjectType,
    AuditResult,
    DownloadCancellationReason,
    DownloadFailureReason,
    DownloadFailureStage,
    DownloadTaskStatus,
    MetadataStatus,
    ModelArtifactStatus,
    ModelOriginType,
    UserRole,
)
from holm_persistence import (
    DEFAULT_REQUESTED_REVISION,
    AsyncSessionFactory,
    UserRepository,
    create_database_engine,
    create_session_factory,
    database_utc_now,
)
from holm_security import (
    OWASP_MINIMUM_MEMORY_COST_KIB,
    OWASP_MINIMUM_TIME_COST,
    Argon2idParameters,
    PasswordHasher,
)
from migration_test_support import upgrade_head
from sqlalchemy.engine import URL, Engine

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
REPOSITORY_SOURCE: Final[Path] = REPOSITORY_ROOT / (
    "packages/persistence/src/holm_persistence/model_acquisition_read_repository.py"
)
RESOLUTION_SOURCE: Final[Path] = REPOSITORY_ROOT / (
    "apps/control_api/src/holm_control_api/model_resolution_service.py"
)
SERVICE_SOURCE: Final[Path] = REPOSITORY_ROOT / (
    "apps/control_api/src/holm_control_api/model_acquisition_read_service.py"
)

ADMIN_USERNAME: Final[str] = "acquisition-read-admin"
MEMBER_USERNAME: Final[str] = "acquisition-read-member"
PASSWORD: Final[str] = "correct-password-for-acquisition-read-tests"
CHEAP: Final = Argon2idParameters(
    memory_cost_kib=OWASP_MINIMUM_MEMORY_COST_KIB,
    time_cost=OWASP_MINIMUM_TIME_COST,
    parallelism=1,
)
SOURCE_ID: Final[UUID] = UUID("50000000-0000-4000-8000-000000000001")
BASE_MOMENT: Final[datetime] = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
MODEL_DOMAIN_TABLE_NAMES: Final[tuple[str, ...]] = (
    "models",
    "model_artifacts",
    "download_tasks",
    "download_subscriptions",
)
UNCLASSIFIED_ERROR_CODE: Final[str] = "unclassified_error_code_sentinel"


class PostgresFixture(Protocol):
    @property
    def engine(self) -> Engine: ...

    @property
    def socket_directory(self) -> Path: ...

    @property
    def port(self) -> int: ...


@dataclass(frozen=True, slots=True)
class _Environment:
    client: AsgiTestClient
    session_factory: AsyncSessionFactory


async def _readiness_ok() -> ReadinessReason:
    return ReadinessReason.OK


def _async_url(fixture: PostgresFixture) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username="ubuntu",
        host=str(fixture.socket_directory),
        port=fixture.port,
        database="postgres",
    )


@asynccontextmanager
async def _environment(
    fixture: PostgresFixture,
    *,
    model_root: Path,
    read_service_type: Any = ModelAcquisitionReadService,
) -> AsyncIterator[_Environment]:
    engine = create_database_engine(_async_url(fixture), connect_timeout_seconds=5)
    try:
        session_factory = create_session_factory(engine)
        app = create_app(
            version_payload=VersionPayload(
                build_version="2.2.0+m212a0001",
                commit="e" * 40,
                schema_version=1,
                runtime_compat_hash="f" * 64,
            ),
            readiness_checks=[DependencyCheck(name="database", probe=_readiness_ok)],
            credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
            authentication=AuthenticationService(
                session_factory=session_factory,
                password_hasher=PasswordHasher(CHEAP),
                minimum_login_failure_seconds=0,
            ),
            model_acquisition_read=read_service_type(
                session_factory=session_factory,
                storage_availability=ModelRootAvailability(model_root),
            ),
        )
        yield _Environment(client=AsgiTestClient(app), session_factory=session_factory)
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def migrated_empty_domain(ephemeral_postgres: EphemeralPostgres) -> Iterator[None]:
    upgrade_head(ephemeral_postgres.engine)
    _truncate(ephemeral_postgres.engine)
    yield
    _truncate(ephemeral_postgres.engine)


def _truncate(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "TRUNCATE core.idempotency_records, core.download_subscriptions, "
                "core.download_tasks, core.model_artifacts, core.models, core.audit_events, "
                "core.login_protection, core.sessions, core.model_sources, core.users CASCADE"
            )
        )


async def _create_accounts(environment: _Environment) -> None:
    async with environment.session_factory() as session:
        password_hash = await asyncio.to_thread(PasswordHasher(CHEAP).hash, PASSWORD)
        now = await database_utc_now(session)
        repository = UserRepository(session)
        for username, role in (
            (ADMIN_USERNAME, UserRole.ADMIN),
            (MEMBER_USERNAME, UserRole.USER),
        ):
            await repository.create(
                username=username,
                display_name=username,
                role=role,
                password_hash=password_hash,
                now=now,
                must_change_password=False,
            )
        await session.commit()


def _cookie(response: AsgiResponse, name: str) -> str:
    values: list[str] = []
    for key, raw in response.headers:
        if key.casefold() != "set-cookie":
            continue
        parsed = SimpleCookie()
        parsed.load(raw)
        if name in parsed:
            values.append(parsed[name].value)
    assert len(values) == 1
    return values[0]


async def _login(environment: _Environment, username: str) -> Mapping[str, str]:
    response = await environment.client.request(
        "POST",
        "/api/v1/auth/login",
        json_body={"username": username, "password": PASSWORD},
    )
    assert response.status == 200
    return {SESSION_COOKIE_NAME: _cookie(response, SESSION_COOKIE_NAME)}


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------


def _seed_source(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO core.model_sources (id, type, created_at, updated_at) "
                "VALUES (:id, :type, clock_timestamp(), clock_timestamp())"
            ),
            {"id": SOURCE_ID, "type": ModelOriginType.HUGGING_FACE.value},
        )


def _seed_model(engine: Engine, model_id: UUID, *, display_name: str) -> None:
    repository_id = f"read-owner/{display_name}"
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO core.models (id, source_id, source_type, source_key, display_name, "
                "repository_id, metadata_status, created_at, updated_at) VALUES "
                "(:id, :source_id, :source_type, :source_key, :display_name, :repository_id, "
                ":metadata_status, clock_timestamp(), clock_timestamp())"
            ),
            {
                "id": model_id,
                "source_id": SOURCE_ID,
                "source_type": ModelOriginType.HUGGING_FACE.value,
                "source_key": repository_id,
                "display_name": display_name,
                "repository_id": repository_id,
                "metadata_status": MetadataStatus.CONFIRMED.value,
            },
        )


def _seed_task(
    engine: Engine,
    task_id: UUID,
    model_id: UUID,
    *,
    status: DownloadTaskStatus,
    created_at: datetime,
    resolved_revision: str | None = None,
    total_bytes: int | None = None,
    downloaded_bytes: int = 0,
    staging_path: str | None = None,
    error_code: str | None = None,
    suffix: str = "task",
) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO core.download_tasks (id, model_id, source_type, repository_id, "
                "requested_revision_norm, resolved_revision, status, attempt, downloaded_bytes, "
                "total_bytes, staging_path, error_code, created_at, updated_at) VALUES "
                "(:id, :model_id, :source_type, :repository_id, :requested, :resolved, :status, "
                "1, :downloaded, :total, :staging, :error_code, :created_at, :created_at)"
            ),
            {
                "id": task_id,
                "model_id": model_id,
                "source_type": ModelOriginType.HUGGING_FACE.value,
                "repository_id": f"read-owner/{suffix}",
                "requested": DEFAULT_REQUESTED_REVISION,
                "resolved": resolved_revision,
                "status": status.value,
                "downloaded": downloaded_bytes,
                "total": total_bytes,
                "staging": staging_path,
                "error_code": error_code,
                "created_at": created_at,
            },
        )


def _seed_artifact(
    engine: Engine,
    artifact_id: UUID,
    model_id: UUID,
    *,
    created_at: datetime,
    path: str,
    size_bytes: int,
    storage_type: ArtifactStorageType = ArtifactStorageType.MANAGED,
    status: ModelArtifactStatus = ModelArtifactStatus.READY,
    source_type: ModelOriginType = ModelOriginType.HUGGING_FACE,
    revision: str | None = None,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO core.model_artifacts (id, model_id, source_type, repository_id, "
                "resolved_revision, storage_type, status, path, manifest_hash, size_bytes, "
                "created_at, updated_at) VALUES (:id, :model_id, :source_type, :repository_id, "
                ":revision, :storage_type, :status, :path, :manifest_hash, :size_bytes, "
                ":created_at, :created_at)"
            ),
            {
                "id": artifact_id,
                "model_id": model_id,
                "source_type": source_type.value,
                "repository_id": f"read-owner/{artifact_id}",
                "revision": revision or str(artifact_id).replace("-", "")[:40],
                "storage_type": storage_type.value,
                "status": status.value,
                "path": path,
                "manifest_hash": "a" * 64,
                "size_bytes": size_bytes,
                "created_at": created_at,
            },
        )


def _model_domain_snapshot(engine: Engine) -> str:
    return model_domain_snapshot(engine, MODEL_DOMAIN_TABLE_NAMES)


def _audit_details(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(
            connection.exec_driver_sql(
                "SELECT coalesce(json_agg(row_to_json(scanned))::text, '[]') "
                "FROM core.audit_events AS scanned"
            ).scalar_one()
        )


def _uuid(tag: int) -> UUID:
    return UUID(f"60000000-0000-4000-8000-{tag:012d}")


# --------------------------------------------------------------------------
# Read end to end
# --------------------------------------------------------------------------


def _assert_downloads_empty(payload: Any) -> None:
    assert payload == {"items": [], "next_cursor": None}


def _assert_artifacts_empty(payload: Any, *, available_bytes_known: bool) -> None:
    assert payload["items"] == []
    assert payload["next_cursor"] is None
    summary = payload["summary"]
    assert summary["managed_bytes"] == 0
    assert summary["active_download_tasks"] == 0
    if available_bytes_known:
        assert isinstance(summary["storage_available_bytes"], int)
    else:
        assert summary["storage_available_bytes"] is None


def test_M2_12A_TC_UI_11_fresh_system_returns_explicit_empty_lists(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    model_root = tmp_path / "models"
    model_root.mkdir()

    async def scenario() -> tuple[AsgiResponse, AsgiResponse]:
        async with _environment(ephemeral_postgres, model_root=model_root) as environment:
            await _create_accounts(environment)
            cookies = await _login(environment, ADMIN_USERNAME)
            return (
                await environment.client.get(DOWNLOADS_PATH, cookies=cookies),
                await environment.client.get(ARTIFACTS_PATH, cookies=cookies),
            )

    downloads, artifacts = asyncio.run(scenario())
    assert downloads.status == 200, downloads.text
    assert artifacts.status == 200, artifacts.text
    _assert_downloads_empty(downloads.json())
    _assert_artifacts_empty(artifacts.json(), available_bytes_known=True)


def test_M2_12A_TC_UI_11_positive_control_preloaded_rows_fail_loudly() -> None:
    with pytest.raises(AssertionError):
        _assert_downloads_empty({"items": [{"id": "preloaded"}], "next_cursor": None})


def test_M2_12A_ruling_3_TC_UI_08_missing_model_root_is_null_not_zero(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    # Root bypasses POSIX mode bits inside the development container, so the
    # unreadable branch is produced with a directory that does not exist.
    async def scenario() -> AsgiResponse:
        async with _environment(
            ephemeral_postgres,
            model_root=tmp_path / "never-created",
        ) as environment:
            await _create_accounts(environment)
            cookies = await _login(environment, ADMIN_USERNAME)
            return await environment.client.get(ARTIFACTS_PATH, cookies=cookies)

    response = asyncio.run(scenario())
    assert response.status == 200, response.text
    _assert_artifacts_empty(response.json(), available_bytes_known=False)


def test_M2_12A_ruling_3_positive_control_zero_free_space_fails_loudly() -> None:
    with pytest.raises(AssertionError):
        _assert_artifacts_empty(
            {
                "items": [],
                "next_cursor": None,
                "summary": {
                    "managed_bytes": 0,
                    "storage_available_bytes": 0,
                    "active_download_tasks": 0,
                },
            },
            available_bytes_known=False,
        )


def _seed_reference_count_artifact(engine: Engine, tmp_path: Path) -> None:
    _seed_source(engine)
    _seed_model(engine, _uuid(1), display_name="reference-count-model")
    _seed_artifact(
        engine,
        _uuid(21),
        _uuid(1),
        created_at=BASE_MOMENT,
        path=str(tmp_path / "managed-reference-count"),
        size_bytes=1024,
    )


def _assert_reference_count_from_real_get_is_unknown(response: AsgiResponse) -> None:
    assert response.status == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["referencing_instances"] is None


def test_M2_12A_ruling_3b_TC_UI_08_real_get_without_instance_probe_returns_null(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    """The service, repository and route assemble the missing M3-01 fact."""

    _seed_reference_count_artifact(ephemeral_postgres.engine, tmp_path)
    model_root = tmp_path / "models"
    model_root.mkdir()

    async def scenario() -> AsgiResponse:
        async with _environment(ephemeral_postgres, model_root=model_root) as environment:
            await _create_accounts(environment)
            cookies = await _login(environment, ADMIN_USERNAME)
            return await environment.client.get(ARTIFACTS_PATH, cookies=cookies)

    _assert_reference_count_from_real_get_is_unknown(asyncio.run(scenario()))


def test_M2_12A_ruling_3b_positive_control_service_zero_reference_count_breaks_real_get(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    """The exact rejected ``return 0`` mutation must fail on the real GET path."""

    _seed_reference_count_artifact(ephemeral_postgres.engine, tmp_path)
    mutated = load_mutated_module(
        SERVICE_SOURCE,
        [
            (
                "        if self._artifact_instance_references is None:\n            return None",
                "        if self._artifact_instance_references is None:\n            return 0",
            )
        ],
        module_name="_mutated_m2_12a_service_zero_reference_count",
        destination=tmp_path / "mutated_service_zero_reference_count.py",
    )
    model_root = tmp_path / "models"
    model_root.mkdir()

    async def scenario() -> AsgiResponse:
        async with _environment(
            ephemeral_postgres,
            model_root=model_root,
            read_service_type=mutated.ModelAcquisitionReadService,
        ) as environment:
            await _create_accounts(environment)
            cookies = await _login(environment, ADMIN_USERNAME)
            return await environment.client.get(ARTIFACTS_PATH, cookies=cookies)

    with pytest.raises(AssertionError):
        _assert_reference_count_from_real_get_is_unknown(asyncio.run(scenario()))


# --------------------------------------------------------------------------
# both endpoints write nothing
# --------------------------------------------------------------------------


def _seed_mixed_domain(engine: Engine, *, staging_path: str, artifact_path: str) -> None:
    _seed_source(engine)
    _seed_model(engine, _uuid(1), display_name="alpha-model")
    _seed_model(engine, _uuid(2), display_name="beta-model")
    _seed_task(
        engine,
        _uuid(11),
        _uuid(1),
        status=DownloadTaskStatus.DOWNLOADING,
        created_at=BASE_MOMENT,
        resolved_revision="b" * 40,
        total_bytes=4096,
        downloaded_bytes=1024,
        staging_path=staging_path,
        suffix="downloading",
    )
    _seed_task(
        engine,
        _uuid(12),
        _uuid(1),
        status=DownloadTaskStatus.QUEUED,
        created_at=BASE_MOMENT + timedelta(minutes=1),
        suffix="queued",
    )
    _seed_task(
        engine,
        _uuid(13),
        _uuid(2),
        status=DownloadTaskStatus.FAILED,
        created_at=BASE_MOMENT + timedelta(minutes=2),
        resolved_revision="c" * 40,
        error_code=DownloadFailureReason.ARTIFACT_PUBLISH_FAILED.value,
        suffix="failed",
    )
    _seed_task(
        engine,
        _uuid(14),
        _uuid(2),
        status=DownloadTaskStatus.CANCELLED,
        created_at=BASE_MOMENT + timedelta(minutes=3),
        resolved_revision="d" * 40,
        error_code=DownloadCancellationReason.DUPLICATE_RESOLVED_REVISION.value,
        suffix="merged",
    )
    _seed_artifact(
        engine,
        _uuid(21),
        _uuid(1),
        created_at=BASE_MOMENT,
        path=artifact_path,
        size_bytes=2048,
    )


def test_M2_12A_ruling_1_TC_RES_02_both_reads_leave_every_row_untouched(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    sentinel = f"READONLY_PATH_SENTINEL_{os.getpid()}"
    staging = str(tmp_path / sentinel / "staging")
    artifact = str(tmp_path / sentinel / "managed")
    _seed_mixed_domain(ephemeral_postgres.engine, staging_path=staging, artifact_path=artifact)
    model_root = tmp_path / "models"
    model_root.mkdir()

    async def scenario() -> tuple[AsgiResponse, AsgiResponse, str, str]:
        async with _environment(ephemeral_postgres, model_root=model_root) as environment:
            await _create_accounts(environment)
            cookies = await _login(environment, ADMIN_USERNAME)
            before = _model_domain_snapshot(ephemeral_postgres.engine)
            downloads = await environment.client.get(DOWNLOADS_PATH, cookies=cookies)
            artifacts = await environment.client.get(ARTIFACTS_PATH, cookies=cookies)
            after = _model_domain_snapshot(ephemeral_postgres.engine)
            return downloads, artifacts, before, after

    downloads, artifacts, before, after = asyncio.run(scenario())
    assert downloads.status == 200, downloads.text
    assert artifacts.status == 200, artifacts.text
    assert_no_model_domain_writes(before, after)
    # The paths themselves are in scope for these two lists (PDD 9.3), so the
    # sentinel is expected in the bodies and forbidden everywhere else.
    assert sentinel in downloads.text
    assert sentinel in artifacts.text
    assert sentinel not in caplog.text
    assert sentinel not in _audit_details(ephemeral_postgres.engine)
    assert sentinel not in f"{downloads.server_error}{artifacts.server_error}"


def test_M2_12A_ruling_1_positive_control_a_single_update_is_detected(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _seed_source(ephemeral_postgres.engine)
    _seed_model(ephemeral_postgres.engine, _uuid(1), display_name="control-model")
    _seed_task(
        ephemeral_postgres.engine,
        _uuid(11),
        _uuid(1),
        status=DownloadTaskStatus.QUEUED,
        created_at=BASE_MOMENT,
    )
    before = _model_domain_snapshot(ephemeral_postgres.engine)
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE core.download_tasks SET updated_at = now() WHERE id = :id"),
            {"id": _uuid(11)},
        )
    with pytest.raises(AssertionError, match="wrote to the model domain"):
        assert_no_model_domain_writes(before, _model_domain_snapshot(ephemeral_postgres.engine))


# --------------------------------------------------------------------------
# the Download Task row
# --------------------------------------------------------------------------


def test_M2_12A_ruling_4_5_6_download_rows_derive_commands_and_stage(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    staging = str(tmp_path / "staging")
    _seed_mixed_domain(
        ephemeral_postgres.engine,
        staging_path=staging,
        artifact_path=str(tmp_path / "managed"),
    )
    model_root = tmp_path / "models"
    model_root.mkdir()

    async def scenario() -> AsgiResponse:
        async with _environment(ephemeral_postgres, model_root=model_root) as environment:
            await _create_accounts(environment)
            cookies = await _login(environment, ADMIN_USERNAME)
            return await environment.client.get(f"{DOWNLOADS_PATH}?page_size=50", cookies=cookies)

    response = asyncio.run(scenario())
    assert response.status == 200, response.text
    rows = {UUID(item["id"]): item for item in response.json()["items"]}
    assert set(rows) == {_uuid(11), _uuid(12), _uuid(13), _uuid(14)}

    downloading = rows[_uuid(11)]
    assert downloading["available_commands"] == [
        DownloadCommand.CANCEL.value,
        DownloadCommand.PAUSE.value,
    ]
    assert downloading["failure_stage"] is None
    assert downloading["total_bytes"] == 4096
    assert downloading["downloaded_bytes"] == 1024
    assert downloading["staging_path"] == staging

    queued = rows[_uuid(12)]
    assert queued["available_commands"] == [DownloadCommand.CANCEL.value]
    # An unresolved task has no Revision to show; the persisted
    # ``requested_revision_norm`` sentinel must never stand in for one.
    assert queued["revision"] is None
    assert queued["total_bytes"] is None

    failed = rows[_uuid(13)]
    assert failed["failure_stage"] == DownloadFailureStage.PUBLISHING.value
    assert sorted(failed["available_commands"]) == sorted(
        [DownloadCommand.RESUME.value, DownloadCommand.RETRY.value]
    )

    merged = rows[_uuid(14)]
    assert merged["failure_stage"] == DownloadFailureStage.RESOLVING.value
    assert merged["available_commands"] == []

    assert DEFAULT_REQUESTED_REVISION not in response.text
    for key in ("speed", "eta", "progress", "percent"):
        assert key not in downloading


def _seed_publishing_download(engine: Engine) -> None:
    _seed_source(engine)
    _seed_model(engine, _uuid(1), display_name="publishing-command-model")
    _seed_task(
        engine,
        _uuid(11),
        _uuid(1),
        status=DownloadTaskStatus.PUBLISHING,
        created_at=BASE_MOMENT,
        resolved_revision="f" * 40,
    )


def _assert_matrix_definition_reaches_real_get(response: AsgiResponse) -> None:
    assert response.status == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["available_commands"] == [DownloadCommand.CANCEL.value]


def _publishing_projector_with_cancel(
    status: DownloadTaskStatus,
    error_code: str | None = None,
    *,
    source_type: ModelOriginType | None = None,
) -> tuple[DownloadCommand, ...]:
    matrix = dict(DOWNLOAD_COMMAND_MATRIX)
    matrix[(DownloadTaskStatus.PUBLISHING, None)] = frozenset({DownloadCommand.CANCEL})
    return available_commands_for(status, error_code, matrix=matrix, source_type=source_type)


def test_M2_12A_ruling_4_matrix_definition_is_consumed_by_real_get(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_publishing_download(ephemeral_postgres.engine)
    monkeypatch.setattr(
        read_service_module,
        "available_commands_for",
        _publishing_projector_with_cancel,
    )
    model_root = tmp_path / "models"
    model_root.mkdir()

    async def scenario() -> AsgiResponse:
        async with _environment(ephemeral_postgres, model_root=model_root) as environment:
            await _create_accounts(environment)
            cookies = await _login(environment, ADMIN_USERNAME)
            return await environment.client.get(DOWNLOADS_PATH, cookies=cookies)

    _assert_matrix_definition_reaches_real_get(asyncio.run(scenario()))


def test_M2_12A_ruling_4_positive_control_literal_command_table_breaks_definition_link(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact rejected literal table must ignore the sole matrix definition."""

    _seed_publishing_download(ephemeral_postgres.engine)
    mutated = load_mutated_module(
        SERVICE_SOURCE,
        [
            (
                "                available_commands=available_commands_for(\n"
                "                    record.status, record.error_code, "
                "source_type=record.source_type\n"
                "                ),",
                "                available_commands=tuple(\n"
                "                    DownloadCommand(value)\n"
                "                    for value in {\n"
                '                        "Queued": ("cancel",),\n'
                '                        "Resolving": ("cancel",),\n'
                '                        "Downloading": ("cancel", "pause"),\n'
                '                        "Paused": ("cancel", "resume"),\n'
                '                        "Verifying": ("cancel",),\n'
                '                        "Publishing": (),\n'
                '                        "Completed": (),\n'
                '                        "Failed": ("resume", "retry"),\n'
                '                        "Cancelled": (),\n'
                "                    }[record.status.value]\n"
                "                ),",
            )
        ],
        module_name="_mutated_m2_12a_service_literal_command_table",
        destination=tmp_path / "mutated_service_literal_command_table.py",
    )
    monkeypatch.setattr(
        mutated,
        "available_commands_for",
        _publishing_projector_with_cancel,
    )
    model_root = tmp_path / "models"
    model_root.mkdir()

    async def scenario() -> AsgiResponse:
        async with _environment(
            ephemeral_postgres,
            model_root=model_root,
            read_service_type=mutated.ModelAcquisitionReadService,
        ) as environment:
            await _create_accounts(environment)
            cookies = await _login(environment, ADMIN_USERNAME)
            return await environment.client.get(DOWNLOADS_PATH, cookies=cookies)

    with pytest.raises(AssertionError):
        _assert_matrix_definition_reaches_real_get(asyncio.run(scenario()))


def test_M2_12A_ruling_5_TC_UI_21_unclassified_error_code_fails_observably(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    _seed_source(ephemeral_postgres.engine)
    _seed_model(ephemeral_postgres.engine, _uuid(1), display_name="unclassified-model")
    _seed_task(
        ephemeral_postgres.engine,
        _uuid(11),
        _uuid(1),
        status=DownloadTaskStatus.FAILED,
        created_at=BASE_MOMENT,
        resolved_revision="e" * 40,
        error_code=UNCLASSIFIED_ERROR_CODE,
    )
    model_root = tmp_path / "models"
    model_root.mkdir()

    async def scenario() -> AsgiResponse:
        async with _environment(ephemeral_postgres, model_root=model_root) as environment:
            await _create_accounts(environment)
            cookies = await _login(environment, ADMIN_USERNAME)
            return await environment.client.get(DOWNLOADS_PATH, cookies=cookies)

    response = asyncio.run(scenario())
    assert response.status == 500, response.text
    assert response.json()["error"]["code"] == "internal_error"
    assert UNCLASSIFIED_ERROR_CODE not in response.text
    assert UNCLASSIFIED_ERROR_CODE not in caplog.text
    assert response.server_error is not None
    assert UNCLASSIFIED_ERROR_CODE not in f"{response.server_error!r}"


# --------------------------------------------------------------------------
# the storage summary has one definition source per figure
# --------------------------------------------------------------------------


def _seed_storage_mixture(engine: Engine, tmp_path: Path) -> None:
    _seed_source(engine)
    _seed_model(engine, _uuid(1), display_name="managed-model")
    _seed_model(engine, _uuid(2), display_name="external-model")
    _seed_artifact(
        engine,
        _uuid(21),
        _uuid(1),
        created_at=BASE_MOMENT,
        path=str(tmp_path / "managed-a"),
        size_bytes=1000,
    )
    _seed_artifact(
        engine,
        _uuid(22),
        _uuid(1),
        created_at=BASE_MOMENT + timedelta(minutes=1),
        path=str(tmp_path / "managed-b"),
        size_bytes=2000,
    )
    _seed_artifact(
        engine,
        _uuid(23),
        _uuid(2),
        created_at=BASE_MOMENT + timedelta(minutes=2),
        path=str(tmp_path / "external"),
        size_bytes=9_000_000,
        storage_type=ArtifactStorageType.EXTERNAL,
        source_type=ModelOriginType.LOCAL_PATH,
    )
    for offset, status in enumerate(DownloadTaskStatus):
        _seed_task(
            engine,
            _uuid(100 + offset),
            _uuid(1),
            status=status,
            created_at=BASE_MOMENT + timedelta(minutes=10 + offset),
            resolved_revision=None,
            suffix=f"summary-{offset}",
        )


def test_M2_12A_ruling_7_summary_counts_only_managed_bytes_and_active_tasks(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    _seed_storage_mixture(ephemeral_postgres.engine, tmp_path)
    model_root = tmp_path / "models"
    model_root.mkdir()

    async def scenario() -> AsgiResponse:
        async with _environment(ephemeral_postgres, model_root=model_root) as environment:
            await _create_accounts(environment)
            cookies = await _login(environment, ADMIN_USERNAME)
            return await environment.client.get(ARTIFACTS_PATH, cookies=cookies)

    response = asyncio.run(scenario())
    assert response.status == 200, response.text
    summary = response.json()["summary"]
    assert summary["managed_bytes"] == 3000
    assert summary["active_download_tasks"] == len(ACTIVE_DOWNLOAD_STATUSES)
    assert isinstance(summary["storage_available_bytes"], int)
    external = next(
        item
        for item in response.json()["items"]
        if item["storage_type"] == ArtifactStorageType.EXTERNAL.value
    )
    # Excluded from the total, still reported on its own row.
    assert external["size_bytes"] == 9_000_000


def _assert_active_status_definition_reaches_real_get(response: AsgiResponse) -> None:
    assert response.status == 200, response.text
    assert response.json()["summary"]["active_download_tasks"] == 1


def test_M2_12A_ruling_7_active_status_definition_is_consumed_by_real_get(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_storage_mixture(ephemeral_postgres.engine, tmp_path)
    monkeypatch.setattr(
        read_service_module,
        "ACTIVE_DOWNLOAD_STATUSES",
        (DownloadTaskStatus.QUEUED.value,),
    )
    model_root = tmp_path / "models"
    model_root.mkdir()

    async def scenario() -> AsgiResponse:
        async with _environment(ephemeral_postgres, model_root=model_root) as environment:
            await _create_accounts(environment)
            cookies = await _login(environment, ADMIN_USERNAME)
            return await environment.client.get(ARTIFACTS_PATH, cookies=cookies)

    _assert_active_status_definition_reaches_real_get(asyncio.run(scenario()))


def test_M2_12A_ruling_7_positive_control_literal_active_statuses_break_definition_link(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact rejected six-state tuple must ignore the sole active definition."""

    _seed_storage_mixture(ephemeral_postgres.engine, tmp_path)
    mutated = load_mutated_module(
        SERVICE_SOURCE,
        [
            (
                "                active_download_statuses=ACTIVE_DOWNLOAD_STATUSES,",
                "                active_download_statuses=(\n"
                '                    "Queued",\n'
                '                    "Resolving",\n'
                '                    "Downloading",\n'
                '                    "Paused",\n'
                '                    "Verifying",\n'
                '                    "Publishing",\n'
                "                ),",
            )
        ],
        module_name="_mutated_m2_12a_service_literal_active_statuses",
        destination=tmp_path / "mutated_service_literal_active_statuses.py",
    )
    monkeypatch.setattr(
        mutated,
        "ACTIVE_DOWNLOAD_STATUSES",
        (DownloadTaskStatus.QUEUED.value,),
    )
    model_root = tmp_path / "models"
    model_root.mkdir()

    async def scenario() -> AsgiResponse:
        async with _environment(
            ephemeral_postgres,
            model_root=model_root,
            read_service_type=mutated.ModelAcquisitionReadService,
        ) as environment:
            await _create_accounts(environment)
            cookies = await _login(environment, ADMIN_USERNAME)
            return await environment.client.get(ARTIFACTS_PATH, cookies=cookies)

    with pytest.raises(AssertionError):
        _assert_active_status_definition_reaches_real_get(asyncio.run(scenario()))


def _totals(
    fixture: PostgresFixture,
    *,
    repository_module: Any,
    active_download_statuses: Sequence[str],
) -> tuple[int, int]:
    async def run() -> tuple[int, int]:
        engine = create_database_engine(_async_url(fixture), connect_timeout_seconds=5)
        try:
            session_factory = create_session_factory(engine)
            async with session_factory() as session:
                totals = await repository_module.ModelAcquisitionReadRepository(
                    session
                ).storage_totals(active_download_statuses=tuple(active_download_statuses))
            return totals.managed_bytes, totals.active_download_tasks
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_M2_12A_ruling_7_positive_control_counting_external_bytes_changes_the_total(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    """Drop the Managed restriction and the reported occupancy must move."""

    _seed_storage_mixture(ephemeral_postgres.engine, tmp_path)
    import holm_persistence.model_acquisition_read_repository as production

    mutated = load_mutated_module(
        REPOSITORY_SOURCE,
        [
            (
                "            .where(MODEL_ARTIFACTS.c.storage_type "
                "== ArtifactStorageType.MANAGED.value)\n",
                "",
            )
        ],
        module_name="_mutated_m2_12a_repository_all_storage",
        destination=tmp_path / "mutated_read_repository_all_storage.py",
    )
    honest, _ = _totals(
        ephemeral_postgres,
        repository_module=production,
        active_download_statuses=ACTIVE_DOWNLOAD_STATUSES,
    )
    inflated, _ = _totals(
        ephemeral_postgres,
        repository_module=mutated,
        active_download_statuses=ACTIVE_DOWNLOAD_STATUSES,
    )
    assert honest == 3000
    assert inflated == 9_003_000


def test_M2_12A_ruling_7_positive_control_active_status_change_changes_the_count(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    """Edit ``ACTIVE_DOWNLOAD_STATUSES`` and the in-progress figure follows."""

    _seed_storage_mixture(ephemeral_postgres.engine, tmp_path)
    import holm_persistence.model_acquisition_read_repository as production

    mutated = load_mutated_module(
        RESOLUTION_SOURCE,
        [("        DownloadTaskStatus.PAUSED,\n", "")],
        module_name="_mutated_m2_12a_active_statuses",
        destination=tmp_path / "mutated_model_resolution_service.py",
    )
    assert len(mutated.ACTIVE_DOWNLOAD_STATUSES) == len(ACTIVE_DOWNLOAD_STATUSES) - 1
    _, honest = _totals(
        ephemeral_postgres,
        repository_module=production,
        active_download_statuses=ACTIVE_DOWNLOAD_STATUSES,
    )
    _, narrowed = _totals(
        ephemeral_postgres,
        repository_module=production,
        active_download_statuses=mutated.ACTIVE_DOWNLOAD_STATUSES,
    )
    assert honest == len(ACTIVE_DOWNLOAD_STATUSES)
    assert narrowed == honest - 1


# --------------------------------------------------------------------------
# keyset pagination, newest first, opaque cursor
# --------------------------------------------------------------------------


def test_M2_12A_ruling_8_pages_are_newest_first_and_the_cursor_continues(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    _seed_source(ephemeral_postgres.engine)
    _seed_model(ephemeral_postgres.engine, _uuid(1), display_name="paged-model")
    for offset in range(5):
        _seed_task(
            ephemeral_postgres.engine,
            _uuid(200 + offset),
            _uuid(1),
            status=DownloadTaskStatus.QUEUED,
            created_at=BASE_MOMENT + timedelta(minutes=offset),
            suffix=f"paged-{offset}",
        )
    model_root = tmp_path / "models"
    model_root.mkdir()

    async def scenario() -> tuple[list[str], AsgiResponse, AsgiResponse]:
        async with _environment(ephemeral_postgres, model_root=model_root) as environment:
            await _create_accounts(environment)
            cookies = await _login(environment, ADMIN_USERNAME)
            collected: list[str] = []
            cursor: str | None = None
            pages = 0
            while True:
                query = f"{DOWNLOADS_PATH}?page_size=2"
                if cursor is not None:
                    query = f"{query}&cursor={cursor}"
                page = await environment.client.get(query, cookies=cookies)
                assert page.status == 200, page.text
                collected.extend(item["id"] for item in page.json()["items"])
                cursor = page.json()["next_cursor"]
                pages += 1
                if cursor is None or pages > 5:
                    break
            foreign = await environment.client.get(
                f"{DOWNLOADS_PATH}?cursor="
                + encode_cursor(
                    ARTIFACTS_CURSOR_KIND,
                    created_at=BASE_MOMENT,
                    row_id=_uuid(200),
                ),
                cookies=cookies,
            )
            unknown = await environment.client.get(f"{DOWNLOADS_PATH}?limit=2", cookies=cookies)
            return collected, foreign, unknown

    collected, foreign, unknown = asyncio.run(scenario())
    assert collected == [str(_uuid(200 + offset)) for offset in reversed(range(5))]
    assert len(set(collected)) == 5
    assert foreign.status == 400, foreign.text
    assert foreign.json()["error"]["code"] == "invalid_request"
    assert unknown.status == 400, unknown.text
    assert unknown.json()["error"]["code"] == "invalid_request"


# --------------------------------------------------------------------------
# Filters, and the four branches exercised by the tests
# --------------------------------------------------------------------------


def test_M2_12A_ruling_9_every_read_branch_runs_with_its_own_path_sentinel(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A positive control proves the scanner works; this proves the code ran.

    Four branches -- unfiltered listing, a filter that matches, a filter that
    matches nothing, and a continued page -- each carry a sentinel only that
    branch can reach, so a green scan cannot mean "the branch was never taken".
    """

    caplog.set_level(logging.DEBUG)
    listed = f"BRANCH_LISTED_{os.getpid()}"
    matched = f"BRANCH_MATCHED_{os.getpid()}"
    excluded = f"BRANCH_EXCLUDED_{os.getpid()}"
    continued = f"BRANCH_CONTINUED_{os.getpid()}"
    _seed_source(ephemeral_postgres.engine)
    _seed_model(ephemeral_postgres.engine, _uuid(1), display_name="corrupted-searchable-model")
    _seed_model(ephemeral_postgres.engine, _uuid(2), display_name="ready-plain-model")
    _seed_artifact(
        ephemeral_postgres.engine,
        _uuid(21),
        _uuid(1),
        created_at=BASE_MOMENT + timedelta(minutes=2),
        path=str(tmp_path / matched),
        size_bytes=10,
        status=ModelArtifactStatus.CORRUPTED,
    )
    _seed_artifact(
        ephemeral_postgres.engine,
        _uuid(22),
        _uuid(2),
        created_at=BASE_MOMENT + timedelta(minutes=1),
        path=str(tmp_path / listed),
        size_bytes=20,
    )
    _seed_artifact(
        ephemeral_postgres.engine,
        _uuid(23),
        _uuid(2),
        created_at=BASE_MOMENT,
        path=str(tmp_path / continued),
        size_bytes=30,
        storage_type=ArtifactStorageType.EXTERNAL,
        source_type=ModelOriginType.LOCAL_PATH,
    )
    _seed_model(ephemeral_postgres.engine, _uuid(3), display_name=f"never-{excluded}")
    model_root = tmp_path / "models"
    model_root.mkdir()

    async def scenario() -> dict[str, AsgiResponse]:
        async with _environment(ephemeral_postgres, model_root=model_root) as environment:
            await _create_accounts(environment)
            cookies = await _login(environment, ADMIN_USERNAME)
            first = await environment.client.get(f"{ARTIFACTS_PATH}?page_size=2", cookies=cookies)
            assert first.status == 200, first.text
            cursor = first.json()["next_cursor"]
            assert cursor is not None
            return {
                "listed": first,
                "matched": await environment.client.get(
                    f"{ARTIFACTS_PATH}?search=corrupted&status=Corrupted",
                    cookies=cookies,
                ),
                "excluded": await environment.client.get(
                    f"{ARTIFACTS_PATH}?search=never-{excluded}",
                    cookies=cookies,
                ),
                "continued": await environment.client.get(
                    f"{ARTIFACTS_PATH}?page_size=2&cursor={cursor}",
                    cookies=cookies,
                ),
                "unknown_filter": await environment.client.get(
                    f"{ARTIFACTS_PATH}?status=NotAStatus",
                    cookies=cookies,
                ),
            }

    responses = asyncio.run(scenario())
    audit = _audit_details(ephemeral_postgres.engine)

    listing = responses["listed"]
    assert [item["id"] for item in listing.json()["items"]] == [str(_uuid(21)), str(_uuid(22))]
    assert listed in listing.text

    hit = responses["matched"]
    assert hit.status == 200, hit.text
    assert [item["id"] for item in hit.json()["items"]] == [str(_uuid(21))]
    assert matched in hit.text

    miss = responses["excluded"]
    assert miss.status == 200, miss.text
    # A filter that matches nothing is an empty page, not an empty system: the
    # storage summary still reports the platform's real occupancy.
    assert miss.json()["items"] == []
    assert miss.json()["summary"]["managed_bytes"] == 30
    assert excluded not in miss.text

    rest = responses["continued"]
    assert rest.status == 200, rest.text
    assert [item["id"] for item in rest.json()["items"]] == [str(_uuid(23))]
    assert continued in rest.text
    assert rest.json()["next_cursor"] is None

    rejected = responses["unknown_filter"]
    assert rejected.status == 400, rejected.text
    assert rejected.json()["error"]["code"] == "invalid_request"
    assert "NotAStatus" not in rejected.text

    for sentinel, response in (
        (listed, listing),
        (matched, hit),
        (excluded, miss),
        (continued, rest),
    ):
        assert sentinel not in caplog.text
        assert sentinel not in audit
        assert sentinel not in f"{response.server_error}{response.server_error!r}"


def test_M2_12A_ruling_9_positive_control_a_sentinel_in_a_sink_is_detected(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    sentinel = "SINK_CONTROL_SENTINEL"
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO core.audit_events (id, actor_type, action, object_type, result, "
                "request_id, details, created_at) VALUES (gen_random_uuid(), :actor_type, "
                ":action, :object_type, :result, gen_random_uuid(), "
                "CAST(:details AS jsonb), clock_timestamp())"
            ),
            {
                "actor_type": AuditActorType.SYSTEM.value,
                "action": AuditAction.DOWNLOAD_CHANGE.value,
                "object_type": AuditObjectType.DOWNLOAD_TASK.value,
                "result": AuditResult.SUCCEEDED.value,
                "details": f'{{"path": "{sentinel}"}}',
            },
        )
    assert sentinel in _audit_details(ephemeral_postgres.engine)


def test_M2_12A_TC_SEC_11_both_endpoints_deny_a_user_without_the_capability(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    model_root = tmp_path / "models"
    model_root.mkdir()

    async def scenario() -> tuple[AsgiResponse, AsgiResponse]:
        async with _environment(ephemeral_postgres, model_root=model_root) as environment:
            await _create_accounts(environment)
            cookies = await _login(environment, MEMBER_USERNAME)
            return (
                await environment.client.get(DOWNLOADS_PATH, cookies=cookies),
                await environment.client.get(ARTIFACTS_PATH, cookies=cookies),
            )

    downloads, artifacts = asyncio.run(scenario())
    assert downloads.status == artifacts.status == 403
    assert downloads.json()["error"]["code"] == "permission_denied"
    assert artifacts.json()["error"]["code"] == "permission_denied"


def test_M2_12A_repository_never_selects_a_credential_column(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    """No secret is in these tables today; this keeps it that way by assertion."""

    forbidden = {
        "credential",
        "credential_key_id",
        "secret",
        "token",
        "password",
        "pepper",
    }
    source = REPOSITORY_SOURCE.read_text(encoding="utf-8")
    for name in forbidden:
        assert name not in source
    with ephemeral_postgres.engine.connect() as connection:
        columns = {
            str(row[0])
            for row in connection.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'core' AND table_name IN "
                    "('download_tasks', 'model_artifacts', 'models')"
                )
            )
        }
    assert not (columns & forbidden)

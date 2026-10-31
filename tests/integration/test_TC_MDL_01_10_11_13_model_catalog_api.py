"""TC-MDL-01/10/11/13: Model catalog read and cleanup over real PostgreSQL."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Final, Protocol
from uuid import UUID

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from fixtures.asgi_client import (
    NO_CONFIGURED_CREDENTIAL_ROWS,
    AsgiResponse,
    AsgiTestClient,
    SessionCookies,
)
from fixtures.model_domain_snapshot import (
    assert_no_model_domain_writes,
    model_domain_snapshot,
)
from holm_contracts import VersionPayload
from holm_control_api.application import create_app
from holm_control_api.authentication import AuthenticationService
from holm_control_api.http_constants import SESSION_COOKIE_NAME
from holm_control_api.model_catalog_routes import MODELS_PATH, REVISION_UNAVAILABLE
from holm_control_api.model_catalog_service import ModelCatalogService
from holm_control_api.readiness import DependencyCheck, ReadinessReason
from holm_domain import (
    ArtifactStorageType,
    DownloadTaskStatus,
    LocalAvailability,
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

ADMIN_USERNAME: Final[str] = "model-catalog-admin"
MEMBER_USERNAME: Final[str] = "model-catalog-member"
PASSWORD: Final[str] = "correct-password-for-model-catalog-tests"
CHEAP: Final = Argon2idParameters(
    memory_cost_kib=OWASP_MINIMUM_MEMORY_COST_KIB,
    time_cost=OWASP_MINIMUM_TIME_COST,
    parallelism=1,
)
SOURCE_ID: Final[UUID] = UUID("10000000-0000-4000-8000-000000000001")
READY_MODEL_ID: Final[UUID] = UUID("20000000-0000-4000-8000-000000000001")
ACTIVE_MODEL_ID: Final[UUID] = UUID("20000000-0000-4000-8000-000000000002")
QUEUED_MODEL_ID: Final[UUID] = UUID("20000000-0000-4000-8000-000000000003")
ABSENT_MODEL_ID: Final[UUID] = UUID("20000000-0000-4000-8000-000000000004")
LOCAL_LIST_MODEL_ID: Final[UUID] = UUID("20000000-0000-4000-8000-000000000005")
READY_REVISION: Final[str] = "a" * 40
ACTIVE_REVISION: Final[str] = "b" * 40
READY_SHADOWED_ACTIVE_REVISION: Final[str] = "c" * 40
MODEL_DOMAIN_TABLE_NAMES: Final[tuple[str, ...]] = (
    "models",
    "model_artifacts",
    "download_tasks",
    "download_subscriptions",
    "idempotency_records",
)


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
async def _environment(fixture: PostgresFixture) -> AsyncIterator[_Environment]:
    engine = create_database_engine(_async_url(fixture), connect_timeout_seconds=5)
    try:
        session_factory = create_session_factory(engine)
        authentication = AuthenticationService(
            session_factory=session_factory,
            password_hasher=PasswordHasher(CHEAP),
            minimum_login_failure_seconds=0,
        )
        app = create_app(
            version_payload=VersionPayload(
                build_version="2.2.0+mdl150002",
                commit="c" * 40,
                schema_version=1,
                runtime_compat_hash="d" * 64,
            ),
            readiness_checks=[DependencyCheck(name="database", probe=_readiness_ok)],
            credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
            authentication=authentication,
            model_catalog=ModelCatalogService(
                session_factory=session_factory,
            ),
        )
        yield _Environment(client=AsgiTestClient(app), session_factory=session_factory)
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def migrated_empty_catalog(ephemeral_postgres: EphemeralPostgres) -> Iterator[None]:
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


async def _login(
    environment: _Environment,
    username: str,
) -> Mapping[str, str]:
    response = await environment.client.request(
        "POST",
        "/api/v1/auth/login",
        json_body={"username": username, "password": PASSWORD},
    )
    assert response.status == 200
    return SessionCookies(
        {SESSION_COOKIE_NAME: _cookie(response, SESSION_COOKIE_NAME)},
        csrf_token=str(response.json()["csrf_token"]),
    )


def _seed_source(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO core.model_sources (id, type, created_at, updated_at) "
                "VALUES (:id, :type, clock_timestamp(), clock_timestamp())"
            ),
            {"id": SOURCE_ID, "type": ModelOriginType.HUGGING_FACE.value},
        )


def _seed_remote_model(engine: Engine, model_id: UUID, *, suffix: str) -> None:
    repository_id = f"catalog-owner/{suffix}"
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO core.models (id, source_id, source_type, source_key, display_name, "
                "repository_id, architecture, modalities, estimated_size_bytes, "
                "supported_runtimes, metadata_status, created_at, updated_at) VALUES "
                "(:id, :source_id, :source_type, :source_key, :display_name, :repository_id, "
                ":architecture, CAST(:modalities AS jsonb), :size, CAST(:runtimes AS jsonb), "
                ":metadata_status, clock_timestamp(), clock_timestamp())"
            ),
            {
                "id": model_id,
                "source_id": SOURCE_ID,
                "source_type": ModelOriginType.HUGGING_FACE.value,
                "source_key": repository_id,
                "display_name": suffix,
                "repository_id": repository_id,
                "architecture": "CatalogArchitecture",
                "modalities": '["text"]',
                "size": 2048,
                "runtimes": '["vllm"]',
                "metadata_status": MetadataStatus.CONFIRMED.value,
            },
        )


def _seed_local_model(
    engine: Engine,
    model_id: UUID,
    *,
    external_path: Path,
    display_name: str | None = None,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO core.models (id, source_id, source_type, source_key, display_name, "
                "external_path, modalities, supported_runtimes, metadata_status, created_at, "
                "updated_at) VALUES (:id, NULL, :source_type, :source_key, :display_name, "
                ":external_path, NULL, NULL, :metadata_status, clock_timestamp(), "
                "clock_timestamp())"
            ),
            {
                "id": model_id,
                "source_type": ModelOriginType.LOCAL_PATH.value,
                "source_key": str(external_path),
                "display_name": display_name or external_path.name,
                "external_path": str(external_path),
                "metadata_status": MetadataStatus.UNCONFIRMED.value,
            },
        )


def _seed_artifact(
    engine: Engine,
    model_id: UUID,
    *,
    path: Path,
    revision: str = READY_REVISION,
    storage_type: ArtifactStorageType = ArtifactStorageType.MANAGED,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO core.model_artifacts (id, model_id, source_type, repository_id, "
                "resolved_revision, storage_type, status, path, manifest_hash, size_bytes, "
                "created_at, updated_at) VALUES (gen_random_uuid(), :model_id, :source_type, "
                ":repository_id, :revision, :storage_type, :status, :path, :manifest_hash, "
                "1024, clock_timestamp(), clock_timestamp())"
            ),
            {
                "model_id": model_id,
                "source_type": ModelOriginType.LOCAL_PATH.value,
                "repository_id": "catalog-artifact",
                "revision": revision,
                "storage_type": storage_type.value,
                "status": ModelArtifactStatus.READY.value,
                "path": str(path),
                "manifest_hash": "e" * 64,
            },
        )


def _seed_download(
    engine: Engine,
    model_id: UUID,
    *,
    suffix: str,
    resolved_revision: str | None,
    staging_path: str | None = None,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO core.download_tasks (id, model_id, source_type, repository_id, "
                "requested_revision_norm, resolved_revision, status, attempt, downloaded_bytes, "
                "staging_path, created_at, updated_at) VALUES (gen_random_uuid(), :model_id, "
                ":source_type, :repository_id, :requested_revision, :resolved_revision, :status, "
                "1, 0, :staging_path, clock_timestamp(), clock_timestamp())"
            ),
            {
                "model_id": model_id,
                "source_type": ModelOriginType.HUGGING_FACE.value,
                "repository_id": f"catalog-owner/{suffix}",
                "requested_revision": DEFAULT_REQUESTED_REVISION,
                "resolved_revision": resolved_revision,
                "status": DownloadTaskStatus.QUEUED.value,
                "staging_path": staging_path,
            },
        )


def _model_domain_snapshot(engine: Engine) -> str:
    return model_domain_snapshot(engine, MODEL_DOMAIN_TABLE_NAMES)


type TreeSnapshot = tuple[tuple[str, int, int, int, bytes | None], ...]


def _tree_snapshot(root: Path) -> TreeSnapshot:
    paths = (root, *sorted(root.rglob("*")))
    captured: list[tuple[str, int, int, int, bytes | None]] = []
    for path in paths:
        stat = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        captured.append(
            (
                relative,
                stat.st_mode,
                stat.st_ino,
                stat.st_mtime_ns,
                path.read_bytes() if path.is_file() else None,
            )
        )
    return tuple(captured)


def _assert_tree_unchanged(root: Path, before: TreeSnapshot) -> None:
    assert root.exists(), "External source root was deleted"
    assert _tree_snapshot(root) == before, "External source bytes or metadata changed"


def _assert_sentinel_absent(sentinel: str, *values: object) -> None:
    rendered = "\n".join(str(value) for value in values)
    assert sentinel not in rendered


def _assert_catalog_empty(payload: object) -> None:
    assert payload == {"items": [], "next_cursor": None}


def test_TC_MDL_01_empty_catalog_is_explicitly_empty(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> AsgiResponse:
        async with _environment(ephemeral_postgres) as environment:
            await _create_accounts(environment)
            cookies = await _login(environment, ADMIN_USERNAME)
            return await environment.client.get(MODELS_PATH, cookies=cookies)

    response = asyncio.run(scenario())
    assert response.status == 200
    _assert_catalog_empty(response.json())


def test_TC_MDL_01_positive_control_preloaded_catalog_fails_loudly() -> None:
    with pytest.raises(AssertionError):
        _assert_catalog_empty({"items": [{"id": "preloaded-sentinel"}], "next_cursor": None})


def test_M2_13B_ruling_1_TC_MDL_10_three_states_revision_rules_and_get_are_read_only(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    list_sentinel = f"LIST_PATH_SENTINEL_{os.getpid()}"
    source_root = tmp_path / list_sentinel
    source_root.mkdir()
    (source_root / "weights.bin").write_bytes(b"catalog-list-sentinel-bytes")
    _seed_source(ephemeral_postgres.engine)
    for model_id, suffix in (
        (READY_MODEL_ID, "ready"),
        (ACTIVE_MODEL_ID, "active"),
        (QUEUED_MODEL_ID, "queued-default"),
        (ABSENT_MODEL_ID, "absent"),
    ):
        _seed_remote_model(ephemeral_postgres.engine, model_id, suffix=suffix)
    _seed_local_model(
        ephemeral_postgres.engine,
        LOCAL_LIST_MODEL_ID,
        external_path=source_root,
        display_name="local-path",
    )
    _seed_artifact(ephemeral_postgres.engine, READY_MODEL_ID, path=source_root)
    _seed_download(
        ephemeral_postgres.engine,
        READY_MODEL_ID,
        suffix="ready-shadowed-active",
        resolved_revision=READY_SHADOWED_ACTIVE_REVISION,
    )
    _seed_download(
        ephemeral_postgres.engine,
        ACTIVE_MODEL_ID,
        suffix="active",
        resolved_revision=ACTIVE_REVISION,
    )
    _seed_download(
        ephemeral_postgres.engine,
        QUEUED_MODEL_ID,
        suffix="queued-default",
        resolved_revision=None,
        staging_path=str(source_root / "staging"),
    )
    tree_before = _tree_snapshot(source_root)

    async def scenario() -> tuple[AsgiResponse, str, str]:
        async with _environment(ephemeral_postgres) as environment:
            await _create_accounts(environment)
            cookies = await _login(environment, ADMIN_USERNAME)
            before = _model_domain_snapshot(ephemeral_postgres.engine)
            response = await environment.client.get(MODELS_PATH, cookies=cookies)
            after = _model_domain_snapshot(ephemeral_postgres.engine)
            return response, before, after

    response, before, after = asyncio.run(scenario())
    assert response.status == 200
    assert_no_model_domain_writes(before, after)
    items = {item["display_name"]: item for item in response.json()["items"]}
    assert {name: item["local_availability"] for name, item in items.items()} == {
        "ready": LocalAvailability.ARTIFACT_READY.value,
        "active": LocalAvailability.DOWNLOAD_ACTIVE.value,
        "queued-default": LocalAvailability.DOWNLOAD_ACTIVE.value,
        "absent": LocalAvailability.ABSENT.value,
        "local-path": LocalAvailability.ABSENT.value,
    }
    assert items["ready"]["revision"] == READY_REVISION
    assert items["active"]["revision"] == ACTIVE_REVISION
    assert items["queued-default"]["revision"] == REVISION_UNAVAILABLE
    assert items["absent"]["revision"] == REVISION_UNAVAILABLE
    assert items["local-path"]["revision"] == REVISION_UNAVAILABLE
    assert items["absent"]["repository_id"] == "catalog-owner/absent"
    assert items["absent"]["repository_id"] != items["absent"]["display_name"]
    assert items["local-path"]["repository_id"] is None
    assert {name: item["references"] for name, item in items.items()} == {
        "ready": {"artifacts": 1, "download_tasks": 1, "model_instances": 0},
        "active": {"artifacts": 0, "download_tasks": 1, "model_instances": 0},
        "queued-default": {"artifacts": 0, "download_tasks": 1, "model_instances": 0},
        "absent": {"artifacts": 0, "download_tasks": 0, "model_instances": 0},
        "local-path": {"artifacts": 0, "download_tasks": 0, "model_instances": 0},
    }
    assert DEFAULT_REQUESTED_REVISION not in response.text
    assert set(items["absent"]) == {
        "id",
        "source",
        "display_name",
        "repository_id",
        "architecture",
        "modalities",
        "estimated_size_bytes",
        "revision",
        "local_availability",
        "references",
    }
    _assert_tree_unchanged(source_root, tree_before)
    _assert_sentinel_absent(list_sentinel, response.text, caplog.text, response.server_error)


def test_TC_MDL_10_positive_control_model_domain_rewrite_fails_loudly(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _seed_source(ephemeral_postgres.engine)
    _seed_remote_model(ephemeral_postgres.engine, READY_MODEL_ID, suffix="rewrite-control")
    before = _model_domain_snapshot(ephemeral_postgres.engine)
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE core.models SET display_name = display_name WHERE id = :id"),
            {"id": READY_MODEL_ID},
        )
    with pytest.raises(AssertionError, match="wrote to the model domain"):
        assert_no_model_domain_writes(before, _model_domain_snapshot(ephemeral_postgres.engine))


def test_TC_MDL_10_existing_pagination_contract_is_the_http_surface(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _seed_source(ephemeral_postgres.engine)
    _seed_remote_model(ephemeral_postgres.engine, READY_MODEL_ID, suffix="first")
    _seed_remote_model(ephemeral_postgres.engine, ACTIVE_MODEL_ID, suffix="second")

    async def scenario() -> tuple[AsgiResponse, AsgiResponse, AsgiResponse]:
        async with _environment(ephemeral_postgres) as environment:
            await _create_accounts(environment)
            cookies = await _login(environment, ADMIN_USERNAME)
            first = await environment.client.get(
                f"{MODELS_PATH}?page_size=1",
                cookies=cookies,
            )
            assert first.status == 200, first.text
            cursor = first.json()["next_cursor"]
            second = await environment.client.get(
                f"{MODELS_PATH}?page_size=1&cursor={cursor}",
                cookies=cookies,
            )
            unknown = await environment.client.get(
                f"{MODELS_PATH}?limit=1",
                cookies=cookies,
            )
            return first, second, unknown

    first, second, unknown = asyncio.run(scenario())
    assert first.status == second.status == 200
    assert len(first.json()["items"]) == len(second.json()["items"]) == 1
    assert first.json()["items"][0]["id"] != second.json()["items"][0]["id"]
    assert first.json()["next_cursor"] is not None
    assert second.json()["next_cursor"] is None
    assert unknown.status == 400
    assert unknown.json()["error"]["code"] == "invalid_request"


def test_TC_MDL_11_unreferenced_delete_succeeds_without_touching_external_source(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = f"DELETE_SUCCESS_PATH_SENTINEL_{os.getpid()}"
    root = tmp_path / sentinel
    root.mkdir()
    (root / "config.json").write_text('{"safe": true}', encoding="utf-8")
    model_id = UUID("30000000-0000-4000-8000-000000000001")
    _seed_local_model(ephemeral_postgres.engine, model_id, external_path=root)
    before = _tree_snapshot(root)

    async def scenario() -> AsgiResponse:
        async with _environment(ephemeral_postgres) as environment:
            await _create_accounts(environment)
            cookies = await _login(environment, ADMIN_USERNAME)
            return await environment.client.request(
                "DELETE",
                f"{MODELS_PATH}/{model_id}",
                cookies=cookies,
            )

    response = asyncio.run(scenario())
    assert response.status == 204
    with ephemeral_postgres.engine.connect() as connection:
        assert (
            connection.execute(
                sa.text("SELECT count(*) FROM core.models WHERE id = :id"), {"id": model_id}
            ).scalar_one()
            == 0
        )
    _assert_tree_unchanged(root, before)
    _assert_sentinel_absent(sentinel, response.text, caplog.text, response.server_error)


@pytest.mark.parametrize("reference_kind", ["artifact", "download"])
def test_TC_MDL_11_referenced_delete_is_stable_read_only_and_source_unchanged(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    reference_kind: str,
) -> None:
    sentinel = f"DELETE_REJECTED_{reference_kind}_PATH_SENTINEL_{os.getpid()}"
    root = tmp_path / sentinel
    root.mkdir()
    (root / "weights.bin").write_bytes(b"external-artifact-must-not-change")
    model_id = UUID(
        "30000000-0000-4000-8000-000000000002"
        if reference_kind == "artifact"
        else "30000000-0000-4000-8000-000000000003"
    )
    _seed_local_model(ephemeral_postgres.engine, model_id, external_path=root)
    if reference_kind == "artifact":
        _seed_artifact(
            ephemeral_postgres.engine,
            model_id,
            path=root,
            storage_type=ArtifactStorageType.EXTERNAL,
        )
    else:
        _seed_download(
            ephemeral_postgres.engine,
            model_id,
            suffix="blocked",
            resolved_revision=None,
            staging_path=str(root / "staging"),
        )
    tree_before = _tree_snapshot(root)

    async def scenario() -> tuple[AsgiResponse, str, str]:
        async with _environment(ephemeral_postgres) as environment:
            await _create_accounts(environment)
            cookies = await _login(environment, ADMIN_USERNAME)
            rows_before = _model_domain_snapshot(ephemeral_postgres.engine)
            response = await environment.client.request(
                "DELETE",
                f"{MODELS_PATH}/{model_id}",
                cookies=cookies,
            )
            rows_after = _model_domain_snapshot(ephemeral_postgres.engine)
            return response, rows_before, rows_after

    response, rows_before, rows_after = asyncio.run(scenario())
    assert response.status == 409
    assert response.json()["error"]["code"] == "model_referenced"
    assert_no_model_domain_writes(rows_before, rows_after)
    _assert_tree_unchanged(root, tree_before)
    _assert_sentinel_absent(sentinel, response.text, caplog.text, response.server_error)


def test_TC_MDL_11_positive_control_recursive_external_delete_fails_loudly(
    tmp_path: Path,
) -> None:
    root = tmp_path / "recursive-delete-positive-control"
    root.mkdir()
    (root / "weights.bin").write_bytes(b"must-survive")
    before = _tree_snapshot(root)
    shutil.rmtree(root)
    with pytest.raises(AssertionError, match="deleted"):
        _assert_tree_unchanged(root, before)


def test_TC_MDL_13_non_admin_is_denied_on_both_catalog_routes(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    model_id = UUID("30000000-0000-4000-8000-000000000004")
    _seed_local_model(
        ephemeral_postgres.engine,
        model_id,
        external_path=tmp_path / "rbac-delete-sentinel",
    )

    async def scenario() -> tuple[AsgiResponse, AsgiResponse]:
        async with _environment(ephemeral_postgres) as environment:
            await _create_accounts(environment)
            cookies = await _login(environment, MEMBER_USERNAME)
            listed = await environment.client.get(MODELS_PATH, cookies=cookies)
            deleted = await environment.client.request(
                "DELETE",
                f"{MODELS_PATH}/{model_id}",
                cookies=cookies,
            )
            return listed, deleted

    listed, deleted = asyncio.run(scenario())
    assert listed.status == deleted.status == 403
    assert listed.json()["error"]["code"] == "permission_denied"
    assert deleted.json()["error"]["code"] == "permission_denied"
    with ephemeral_postgres.engine.connect() as connection:
        assert (
            connection.execute(
                sa.text("SELECT count(*) FROM core.models WHERE id = :id"), {"id": model_id}
            ).scalar_one()
            == 1
        )


def test_TC_MDL_10_positive_control_default_revision_leak_fails_loudly() -> None:
    with pytest.raises(AssertionError):
        _assert_sentinel_absent(DEFAULT_REQUESTED_REVISION, json.dumps({"revision": "@default"}))

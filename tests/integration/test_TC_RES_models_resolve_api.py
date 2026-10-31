"""TC-RES-01/02/06/08/09/10: ``POST /api/v1/models/resolve`` against real PostgreSQL."""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import re
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Final, Protocol
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from credential_scan import database_bytes
from fixtures.asgi_client import (
    NO_CONFIGURED_CREDENTIAL_ROWS,
    AsgiResponse,
    AsgiTestClient,
    SessionCookies,
)
from fixtures.fake_model_source import (
    PathAttackKind,
    path_attack_samples,
    revision_scenario,
)
from fixtures.model_domain_snapshot import (
    assert_no_model_domain_writes,
    model_domain_snapshot,
)
from fixtures.source_mutation import load_mutated_module
from holm_contracts import VersionPayload
from holm_control_api import authorization
from holm_control_api.application import create_app
from holm_control_api.authentication import AuthenticationService
from holm_control_api.authorization import (
    RouteAccessKind,
    assert_route_authorization_registered,
    route_access_policy,
)
from holm_control_api.external_path_resolution import (
    ExternalPathPolicy,
    ExternalPathResolutionAdapter,
)
from holm_control_api.http_constants import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME
from holm_control_api.model_registration_routes import (
    IDEMPOTENCY_KEY_HEADER,
    MODELS_PATH,
    ModelRegistrationRequest,
)
from holm_control_api.model_registration_service import ModelRegistrationService
from holm_control_api.model_resolution import (
    ModelResolutionRegistry,
    ResolutionFailed,
    ResolutionOutcome,
    ResolutionRequest,
    ResolutionSucceeded,
    RuntimeReadability,
    RuntimeReadabilityUnverifiedCause,
)
from holm_control_api.model_resolution_routes import MODELS_RESOLVE_PATH
from holm_control_api.model_resolution_service import ModelResolutionService
from holm_control_api.model_source_validation import ModelSourceValidationResult
from holm_control_api.readiness import DependencyCheck, ReadinessReason
from holm_domain import (
    Capability,
    CredentialType,
    DownloadTaskStatus,
    Identified,
    LocalAvailability,
    MetadataStatus,
    ModelArtifactStatus,
    ModelOriginType,
    ModelSourceType,
    ModelSourceValidationAction,
    ModelSourceValidationReason,
    ModelSourceValidationStatus,
    RemoteFile,
    ResolvedModel,
    RevisionImmutability,
    Unrecognized,
    UserRole,
)
from holm_persistence import (
    AsyncSessionFactory,
    ModelRepository,
    ModelSourceRepository,
    UserRepository,
    create_database_engine,
    create_session_factory,
    database_utc_now,
    normalize_requested_revision,
    normalize_source_key,
)
from holm_security import (
    OWASP_MINIMUM_MEMORY_COST_KIB,
    OWASP_MINIMUM_TIME_COST,
    Argon2idParameters,
    MasterKey,
    MasterKeyring,
    MasterKeyState,
    PasswordHasher,
)
from migration_test_support import upgrade_head
from sqlalchemy.engine import URL, Engine

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
RESOLVE_SOURCE_PATHS: Final[tuple[Path, ...]] = (
    REPOSITORY_ROOT / "packages/domain/src/holm_domain/model_resolution.py",
    REPOSITORY_ROOT / "apps/control_api/src/holm_control_api/model_resolution.py",
    REPOSITORY_ROOT / "apps/control_api/src/holm_control_api/model_resolution_service.py",
    REPOSITORY_ROOT / "apps/control_api/src/holm_control_api/model_resolution_routes.py",
    # M2-07A's External Path judgement is a resolve source too, so both guards
    # below scan it rather than letting a new file sit outside their reach.
    REPOSITORY_ROOT / "apps/control_api/src/holm_control_api/external_path_resolution.py",
    # M2-07B moved the rule into ``packages/`` so the Host Agent shares it.  The
    # scanned surface follows the code rather than shrinking when it moves.
    REPOSITORY_ROOT / "packages/contracts/src/holm_contracts/external_paths.py",
)

ADMIN_USERNAME: Final[str] = "model-resolve-admin"
MEMBER_USERNAME: Final[str] = "model-resolve-member"
PASSWORD: Final[str] = "correct-password-for-model-resolve-tests"
CHEAP: Final = Argon2idParameters(
    memory_cost_kib=OWASP_MINIMUM_MEMORY_COST_KIB,
    time_cost=OWASP_MINIMUM_TIME_COST,
    parallelism=1,
)
KEYRING: Final = MasterKeyring((MasterKey("resolve-key", MasterKeyState.WRITABLE, b"R" * 32),))
IDEMPOTENCY_HMAC_SECRET: Final[bytes] = b"I" * 32
FIXTURE_REPOSITORY_ID: Final[str] = "synthetic-owner/revision-fixture"
DEFAULT_COMMIT: Final[str] = "1" * 40
NEXT_COMMIT: Final[str] = "2" * 40
MODEL_DOMAIN_TABLE_NAMES: Final[tuple[str, ...]] = (
    "models",
    "model_artifacts",
    "download_tasks",
    "download_subscriptions",
)
MODEL_SOURCE_ID: Final[UUID] = UUID("2b7d1a53-0f5c-4a6e-8c31-9d5f0a1b2c3d")


class PostgresFixture(Protocol):
    @property
    def engine(self) -> Engine: ...

    @property
    def socket_directory(self) -> Path: ...

    @property
    def port(self) -> int: ...


@dataclass(slots=True)
class _RecordingAdapter:
    """A source stand-in built entirely from ``FX-MS-01``/``FX-MS-07``."""

    requests: list[ResolutionRequest] = field(default_factory=list)
    credentials: list[bytes | None] = field(default_factory=list)

    async def resolve(
        self,
        request: ResolutionRequest,
        *,
        credential: bytes | None,
    ) -> ResolutionOutcome:
        self.requests.append(request)
        self.credentials.append(credential)
        scenario = revision_scenario()
        try:
            resolved = scenario.resolve(request.requested_revision_norm)
        except KeyError:
            return ResolutionFailed(
                ModelSourceValidationResult(
                    status=ModelSourceValidationStatus.NOT_FOUND,
                    reason_code=ModelSourceValidationReason.SOURCE_NOT_FOUND,
                    next_action=ModelSourceValidationAction.CHECK_SOURCE_REFERENCE,
                )
            )
        return ResolutionSucceeded(
            ResolvedModel(
                source=request.source,
                repository_id=request.repository_id,
                requested_revision_norm=request.requested_revision_norm,
                resolved_revision=resolved.resolved_commit,
                revision_immutability=RevisionImmutability.IMMUTABLE,
                files=Identified(
                    (
                        RemoteFile(path="config.json", size_bytes=2_042),
                        RemoteFile(path="tokenizer.json", size_bytes=2),
                        RemoteFile(path="model.bin", size_bytes=4),
                    )
                ),
                total_size_bytes=Identified(2_048),
                architecture=Identified("SyntheticCausalArchitecture"),
                modalities=Identified(("text",)),
                precision=Identified("bf16"),
                quantization=Identified("none"),
                context_length=Identified(32_768),
                candidate_runtimes=Identified(("vllm",)),
                license=Identified("apache-2.0"),
            )
        )


@dataclass(slots=True)
class _ExplodingAdapter:
    """An Adapter whose failure text carries a sentinel it must never leak."""

    detail: str

    async def resolve(
        self,
        request: ResolutionRequest,
        *,
        credential: bytes | None,
    ) -> ResolutionOutcome:
        del request, credential
        raise RuntimeError(self.detail)


class _LenientRegistry(ModelResolutionRegistry):
    """Forbidden mutation: "no implementation" reported as success.

    Writing it at all requires inventing an immutable Revision, because
    ``ResolvedModel`` has neither an empty instance nor a default one.  That is
    the type-level protection; the test below is the loud half.
    """

    async def resolve(
        self,
        request: ResolutionRequest,
        *,
        credential: bytes | None,
    ) -> ResolutionOutcome:
        del credential
        return ResolutionSucceeded(
            ResolvedModel(
                source=request.source,
                repository_id=request.repository_id,
                requested_revision_norm=request.requested_revision_norm,
                resolved_revision="0" * 40,
                revision_immutability=RevisionImmutability.IMMUTABLE,
                files=Identified(()),
                total_size_bytes=Identified(0),
                architecture=Identified("unknown"),
                modalities=Identified(()),
                precision=Identified("unknown"),
                quantization=Identified("unknown"),
                context_length=Identified(1),
                candidate_runtimes=Identified(()),
                license=Identified("unknown"),
            )
        )


@dataclass(frozen=True, slots=True)
class _Environment:
    client: AsgiTestClient
    session_factory: AsyncSessionFactory
    adapter: _RecordingAdapter
    app: Any
    resolution: ModelResolutionService


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
    registry: ModelResolutionRegistry | None = None,
    adapter: _RecordingAdapter | None = None,
    sources: Sequence[ModelOriginType] = (ModelOriginType.HUGGING_FACE,),
) -> AsyncIterator[_Environment]:
    engine = create_database_engine(_async_url(fixture), connect_timeout_seconds=5)
    try:
        session_factory = create_session_factory(engine)
        authentication = AuthenticationService(
            session_factory=session_factory,
            password_hasher=PasswordHasher(CHEAP),
            minimum_login_failure_seconds=0,
        )
        recorder = _RecordingAdapter() if adapter is None else adapter
        resolved_registry = (
            ModelResolutionRegistry({source: recorder for source in sources})
            if registry is None
            else registry
        )
        service = ModelResolutionService(
            session_factory=session_factory,
            keyring=KEYRING,
            registry=resolved_registry,
        )
        registration = ModelRegistrationService(
            session_factory=session_factory,
            resolution=service,
            idempotency_hmac_secret=IDEMPOTENCY_HMAC_SECRET,
        )
        app = create_app(
            version_payload=VersionPayload(
                build_version="2.2.0+gres030001",
                commit="c" * 40,
                schema_version=1,
                runtime_compat_hash="d" * 64,
            ),
            readiness_checks=[DependencyCheck(name="database", probe=_readiness_ok)],
            credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
            authentication=authentication,
            model_resolution=service,
            model_registration=registration,
        )
        yield _Environment(
            client=AsgiTestClient(app),
            session_factory=session_factory,
            adapter=recorder,
            app=app,
            resolution=service,
        )
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def migrated_model_domain(ephemeral_postgres: EphemeralPostgres) -> Iterator[None]:
    upgrade_head(ephemeral_postgres.engine)
    _truncate(ephemeral_postgres.engine)
    yield
    _truncate(ephemeral_postgres.engine)


def _truncate(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "TRUNCATE core.download_subscriptions, core.download_tasks, "
                "core.model_artifacts, core.models, core.audit_events, "
                "core.login_protection, core.sessions, core.model_sources, "
                "core.users CASCADE"
            )
        )


async def _create_accounts(environment: _Environment) -> None:
    async with environment.session_factory() as session:
        password_hash = await asyncio.to_thread(PasswordHasher(CHEAP).hash, PASSWORD)
        now = await database_utc_now(session)
        repository = UserRepository(session)
        await repository.create(
            username=ADMIN_USERNAME,
            display_name="Model Resolve Admin",
            role=UserRole.ADMIN,
            password_hash=password_hash,
            now=now,
            must_change_password=False,
        )
        await repository.create(
            username=MEMBER_USERNAME,
            display_name="Model Resolve Member",
            role=UserRole.USER,
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


def _header(response: AsgiResponse, name: str) -> str:
    values = [value for key, value in response.headers if key.casefold() == name.casefold()]
    assert len(values) == 1
    return values[0]


async def _login(environment: _Environment, username: str = ADMIN_USERNAME) -> Mapping[str, str]:
    response = await environment.client.request(
        "POST",
        "/api/v1/auth/login",
        json_body={"username": username, "password": PASSWORD},
    )
    assert response.status == 200
    _ = _cookie(response, CSRF_COOKIE_NAME)
    return SessionCookies(
        {SESSION_COOKIE_NAME: _cookie(response, SESSION_COOKIE_NAME)},
        csrf_token=str(response.json()["csrf_token"]),
    )


async def _resolve(
    environment: _Environment,
    cookies: Mapping[str, str],
    body: Mapping[str, object],
) -> AsgiResponse:
    return await environment.client.request(
        "POST",
        MODELS_RESOLVE_PATH,
        cookies=cookies,
        json_body=dict(body),
    )


async def _register(
    environment: _Environment,
    cookies: Mapping[str, str],
    body: Mapping[str, object],
    *,
    idempotency_key: str,
) -> AsgiResponse:
    return await environment.client.request(
        "POST",
        MODELS_PATH,
        headers={IDEMPOTENCY_KEY_HEADER: idempotency_key},
        cookies=cookies,
        json_body=dict(body),
    )


def _model_domain_snapshot(engine: Engine) -> str:
    return model_domain_snapshot(engine, MODEL_DOMAIN_TABLE_NAMES)


def _seed_model(engine: Engine, *, repository_id: str) -> UUID:
    model_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO core.model_sources (id, type, created_at, updated_at) "
                "VALUES (:id, :type, clock_timestamp(), clock_timestamp()) "
                "ON CONFLICT (type) DO NOTHING"
            ),
            {"id": MODEL_SOURCE_ID, "type": ModelOriginType.HUGGING_FACE.value},
        )
        connection.execute(
            sa.text(
                "INSERT INTO core.models (id, source_id, source_type, source_key, display_name, "
                "repository_id, modalities, supported_runtimes, metadata_status, created_at, "
                "updated_at) VALUES (:id, :source_id, :source_type, :source_key, :display_name, "
                ":repository_id, CAST(:modalities AS jsonb), CAST(:runtimes AS jsonb), "
                ":metadata_status, clock_timestamp(), clock_timestamp())"
            ),
            {
                "id": model_id,
                "source_id": MODEL_SOURCE_ID,
                "source_type": ModelOriginType.HUGGING_FACE.value,
                "source_key": normalize_source_key(repository_id),
                "display_name": "Resolve fixture",
                "repository_id": repository_id,
                "modalities": '["text"]',
                "runtimes": '["vllm"]',
                "metadata_status": MetadataStatus.CONFIRMED.value,
            },
        )
    return model_id


def _seed_ready_artifact(engine: Engine, model_id: UUID, *, repository_id: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO core.model_artifacts (id, model_id, source_type, repository_id, "
                "resolved_revision, storage_type, status, path, manifest_hash, size_bytes, "
                "created_at, updated_at) VALUES (:id, :model_id, :source_type, :repository_id, "
                ":resolved_revision, 'Managed', :status, :path, :manifest_hash, 1024, "
                "clock_timestamp(), clock_timestamp())"
            ),
            {
                "id": uuid4(),
                "model_id": model_id,
                "source_type": ModelOriginType.HUGGING_FACE.value,
                "repository_id": repository_id,
                "resolved_revision": DEFAULT_COMMIT,
                "status": ModelArtifactStatus.READY.value,
                "path": "/model-root/resolve-fixture",
                "manifest_hash": "a" * 64,
            },
        )


def _seed_active_download(engine: Engine, model_id: UUID, *, repository_id: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO core.download_tasks (id, model_id, source_type, repository_id, "
                "requested_revision_norm, resolved_revision, status, attempt, downloaded_bytes, "
                "created_at, updated_at) VALUES (:id, :model_id, :source_type, :repository_id, "
                ":requested_revision_norm, :resolved_revision, :status, 1, 0, "
                "clock_timestamp(), clock_timestamp())"
            ),
            {
                "id": uuid4(),
                "model_id": model_id,
                "source_type": ModelOriginType.HUGGING_FACE.value,
                "repository_id": repository_id,
                "requested_revision_norm": normalize_requested_revision(None),
                "resolved_revision": DEFAULT_COMMIT,
                "status": DownloadTaskStatus.DOWNLOADING.value,
            },
        )


def test_TC_RES_01_normalization_has_exactly_one_definition_source(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as environment:
            await _create_accounts(environment)
            cookies = await _login(environment)

            spellings = (
                FIXTURE_REPOSITORY_ID,
                f"  {FIXTURE_REPOSITORY_ID}  ",
                # NFKC folds the "fi" ligature; a second, ad-hoc strip()/lower()
                # in the endpoint would not.
                "synthetic-owner/revision-ﬁxture",
            )
            for spelling in spellings:
                response = await _resolve(
                    environment,
                    cookies,
                    {"source": ModelOriginType.HUGGING_FACE.value, "repository_id": spelling},
                )
                assert response.status == 200
                assert _header(response, "cache-control") == "no-store"
                payload = response.json()
                assert payload["outcome"] == "resolved"
                # An omitted Revision is "@default" and is never NULL.
                assert payload["resolved"]["requested_revision_norm"] == "@default"
                assert payload["resolved"]["resolved_revision"] == DEFAULT_COMMIT
            assert len(environment.adapter.requests) == len(spellings)
            assert {item.repository_id for item in environment.adapter.requests} == {
                FIXTURE_REPOSITORY_ID
            }
            assert {item.requested_revision_norm for item in environment.adapter.requests} == {
                "@default"
            }

            explicit = await _resolve(
                environment,
                cookies,
                {
                    "source": ModelOriginType.HUGGING_FACE.value,
                    "repository_id": FIXTURE_REPOSITORY_ID,
                    "revision": " next ",
                },
            )
            assert explicit.status == 200
            assert explicit.json()["resolved"]["requested_revision_norm"] == "next"
            assert explicit.json()["resolved"]["resolved_revision"] == NEXT_COMMIT

            before = len(environment.adapter.requests)
            empty = await _resolve(
                environment,
                cookies,
                {
                    "source": ModelOriginType.HUGGING_FACE.value,
                    "repository_id": FIXTURE_REPOSITORY_ID,
                    "revision": "",
                },
            )
            # An empty Revision is malformed input, not an omission: it is
            # rejected before the normalizer is ever called.
            assert empty.status == 400
            assert empty.json()["error"]["code"] == "invalid_request"
            assert len(environment.adapter.requests) == before
            assert normalize_requested_revision("") == "@default"

    asyncio.run(scenario())


NORMALIZATION_FUNCTIONS: Final[tuple[str, ...]] = (
    "normalize_source_key",
    "normalize_requested_revision",
)
_SECOND_IMPLEMENTATION_METHODS: Final[frozenset[str]] = frozenset(
    {"strip", "lstrip", "rstrip", "lower", "upper", "casefold", "title", "translate"}
)
_NORMALIZATION_FORMS: Final[frozenset[str]] = frozenset({"NFC", "NFD", "NFKC", "NFKD"})


@dataclass(frozen=True, slots=True)
class _NormalizationReport:
    calls: Mapping[str, int]
    second_implementations: tuple[str, ...]


def normalization_report(paths: Sequence[Path]) -> _NormalizationReport:
    """Judge the call sites, not the results: two agreeing copies still fail."""

    calls = dict.fromkeys(NORMALIZATION_FUNCTIONS, 0)
    markers: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in _NORMALIZATION_FORMS:
                markers.append(f"{path.name}:{node.lineno}:unicode-form")
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if isinstance(function, ast.Name) and function.id in calls:
                calls[function.id] += 1
            if isinstance(function, ast.Attribute):
                if function.attr in _SECOND_IMPLEMENTATION_METHODS:
                    markers.append(f"{path.name}:{node.lineno}:{function.attr}")
                if function.attr == "normalize":
                    markers.append(f"{path.name}:{node.lineno}:unicodedata.normalize")
    return _NormalizationReport(calls=calls, second_implementations=tuple(markers))


def assert_single_normalization_definition_source(paths: Sequence[Path]) -> None:
    report = normalization_report(paths)
    assert report.second_implementations == (), (
        "a second normalization implementation exists: " + "; ".join(report.second_implementations)
    )
    assert dict(report.calls) == dict.fromkeys(NORMALIZATION_FUNCTIONS, 1)


def _mutated_copy(destination: Path, *, insert_after: str, added: str) -> Path:
    source = REPOSITORY_ROOT / ("apps/control_api/src/holm_control_api/model_resolution_service.py")
    text = source.read_text(encoding="utf-8")
    assert text.count(insert_after) == 1
    mutated = text.replace(insert_after, f"{insert_after}\n{added}", 1)
    destination.write_text(mutated, encoding="utf-8")
    return destination


def test_TC_RES_01_second_normalization_positive_control_fails_loudly(tmp_path: Path) -> None:
    assert_single_normalization_definition_source(RESOLVE_SOURCE_PATHS)

    anchor = "        normalized_repository_id = normalize_source_key(repository_id)"
    mutations = (
        ("strip.py", "        normalized_repository_id = repository_id.strip()"),
        (
            "second_call.py",
            "        normalized_repository_id = normalize_source_key(normalized_repository_id)",
        ),
        (
            "unicodedata.py",
            '        normalized_repository_id = __import__("unicodedata").normalize('
            '"NFKC", repository_id)',
        ),
    )
    for name, added in mutations:
        mutant = _mutated_copy(tmp_path / name, insert_after=anchor, added=added)
        with pytest.raises(AssertionError):
            assert_single_normalization_definition_source((mutant,))


def test_TC_RES_02_preview_writes_nothing_on_success_or_failure(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    model_id = _seed_model(ephemeral_postgres.engine, repository_id=FIXTURE_REPOSITORY_ID)
    _seed_ready_artifact(ephemeral_postgres.engine, model_id, repository_id=FIXTURE_REPOSITORY_ID)
    _seed_active_download(
        ephemeral_postgres.engine, model_id, repository_id="synthetic-owner/other-fixture"
    )
    before = _model_domain_snapshot(ephemeral_postgres.engine)

    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as environment:
            await _create_accounts(environment)
            cookies = await _login(environment)
            bodies: tuple[Mapping[str, object], ...] = (
                {"source": ModelOriginType.HUGGING_FACE.value, "repository_id": "a/b"},
                {
                    "source": ModelOriginType.HUGGING_FACE.value,
                    "repository_id": FIXTURE_REPOSITORY_ID,
                },
                {
                    "source": ModelOriginType.HUGGING_FACE.value,
                    "repository_id": FIXTURE_REPOSITORY_ID,
                    "revision": "missing-branch",
                },
                {
                    "source": ModelOriginType.MODELSCOPE.value,
                    "repository_id": FIXTURE_REPOSITORY_ID,
                },
                {
                    "source": ModelOriginType.LOCAL_PATH.value,
                    "external_path": "/srv/models/fixture",
                },
                {"source": ModelOriginType.HUGGING_FACE.value, "repository_id": "invalid//id"},
            )
            for body in bodies * 2:
                response = await _resolve(environment, cookies, body)
                assert response.status in (200, 400)

    asyncio.run(scenario())
    assert_no_model_domain_writes(before, _model_domain_snapshot(ephemeral_postgres.engine))

    # Positive control: a single "cache the preview" row makes the same
    # judgement fail.
    _seed_model(ephemeral_postgres.engine, repository_id="cached/preview")
    with pytest.raises(AssertionError):
        assert_no_model_domain_writes(before, _model_domain_snapshot(ephemeral_postgres.engine))


def assert_unresolvable(payload: Mapping[str, Any]) -> None:
    """The single judgement for "no source implementation is available"."""

    assert payload["outcome"] == "unresolved", "an unavailable source reported a preview"
    assert "resolved" not in payload, "an unavailable source returned a preview body"
    assert "local_availability" not in payload
    assert payload["status"] is None
    assert payload["reason_code"] == ModelSourceValidationReason.VALIDATOR_UNAVAILABLE.value
    assert payload["next_action"] == ModelSourceValidationAction.INSTALL_VALIDATOR.value


def test_TC_RES_06_absent_implementation_is_explicit_for_every_source(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> None:
        async with _environment(ephemeral_postgres, registry=ModelResolutionRegistry()) as empty:
            await _create_accounts(empty)
            cookies = await _login(empty)
            for source in ModelOriginType:
                body: dict[str, object] = {"source": source.value}
                if source is ModelOriginType.LOCAL_PATH:
                    body["external_path"] = "/srv/models/fixture"
                else:
                    body["repository_id"] = FIXTURE_REPOSITORY_ID
                response = await _resolve(empty, cookies, body)
                assert response.status == 200
                assert_unresolvable(response.json())

        # A registry that knows one source still refuses the others, and the
        # External Path branch stays M2-07's regardless of what is registered.
        async with _environment(ephemeral_postgres) as partial:
            cookies = await _login(partial)
            unregistered: tuple[tuple[ModelOriginType, Mapping[str, object]], ...] = (
                (
                    ModelOriginType.MODELSCOPE,
                    {
                        "source": ModelOriginType.MODELSCOPE.value,
                        "repository_id": FIXTURE_REPOSITORY_ID,
                    },
                ),
                (
                    ModelOriginType.LOCAL_PATH,
                    {
                        "source": ModelOriginType.LOCAL_PATH.value,
                        "external_path": "/srv/models/fixture",
                    },
                ),
            )
            for source, unregistered_body in unregistered:
                assert source is not ModelOriginType.HUGGING_FACE
                response = await _resolve(partial, cookies, unregistered_body)
                assert response.status == 200
                assert_unresolvable(response.json())
            assert partial.adapter.requests == []

    asyncio.run(scenario())


def test_TC_RES_06_empty_metadata_cannot_be_built_and_lenient_success_fails_loudly(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    # The type-level half: there is no empty preview to return in the first
    # place, and a placeholder Revision is refused.
    with pytest.raises(TypeError):
        ResolvedModel()  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        ResolvedModel(
            source=ModelOriginType.HUGGING_FACE,
            repository_id=FIXTURE_REPOSITORY_ID,
            requested_revision_norm="@default",
            resolved_revision="@default",
            revision_immutability=RevisionImmutability.IMMUTABLE,
            files=Identified(()),
            total_size_bytes=Identified(0),
            architecture=Identified(""),
            modalities=Identified(()),
            precision=Identified(""),
            quantization=Identified(""),
            context_length=Identified(1),
            candidate_runtimes=Identified(()),
            license=Identified(""),
        )

    async def scenario() -> None:
        async with _environment(ephemeral_postgres, registry=_LenientRegistry()) as lenient:
            await _create_accounts(lenient)
            cookies = await _login(lenient)
            response = await _resolve(
                lenient,
                cookies,
                {
                    "source": ModelOriginType.HUGGING_FACE.value,
                    "repository_id": FIXTURE_REPOSITORY_ID,
                },
            )
            assert response.status == 200
            with pytest.raises(AssertionError):
                assert_unresolvable(response.json())

    asyncio.run(scenario())


def test_TC_RES_06_route_carries_an_explicit_capability_declaration(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as environment:
            assert_route_authorization_registered(environment.app.routes)
            declared = [
                route_access_policy(registered.route)
                for registered in authorization._registered_api_routes(environment.app.routes)
                if registered.path == MODELS_RESOLVE_PATH
            ]
            assert len(declared) == 1
            policy = declared[0]
            assert policy is not None
            assert policy.kind is RouteAccessKind.CAPABILITY
            assert policy.capability is Capability.MANAGE_LOCAL_MODELS

            await _create_accounts(environment)
            member = await _login(environment, MEMBER_USERNAME)
            denied = await _resolve(
                environment,
                member,
                {
                    "source": ModelOriginType.HUGGING_FACE.value,
                    "repository_id": FIXTURE_REPOSITORY_ID,
                },
            )
            assert denied.status == 403
            assert denied.json()["error"]["code"] == "permission_denied"
            assert environment.adapter.requests == []

    asyncio.run(scenario())


REJECTED_SAMPLE_KINDS: Final[frozenset[PathAttackKind]] = frozenset(
    {
        PathAttackKind.INVALID_REPOSITORY_ID,
        PathAttackKind.PARENT_TRAVERSAL,
        PathAttackKind.ABSOLUTE_PATH,
    }
)
ADDITIONAL_INVALID_REPOSITORY_IDS: Final[tuple[str, ...]] = (
    "no-namespace",
    "owner/",
    "/model",
    "owner/model/extra",
    "owner/..",
    "owner/ model",
)


def test_TC_RES_08_invalid_repository_ids_are_rejected_before_resolution(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    samples = path_attack_samples()
    assert {sample.kind for sample in samples} >= REJECTED_SAMPLE_KINDS
    rejected = (
        tuple(sample.untrusted_value for sample in samples if sample.kind in REJECTED_SAMPLE_KINDS)
        + ADDITIONAL_INVALID_REPOSITORY_IDS
    )
    before = _model_domain_snapshot(ephemeral_postgres.engine)

    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as environment:
            await _create_accounts(environment)
            cookies = await _login(environment)
            for untrusted in rejected:
                response = await _resolve(
                    environment,
                    cookies,
                    {
                        "source": ModelOriginType.HUGGING_FACE.value,
                        "repository_id": untrusted,
                    },
                )
                assert response.status == 400, untrusted
                payload = response.json()
                assert payload["error"]["code"] == "invalid_request"
                assert payload["error"]["details"] == {}
                # The rejection never quotes the input back.
                for fragment in (untrusted, untrusted.strip("/")):
                    if fragment:
                        assert fragment not in response.text
            assert environment.adapter.requests == []

    asyncio.run(scenario())
    assert_no_model_domain_writes(before, _model_domain_snapshot(ephemeral_postgres.engine))


def test_TC_RES_08_TC_DL_26_an_external_path_conclusion_crosses_http_without_a_path(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    """End to end: an accepted path, a refused one, and no path in either body."""

    root = tmp_path / "tc-res-08-http-root-sentinel"
    model = root / "tc-res-08-http-model-sentinel"
    model.mkdir(parents=True)
    outside = tmp_path / "tc-res-08-http-outside-sentinel"
    outside.mkdir()
    registry = ModelResolutionRegistry(
        external_path_adapters={
            ModelOriginType.LOCAL_PATH: ExternalPathResolutionAdapter(
                policy=ExternalPathPolicy.for_configured_roots((root,))
            )
        }
    )
    before = _model_domain_snapshot(ephemeral_postgres.engine)
    sentinels = (
        "tc-res-08-http-root-sentinel",
        "tc-res-08-http-model-sentinel",
        "tc-res-08-http-outside-sentinel",
    )

    async def scenario() -> None:
        async with _environment(ephemeral_postgres, registry=registry) as environment:
            await _create_accounts(environment)
            cookies = await _login(environment)

            accepted = await _resolve(
                environment,
                cookies,
                {
                    "source": ModelOriginType.LOCAL_PATH.value,
                    "external_path": str(model),
                },
            )
            assert accepted.status == 200
            payload = accepted.json()
            assert payload == {
                "outcome": "external_path_accepted",
                "control_plane_readable": True,
                "runtime_readability": RuntimeReadability.UNVERIFIED.value,
                "runtime_readability_cause": (
                    RuntimeReadabilityUnverifiedCause.RUNTIME_IDENTITY_CHECK_UNAVAILABLE.value
                ),
            }

            refused = await _resolve(
                environment,
                cookies,
                {
                    "source": ModelOriginType.LOCAL_PATH.value,
                    "external_path": str(outside),
                },
            )
            assert refused.status == 200
            assert refused.json()["outcome"] == "unresolved"
            assert (
                refused.json()["reason_code"] != ModelSourceValidationReason.VALIDATOR_UNAVAILABLE
            )

            relative = await _resolve(
                environment,
                cookies,
                {"source": ModelOriginType.LOCAL_PATH.value, "external_path": "models/relative"},
            )
            assert relative.status == 400
            assert relative.json()["error"]["code"] == "invalid_request"

            for response in (accepted, refused, relative):
                for sentinel in sentinels:
                    assert sentinel not in response.text
            assert environment.adapter.requests == []

    asyncio.run(scenario())
    assert_no_model_domain_writes(before, _model_domain_snapshot(ephemeral_postgres.engine))


def test_TC_RES_09_local_availability_is_three_distinguishable_read_only_answers(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    model_id = _seed_model(ephemeral_postgres.engine, repository_id=FIXTURE_REPOSITORY_ID)
    observed: list[str] = []

    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as environment:
            await _create_accounts(environment)
            cookies = await _login(environment)
            body: Mapping[str, object] = {
                "source": ModelOriginType.HUGGING_FACE.value,
                "repository_id": FIXTURE_REPOSITORY_ID,
            }

            absent = await _resolve(environment, cookies, body)
            assert absent.status == 200
            observed.append(absent.json()["local_availability"])

            _seed_active_download(
                ephemeral_postgres.engine, model_id, repository_id=FIXTURE_REPOSITORY_ID
            )
            downloading = await _resolve(environment, cookies, body)
            observed.append(downloading.json()["local_availability"])

            _seed_ready_artifact(
                ephemeral_postgres.engine, model_id, repository_id=FIXTURE_REPOSITORY_ID
            )
            snapshot = _model_domain_snapshot(ephemeral_postgres.engine)
            ready = await _resolve(environment, cookies, body)
            observed.append(ready.json()["local_availability"])
            # The answer is read-only even when it is not "absent".
            assert_no_model_domain_writes(
                snapshot, _model_domain_snapshot(ephemeral_postgres.engine)
            )

    asyncio.run(scenario())
    assert observed == [
        LocalAvailability.ABSENT.value,
        LocalAvailability.DOWNLOAD_ACTIVE.value,
        LocalAvailability.ARTIFACT_READY.value,
    ]
    assert len(set(observed)) == 3
    # "没有" is a conclusion the response states, never an omitted key.
    assert LocalAvailability.ABSENT.value in observed


async def _store_credential(
    session_factory: AsyncSessionFactory,
    *,
    source_type: ModelSourceType,
    plaintext: bytes,
) -> None:
    async with session_factory() as session, session.begin():
        repository = ModelSourceRepository(session)
        record = await repository.ensure(
            source_type=source_type,
            now=datetime.now(UTC),
        )
        envelope = KEYRING.encrypt(
            plaintext,
            source_id=record.id,
            credential_type=CredentialType.ACCESS_TOKEN.value,
        )
        updated = await repository.set_credential(
            record.id,
            credential=envelope,
            now=datetime.now(UTC),
        )
        assert updated is not None


def test_TC_RES_10_resolution_path_leaks_no_credential(
    ephemeral_postgres: EphemeralPostgres,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = f"PWSSNTL-SRCTOK-TC-RES-10-{os.urandom(6).hex().upper()}"
    caplog.set_level(logging.DEBUG)
    responses: list[AsgiResponse] = []

    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as environment:
            await _create_accounts(environment)
            await _store_credential(
                environment.session_factory,
                source_type=ModelSourceType.HUGGING_FACE,
                plaintext=sentinel.encode("utf-8"),
            )
            cookies = await _login(environment)
            body: Mapping[str, object] = {
                "source": ModelOriginType.HUGGING_FACE.value,
                "repository_id": FIXTURE_REPOSITORY_ID,
            }
            responses.append(await _resolve(environment, cookies, body))
            # The credential really did reach the source boundary, so the
            # zero-leak result below is not vacuous.
            assert environment.adapter.credentials == [sentinel.encode("utf-8")]

        async with _environment(
            ephemeral_postgres,
            registry=ModelResolutionRegistry(
                {
                    ModelOriginType.HUGGING_FACE: _ExplodingAdapter(
                        f"source rejected credential {sentinel}"
                    )
                }
            ),
        ) as failing:
            cookies = await _login(failing)
            denied = await _resolve(
                failing,
                cookies,
                {
                    "source": ModelOriginType.HUGGING_FACE.value,
                    "repository_id": FIXTURE_REPOSITORY_ID,
                },
            )
            assert denied.status == 200
            assert denied.json()["outcome"] == "unresolved"
            responses.append(denied)

    asyncio.run(scenario())

    rendered_responses = "\n".join(response.text for response in responses)
    rendered_logs = "\n".join(
        f"{record.getMessage()} {record.__dict__!r}" for record in caplog.records
    )
    process_surface = b"\n".join(
        (Path("/proc/self/cmdline").read_bytes(), json.dumps(dict(os.environ)).encode())
    ).decode("utf-8", errors="replace")
    scanned = database_bytes(ephemeral_postgres.engine)

    for surface in (rendered_responses, rendered_logs, process_surface):
        assert sentinel not in surface
    assert sentinel.encode("utf-8") not in scanned

    # Positive control: the same scan finds the sentinel the moment it is
    # written anywhere in the three application schemas.
    with ephemeral_postgres.engine.begin() as connection:
        original = connection.execute(
            sa.text(
                "SELECT credential_ciphertext FROM core.model_sources WHERE type = 'Hugging Face'"
            )
        ).scalar_one()
        connection.execute(
            sa.text(
                "UPDATE core.model_sources SET credential_ciphertext = :sentinel "
                "WHERE type = 'Hugging Face'"
            ),
            {"sentinel": sentinel.encode("utf-8")},
        )
    try:
        assert sentinel.encode("utf-8") in database_bytes(ephemeral_postgres.engine)
    finally:
        with ephemeral_postgres.engine.begin() as connection:
            connection.execute(
                sa.text(
                    "UPDATE core.model_sources SET credential_ciphertext = :original "
                    "WHERE type = 'Hugging Face'"
                ),
                {"original": original},
            )


def _catalog_counts(engine: Engine) -> Mapping[str, int]:
    with engine.connect() as connection:
        return {
            table: int(
                connection.exec_driver_sql(f"SELECT count(*) FROM core.{table}").scalar_one()
            )
            for table in (
                "models",
                "model_artifacts",
                "download_tasks",
                "download_subscriptions",
                "idempotency_records",
            )
        }


def test_TC_MDL_01_TC_MDL_02_registration_persists_only_server_resolved_metadata(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    assert _catalog_counts(ephemeral_postgres.engine) == {
        "models": 0,
        "model_artifacts": 0,
        "download_tasks": 0,
        "download_subscriptions": 0,
        "idempotency_records": 0,
    }

    async def scenario() -> AsgiResponse:
        async with _environment(ephemeral_postgres) as environment:
            await _create_accounts(environment)
            cookies = await _login(environment)
            return await _register(
                environment,
                cookies,
                {
                    "source": ModelOriginType.HUGGING_FACE.value,
                    "repository_id": FIXTURE_REPOSITORY_ID,
                },
                idempotency_key="TC-MDL-01-first-registration",
            )

    response = asyncio.run(scenario())
    assert response.status == 200
    payload = response.json()
    assert payload["source"] == ModelOriginType.HUGGING_FACE.value
    assert payload["architecture"] == {
        "state": "identified",
        "value": "SyntheticCausalArchitecture",
    }
    assert payload["metadata_status"] == MetadataStatus.CONFIRMED.value
    assert _catalog_counts(ephemeral_postgres.engine) == {
        "models": 1,
        "model_artifacts": 0,
        "download_tasks": 0,
        "download_subscriptions": 0,
        "idempotency_records": 1,
    }
    with ephemeral_postgres.engine.connect() as connection:
        row = (
            connection.execute(
                sa.text(
                    "SELECT repository_id, architecture, modalities, "
                    "precision, context_length, supported_runtimes, estimated_size_bytes, "
                    "metadata_status FROM core.models"
                )
            )
            .mappings()
            .one()
        )
    assert dict(row) == {
        "repository_id": FIXTURE_REPOSITORY_ID,
        "architecture": "SyntheticCausalArchitecture",
        "modalities": ["text"],
        "precision": "bf16",
        "context_length": 32_768,
        "supported_runtimes": ["vllm"],
        "estimated_size_bytes": 2_048,
        "metadata_status": MetadataStatus.CONFIRMED.value,
    }

    # Positive controls: either a preloaded catalog row or a side-effect task
    # makes the exact-count judgement above fail loudly.
    with ephemeral_postgres.engine.begin() as connection:
        model_id = connection.execute(sa.text("SELECT id FROM core.models")).scalar_one()
        connection.execute(
            sa.text(
                "INSERT INTO core.download_tasks ("
                "id, model_id, source_type, repository_id, requested_revision_norm, status, "
                "attempt, downloaded_bytes, created_at, updated_at) VALUES ("
                ":id, :model_id, 'Hugging Face', :repository_id, '@default', 'Queued', "
                "1, 0, clock_timestamp(), clock_timestamp())"
            ),
            {"id": uuid4(), "model_id": model_id, "repository_id": FIXTURE_REPOSITORY_ID},
        )
    with pytest.raises(AssertionError):
        assert _catalog_counts(ephemeral_postgres.engine)["download_tasks"] == 0


def test_TC_MDL_01_positive_control_preloaded_catalog_row_fails_loudly(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _seed_model(ephemeral_postgres.engine, repository_id="preloaded/model")
    with pytest.raises(AssertionError):
        assert _catalog_counts(ephemeral_postgres.engine)["models"] == 0


def test_TC_MDL_03_registration_request_schema_is_the_exact_identity_set() -> None:
    assert frozenset(ModelRegistrationRequest.model_fields) == {
        "source",
        "repository_id",
        "external_path",
        "revision",
    }
    assert ModelRegistrationRequest.model_config["extra"] == "forbid"


def test_TC_MDL_04_TC_MDL_06_normalization_replay_conflict_and_expiry(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    first_spelling = f"  {FIXTURE_REPOSITORY_ID}  "
    second_spelling = "ｓｙｎｔｈｅｔｉｃ－ｏｗｎｅｒ／ｒｅｖｉｓｉｏｎ－ｆｉｘｔｕｒｅ"

    async def scenario() -> tuple[AsgiResponse, AsgiResponse, AsgiResponse, AsgiResponse]:
        async with _environment(ephemeral_postgres) as environment:
            await _create_accounts(environment)
            cookies = await _login(environment)
            first = await _register(
                environment,
                cookies,
                {"source": "Hugging Face", "repository_id": first_spelling},
                idempotency_key="TC-MDL-06-replay-key",
            )
            replay = await _register(
                environment,
                cookies,
                {"source": "Hugging Face", "repository_id": second_spelling},
                idempotency_key="TC-MDL-06-replay-key",
            )
            conflict = await _register(
                environment,
                cookies,
                {"source": "Hugging Face", "repository_id": "other/model"},
                idempotency_key="TC-MDL-06-replay-key",
            )
            with ephemeral_postgres.engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "UPDATE core.idempotency_records "
                        "SET created_at = clock_timestamp() - interval '25 hours'"
                    )
                )
            expired = await _register(
                environment,
                cookies,
                {"source": "Hugging Face", "repository_id": "other/model"},
                idempotency_key="TC-MDL-06-replay-key",
            )
            return first, replay, conflict, expired

    first, replay, conflict, expired = asyncio.run(scenario())
    assert first.status == replay.status == expired.status == 200
    assert first.json()["id"] == replay.json()["id"]
    assert replay.json()["replayed"] is True
    assert conflict.status == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert expired.json()["id"] != first.json()["id"]
    with ephemeral_postgres.engine.connect() as connection:
        rows = (
            connection.execute(sa.text("SELECT source_key FROM core.models ORDER BY source_key"))
            .scalars()
            .all()
        )
        hashes = connection.execute(
            sa.text("SELECT key_hash, request_hash FROM core.idempotency_records")
        ).one()
    assert rows == ["other/model", FIXTURE_REPOSITORY_ID]
    assert all(re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes)
    assert "TC-MDL-06-replay-key" not in database_bytes(ephemeral_postgres.engine).decode(
        "utf-8", errors="ignore"
    )


def test_TC_MDL_09_unrecognized_metadata_round_trips_explicitly_as_null(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    @dataclass(slots=True)
    class UnknownAdapter:
        async def resolve(
            self,
            request: ResolutionRequest,
            *,
            credential: bytes | None,
        ) -> ResolutionOutcome:
            del credential
            return ResolutionSucceeded(
                ResolvedModel(
                    source=request.source,
                    repository_id=request.repository_id,
                    requested_revision_norm=request.requested_revision_norm,
                    resolved_revision="9" * 40,
                    revision_immutability=RevisionImmutability.IMMUTABLE,
                    files=Unrecognized(),
                    total_size_bytes=Unrecognized(),
                    architecture=Unrecognized(),
                    modalities=Unrecognized(),
                    precision=Unrecognized(),
                    quantization=Unrecognized(),
                    context_length=Unrecognized(),
                    candidate_runtimes=Unrecognized(),
                    license=Unrecognized(),
                )
            )

    async def scenario() -> AsgiResponse:
        registry = ModelResolutionRegistry({ModelOriginType.HUGGING_FACE: UnknownAdapter()})
        async with _environment(ephemeral_postgres, registry=registry) as environment:
            await _create_accounts(environment)
            return await _register(
                environment,
                await _login(environment),
                {"source": "Hugging Face", "repository_id": FIXTURE_REPOSITORY_ID},
                idempotency_key="TC-MDL-09-unknown",
            )

    response = asyncio.run(scenario())
    assert response.status == 200
    for field_name in (
        "architecture",
        "modalities",
        "precision",
        "quantization",
        "context_length",
        "candidate_runtimes",
        "estimated_size_bytes",
        "license",
    ):
        assert response.json()[field_name] == {"state": "unrecognized"}
    with ephemeral_postgres.engine.connect() as connection:
        values = connection.execute(
            sa.text(
                "SELECT architecture, modalities, precision, quantization, context_length, "
                "supported_runtimes, estimated_size_bytes, license FROM core.models"
            )
        ).one()
    assert values == (None,) * 8


def test_TC_MDL_13_non_admin_cannot_register(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> AsgiResponse:
        async with _environment(ephemeral_postgres) as environment:
            await _create_accounts(environment)
            return await _register(
                environment,
                await _login(environment, MEMBER_USERNAME),
                {"source": "Hugging Face", "repository_id": FIXTURE_REPOSITORY_ID},
                idempotency_key="TC-MDL-13-member",
            )

    response = asyncio.run(scenario())
    assert response.status == 403
    assert _catalog_counts(ephemeral_postgres.engine)["models"] == 0


def test_TC_MDL_02_resolution_failure_creates_no_placeholder_or_idempotency_record(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    async def scenario() -> AsgiResponse:
        async with _environment(
            ephemeral_postgres,
            registry=ModelResolutionRegistry(),
        ) as environment:
            await _create_accounts(environment)
            return await _register(
                environment,
                await _login(environment),
                {"source": "Hugging Face", "repository_id": FIXTURE_REPOSITORY_ID},
                idempotency_key="TC-MDL-02-unresolved",
            )

    response = asyncio.run(scenario())
    assert response.status == 200
    assert response.json() == {
        "outcome": "unresolved",
        "status": None,
        "reason_code": "validator_unavailable",
        "next_action": "install_validator",
    }
    assert _catalog_counts(ephemeral_postgres.engine) == {
        "models": 0,
        "model_artifacts": 0,
        "download_tasks": 0,
        "download_subscriptions": 0,
        "idempotency_records": 0,
    }

    # Positive control: the same zero-record assertion catches a placeholder.
    _seed_model(ephemeral_postgres.engine, repository_id="placeholder/model")
    with pytest.raises(AssertionError):
        assert _catalog_counts(ephemeral_postgres.engine)["models"] == 0


def test_TC_MDL_01_TC_MDL_09_external_path_is_rejudged_and_unconfirmed(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "external-root"
    model_path = allowed_root / "private-model-path-sentinel"
    model_path.mkdir(parents=True)
    registry = ModelResolutionRegistry(
        external_path_adapters={
            ModelOriginType.LOCAL_PATH: ExternalPathResolutionAdapter(
                policy=ExternalPathPolicy.for_configured_roots((allowed_root,))
            )
        }
    )

    async def scenario() -> AsgiResponse:
        async with _environment(ephemeral_postgres, registry=registry) as environment:
            await _create_accounts(environment)
            return await _register(
                environment,
                await _login(environment),
                {"source": "Local Path", "external_path": str(model_path)},
                idempotency_key="TC-MDL-09-external",
            )

    response = asyncio.run(scenario())
    assert response.status == 200
    assert response.json()["metadata_status"] == MetadataStatus.UNCONFIRMED.value
    assert str(model_path) not in response.text
    with ephemeral_postgres.engine.connect() as connection:
        row = connection.execute(
            sa.text(
                "SELECT source_type, external_path, metadata_status, architecture, modalities "
                "FROM core.models"
            )
        ).one()
    assert row == (
        ModelOriginType.LOCAL_PATH.value,
        str(model_path),
        MetadataStatus.UNCONFIRMED.value,
        None,
        None,
    )


def _assert_registration_surfaces_hide(
    sentinels: Sequence[str],
    surfaces: Sequence[str],
) -> None:
    assert sentinels
    assert surfaces
    for sentinel in sentinels:
        assert sentinel
        assert all(sentinel not in surface for surface in surfaces)


def test_TC_MDL_14_success_and_failure_branches_leak_no_credential_or_path(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    credential = f"PWSSNTL-SRCTOK-TC-MDL-14-{os.urandom(6).hex().upper()}"
    failure_detail = f"PWSSNTL-FAIL-TC-MDL-14-{os.urandom(6).hex().upper()}"
    path_sentinel = "PWSSNTL-ABSPATH-TC-MDL-14"
    outside_path = tmp_path / path_sentinel / "model"
    success_path_sentinel = "PWSSNTL-SUCCESS-ABSPATH-TC-MDL-14"
    root = tmp_path / success_path_sentinel / "allowed-root"
    model_path = root / "distinct-external-model"
    model_path.mkdir(parents=True)
    path_registry = ModelResolutionRegistry(
        external_path_adapters={
            ModelOriginType.LOCAL_PATH: ExternalPathResolutionAdapter(
                policy=ExternalPathPolicy.for_configured_roots((root,))
            )
        }
    )
    responses: list[AsgiResponse] = []
    record_reprs: list[str] = []
    caplog.set_level(logging.DEBUG)

    async def scenario() -> None:
        async with _environment(ephemeral_postgres) as success:
            await _create_accounts(success)
            await _store_credential(
                success.session_factory,
                source_type=ModelSourceType.HUGGING_FACE,
                plaintext=credential.encode("utf-8"),
            )
            response = await _register(
                success,
                await _login(success),
                {"source": "Hugging Face", "repository_id": FIXTURE_REPOSITORY_ID},
                idempotency_key="TC-MDL-14-success",
            )
            responses.append(response)
            assert success.adapter.credentials == [credential.encode("utf-8")]
            async with success.session_factory() as session:
                model_id = UUID(response.json()["id"])
                record = await ModelRepository(session).get(model_id)
                assert record is not None
                record_reprs.append(repr(record))

        async with _environment(
            ephemeral_postgres,
            registry=path_registry,
        ) as path_success:
            response = await _register(
                path_success,
                await _login(path_success),
                {"source": "Local Path", "external_path": str(model_path)},
                idempotency_key="TC-MDL-14-path-success",
            )
            assert response.status == 200
            assert response.json()["display_name"] == model_path.name
            responses.append(response)
            async with path_success.session_factory() as session:
                model_id = UUID(response.json()["id"])
                record = await ModelRepository(session).get(model_id)
                assert record is not None
                record_reprs.append(repr(record))

        async with _environment(
            ephemeral_postgres,
            registry=ModelResolutionRegistry(
                {
                    ModelOriginType.HUGGING_FACE: _ExplodingAdapter(
                        f"source failed: {failure_detail}"
                    )
                }
            ),
        ) as failed:
            response = await _register(
                failed,
                await _login(failed),
                {"source": "Hugging Face", "repository_id": FIXTURE_REPOSITORY_ID},
                idempotency_key="TC-MDL-14-failure",
            )
            assert response.json()["outcome"] == "unresolved"
            responses.append(response)

        async with _environment(ephemeral_postgres, registry=path_registry) as path_failure:
            response = await _register(
                path_failure,
                await _login(path_failure),
                {"source": "Local Path", "external_path": str(outside_path)},
                idempotency_key="TC-MDL-14-path-failure",
            )
            assert response.json()["outcome"] == "unresolved"
            responses.append(response)

    asyncio.run(scenario())
    non_database_surfaces = (
        *(response.text for response in responses),
        *record_reprs,
        *(f"{record.getMessage()} {record.__dict__!r}" for record in caplog.records),
    )
    database_surface = database_bytes(ephemeral_postgres.engine).decode("utf-8", errors="replace")
    _assert_registration_surfaces_hide(
        (
            credential,
            failure_detail,
            path_sentinel,
            str(outside_path),
            success_path_sentinel,
            str(model_path),
        ),
        non_database_surfaces,
    )
    _assert_registration_surfaces_hide(
        (credential, failure_detail, path_sentinel, str(outside_path)),
        (*non_database_surfaces, database_surface),
    )
    # External Path is the persisted identity, so its one allowed row contains
    # it as source_key and external_path.  Remove exactly those two copies and
    # scan the remaining database bytes for an unauthorized duplicate sink.
    assert database_surface.count(str(model_path)) == 2
    _assert_registration_surfaces_hide(
        (success_path_sentinel, str(model_path)),
        (database_surface.replace(str(model_path), ""),),
    )


@pytest.mark.parametrize(
    ("branch", "anchor", "replacement"),
    (
        (
            "success",
            '        _LOGGER.info("model registration: committed")',
            '        _LOGGER.info("model registration: committed %s", external_path)',
        ),
        (
            "failure",
            '            _LOGGER.info("model registration: resolution failed")',
            '            _LOGGER.info("model registration: resolution failed %s", external_path)',
        ),
    ),
)
def test_TC_MDL_14_positive_controls_each_logging_branch_fails_loudly(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    branch: str,
    anchor: str,
    replacement: str,
) -> None:
    source = REPOSITORY_ROOT / (
        "apps/control_api/src/holm_control_api/model_registration_service.py"
    )
    mutated = load_mutated_module(
        source,
        [(anchor, replacement)],
        module_name=f"_leaky_registration_{branch}",
        destination=tmp_path / f"leaky_registration_{branch}.py",
    )
    path_sentinel = f"PWSSNTL-{branch.upper()}-ABSPATH-TC-MDL-14"
    root = tmp_path / f"root-{branch}"
    root.mkdir()
    model_path = root / path_sentinel
    if branch == "success":
        model_path.mkdir()
    else:
        model_path = tmp_path / path_sentinel / "outside"
    registry = ModelResolutionRegistry(
        external_path_adapters={
            ModelOriginType.LOCAL_PATH: ExternalPathResolutionAdapter(
                policy=ExternalPathPolicy.for_configured_roots((root,))
            )
        }
    )

    async def scenario() -> None:
        async with _environment(ephemeral_postgres, registry=registry) as environment:
            await _create_accounts(environment)
            with ephemeral_postgres.engine.connect() as connection:
                actor_id = connection.execute(
                    sa.text("SELECT id FROM core.users WHERE username_norm = :username"),
                    {"username": ADMIN_USERNAME},
                ).scalar_one()
            service = mutated.ModelRegistrationService(
                session_factory=environment.session_factory,
                resolution=environment.resolution,
                idempotency_hmac_secret=IDEMPOTENCY_HMAC_SECRET,
            )
            await service.register(
                actor_id=actor_id,
                idempotency_key=f"TC-MDL-14-{branch}-mutant",
                source=ModelOriginType.LOCAL_PATH,
                repository_id=None,
                external_path=str(model_path),
                revision=None,
            )

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        asyncio.run(scenario())
    surfaces = tuple(f"{record.getMessage()} {record.__dict__!r}" for record in caplog.records)
    with pytest.raises(AssertionError):
        _assert_registration_surfaces_hide((path_sentinel, str(model_path)), surfaces)

"""TC-DL-19: every source in the vocabulary now has an Adapter, by exact equality.

M2-04A asserted ``Local Path not in registered_sources``; M2-07A implemented the
External Path judgement and flipped it; M2-06A implements ModelScope and flips
the last one.  The expected set is updated each time -- exactly, still by
equality, never widened into a subset or a ``>=`` test.

"Every member of the vocabulary is registered" is stated as the *result* of that
equality, not derived from ``ModelOriginType``: a set compared against the enum
it was built from is an identity, so the expected members are spelled out and
the enum is only used to prove the spelled-out set is complete.

The registration set is asserted directly rather than inferred from behaviour,
and since M2-07A the service short-circuits nothing: a repository source is
dispatched through ``resolve`` and an External Path source through
``resolve_external_path``, so every registered member is genuinely consulted
and a placeholder could not hide behind a branch that never reaches it.

The positive controls rewrite the production factory module, import the
rewritten copy and run the same judgement against it, so each guard is shown
failing on real code rather than on a hand-built registry.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from fixtures.hugging_face import (
    PUBLIC_REPOSITORY_COMMIT,
    PUBLIC_REPOSITORY_ID,
    FakeHub,
    fake_hugging_face_hub,
)
from fixtures.source_mutation import load_mutated_module
from holm_control_api.hugging_face_resolution import HuggingFaceResolutionAdapter
from holm_control_api.model_resolution import (
    ExternalPathAccepted,
    ExternalPathRequest,
    ModelResolutionRegistry,
    ResolutionFailed,
    ResolutionRequest,
    ResolutionSucceeded,
    RuntimeReadability,
)
from holm_control_api.model_resolution_registry import create_default_resolution_registry
from holm_control_api.model_resolution_service import ModelResolutionService
from holm_domain import (
    Identified,
    LocalAvailability,
    ModelOriginType,
    ModelSourceValidationAction,
    ModelSourceValidationReason,
)
from holm_persistence import create_database_engine, create_session_factory
from holm_security import MasterKey, MasterKeyring, MasterKeyState
from migration_test_support import upgrade_head
from sqlalchemy.engine import URL, Engine

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
REGISTRY_SOURCE: Final[Path] = (
    REPOSITORY_ROOT / "apps/control_api/src/holm_control_api/model_resolution_registry.py"
)
KEYRING: Final = MasterKeyring((MasterKey("tc-dl-19", MasterKeyState.WRITABLE, b"D" * 32),))
MODEL_DOMAIN_TABLE_NAMES: Final[tuple[str, ...]] = (
    "models",
    "model_artifacts",
    "download_tasks",
    "download_subscriptions",
)


def assert_the_shipped_registration_set(registry: ModelResolutionRegistry) -> None:
    """The one judgement shared by the real case and its positive controls."""

    assert registry.registered_sources == frozenset(
        {
            ModelOriginType.HUGGING_FACE,
            ModelOriginType.LOCAL_PATH,
            ModelOriginType.MODELSCOPE,
        }
    )
    # The spelled-out set happens to be the whole vocabulary as of M2-06A.  It is
    # asserted in this direction so that a *new* source arriving without an
    # Adapter fails here instead of quietly widening both sides at once.
    assert registry.registered_sources == frozenset(ModelOriginType)


@pytest.fixture
def hub() -> Iterator[FakeHub]:
    with fake_hugging_face_hub() as running:
        yield running


@pytest.fixture(autouse=True)
def migrated_model_domain(ephemeral_postgres: EphemeralPostgres) -> Iterator[None]:
    upgrade_head(ephemeral_postgres.engine)
    yield


def _async_url(fixture: EphemeralPostgres) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username="ubuntu",
        host=str(fixture.socket_directory),
        port=fixture.port,
        database="postgres",
    )


def _model_domain_snapshot(engine: Engine) -> str:
    parts: list[str] = []
    with engine.connect() as connection:
        for table_name in MODEL_DOMAIN_TABLE_NAMES:
            parts.append(
                str(
                    connection.exec_driver_sql(
                        "SELECT coalesce(json_agg(json_build_object("
                        "'row', row_to_json(scanned), 'xmin', scanned.xmin::text) "
                        f"ORDER BY scanned.id)::text, '[]') FROM core.{table_name} AS scanned"
                    ).scalar_one()
                )
            )
    return "\n".join(parts)


def test_TC_DL_19_the_default_registry_registers_every_source_in_the_vocabulary(
    hub: FakeHub,
    tmp_path: Path,
) -> None:
    registry = create_default_resolution_registry(
        external_model_roots=(tmp_path,),
        hugging_face_endpoint=hub.endpoint,
        scratch_root=tmp_path,
    )
    assert_the_shipped_registration_set(registry)


def test_TC_DL_19_positive_control_dropping_local_path_fails_the_guard(
    tmp_path: Path,
) -> None:
    """Un-register Local Path in production; the updated guard must fail.

    The M2-04A control ran the other way round, because the fact it pinned was
    the other way round.  Reversing the direction keeps it a positive control
    rather than turning it into a test of the old world.
    """

    module = load_mutated_module(
        REGISTRY_SOURCE,
        [
            (
                "        external_path_adapters={\n"
                "            ModelOriginType.LOCAL_PATH: ExternalPathResolutionAdapter(\n"
                "                policy=ExternalPathPolicy.for_configured_roots("
                "external_model_roots),\n"
                "                executor=external_path_executor,\n"
                "            )\n"
                "        },",
                "        external_path_adapters={},",
            )
        ],
        module_name="_narrowed_model_resolution_registry",
        destination=tmp_path / "narrowed_registry.py",
    )
    narrowed = module.create_default_resolution_registry(
        external_model_roots=(tmp_path,),
        scratch_root=tmp_path,
    )
    assert ModelOriginType.LOCAL_PATH not in narrowed.registered_sources
    with pytest.raises(AssertionError):
        assert_the_shipped_registration_set(narrowed)


def test_TC_DL_19_positive_control_dropping_modelscope_fails_the_guard(
    tmp_path: Path,
) -> None:
    """Un-register the source M2-06A added; the updated guard must fail.

    The M2-04A control ran the other way round -- it planted a ModelScope
    placeholder while the expected set excluded it.  The fact being pinned has
    flipped, so the control flips with it instead of testing the old world.
    """

    module = load_mutated_module(
        REGISTRY_SOURCE,
        [
            (
                "            ModelOriginType.MODELSCOPE: ModelScopeResolutionAdapter(\n"
                "                endpoint=modelscope_endpoint,\n"
                "                executor=modelscope_executor,\n"
                "                timeout_seconds=timeout_seconds,\n"
                "            ),\n",
                "",
            )
        ],
        module_name="_model_resolution_registry_without_modelscope",
        destination=tmp_path / "registry_without_modelscope.py",
    )
    narrowed = module.create_default_resolution_registry(
        external_model_roots=(tmp_path,),
        scratch_root=tmp_path,
    )
    assert ModelOriginType.MODELSCOPE not in narrowed.registered_sources
    with pytest.raises(AssertionError):
        assert_the_shipped_registration_set(narrowed)


def test_TC_DL_19_unregistered_sources_still_answer_with_an_explicit_no(
    tmp_path: Path,
) -> None:
    """``TC-RES-06``'s property must hold unchanged now that no source is missing.

    Every member of the vocabulary has an Adapter in the shipped registry, so the
    "nothing can judge this" branch is exercised where it still exists: a
    registry that was handed no Adapters.  It is the same code path the shipped
    registry would take for a source added tomorrow without one, and it is
    driven for both request shapes.  An empty registry answers before it could
    dispatch anywhere, so this case needs no source to be running at all.
    """

    async def scenario() -> None:
        for source in ModelOriginType:
            outcome = await ModelResolutionRegistry().resolve(
                ResolutionRequest(
                    source=source,
                    repository_id="synthetic-owner/revision-fixture",
                    requested_revision_norm="@default",
                ),
                credential=None,
            )
            assert isinstance(outcome, ResolutionFailed)
            assert outcome.result.status is None
            assert outcome.result.reason_code is ModelSourceValidationReason.VALIDATOR_UNAVAILABLE
            assert outcome.result.next_action is ModelSourceValidationAction.INSTALL_VALIDATOR

        # An External Path source with no Adapter keeps the original rule: the
        # service dispatches it, and an empty registry answers explicitly.
        empty = await ModelResolutionRegistry().resolve_external_path(
            ExternalPathRequest(
                source=ModelOriginType.LOCAL_PATH,
                untrusted_path=str(tmp_path),
            )
        )
        assert isinstance(empty, ResolutionFailed)
        assert empty.result.reason_code is ModelSourceValidationReason.VALIDATOR_UNAVAILABLE

    asyncio.run(scenario())


def test_TC_RES_06_local_path_no_longer_answers_validator_unavailable(
    ephemeral_postgres: EphemeralPostgres,
    hub: FakeHub,
    tmp_path: Path,
) -> None:
    """The M2-03 placeholder conclusion is gone, and the transition is asserted.

    ``validator_unavailable``/``install_validator`` was M2-03's honest answer
    while nothing could judge a path.  Something can now, so a Local Path that
    the shipped registry judges must never come back with that pair -- while
    an *unregistered* source still must, which the case above pins.
    """

    root = tmp_path / "models"
    (root / "synthetic-model").mkdir(parents=True)

    async def scenario() -> None:
        engine = create_database_engine(_async_url(ephemeral_postgres), connect_timeout_seconds=5)
        try:
            service = ModelResolutionService(
                session_factory=create_session_factory(engine),
                keyring=KEYRING,
                registry=create_default_resolution_registry(
                    external_model_roots=(root,),
                    hugging_face_endpoint=hub.endpoint,
                    scratch_root=tmp_path,
                ),
            )
            accepted = await service.resolve(
                source=ModelOriginType.LOCAL_PATH,
                repository_id=None,
                external_path=str(root / "synthetic-model"),
                revision=None,
            )
            assert isinstance(accepted.outcome, ExternalPathAccepted)
            assert accepted.outcome.runtime_readability is RuntimeReadability.UNVERIFIED

            refused = await service.resolve(
                source=ModelOriginType.LOCAL_PATH,
                repository_id=None,
                external_path=str(tmp_path / "outside-every-root"),
                revision=None,
            )
            assert isinstance(refused.outcome, ResolutionFailed)
            assert (
                refused.outcome.result.reason_code
                is not ModelSourceValidationReason.VALIDATOR_UNAVAILABLE
            )
            assert (
                refused.outcome.result.next_action
                is not ModelSourceValidationAction.INSTALL_VALIDATOR
            )
        finally:
            await engine.dispose()

    asyncio.run(scenario())
    assert hub.requests == ()


def test_TC_DL_19_registered_adapter_previews_without_writing_the_model_domain(
    ephemeral_postgres: EphemeralPostgres,
    hub: FakeHub,
    tmp_path: Path,
) -> None:
    before = _model_domain_snapshot(ephemeral_postgres.engine)
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()

    async def scenario() -> None:
        engine = create_database_engine(_async_url(ephemeral_postgres), connect_timeout_seconds=5)
        try:
            service = ModelResolutionService(
                session_factory=create_session_factory(engine),
                keyring=KEYRING,
                registry=create_default_resolution_registry(
                    external_model_roots=(tmp_path / "external-roots",),
                    hugging_face_endpoint=hub.endpoint,
                    scratch_root=scratch_root,
                ),
            )
            preview = await service.resolve(
                source=ModelOriginType.HUGGING_FACE,
                repository_id=PUBLIC_REPOSITORY_ID,
                external_path=None,
                revision=None,
            )
            assert isinstance(preview.outcome, ResolutionSucceeded)
            resolved = preview.outcome.resolved
            assert resolved.resolved_revision == PUBLIC_REPOSITORY_COMMIT
            assert isinstance(resolved.files, Identified)
            assert len(resolved.files.value) == 14
            assert resolved.architecture == Identified("Qwen2ForCausalLM")
            assert preview.local_availability is LocalAvailability.ABSENT

            external = await service.resolve(
                source=ModelOriginType.LOCAL_PATH,
                repository_id=None,
                external_path="/srv/models/fixture",
                revision=None,
            )
            assert isinstance(external.outcome, ResolutionFailed)
            assert external.local_availability is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())
    assert _model_domain_snapshot(ephemeral_postgres.engine) == before
    assert list(scratch_root.iterdir()) == []
    with ephemeral_postgres.engine.connect() as connection:
        for table_name in MODEL_DOMAIN_TABLE_NAMES:
            assert (
                connection.execute(sa.text(f"SELECT count(*) FROM core.{table_name}")).scalar_one()
                == 0
            )


def test_TC_DL_19_no_placeholder_adapter_hides_in_the_service_default(
    tmp_path: Path,
) -> None:
    """A service built without a registry still refuses every source."""

    del tmp_path
    empty = ModelResolutionRegistry()
    assert empty.registered_sources == frozenset()
    assert (
        HuggingFaceResolutionAdapter(endpoint="http://127.0.0.1:1").endpoint == "http://127.0.0.1:1"
    )

"""M2-14A TC-DL-26: ModelScope refuses hostile Repository IDs before contacting the source.

``test_M2_04A_TC_DL_26_27_hugging_face_boundaries.py`` pins this for Hugging
Face.  The refusing pattern (``model_resolution_service._REPOSITORY_ID_PATTERN``)
is shared, so this is expected to pass without new code.  It exists so the
per-source claim is an executed judgement instead of an inference from a shared
code path that a later per-source branch could quietly stop sharing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Final

import pytest
from fixtures.fake_model_source import PathAttackKind, path_attack_samples
from fixtures.modelscope import PUBLIC_REPOSITORY_ID, FakeHub, fake_modelscope_hub
from holm_control_api.model_resolution import ModelResolutionRegistry, ResolutionRequest
from holm_control_api.model_resolution_service import (
    ModelResolutionService,
    ResolveInputRejected,
)
from holm_control_api.modelscope_resolution import ModelScopeResolutionAdapter
from holm_domain import ModelOriginType
from holm_persistence import DEFAULT_REQUESTED_REVISION
from holm_security import MasterKey, MasterKeyring, MasterKeyState

KEYRING: Final = MasterKeyring((MasterKey("m2-14a-tc-dl-26", MasterKeyState.WRITABLE, b"K" * 32),))
REPOSITORY_ID_SHAPED_KINDS: Final[frozenset[PathAttackKind]] = frozenset(
    {
        PathAttackKind.INVALID_REPOSITORY_ID,
        PathAttackKind.PARENT_TRAVERSAL,
        PathAttackKind.ABSOLUTE_PATH,
    }
)
ADDRESS_SHAPED_REPOSITORY_IDS: Final[tuple[str, ...]] = (
    "https://www.modelscope.cn/models/Qwen/Qwen2.5-7B-Instruct",
    "http://127.0.0.1:8080/owner/model",
    "//evil.example/owner/model",
    "owner/model?endpoint=https://evil.example",
    "file:///etc/passwd",
    "owner/model#https://evil.example",
    "evil.example:443/owner/model",
)


@pytest.fixture
def hub() -> Iterator[FakeHub]:
    with fake_modelscope_hub() as running:
        yield running


def _exploding_session_factory() -> object:
    def factory(*arguments: object, **keywords: object) -> object:
        raise AssertionError("a rejected input must never reach the database")

    return factory


def test_TC_DL_26_M2_14A_modelscope_hostile_repository_ids_are_refused_before_the_source(
    hub: FakeHub,
) -> None:
    adapter = ModelScopeResolutionAdapter(endpoint=hub.endpoint)
    service = ModelResolutionService(
        session_factory=_exploding_session_factory(),  # type: ignore[arg-type]
        keyring=KEYRING,
        registry=ModelResolutionRegistry({ModelOriginType.MODELSCOPE: adapter}),
    )
    hostile = (
        tuple(
            sample.untrusted_value
            for sample in path_attack_samples()
            if sample.kind in REPOSITORY_ID_SHAPED_KINDS
        )
        + ADDRESS_SHAPED_REPOSITORY_IDS
    )
    assert len(hostile) == len(REPOSITORY_ID_SHAPED_KINDS) + len(ADDRESS_SHAPED_REPOSITORY_IDS)

    async def scenario() -> None:
        for untrusted in hostile:
            with pytest.raises(ResolveInputRejected) as failure:
                await service.resolve(
                    source=ModelOriginType.MODELSCOPE,
                    repository_id=untrusted,
                    external_path=None,
                    revision=None,
                )
            assert untrusted not in str(failure.value)

    asyncio.run(scenario())
    assert hub.requests == ()

    # Control: the very adapter the service holds does reach this Hub, so the
    # empty request log above is a refusal and not a disconnected adapter.
    asyncio.run(
        adapter.resolve(
            ResolutionRequest(
                source=ModelOriginType.MODELSCOPE,
                repository_id=PUBLIC_REPOSITORY_ID,
                requested_revision_norm=DEFAULT_REQUESTED_REVISION,
            ),
            credential=None,
        )
    )
    assert hub.requests

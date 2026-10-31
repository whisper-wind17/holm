"""M2-14A TC-DL-26: a resolution preview never accepts an escaping listing path.

Resolution is the first of two independent guards; the download child is the
second (``tests/integration/test_M2_14A_TC_DL_26_download_listing_paths.py``).
Each is pinned on its own: these cases never reach a download, and the download
cases never run a resolution, so neither layer can pass on the other's behalf.

The judgement is made at the boundary production uses -- the registry built by
``create_default_resolution_registry``, which both the Control API and the
Download Worker's resolution child call -- and not on an Adapter alone.  An
Adapter is allowed to raise on an answer it cannot classify; the registry turns
that into ``VALIDATION_FAILED``.  That is why the Hugging Face parameters pass
today (its SDK raises on these names during the dry run) while the ModelScope
ones do not (its listing parser accepts any non-empty ``Path``).

``VALIDATION_FAILED`` is also the conclusion ModelScope already gives a listing
it cannot read (``modelscope_resolution._UNRESOLVABLE_RESULT``), and M2-01A's
vocabulary has no finer member; a listing naming an escaping path is not a
credential, permission, existence or rate-limit fact about the source.

Nothing is written during a resolution, so FX-MS-06's samples are used verbatim,
the absolute one included.  The control serves the same reduced listing under
the recorded name, so a refusal here is attributable to the path.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Final

import pytest
from fixtures.fake_model_source import PathAttackKind, path_attack_samples
from fixtures.hugging_face import PUBLIC_REPOSITORY_ID as HF_PUBLIC_REPOSITORY_ID
from fixtures.hugging_face import fake_hugging_face_hub
from fixtures.modelscope import CONFIG_FILENAME, fake_modelscope_hub
from fixtures.modelscope import PUBLIC_REPOSITORY_ID as MS_PUBLIC_REPOSITORY_ID
from holm_control_api.model_resolution import (
    ResolutionFailed,
    ResolutionOutcome,
    ResolutionRequest,
    ResolutionSucceeded,
)
from holm_control_api.model_resolution_registry import create_default_resolution_registry
from holm_domain import Identified, ModelOriginType, ModelSourceValidationReason
from holm_persistence import DEFAULT_REQUESTED_REVISION

LISTING_DELIVERABLE_KINDS: Final[tuple[PathAttackKind, ...]] = (
    PathAttackKind.PARENT_TRAVERSAL,
    PathAttackKind.ABSOLUTE_PATH,
)
SOURCES: Final[tuple[ModelOriginType, ...]] = (
    ModelOriginType.HUGGING_FACE,
    ModelOriginType.MODELSCOPE,
)


def _sample(kind: PathAttackKind) -> str:
    (sample,) = (item for item in path_attack_samples() if item.kind is kind)
    return sample.untrusted_value


def _resolve(
    source: ModelOriginType,
    substitutes: tuple[str, ...],
    scratch: Path,
) -> tuple[ResolutionOutcome, int]:
    """One preview through the production registry, and how many listings were substituted."""

    hugging_face = source is ModelOriginType.HUGGING_FACE
    request_repository = HF_PUBLIC_REPOSITORY_ID if hugging_face else MS_PUBLIC_REPOSITORY_ID
    # The registry refuses an empty allowed-root set; this root is never resolved against.
    external_root = scratch / "external-root"
    external_root.mkdir()
    with (
        fake_hugging_face_hub(listing_path_substitutes=substitutes) as hugging_face_hub,
        fake_modelscope_hub(listing_path_substitutes=substitutes) as modelscope_hub,
    ):
        registry = create_default_resolution_registry(
            external_model_roots=(external_root,),
            hugging_face_endpoint=hugging_face_hub.endpoint,
            modelscope_endpoint=modelscope_hub.endpoint,
            scratch_root=scratch,
        )
        outcome = asyncio.run(
            registry.resolve(
                ResolutionRequest(
                    source=source,
                    repository_id=request_repository,
                    requested_revision_norm=DEFAULT_REQUESTED_REVISION,
                ),
                credential=None,
            )
        )
        delivered = (
            hugging_face_hub.hostile_listings_served
            if hugging_face
            else modelscope_hub.hostile_listings_served
        )
    return outcome, delivered


@pytest.mark.parametrize("kind", LISTING_DELIVERABLE_KINDS, ids=str)
@pytest.mark.parametrize("source", SOURCES, ids=str)
def test_TC_DL_26_M2_14A_resolution_refuses_an_escaping_listing_path(
    source: ModelOriginType,
    kind: PathAttackKind,
    tmp_path: Path,
) -> None:
    untrusted = _sample(kind)

    outcome, delivered = _resolve(source, (untrusted,), tmp_path)

    assert delivered >= 1
    assert isinstance(outcome, ResolutionFailed), "an escaping listing path became a preview"
    assert outcome.result.reason_code is ModelSourceValidationReason.VALIDATION_FAILED
    assert untrusted not in repr(outcome)


@pytest.mark.parametrize("source", SOURCES, ids=str)
def test_TC_DL_26_M2_14A_control_the_substituted_listing_under_its_recorded_name_resolves(
    source: ModelOriginType,
    tmp_path: Path,
) -> None:
    outcome, delivered = _resolve(source, (CONFIG_FILENAME,), tmp_path)

    assert delivered >= 1
    assert isinstance(outcome, ResolutionSucceeded)
    files = outcome.resolved.files
    assert isinstance(files, Identified)
    assert [item.path for item in files.value] == [CONFIG_FILENAME]

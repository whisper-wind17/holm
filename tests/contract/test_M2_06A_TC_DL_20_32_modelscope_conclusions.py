"""TC-DL-20 (ModelScope half) and TC-DL-32: credential and visibility conclusions.

Two properties are pinned here, both driven by real recordings replayed through
each source's own fake Hub:

* **"Credential valid" may only come from an endpoint that requires
  authentication.**  Reading a *public* ModelScope repository with an invalid
  token returns a full 200 (``FX-MS-09``), so a successful parse says nothing
  about a credential.  ``GET /api/v1/users/info`` is the only positive
  evidence, and the case where that endpoint accepts a token is **suspended**:
  the recording session had no ModelScope account, and synthesizing a 200 would
  make the control below green forever.
* **``anonymous_not_found_observable`` is asserted in both directions.**
  Hugging Face takes ``False`` and its two conclusions must be
  indistinguishable; ModelScope takes ``True`` and "does not exist" must be a
  conclusion of its own.  Only the ``False`` side would be satisfied by an
  implementation that collapses everything.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Final

import pytest
from fixtures.fake_model_source import (
    AccessOutcome,
    CredentialCase,
    CredentialReference,
    anonymous_not_found_observable,
    credential_scenario,
)
from fixtures.hugging_face import MISSING_REPOSITORY_ID as HF_MISSING_REPOSITORY_ID
from fixtures.hugging_face import fake_hugging_face_hub
from fixtures.modelscope import (
    MISSING_REPOSITORY_ID,
    PUBLIC_REPOSITORY_ID,
    USERS_INFO_PATH,
    CredentialPresence,
    body_without_request_id,
    fake_modelscope_hub,
    load_recording,
)
from fixtures.source_mutation import load_mutated_module
from holm_control_api.hugging_face_resolution import HuggingFaceResolutionAdapter
from holm_control_api.model_resolution import (
    ResolutionFailed,
    ResolutionOutcome,
    ResolutionRequest,
    ResolutionSucceeded,
)
from holm_control_api.model_source_validation import (
    ModelSourceValidationResult,
    ModelSourceValidatorRegistry,
)
from holm_control_api.modelscope_resolution import ModelScopeResolutionAdapter
from holm_domain import (
    ModelOriginType,
    ModelSourceType,
    ModelSourceValidationAction,
    ModelSourceValidationReason,
    ModelSourceValidationStatus,
)
from holm_persistence import DEFAULT_REQUESTED_REVISION

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
VALIDATOR_SOURCE: Final[Path] = (
    REPOSITORY_ROOT / "apps/control_api/src/holm_control_api/model_source_validation.py"
)
ADAPTER_SOURCE: Final[Path] = (
    REPOSITORY_ROOT / "apps/control_api/src/holm_control_api/modelscope_resolution.py"
)
SOURCE_READ_SOURCE: Final[Path] = (
    REPOSITORY_ROOT / "apps/control_api/src/holm_control_api/modelscope_source.py"
)
SERVICE_SOURCE: Final[Path] = (
    REPOSITORY_ROOT / "apps/control_api/src/holm_control_api/model_resolution_service.py"
)
VISIBILITY_SOURCE: Final[Path] = (
    REPOSITORY_ROOT / "tests/fixtures/fake_model_source/anonymous_visibility.py"
)

INVALID_TOKEN: Final[bytes] = b"ms-PWSSNTL-M2-06A-INVALID-0000-000000000000"
UNRECORDED_VALID_TOKEN: Final[str] = "ms-PWSSNTL-M2-06A-UNRECORDED-VALID-000000000"


def resolve_modelscope(
    adapter: Any,
    repository_id: str,
    credential: bytes | None,
) -> ResolutionOutcome:
    return asyncio.run(
        adapter.resolve(
            ResolutionRequest(
                source=ModelOriginType.MODELSCOPE,
                repository_id=repository_id,
                requested_revision_norm=DEFAULT_REQUESTED_REVISION,
            ),
            credential=credential,
        )
    )


def resolve_hugging_face(adapter: Any, repository_id: str) -> ResolutionOutcome:
    return asyncio.run(
        adapter.resolve(
            ResolutionRequest(
                source=ModelOriginType.HUGGING_FACE,
                repository_id=repository_id,
                requested_revision_norm=DEFAULT_REQUESTED_REVISION,
            ),
            credential=None,
        )
    )


def assert_invalid_credential(result: ModelSourceValidationResult) -> None:
    assert result.status is ModelSourceValidationStatus.INVALID_CREDENTIAL
    assert result.reason_code is ModelSourceValidationReason.CREDENTIAL_INVALID
    assert result.next_action is ModelSourceValidationAction.UPDATE_CREDENTIAL


def assert_anonymous_missing_matches_the_dimension(
    outcome: ResolutionOutcome,
    *,
    observable: bool,
) -> None:
    """The one judgement shared by the real cases and the swapped control.

    ``observable`` is the fixture's declaration for the source that produced
    ``outcome``.  When it is ``True`` the platform must state "not found"; when
    it is ``False`` it must state the collapsed "missing or invisible, and the
    platform cannot tell", which carries no verification status at all and
    directs the operator at both possible causes at once.
    """

    assert isinstance(outcome, ResolutionFailed)
    if observable:
        assert outcome.result.status is ModelSourceValidationStatus.NOT_FOUND
        assert outcome.result.reason_code is ModelSourceValidationReason.SOURCE_NOT_FOUND
        assert outcome.result.next_action is ModelSourceValidationAction.CHECK_SOURCE_REFERENCE
        return
    assert outcome.result.status is None
    assert (
        outcome.result.reason_code is ModelSourceValidationReason.SOURCE_NOT_FOUND_OR_INACCESSIBLE
    )
    assert (
        outcome.result.next_action
        is ModelSourceValidationAction.CHECK_SOURCE_REFERENCE_AND_CONFIGURE_CREDENTIAL
    )


def test_TC_DL_20_the_eight_fixture_cases_keep_one_declaration_per_source() -> None:
    """The case set is source-independent; only this one dimension is per source."""

    assert len(set(CredentialCase)) == 8
    assert anonymous_not_found_observable(ModelSourceType.HUGGING_FACE) is False
    assert anonymous_not_found_observable(ModelSourceType.MODELSCOPE) is True
    # Collapse (a), source-independent: an expired credential is a separately
    # injectable case that reaches the same conclusion as a wrong one.
    invalid = credential_scenario(CredentialCase.INVALID_CREDENTIAL)
    expired = credential_scenario(CredentialCase.EXPIRED_CREDENTIAL)
    assert invalid.case is not expired.case
    assert invalid.outcome is expired.outcome is AccessOutcome.INVALID_CREDENTIAL
    # A source nobody has measured has no default in either direction.
    with pytest.raises(KeyError):
        anonymous_not_found_observable("Bitbucket")  # type: ignore[arg-type]


def test_TC_DL_20_a_missing_repository_is_named_on_one_source_and_collapsed_on_the_other() -> None:
    """Both directions of the dimension, each against its own recordings."""

    with fake_modelscope_hub() as modelscope:
        anonymous = resolve_modelscope(
            ModelScopeResolutionAdapter(endpoint=modelscope.endpoint),
            MISSING_REPOSITORY_ID,
            None,
        )
        assert [request.credential for request in modelscope.requests] == [
            CredentialPresence.ABSENT
        ]
    assert_anonymous_missing_matches_the_dimension(
        anonymous,
        observable=anonymous_not_found_observable(ModelSourceType.MODELSCOPE),
    )

    with fake_hugging_face_hub() as hugging_face:
        collapsed = resolve_hugging_face(
            HuggingFaceResolutionAdapter(endpoint=hugging_face.endpoint),
            HF_MISSING_REPOSITORY_ID,
        )
    assert_anonymous_missing_matches_the_dimension(
        collapsed,
        observable=anonymous_not_found_observable(ModelSourceType.HUGGING_FACE),
    )

    # The two sources really do answer differently, and the difference is the
    # source's, not the platform's: 404 with a numeric code versus 401.
    assert load_recording("missing-repo-metadata-anonymous").status == 404
    assert load_recording("missing-repo-metadata-anonymous").json_body()["Code"] == 10010205001
    assert collapsed != anonymous


def test_TC_DL_20_positive_control_swapping_the_dimension_fails_on_both_sources(
    tmp_path: Path,
) -> None:
    """Swap the fixture's declared values; each source must fail its own case."""

    module = load_mutated_module(
        VISIBILITY_SOURCE,
        [
            (
                "        ModelSourceType.HUGGING_FACE: False,\n",
                "        ModelSourceType.HUGGING_FACE: True,\n",
            ),
            (
                "        ModelSourceType.MODELSCOPE: True,\n",
                "        ModelSourceType.MODELSCOPE: False,\n",
            ),
        ],
        module_name="_swapped_anonymous_visibility",
        destination=tmp_path / "swapped_anonymous_visibility.py",
    )
    assert module.anonymous_not_found_observable(ModelSourceType.HUGGING_FACE) is True
    assert module.anonymous_not_found_observable(ModelSourceType.MODELSCOPE) is False

    with fake_modelscope_hub() as modelscope:
        anonymous = resolve_modelscope(
            ModelScopeResolutionAdapter(endpoint=modelscope.endpoint),
            MISSING_REPOSITORY_ID,
            None,
        )
    with fake_hugging_face_hub() as hugging_face:
        collapsed = resolve_hugging_face(
            HuggingFaceResolutionAdapter(endpoint=hugging_face.endpoint),
            HF_MISSING_REPOSITORY_ID,
        )
    swapped_pairs = (
        (anonymous, ModelSourceType.MODELSCOPE),
        (collapsed, ModelSourceType.HUGGING_FACE),
    )
    for outcome, source in swapped_pairs:
        with pytest.raises(AssertionError):
            assert_anonymous_missing_matches_the_dimension(
                outcome,
                observable=module.anonymous_not_found_observable(source),
            )


def test_TC_DL_20_a_private_repository_is_distinguishable_BLOCKED_ENV() -> None:
    """The visible half of the ``True`` side is asserted; the invisible half is not.

    ``anonymous_not_found_observable = True`` claims that on ModelScope "does
    not exist" and "exists but is invisible to you" are *different* answers.
    The first half is proven above by a real 404.  The second half needs a
    private or gated ModelScope repository, and no recording covers one: the
    recording session had no account to create one with, and no public
    repository could be identified as reliably gated
    (``tests/fixtures/modelscope/recorded/PROVENANCE.md``, known gaps).

    Whether ModelScope collapses those two answers is therefore **unmeasured**.
    Asserting either way would be a claim about a source nobody has observed,
    and fabricating the response in the fake Hub would make this case green
    without measuring anything.
    """

    pytest.xfail(
        "BLOCKED_ENV: no ModelScope private/gated repository recording exists "
        "(needs a real account and egress); M2-14 records it and lifts this"
    )


def test_TC_DL_32_the_default_registry_validates_modelscope_against_users_info() -> None:
    with fake_modelscope_hub() as hub:
        registry = ModelSourceValidatorRegistry(modelscope_endpoint=hub.endpoint)
        assert registry.registered_sources == {
            ModelSourceType.HUGGING_FACE,
            ModelSourceType.MODELSCOPE,
        }
        result = asyncio.run(
            registry.validate(ModelSourceType.MODELSCOPE, credential=INVALID_TOKEN)
        )
        assert_invalid_credential(result)
        assert [request.path for request in hub.requests] == [USERS_INFO_PATH]
        assert hub.requests[0].credential is CredentialPresence.INVALID


def test_TC_DL_32_invalid_and_absent_credentials_are_explicitly_indistinguishable() -> None:
    with fake_modelscope_hub() as hub:
        registry = ModelSourceValidatorRegistry(modelscope_endpoint=hub.endpoint)
        invalid = asyncio.run(
            registry.validate(ModelSourceType.MODELSCOPE, credential=INVALID_TOKEN)
        )
        anonymous = asyncio.run(registry.validate(ModelSourceType.MODELSCOPE, credential=None))
        assert [request.credential for request in hub.requests] == [
            CredentialPresence.INVALID,
            CredentialPresence.ABSENT,
        ]
    assert_invalid_credential(invalid)
    assert_invalid_credential(anonymous)
    assert invalid == anonymous
    # The collapse is the source's: the two recordings are one answer.
    assert body_without_request_id("invalid-credential-users-info") == body_without_request_id(
        "anonymous-users-info"
    )


def test_TC_DL_32_parsing_a_public_repository_with_a_bad_token_never_validates_it() -> None:
    """The resolve path cannot even express "credential invalid"."""

    with fake_modelscope_hub() as hub:
        outcome = resolve_modelscope(
            ModelScopeResolutionAdapter(endpoint=hub.endpoint),
            PUBLIC_REPOSITORY_ID,
            INVALID_TOKEN,
        )
        assert {request.credential for request in hub.requests} == {CredentialPresence.INVALID}
    assert isinstance(outcome, ResolutionSucceeded)
    assert load_recording("invalid-credential-public-repo").status == 200

    # Scanned over the whole request path, not one function: the Adapter and the
    # HTTP read it uses are everything that turns a source answer into a
    # conclusion here, and neither can name that status.
    source_answer_path = ADAPTER_SOURCE.read_text(encoding="utf-8") + SOURCE_READ_SOURCE.read_text(
        encoding="utf-8"
    )
    assert "INVALID_CREDENTIAL" not in source_answer_path
    # The service does name it, exactly once, and not as a source judgement: it
    # is the branch for a stored credential this platform cannot decrypt, which
    # is decided before anything is sent anywhere.
    service = SERVICE_SOURCE.read_text(encoding="utf-8")
    assert service.count("ModelSourceValidationStatus.INVALID_CREDENTIAL") == 1
    assert "_UNUSABLE_CREDENTIAL_RESULT" in service


def test_TC_DL_32_positive_control_validating_by_public_parse_false_greens_a_bad_token(
    tmp_path: Path,
) -> None:
    module = load_mutated_module(
        VALIDATOR_SOURCE,
        [
            (
                "                path=MODELSCOPE_USERS_INFO_PATH,\n",
                '                path="/api/v1/models/Qwen/Qwen2.5-7B-Instruct",\n',
            )
        ],
        module_name="_public_parse_modelscope_validator_mutation",
        destination=tmp_path / "public_parse_modelscope_validator_mutation.py",
    )
    with fake_modelscope_hub() as hub:
        registry = module.ModelSourceValidatorRegistry(modelscope_endpoint=hub.endpoint)
        false_green = asyncio.run(
            registry.validate(ModelSourceType.MODELSCOPE, credential=INVALID_TOKEN)
        )
        assert hub.requests[0].path == f"/api/v1/models/{PUBLIC_REPOSITORY_ID}"
        assert hub.requests[0].credential is CredentialPresence.INVALID
    assert false_green.status is ModelSourceValidationStatus.ACCESSIBLE
    with pytest.raises(AssertionError):
        assert_invalid_credential(false_green)


def test_TC_DL_32_an_unrecorded_valid_token_is_not_synthesized_as_success() -> None:
    """Until the gap is recorded, a token the fixture calls valid gets no verdict."""

    with fake_modelscope_hub(valid_token=UNRECORDED_VALID_TOKEN) as hub:
        registry = ModelSourceValidatorRegistry(modelscope_endpoint=hub.endpoint)
        result = asyncio.run(
            registry.validate(
                ModelSourceType.MODELSCOPE,
                credential=UNRECORDED_VALID_TOKEN.encode("utf-8"),
            )
        )
        assert hub.requests[0].path == USERS_INFO_PATH
        assert hub.requests[0].credential is CredentialPresence.VALID
    assert result.status is None
    assert result.reason_code is ModelSourceValidationReason.VALIDATION_FAILED
    assert result.next_action is ModelSourceValidationAction.RETRY_LATER
    assert credential_scenario(CredentialCase.PRIVATE_ACCESSIBLE).credential_reference is (
        CredentialReference.SYNTHETIC_AUTHORIZED
    )


def test_TC_DL_32_a_valid_token_is_judged_valid_BLOCKED_ENV() -> None:
    """The one case that says a credential *works* has no recording.

    ``FX-MS-04``'s ``PRIVATE_ACCESSIBLE`` case expects ``ACCESSIBLE``, and on
    this source the only evidence that could produce it is a 200 from
    ``/api/v1/users/info`` with a real token.  The recording session had no
    ModelScope account (``recorded/PROVENANCE.md``, known gaps), and a
    fabricated 200 in the fake Hub would permanently green the control above,
    which is the only thing keeping "resolve a public repository" from being
    accepted as credential evidence.
    """

    pytest.xfail(
        "BLOCKED_ENV: no successful ModelScope /users/info recording exists "
        "(needs a real token and egress); M2-14 records it and lifts this"
    )

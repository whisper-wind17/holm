"""TC-DL-18/20 and TC-SRC-18: recorded Hugging Face source conclusions."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Final

import pytest
from fixtures.fake_model_source import (
    AccessOutcome,
    CredentialCase,
    CredentialReference,
    credential_scenario,
)
from fixtures.hugging_face import (
    MISSING_REPOSITORY_ID,
    PUBLIC_REPOSITORY_ID,
    CredentialPresence,
    fake_hugging_face_hub,
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
from holm_domain import (
    ModelOriginType,
    ModelSourceType,
    ModelSourceValidationAction,
    ModelSourceValidationReason,
    ModelSourceValidationStatus,
)
from holm_persistence import DEFAULT_REQUESTED_REVISION

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
ADAPTER_SOURCE: Final[Path] = (
    REPOSITORY_ROOT / "apps/control_api/src/holm_control_api/hugging_face_resolution.py"
)
VALIDATOR_SOURCE: Final[Path] = (
    REPOSITORY_ROOT / "apps/control_api/src/holm_control_api/model_source_validation.py"
)


def _resolve(
    adapter: HuggingFaceResolutionAdapter,
    repository_id: str,
    credential: bytes | None,
) -> ResolutionOutcome:
    return asyncio.run(
        adapter.resolve(
            ResolutionRequest(
                source=ModelOriginType.HUGGING_FACE,
                repository_id=repository_id,
                requested_revision_norm=DEFAULT_REQUESTED_REVISION,
            ),
            credential=credential,
        )
    )


def _assert_not_found_or_inaccessible(outcome: ResolutionOutcome) -> None:
    assert isinstance(outcome, ResolutionFailed)
    assert outcome.result.status is None
    assert (
        outcome.result.reason_code is ModelSourceValidationReason.SOURCE_NOT_FOUND_OR_INACCESSIBLE
    )
    assert (
        outcome.result.next_action
        is ModelSourceValidationAction.CHECK_SOURCE_REFERENCE_AND_CONFIGURE_CREDENTIAL
    )


def _assert_invalid_credential(result: ModelSourceValidationResult) -> None:
    assert result.status is ModelSourceValidationStatus.INVALID_CREDENTIAL
    assert result.reason_code is ModelSourceValidationReason.CREDENTIAL_INVALID
    assert result.next_action is ModelSourceValidationAction.UPDATE_CREDENTIAL


def test_TC_DL_20_collapse_a_invalid_and_expired_credentials_share_one_outcome() -> None:
    invalid = credential_scenario(CredentialCase.INVALID_CREDENTIAL)
    expired = credential_scenario(CredentialCase.EXPIRED_CREDENTIAL)

    assert invalid.case is not expired.case
    assert invalid.credential_reference is CredentialReference.SYNTHETIC_INVALID
    assert expired.credential_reference is CredentialReference.SYNTHETIC_EXPIRED
    assert invalid.outcome is expired.outcome is AccessOutcome.INVALID_CREDENTIAL


def test_TC_DL_18_TC_DL_20_collapse_b_anonymous_missing_or_invisible_is_explicit() -> None:
    scenario = credential_scenario(CredentialCase.ANONYMOUS_INDISTINGUISHABLE)
    assert scenario.credential_reference is CredentialReference.NOT_SUPPLIED
    # No verification status carries this conclusion, so the fixture states the
    # absence instead of collapsing it onto ``NOT_FOUND``.
    assert scenario.outcome is None
    assert AccessOutcome.NOT_FOUND not in {scenario.outcome}

    with fake_hugging_face_hub() as hub:
        outcome = _resolve(
            HuggingFaceResolutionAdapter(endpoint=hub.endpoint),
            MISSING_REPOSITORY_ID,
            None,
        )
        _assert_not_found_or_inaccessible(outcome)
        assert len(hub.requests) == 1
        sentinel_request = hub.requests[0]
        assert sentinel_request.credential is CredentialPresence.ABSENT
        assert MISSING_REPOSITORY_ID in sentinel_request.path


def test_TC_DL_18_TC_DL_20_collapse_c_bad_and_absent_tokens_match_in_resolution() -> None:
    invalid_sentinel = b"hf_PWSSNTL_M2_04B_INVALID_RESOLUTION"
    with fake_hugging_face_hub() as hub:
        adapter = HuggingFaceResolutionAdapter(endpoint=hub.endpoint)
        anonymous = _resolve(adapter, MISSING_REPOSITORY_ID, None)
        invalid = _resolve(adapter, MISSING_REPOSITORY_ID, invalid_sentinel)

        _assert_not_found_or_inaccessible(anonymous)
        _assert_not_found_or_inaccessible(invalid)
        assert invalid == anonymous
        assert [request.credential for request in hub.requests] == [
            CredentialPresence.ABSENT,
            CredentialPresence.INVALID,
        ]


def test_TC_DL_20_not_found_is_reachable_only_on_the_authenticated_half() -> None:
    valid_sentinel = "hf_PWSSNTL_M2_04B_VALID_FOR_RECORDED_404"
    with fake_hugging_face_hub(valid_token=valid_sentinel) as hub:
        adapter = HuggingFaceResolutionAdapter(endpoint=hub.endpoint)
        anonymous = _resolve(adapter, MISSING_REPOSITORY_ID, None)
        authenticated = _resolve(
            adapter,
            MISSING_REPOSITORY_ID,
            valid_sentinel.encode("utf-8"),
        )

        _assert_not_found_or_inaccessible(anonymous)
        assert isinstance(authenticated, ResolutionFailed)
        assert authenticated.result.status is ModelSourceValidationStatus.NOT_FOUND
        assert authenticated.result.reason_code is ModelSourceValidationReason.SOURCE_NOT_FOUND
        assert (
            authenticated.result.next_action is ModelSourceValidationAction.CHECK_SOURCE_REFERENCE
        )
        assert [request.credential for request in hub.requests] == [
            CredentialPresence.ABSENT,
            CredentialPresence.VALID,
        ]


def test_TC_DL_18_public_parse_with_bad_token_succeeds_but_never_validates_it() -> None:
    invalid_sentinel = b"hf_PWSSNTL_M2_04B_PUBLIC_PARSE_BAD_TOKEN"
    with fake_hugging_face_hub() as hub:
        outcome = _resolve(
            HuggingFaceResolutionAdapter(endpoint=hub.endpoint),
            PUBLIC_REPOSITORY_ID,
            invalid_sentinel,
        )
        assert isinstance(outcome, ResolutionSucceeded)
        assert {request.credential for request in hub.requests} == {CredentialPresence.INVALID}

    source = ADAPTER_SOURCE.read_text(encoding="utf-8")
    assert "ModelSourceValidationStatus.INVALID_CREDENTIAL" not in source


def test_TC_DL_18_positive_control_mapping_anonymous_401_to_invalid_fails(
    tmp_path: Path,
) -> None:
    module = load_mutated_module(
        ADAPTER_SOURCE,
        [
            (
                "    status=None,\n"
                "    reason_code=ModelSourceValidationReason."
                "SOURCE_NOT_FOUND_OR_INACCESSIBLE,",
                "    status=ModelSourceValidationStatus.INVALID_CREDENTIAL,\n"
                "    reason_code=ModelSourceValidationReason."
                "SOURCE_NOT_FOUND_OR_INACCESSIBLE,",
            )
        ],
        module_name="_invalid_credential_parsing_mutation",
        destination=tmp_path / "invalid_credential_parsing_mutation.py",
    )
    with fake_hugging_face_hub() as hub:
        mutated = _resolve(
            module.HuggingFaceResolutionAdapter(endpoint=hub.endpoint),
            MISSING_REPOSITORY_ID,
            None,
        )
        assert hub.requests[0].credential is CredentialPresence.ABSENT
    with pytest.raises(AssertionError):
        _assert_not_found_or_inaccessible(mutated)


def test_TC_SRC_18_default_registry_registers_hf_and_whoami_rejects_bad_token() -> None:
    invalid_sentinel = b"hf_PWSSNTL_M2_04B_INVALID_WHOAMI"
    with fake_hugging_face_hub() as hub:
        registry = ModelSourceValidatorRegistry(hugging_face_endpoint=hub.endpoint)
        # M2-06A added the second credential-bearing source.  Still an exact
        # equality, never widened into a subset: the point of this line is that
        # a source without a validator cannot hide inside the default registry.
        assert registry.registered_sources == {
            ModelSourceType.HUGGING_FACE,
            ModelSourceType.MODELSCOPE,
        }
        result = asyncio.run(
            registry.validate(ModelSourceType.HUGGING_FACE, credential=invalid_sentinel)
        )
        _assert_invalid_credential(result)
        assert len(hub.requests) == 1
        assert hub.requests[0].path == "/api/whoami-v2"
        assert hub.requests[0].credential is CredentialPresence.INVALID


def test_TC_SRC_18_invalid_and_anonymous_whoami_are_explicitly_indistinguishable() -> None:
    invalid_sentinel = b"hf_PWSSNTL_M2_04B_WHOAMI_COLLAPSE"
    with fake_hugging_face_hub() as hub:
        registry = ModelSourceValidatorRegistry(hugging_face_endpoint=hub.endpoint)
        invalid = asyncio.run(
            registry.validate(ModelSourceType.HUGGING_FACE, credential=invalid_sentinel)
        )
        anonymous = asyncio.run(registry.validate(ModelSourceType.HUGGING_FACE, credential=None))
        _assert_invalid_credential(invalid)
        _assert_invalid_credential(anonymous)
        assert invalid == anonymous
        assert [request.credential for request in hub.requests] == [
            CredentialPresence.INVALID,
            CredentialPresence.ABSENT,
        ]


def test_TC_SRC_18_unrecorded_valid_whoami_is_not_synthesized_as_success() -> None:
    valid_sentinel = "hf_PWSSNTL_M2_04B_UNRECORDED_VALID_WHOAMI"
    with fake_hugging_face_hub(valid_token=valid_sentinel) as hub:
        registry = ModelSourceValidatorRegistry(hugging_face_endpoint=hub.endpoint)
        result = asyncio.run(
            registry.validate(
                ModelSourceType.HUGGING_FACE,
                credential=valid_sentinel.encode("utf-8"),
            )
        )
        assert result.status is None
        assert result.reason_code is ModelSourceValidationReason.VALIDATION_FAILED
        assert result.next_action is ModelSourceValidationAction.RETRY_LATER
        assert hub.requests[0].path == "/api/whoami-v2"
        assert hub.requests[0].credential is CredentialPresence.VALID


def test_TC_SRC_18_positive_control_public_parse_false_green_fails(
    tmp_path: Path,
) -> None:
    module = load_mutated_module(
        VALIDATOR_SOURCE,
        [
            (
                '                f"{self._endpoint}/api/whoami-v2",',
                '                f"{self._endpoint}/api/models/'
                'Qwen/Qwen2.5-7B-Instruct/revision/main?blobs=true",',
            )
        ],
        module_name="_public_parse_credential_validator_mutation",
        destination=tmp_path / "public_parse_credential_validator_mutation.py",
    )
    invalid_sentinel = b"hf_PWSSNTL_M2_04B_FALSE_GREEN"
    with fake_hugging_face_hub() as hub:
        registry = module.ModelSourceValidatorRegistry(hugging_face_endpoint=hub.endpoint)
        false_green = asyncio.run(
            registry.validate(ModelSourceType.HUGGING_FACE, credential=invalid_sentinel)
        )
        assert hub.requests[0].credential is CredentialPresence.INVALID
        assert hub.requests[0].path.startswith(f"/api/models/{PUBLIC_REPOSITORY_ID}/")
    with pytest.raises(AssertionError):
        _assert_invalid_credential(false_green)

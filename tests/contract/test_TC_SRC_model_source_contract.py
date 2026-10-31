"""TC-SRC-04/06: closed write Schema and FX-MS-04 validation behavior."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Final

import pytest
from fixtures.fake_model_source import (
    AccessOutcome,
    CredentialCase,
    CredentialScenario,
    credential_scenario,
)
from holm_control_api.model_source_routes import ModelSourceCredentialRequest
from holm_control_api.model_source_validation import (
    ModelSourceValidationResult,
    ModelSourceValidatorRegistry,
)
from holm_domain import (
    AuditAction,
    ModelSourceType,
    ModelSourceValidationAction,
    ModelSourceValidationReason,
    ModelSourceValidationStatus,
)
from holm_security import build_audit_details
from pydantic import ValidationError

_OUTCOME_RESULTS: Final = {
    AccessOutcome.ACCESSIBLE: ModelSourceValidationResult(
        ModelSourceValidationStatus.ACCESSIBLE,
        ModelSourceValidationReason.CREDENTIAL_VALID,
        None,
    ),
    AccessOutcome.INVALID_CREDENTIAL: ModelSourceValidationResult(
        ModelSourceValidationStatus.INVALID_CREDENTIAL,
        ModelSourceValidationReason.CREDENTIAL_INVALID,
        ModelSourceValidationAction.UPDATE_CREDENTIAL,
    ),
    AccessOutcome.PERMISSION_DENIED: ModelSourceValidationResult(
        ModelSourceValidationStatus.PERMISSION_DENIED,
        ModelSourceValidationReason.SOURCE_PERMISSION_DENIED,
        ModelSourceValidationAction.CHECK_SOURCE_ACCESS,
    ),
    AccessOutcome.NOT_FOUND: ModelSourceValidationResult(
        ModelSourceValidationStatus.NOT_FOUND,
        ModelSourceValidationReason.SOURCE_NOT_FOUND,
        ModelSourceValidationAction.CHECK_SOURCE_REFERENCE,
    ),
    # Keyed by ``None`` because the conclusion carries no verification status
    # at all (``CredentialScenario.outcome``); it is a key of its own, so the
    # lookup below stays total and no fixture outcome falls through.
    None: ModelSourceValidationResult(
        None,
        ModelSourceValidationReason.SOURCE_NOT_FOUND_OR_INACCESSIBLE,
        ModelSourceValidationAction.CHECK_SOURCE_REFERENCE_AND_CONFIGURE_CREDENTIAL,
    ),
    AccessOutcome.RATE_LIMITED: ModelSourceValidationResult(
        ModelSourceValidationStatus.RATE_LIMITED,
        ModelSourceValidationReason.SOURCE_RATE_LIMITED,
        ModelSourceValidationAction.RETRY_LATER,
    ),
}


@dataclass(frozen=True, slots=True)
class _FixtureDrivenValidator:
    scenario: CredentialScenario

    async def validate(self, *, credential: bytes | None) -> ModelSourceValidationResult:
        del credential
        return _OUTCOME_RESULTS[self.scenario.outcome]


def _validate_case(case: CredentialCase) -> ModelSourceValidationResult:
    validator = _FixtureDrivenValidator(credential_scenario(case))
    registry = ModelSourceValidatorRegistry({ModelSourceType.HUGGING_FACE: validator})
    return asyncio.run(
        registry.validate(ModelSourceType.HUGGING_FACE, credential=b"opaque-test-reference")
    )


def test_TC_SRC_04_write_schema_has_no_address_field_and_rejects_every_extra() -> None:
    assert set(ModelSourceCredentialRequest.model_fields) == {
        "source_type",
        "credential",
        "clear",
        "current_password",
        "confirm_unverified",
    }
    forbidden = ("endpoint", "base_url", "mirror_url", "proxy", "unknown_field")
    for field_name in forbidden:
        with pytest.raises(ValidationError) as failure:
            ModelSourceCredentialRequest.model_validate(
                {
                    "source_type": ModelSourceType.HUGGING_FACE.value,
                    "credential": "opaque-value",
                    "current_password": "opaque-password",
                    field_name: "https://invalid.example",
                }
            )
        assert failure.value.error_count() == 1


def test_TC_SRC_04_unknown_field_positive_control_proves_closed_schema_guard() -> None:
    payload = {
        "source_type": ModelSourceType.HUGGING_FACE.value,
        "credential": "opaque-value",
        "current_password": "opaque-password",
        "new_future_field": True,
    }
    with pytest.raises(ValidationError):
        ModelSourceCredentialRequest.model_validate(payload)

    permissive_fields = set(payload) - {"new_future_field"}
    with pytest.raises(AssertionError, match="unknown field was accepted"):
        assert set(payload) <= permissive_fields, "unknown field was accepted"


def test_TC_SRC_06_fixture_behavior_matrix_is_total_stable_and_rate_limit_distinct() -> None:
    # Exact, not a subset: every mapped status plus the one conclusion that has
    # no status (``None``), and nothing else.
    assert set(_OUTCOME_RESULTS) == set(AccessOutcome) | {None}
    results = {case: _validate_case(case) for case in CredentialCase}

    assert results[CredentialCase.PUBLIC].status is ModelSourceValidationStatus.ACCESSIBLE
    assert (
        results[CredentialCase.PRIVATE_ACCESSIBLE].status is ModelSourceValidationStatus.ACCESSIBLE
    )
    assert results[CredentialCase.INVALID_CREDENTIAL] == results[CredentialCase.EXPIRED_CREDENTIAL]
    assert (
        results[CredentialCase.INVALID_CREDENTIAL].next_action
        is ModelSourceValidationAction.UPDATE_CREDENTIAL
    )
    assert results[CredentialCase.RATE_LIMITED].status is ModelSourceValidationStatus.RATE_LIMITED
    assert (
        results[CredentialCase.RATE_LIMITED].status
        is not ModelSourceValidationStatus.INVALID_CREDENTIAL
    )
    # Six distinct reason codes across the eight cases: credential_valid,
    # credential_invalid, source_permission_denied, source_not_found,
    # source_not_found_or_inaccessible and source_rate_limited.  Written out
    # rather than derived from ``AccessOutcome``, which no longer counts the
    # statusless conclusion.
    assert len({result.reason_code for result in results.values()}) == 6


def test_TC_SRC_06_unmapped_fixture_outcome_positive_control_fails_without_fallback() -> None:
    incomplete = dict(_OUTCOME_RESULTS)
    incomplete.pop(AccessOutcome.RATE_LIMITED)
    with pytest.raises(KeyError):
        _ = incomplete[credential_scenario(CredentialCase.RATE_LIMITED).outcome]


def test_TC_SRC_07_empty_registry_is_explicitly_unavailable_and_never_valid() -> None:
    result = asyncio.run(
        ModelSourceValidatorRegistry({}).validate(
            ModelSourceType.HUGGING_FACE,
            credential=b"opaque-test-reference",
        )
    )
    assert result.status is None
    assert result.reason_code is ModelSourceValidationReason.VALIDATOR_UNAVAILABLE
    assert result.next_action is ModelSourceValidationAction.INSTALL_VALIDATOR

    def assert_missing_is_not_valid(candidate: ModelSourceValidationResult) -> None:
        assert candidate.status is not ModelSourceValidationStatus.ACCESSIBLE

    assert_missing_is_not_valid(result)
    with pytest.raises(AssertionError):
        assert_missing_is_not_valid(_OUTCOME_RESULTS[AccessOutcome.ACCESSIBLE])


def test_TC_SRC_09_model_source_audit_fields_are_explicit_and_unknown_secret_is_dropped() -> None:
    details = build_audit_details(
        AuditAction.MODEL_SOURCE_CREDENTIAL_CHANGE.value,
        {
            "operation": "write",
            "reason_code": "credential_valid",
            "source_type": ModelSourceType.HUGGING_FACE.value,
            "validation_status": ModelSourceValidationStatus.ACCESSIBLE.value,
            "future_unapproved_field": "PWSSNTL-SRCTOK-TC-SRC-09-ABCDEF123456",
        },
    )
    assert details == {
        "operation": "write",
        "reason_code": "credential_valid",
        "source_type": ModelSourceType.HUGGING_FACE.value,
        "validation_status": ModelSourceValidationStatus.ACCESSIBLE.value,
    }

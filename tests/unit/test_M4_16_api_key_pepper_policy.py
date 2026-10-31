"""M4-16 unit coverage for Pepper deletion policy and safe startup errors."""

from __future__ import annotations

import pytest
from holm_security import (
    API_KEY_PEPPER_MISSING_ERROR_CODE,
    ApiKeyPepperKeyring,
    ApiKeyPepperRemovalReason,
    ApiKeyPepperStartupError,
    assess_api_key_pepper_keyring,
    issue_api_key,
    require_api_key_pepper_references_available,
)


def _keyring() -> ApiKeyPepperKeyring:
    return ApiKeyPepperKeyring(
        current_key_id="pepper-b",
        keys={"pepper-a": b"A" * 32, "pepper-b": b"B" * 32},
    )


def test_M4_16_deletion_policy_blocks_current_and_referenced_peppers() -> None:
    assessment = assess_api_key_pepper_keyring(
        _keyring(),
        {"pepper-a": 2, "pepper-b": 1},
    )

    old = assessment.for_key("pepper-a")
    current = assessment.for_key("pepper-b")
    assert (old.can_remove, old.reason, old.active_reference_count) == (
        False,
        ApiKeyPepperRemovalReason.ACTIVE_KEY_REFERENCES,
        2,
    )
    assert (current.can_remove, current.reason, current.active_reference_count) == (
        False,
        ApiKeyPepperRemovalReason.CURRENT_PEPPER,
        1,
    )


def test_M4_16_deletion_policy_allows_unreferenced_non_current_pepper() -> None:
    old = assess_api_key_pepper_keyring(_keyring(), {}).for_key("pepper-a")

    assert old.can_remove is True
    assert old.reason is None
    assert old.active_reference_count == 0


def test_M4_16_deletion_policy_returns_explicit_unknown_result() -> None:
    unknown = assess_api_key_pepper_keyring(_keyring(), {}).for_key("pepper-z")

    assert unknown.can_remove is False
    assert unknown.reason is ApiKeyPepperRemovalReason.PEPPER_NOT_FOUND


def test_M4_16_assessment_reports_only_positive_missing_references() -> None:
    assessment = assess_api_key_pepper_keyring(
        _keyring(),
        {"missing-active": 3, "missing-zero": 0},
    )

    assert assessment.missing_referenced_key_ids == ("missing-active",)


def test_M4_16_assessment_rejects_invalid_reference_counts() -> None:
    for invalid in (-1, True, 1.5):
        with pytest.raises(ValueError, match="non-negative integers"):
            assess_api_key_pepper_keyring(_keyring(), {"pepper-a": invalid})  # type: ignore[dict-item]


def test_M4_16_new_issuance_uses_only_current_pepper() -> None:
    issued = issue_api_key(_keyring())

    assert issued.pepper_key_id == "pepper-b"


def test_M4_16_startup_error_contains_only_stable_code_and_missing_ids() -> None:
    assessment = assess_api_key_pepper_keyring(
        _keyring(),
        {"missing-a": 1, "missing-b": 2},
    )

    with pytest.raises(ApiKeyPepperStartupError) as raised:
        require_api_key_pepper_references_available(assessment)

    assert raised.value.code == API_KEY_PEPPER_MISSING_ERROR_CODE
    assert str(raised.value) == "api_key_pepper_missing:missing-a,missing-b"
    assert "A" * 32 not in str(raised.value)
    assert "B" * 32 not in str(raised.value)

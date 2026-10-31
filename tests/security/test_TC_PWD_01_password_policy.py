from __future__ import annotations

import json

import pytest
from holm_security import (
    MINIMUM_COMMON_PASSWORD_ENTRIES,
    PasswordHasher,
    PasswordPolicyConfigurationError,
    PasswordPolicyContextError,
    PasswordPolicyError,
    PasswordPolicyReason,
    assess_user_password,
    issue_temporary_password,
    load_common_passwords,
)
from holm_security import password_policy as policy_module
from holm_security import passwords as passwords_module

USERNAME = "alice"
DISPLAY_NAME = "Alice Example"
STRONG_PASSPHRASE = "orchid maple zephyr quartz"
# One substitution away from USERNAME, and reachable by no cheaper rule:
# not a substring of the username, not its reverse, not a leet spelling.
EDIT_DISTANCE_ONLY_PASSWORD = "alicf-secure-2026"


def _reason_codes(password: str) -> set[PasswordPolicyReason]:
    return {
        violation.code
        for violation in assess_user_password(
            password,
            username=USERNAME,
            display_name=DISPLAY_NAME,
        ).violations
    }


def _assert_accepted(password: str) -> None:
    result = assess_user_password(password, username=USERNAME, display_name=DISPLAY_NAME)
    assert result.accepted, result.violations


def test_TC_PWD_01_dictionary_has_a_fail_loud_ten_thousand_entry_floor() -> None:
    assert len(policy_module.COMMON_PASSWORDS) >= MINIMUM_COMMON_PASSWORD_ENTRIES
    with pytest.raises(PasswordPolicyConfigurationError, match="fewer than"):
        load_common_passwords("")


def test_TC_PWD_02_weak_variants_are_rejected_without_rejecting_a_long_passphrase() -> None:
    for password in ("Password1!", "P@ssw0rd!", "password123", "PASSWORD", "passw0rd"):
        assert PasswordPolicyReason.COMMON_PASSWORD in _reason_codes(password)
    _assert_accepted(STRONG_PASSPHRASE)


def test_TC_PWD_03_all_match_mutation_makes_the_strong_control_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(policy_module, "_is_common_password", lambda _password: True)
    with pytest.raises(AssertionError):
        _assert_accepted(STRONG_PASSPHRASE)


def test_TC_PWD_04_similarity_context_is_mandatory_and_plain_substring_is_insufficient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the mandatory similarity context and the insufficiency of naive substring.

    The control below replaces ``_is_identity_similar`` with a forward-substring
    stand-in, so what it demonstrates is that plain containment cannot catch
    ``ecila-secure-2026`` -- a statement about the naive alternative, not about
    the production code. The production edit-distance branch is pinned instead by
    ``test_TC_PWD_04_edit_distance_alone_rejects_a_near_username_password``, whose
    control disables that branch inside the real code path.
    """
    with pytest.raises(PasswordPolicyContextError, match="username"):
        assess_user_password(STRONG_PASSPHRASE, username="", display_name=DISPLAY_NAME)
    with pytest.raises(PasswordPolicyContextError, match="display_name"):
        assess_user_password(STRONG_PASSPHRASE, username=USERNAME, display_name="")

    for similar in ("ecila-secure-2026", "Al1ce-secure-2026"):
        assert PasswordPolicyReason.SIMILAR_TO_USERNAME in _reason_codes(similar)

    monkeypatch.setattr(
        policy_module,
        "_is_identity_similar",
        lambda password, identity: (
            policy_module._normalize(identity) in policy_module._normalize(password)
        ),
    )
    with pytest.raises(AssertionError):
        assert PasswordPolicyReason.SIMILAR_TO_USERNAME in _reason_codes("ecila-secure-2026")


def test_TC_PWD_04_edit_distance_alone_rejects_a_near_username_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the production edit-distance branch through the real policy entry point.

    ``alicf-secure-2026`` is one substitution away from ``alice`` while being
    none of the shapes the cheaper rules already cover: it does not contain the
    username, nor its reverse, nor a leet spelling of it, and it is not in the
    vendored dictionary. So the sliding edit-distance window is the only rule
    that can explain its rejection.

    The positive control disables that branch inside the production code path by
    neutering the lower-level ``_edit_distance_at_most`` collaborator -- which is
    not the property under test -- instead of replacing ``_is_identity_similar``
    with a stand-in. Under it the assertion above must fail and the password must
    be accepted outright, proving no other rule silently keeps this test green.
    """
    normalized_username = policy_module._normalize(USERNAME)
    normalized_password = policy_module._normalize(EDIT_DISTANCE_ONLY_PASSWORD)
    assert normalized_username not in normalized_password
    assert normalized_username[::-1] not in normalized_password
    identity_key = policy_module._identity_key(USERNAME)
    candidate_key = policy_module._identity_key(EDIT_DISTANCE_ONLY_PASSWORD)
    assert identity_key not in candidate_key
    assert identity_key[::-1] not in candidate_key
    assert PasswordPolicyReason.COMMON_PASSWORD not in _reason_codes(EDIT_DISTANCE_ONLY_PASSWORD)

    assert PasswordPolicyReason.SIMILAR_TO_USERNAME in _reason_codes(EDIT_DISTANCE_ONLY_PASSWORD)

    monkeypatch.setattr(
        policy_module,
        "_edit_distance_at_most",
        lambda _left, _right, _limit: False,
    )
    with pytest.raises(AssertionError):
        assert PasswordPolicyReason.SIMILAR_TO_USERNAME in _reason_codes(
            EDIT_DISTANCE_ONLY_PASSWORD
        )
    _assert_accepted(EDIT_DISTANCE_ONLY_PASSWORD)


def test_TC_PWD_04_short_or_symbol_only_display_name_does_not_reject_every_password() -> None:
    for display_name in ("李", "😊"):
        result = assess_user_password(
            STRONG_PASSPHRASE,
            username=USERNAME,
            display_name=display_name,
        )
        assert result.accepted, result.violations


def test_TC_PWD_05_length_counts_characters_and_byte_count_mutation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cjk_password = "界" * 128
    _assert_accepted(cjk_password)
    monkeypatch.setattr(
        policy_module,
        "password_character_length",
        lambda password: len(password.encode("utf-8")),
    )
    with pytest.raises(AssertionError):
        _assert_accepted(cjk_password)


def test_TC_PWD_06_rejection_returns_all_reasons_and_structured_current_length() -> None:
    result = assess_user_password("alice", username=USERNAME, display_name=DISPLAY_NAME)
    assert not result.accepted
    assert len(result.violations) >= 2
    assert {violation.code for violation in result.violations} >= {
        PasswordPolicyReason.TOO_SHORT,
        PasswordPolicyReason.SIMILAR_TO_USERNAME,
    }
    length = next(
        violation
        for violation in result.violations
        if violation.code is PasswordPolicyReason.TOO_SHORT
    )
    assert length.as_dict() == {"code": "too_short", "current_length": 5}


def test_TC_PWD_07_policy_error_serialization_has_no_password_fragment() -> None:
    rejected = "PWSSNTL-PASSWD-TC-PWD-07-A1B2C3D4E5F6"
    with pytest.raises(PasswordPolicyError) as raised:
        PasswordHasher().hash_user_password(
            rejected,
            username=rejected[:16],
            display_name="Sentinel account",
        )
    serialized = json.dumps(
        [violation.as_dict() for violation in raised.value.violations],
        sort_keys=True,
    )
    fragment_starts = range(len(rejected) - 11)
    assert rejected not in serialized
    assert all(rejected[index : index + 12] not in serialized for index in fragment_starts)

    malicious = serialized + rejected[8:20]
    with pytest.raises(AssertionError):
        assert all(rejected[index : index + 12] not in malicious for index in fragment_starts)


def test_TC_PWD_08_generated_temporary_password_bypasses_user_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("temporary password issuance called user policy")

    monkeypatch.setattr(passwords_module, "require_user_password", reject_if_called)
    issued = issue_temporary_password()
    assert issued.must_change_password is True
    assert issued.password_hash.startswith("$argon2id$")

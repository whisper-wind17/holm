"""TC-SEC-16: identifier-neutral login backoff primitives."""

from __future__ import annotations

import pytest
from holm_security import (
    INITIAL_LOGIN_LOCK_SECONDS,
    LOGIN_FAILURE_THRESHOLD,
    MAXIMUM_LOGIN_LOCK_SECONDS,
    login_lock_seconds,
    login_principal_hash,
)


def test_TC_SEC_16_lock_starts_after_five_failures_and_doubles_to_the_cap() -> None:
    assert LOGIN_FAILURE_THRESHOLD == 5
    assert INITIAL_LOGIN_LOCK_SECONDS == 60
    assert MAXIMUM_LOGIN_LOCK_SECONDS == 15 * 60
    assert [login_lock_seconds(attempt) for attempt in range(1, 11)] == [
        0,
        0,
        0,
        0,
        60,
        120,
        240,
        480,
        900,
        900,
    ]


def test_TC_SEC_16_equivalent_username_forms_share_one_protection_digest() -> None:
    digests = {login_principal_hash(value) for value in ("Alice", " alice ", "ＡＬＩＣＥ", "ALICE")}
    assert len(digests) == 1
    digest = digests.pop()
    assert len(digest) == 64
    assert "alice" not in digest


@pytest.mark.parametrize("attempts", [-1, -100])
def test_TC_SEC_16_negative_failure_counts_are_rejected(attempts: int) -> None:
    with pytest.raises(ValueError):
        login_lock_seconds(attempts)


def test_TC_SEC_16_boolean_failure_count_is_not_treated_as_an_integer() -> None:
    with pytest.raises(TypeError):
        login_lock_seconds(True)


def test_TC_SEC_16_json_lone_surrogate_still_has_a_stable_protection_digest() -> None:
    digest = login_principal_hash("invalid-\ud800-identifier")
    assert len(digest) == 64
    assert digest == login_principal_hash("invalid-\ud800-identifier")

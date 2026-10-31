from holm_security import assess_user_password


def test_FIX_16_TC_PWD_05_seven_rejected_eight_accepted() -> None:
    # FX-FIX-16: synthetic non-dictionary passwords, unrelated to the identity.
    rejected = assess_user_password("Zq7!vR2", username="alice", display_name="Alice Example")
    assert not rejected.accepted
    assert [violation.as_dict() for violation in rejected.violations] == [
        {"code": "too_short", "current_length": 7}
    ]
    accepted = assess_user_password("Zq7!vR2x", username="alice", display_name="Alice Example")
    assert accepted.accepted
    assert accepted.violations == ()

"""TC-SEC-10: Argon2id hashing, temporary passwords and session/CSRF tokens.

These cases cover the cryptographic half of the account model. The database half
is ``tests/integration/test_TC_SEC_10_user_session_model.py``; the HTTP login
flow, cookie attributes, CSRF enforcement and lockout are M1-01C and are not
exercised anywhere yet.

Most cases deliberately hash with a *reduced but still OWASP-compliant* cost so
the suite stays fast. Exactly one case pays the full TDD 15.2 baseline, and it
is the one that asserts what the baseline is.
"""

from __future__ import annotations

import ast
import secrets
from pathlib import Path
from typing import Final

import pytest
from holm_security import temporary_passwords as temporary_passwords_module
from holm_security import tokens as tokens_module
from holm_security.passwords import (
    DEFAULT_ARGON2ID_PARAMETERS,
    DEFAULT_HASH_LENGTH_BYTES,
    DEFAULT_MEMORY_COST_KIB,
    DEFAULT_PARALLELISM,
    DEFAULT_SALT_LENGTH_BYTES,
    DEFAULT_TIME_COST,
    ENCODED_HASH_PREFIX,
    MAX_PASSWORD_BYTES,
    OWASP_MINIMUM_MEMORY_COST_KIB,
    OWASP_MINIMUM_TIME_COST,
    Argon2idParameters,
    PasswordHasher,
    PasswordHashError,
    parse_argon2id_parameters,
)
from holm_security.temporary_passwords import (
    DEFAULT_TEMPORARY_PASSWORD_ENTROPY_BITS,
    MINIMUM_TEMPORARY_PASSWORD_ENTROPY_BITS,
    TEMPORARY_PASSWORD_ALPHABET,
    TemporaryPasswordError,
    generate_temporary_password,
    issue_temporary_password,
    temporary_password_entropy_bits,
    temporary_password_length,
)
from holm_security.tokens import (
    CSRF_TOKEN_ENTROPY_BITS,
    SESSION_TOKEN_ENTROPY_BITS,
    TOKEN_HASH_LENGTH,
    TokenError,
    generate_session_token_pair,
    generate_token,
    hash_token,
    is_token_hash,
    token_matches_hash,
)

SECURITY_SOURCE_ROOT: Final[Path] = (
    Path(__file__).resolve().parents[2] / "packages" / "security" / "src"
)
#: Cheap enough to hash repeatedly, still at or above the OWASP floor the module
#: enforces, so it exercises the same code path as production parameters.
CHEAP: Final[Argon2idParameters] = Argon2idParameters(
    memory_cost_kib=OWASP_MINIMUM_MEMORY_COST_KIB,
    time_cost=OWASP_MINIMUM_TIME_COST,
    parallelism=1,
)
RECALIBRATED: Final[Argon2idParameters] = Argon2idParameters(
    memory_cost_kib=OWASP_MINIMUM_MEMORY_COST_KIB * 2,
    time_cost=OWASP_MINIMUM_TIME_COST + 1,
    parallelism=2,
)
PASSWORD: Final[str] = "correct horse battery staple"


# --------------------------------------------------------------- parameters


def test_TC_SEC_10_default_parameters_are_the_TDD_baseline() -> None:
    """TDD 15.2: m=64 MiB, t=3, p=1, and a 32-byte tag over a 16-byte salt."""
    assert DEFAULT_MEMORY_COST_KIB == 64 * 1024
    assert DEFAULT_TIME_COST == 3
    assert DEFAULT_PARALLELISM == 1
    assert DEFAULT_HASH_LENGTH_BYTES == 32
    assert DEFAULT_SALT_LENGTH_BYTES == 16
    assert Argon2idParameters() == DEFAULT_ARGON2ID_PARAMETERS

    encoded = PasswordHasher().hash(PASSWORD)
    assert encoded.startswith(ENCODED_HASH_PREFIX)
    assert encoded.startswith("$argon2id$v=19$m=65536,t=3,p=1$")
    assert parse_argon2id_parameters(encoded) == DEFAULT_ARGON2ID_PARAMETERS


@pytest.mark.parametrize(
    ("parameters", "code"),
    [
        ({"memory_cost_kib": OWASP_MINIMUM_MEMORY_COST_KIB - 1}, "memory_cost_below_owasp_minimum"),
        ({"time_cost": OWASP_MINIMUM_TIME_COST - 1}, "time_cost_below_owasp_minimum"),
        ({"parallelism": 0}, "invalid_parameters"),
        ({"hash_length_bytes": 31}, "hash_length_below_minimum"),
        ({"salt_length_bytes": 15}, "salt_length_below_minimum"),
    ],
)
def test_TC_SEC_10_parameters_below_the_owasp_floor_are_refused(
    parameters: dict[str, int], code: str
) -> None:
    """TDD 15.2 allows calibration but forbids dropping below OWASP minimum."""
    with pytest.raises(PasswordHashError) as raised:
        Argon2idParameters(**parameters)
    assert raised.value.code == code


def test_TC_SEC_10_recalibrated_hasher_still_verifies_old_hashes() -> None:
    """The point of PHC encoding: recalibration must not lock existing users out.

    TDD 15.2 has the installer calibrate the cost to 250-500 ms on the target
    machine, which means the parameters change *after* accounts exist. Because
    the cost travels inside the stored hash, a hasher configured with the new
    parameters reads the old ones back out and verifies against them.
    """
    original = PasswordHasher(CHEAP)
    stored = original.hash(PASSWORD)
    assert parse_argon2id_parameters(stored) == CHEAP

    recalibrated = PasswordHasher(RECALIBRATED)
    assert recalibrated.parameters == RECALIBRATED
    assert recalibrated.parameters != CHEAP

    # The old hash still verifies, and a wrong password still fails.
    assert recalibrated.verify(stored, PASSWORD) is True
    assert recalibrated.verify(stored, PASSWORD + "!") is False
    # ...and the caller is told to upgrade it at the next successful login.
    assert recalibrated.needs_rehash(stored) is True

    upgraded = recalibrated.hash(PASSWORD)
    assert parse_argon2id_parameters(upgraded) == RECALIBRATED
    assert recalibrated.needs_rehash(upgraded) is False
    # A rollback of the calibration is symmetrical: the old hasher verifies the
    # new hash too, because neither one uses its own configuration to verify.
    assert original.verify(upgraded, PASSWORD) is True
    assert original.needs_rehash(upgraded) is True


def test_TC_SEC_10_every_hash_uses_a_fresh_salt() -> None:
    hasher = PasswordHasher(CHEAP)
    first = hasher.hash(PASSWORD)
    second = hasher.hash(PASSWORD)
    assert first != second
    assert hasher.verify(first, PASSWORD) is True
    assert hasher.verify(second, PASSWORD) is True


def test_TC_SEC_10_hash_never_contains_the_password() -> None:
    hasher = PasswordHasher(CHEAP)
    encoded = hasher.hash(PASSWORD)
    assert PASSWORD not in encoded
    for word in PASSWORD.split():
        assert word not in encoded


@pytest.mark.parametrize(
    "stored",
    [
        "",
        "not-a-hash",
        PASSWORD,
        "$argon2i$v=19$m=19456,t=2,p=1$c29tZXNhbHRzYWx0cw$c29tZXRhZ3ZhbHVlaGVyZQ",
        "$argon2id$v=16$m=19456,t=2,p=1$c29tZXNhbHRzYWx0cw$c29tZXRhZ3ZhbHVlaGVyZQ",
        "$argon2id$m=19456,t=2,p=1$c29tZXNhbHRzYWx0cw$c29tZXRhZ3ZhbHVlaGVyZQ",
    ],
)
def test_TC_SEC_10_a_corrupt_stored_hash_is_not_reported_as_a_wrong_password(
    stored: str,
) -> None:
    """A caller must be able to tell "wrong password" from "broken record"."""
    hasher = PasswordHasher(CHEAP)
    with pytest.raises(PasswordHashError) as raised:
        hasher.verify(stored, PASSWORD)
    assert raised.value.code in {"invalid_hash", "unsupported_argon2_version"}


@pytest.mark.parametrize(
    ("password", "code"),
    [("", "empty"), ("a" * (MAX_PASSWORD_BYTES + 1), "too_long")],
)
def test_TC_SEC_10_password_bounds_are_enforced(password: str, code: str) -> None:
    hasher = PasswordHasher(CHEAP)
    with pytest.raises(PasswordHashError) as raised:
        hasher.hash(password)
    assert raised.value.code == code


def test_TC_SEC_10_unicode_passwords_round_trip() -> None:
    hasher = PasswordHasher(CHEAP)
    password = "护城河-\U0001f510-passphrase"
    stored = hasher.hash(password)
    assert hasher.verify(stored, password) is True
    assert hasher.verify(stored, password + "x") is False


# ------------------------------------------------------- temporary passwords


def _security_sources() -> tuple[Path, ...]:
    sources = tuple(sorted(SECURITY_SOURCE_ROOT.rglob("*.py")))
    assert sources, "the security package source tree was not found"
    return sources


def test_TC_SEC_10_temporary_password_never_uses_the_random_module() -> None:
    """``random`` is a Mersenne Twister: 624 outputs reveal the whole state.

    TDD 15.2 says CSPRNG, so the guarantee is checked at the source level rather
    than inferred from behavior -- a statistical test cannot tell the two apart.
    """
    for source in _security_sources():
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name.split(".")[0] != "random" for alias in node.names), source
            elif isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] != "random", source


def test_TC_SEC_10_temporary_password_draws_from_the_secrets_module() -> None:
    assert temporary_passwords_module.secrets is secrets
    assert tokens_module.secrets is secrets

    source = (SECURITY_SOURCE_ROOT / "holm_security" / "temporary_passwords.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    generator = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "generate_temporary_password"
    )
    calls = {
        f"{call.func.value.id}.{call.func.attr}"
        for call in ast.walk(generator)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
    }
    assert "secrets.choice" in calls


def test_TC_SEC_10_temporary_password_entropy_is_at_least_128_bit() -> None:
    """TDD 15.2: 不少于 128 bit 熵."""
    assert MINIMUM_TEMPORARY_PASSWORD_ENTROPY_BITS == 128
    assert DEFAULT_TEMPORARY_PASSWORD_ENTROPY_BITS >= 128
    # 32 distinct symbols is exactly 5 bits each; the arithmetic below is exact.
    assert len(TEMPORARY_PASSWORD_ALPHABET) == 32
    assert len(set(TEMPORARY_PASSWORD_ALPHABET)) == 32

    password = generate_temporary_password()
    assert len(password) == temporary_password_length(DEFAULT_TEMPORARY_PASSWORD_ENTROPY_BITS)
    assert len(password) == 32
    assert temporary_password_entropy_bits(password) == 160.0
    assert temporary_password_entropy_bits(password) >= 128

    at_the_floor = generate_temporary_password(entropy_bits=128)
    assert temporary_password_entropy_bits(at_the_floor) >= 128


def test_TC_SEC_10_temporary_password_below_the_entropy_floor_is_refused() -> None:
    with pytest.raises(TemporaryPasswordError) as raised:
        generate_temporary_password(entropy_bits=127)
    assert raised.value.code == "entropy_below_minimum"


def test_TC_SEC_10_temporary_passwords_are_unique_and_cover_the_alphabet() -> None:
    """A smoke test for the draw itself; the CSPRNG proof is the source check."""
    drawn = [generate_temporary_password() for _ in range(256)]
    assert len(set(drawn)) == len(drawn)
    observed = set("".join(drawn))
    assert observed <= set(TEMPORARY_PASSWORD_ALPHABET)
    # 256 * 32 = 8192 draws over 32 symbols; a symbol missing entirely would
    # mean the draw is not uniform.
    assert observed == set(TEMPORARY_PASSWORD_ALPHABET)


def test_TC_SEC_10_issued_temporary_password_forces_a_change_and_stores_only_a_hash() -> None:
    issued = issue_temporary_password(PasswordHasher(CHEAP))

    assert issued.must_change_password is True
    assert issued.password_hash.startswith(ENCODED_HASH_PREFIX)
    assert issued.password not in issued.password_hash
    assert PasswordHasher(CHEAP).verify(issued.password_hash, issued.password) is True
    # The plaintext is excluded from repr, so a log line or traceback that
    # renders this object cannot carry the credential.
    assert issued.password not in repr(issued)


# -------------------------------------------------------------------- tokens


def test_TC_SEC_10_session_and_csrf_tokens_are_256_bit() -> None:
    """TDD 15.3: Session ID 使用 256 bit CSPRNG."""
    assert SESSION_TOKEN_ENTROPY_BITS == 256
    assert CSRF_TOKEN_ENTROPY_BITS == 256

    pair = generate_session_token_pair()
    assert pair.session_token != pair.csrf_token
    # token_urlsafe(32) is 32 random bytes rendered as unpadded base64url.
    for token in (pair.session_token, pair.csrf_token):
        assert len(token) >= 43

    assert len({generate_token() for _ in range(128)}) == 128


def test_TC_SEC_10_only_digests_leave_the_token_pair() -> None:
    pair = generate_session_token_pair()

    assert is_token_hash(pair.session_hash)
    assert is_token_hash(pair.csrf_hash)
    assert len(pair.session_hash) == TOKEN_HASH_LENGTH
    assert pair.session_hash != pair.csrf_hash
    assert pair.session_token not in pair.session_hash
    assert pair.csrf_token not in pair.csrf_hash
    # The plaintexts are excluded from repr for the same reason as above.
    assert pair.session_token not in repr(pair)
    assert pair.csrf_token not in repr(pair)


def test_TC_SEC_10_token_comparison_is_digest_based() -> None:
    pair = generate_session_token_pair()

    assert token_matches_hash(pair.session_token, pair.session_hash) is True
    assert token_matches_hash(pair.csrf_token, pair.session_hash) is False
    assert hash_token(pair.session_token) == pair.session_hash

    with pytest.raises(TokenError) as raised:
        token_matches_hash(pair.session_token, pair.session_token)
    assert raised.value.code == "invalid_token_hash"


@pytest.mark.parametrize("entropy_bits", [0, 64, 127])
def test_TC_SEC_10_low_entropy_tokens_are_refused(entropy_bits: int) -> None:
    with pytest.raises(TokenError) as raised:
        generate_token(entropy_bits=entropy_bits)
    assert raised.value.code == "entropy_below_minimum"


@pytest.mark.parametrize(
    "value",
    ["", "z" * 64, "A" * 64, "0" * 63, "0" * 65, "HOLM-PLAINTEXT-SESSION-TOKEN"],
)
def test_TC_SEC_10_a_plaintext_token_is_not_a_token_hash(value: str) -> None:
    """The same shape the database check constraint enforces on the columns."""
    assert is_token_hash(value) is False

"""TC-SEC-10: the normalization rule the ``username_norm`` UNIQUE index rests on.

The interesting cases are the *collisions*: pairs of names that look like one
account to a person and would be two rows if uniqueness were decided on the raw
text. Each group below is a way to register a second account that renders
identically to an existing one -- the case, width and combining-mark bypasses --
and each group has to collapse to a single normalized string.

Every non-ASCII character is written as an escape on purpose. A literal
full-width or decomposed character in a source file is invisible in review and
survives exactly one careless editor round trip, which would silently turn a
collision case into a trivially-passing one.

The database side of the same rule is
``tests/integration/test_TC_SEC_10_user_session_model.py``, which proves the
UNIQUE constraint really rejects the second insert.
"""

from __future__ import annotations

import unicodedata

import pytest
from holm_common.usernames import (
    DISPLAY_NAME_MAX_LENGTH,
    USERNAME_MAX_LENGTH,
    UsernameError,
    normalize_display_name,
    normalize_username,
)

#: Each group must collapse to the group's first entry.
COLLIDING_VARIANTS: tuple[tuple[str, ...], ...] = (
    # Plain ASCII case.
    ("alice", "Alice", "ALICE", "aLiCe"),
    # Surrounding whitespace: tab, newline, U+00A0 no-break space and U+3000
    # ideographic space. NFKC maps the last two onto an ordinary space, which
    # strip() then removes.
    ("bob", "  bob", "bob\t", " bob ", "\n bob \r", "\u00a0bob\u00a0", "\u3000bob"),
    # Full-width Latin letters (U+FF01..U+FF5E) versus their ASCII originals.
    ("carol", "\uff43\uff41\uff52\uff4f\uff4c", "\uff23\uff21\uff32\uff2f\uff2c"),
    # Combining marks: precomposed U+00E9 versus "e" + U+0301.
    ("ren\u00e9", "ren\u0065\u0301", "REN\u00c9", "REN\u0045\u0301"),
    # The "ffi" ligature U+FB03 has a compatibility decomposition to "ffi".
    ("office", "o\ufb03ce", "OFFICE", "O\uff26\uff26ICE"),
    # Full case folding, which str.lower() would not do: capital sharp s
    # (U+1E9E) and small sharp s (U+00DF) both fold to "ss".
    ("strasse", "STRA\u1e9eE", "stra\u00dfe", "STRASSE"),
    # Roman numeral four (U+2163) decomposes to the letters "IV".
    ("useriv", "user\u2163", "userIV", "USERIV"),
)

DISTINCT_NAMES: tuple[str, ...] = ("alice", "alicia", "alice1", "alice-b", "alice.b", "bob")


@pytest.mark.parametrize("variants", COLLIDING_VARIANTS)
def test_TC_SEC_10_visually_identical_names_normalize_to_one_value(
    variants: tuple[str, ...],
) -> None:
    normalized = {normalize_username(variant) for variant in variants}
    assert normalized == {variants[0]}, (variants, normalized)


def test_TC_SEC_10_distinct_names_stay_distinct() -> None:
    """Normalization must not over-collapse: different accounts stay different."""
    normalized = [normalize_username(name) for name in DISTINCT_NAMES]
    assert len(set(normalized)) == len(DISTINCT_NAMES)


@pytest.mark.parametrize("variants", [*COLLIDING_VARIANTS, DISTINCT_NAMES])
def test_TC_SEC_10_normalization_is_idempotent(variants: tuple[str, ...]) -> None:
    """Storing a normalized name and normalizing it again must not move it."""
    for variant in variants:
        once = normalize_username(variant)
        assert normalize_username(once) == once
        assert unicodedata.normalize("NFKC", once) == once
        assert once.casefold() == once


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ("\u3000 \u00a0", "empty"),
        # Inner whitespace: two names differing only by a space read as one.
        ("ali ce", "whitespace"),
        ("ali\tce", "whitespace"),
        ("ali\u00a0ce", "whitespace"),
        ("ali\u3000ce", "whitespace"),
        # Invisible characters that survive NFKC. A reader cannot tell these
        # apart from the name without them, so they must not be storable:
        # zero-width joiner, zero-width space, soft hyphen, right-to-left
        # override (which reverses how everything after it renders) and NUL.
        ("ali\u200dce", "invisible_character"),
        ("ali\u200bce", "invisible_character"),
        ("ali\u00adce", "invisible_character"),
        ("ali\u202ece", "invisible_character"),
        ("ali\u0000ce", "invisible_character"),
        ("a" * (USERNAME_MAX_LENGTH + 1), "too_long"),
        ("a" * (USERNAME_MAX_LENGTH * 10 + 1), "too_long"),
    ],
)
def test_TC_SEC_10_unsafe_names_are_refused(raw: str, code: str) -> None:
    with pytest.raises(UsernameError) as raised:
        normalize_username(raw)
    assert raised.value.code == code


def test_TC_SEC_10_maximum_length_is_measured_after_normalization() -> None:
    """A decomposed name is longer than its normalized form; the bound uses the latter."""
    decomposed = "e\u0301" * USERNAME_MAX_LENGTH
    assert len(decomposed) == USERNAME_MAX_LENGTH * 2
    assert len(normalize_username(decomposed)) == USERNAME_MAX_LENGTH


def test_TC_SEC_10_display_name_keeps_capitalization_and_inner_spaces() -> None:
    """Display names carry no uniqueness meaning, so they keep the operator's text."""
    assert normalize_display_name("  Alice Zhang  ") == "Alice Zhang"
    assert normalize_display_name("\uff21\uff2c\uff29\uff23\uff25") == "ALICE"
    assert normalize_username("AliceZhang") == "alicezhang"


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ("Alice\u200dZhang", "invisible_character"),
        ("a" * (DISPLAY_NAME_MAX_LENGTH + 1), "too_long"),
    ],
)
def test_TC_SEC_10_unsafe_display_names_are_refused(raw: str, code: str) -> None:
    with pytest.raises(UsernameError) as raised:
        normalize_display_name(raw)
    assert raised.value.code == code

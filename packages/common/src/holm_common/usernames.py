"""Canonical account-name normalization shared by every account consumer.

The uniqueness of a local account is decided on the *normalized* name, never on
what the operator typed. Without a single normalization rule, ``Alice``,
``alice``, ``ａｌｉｃｅ`` (full width) and ``alice`` written with a combining
acute accent are four different byte strings that all read as one person, and a
``UNIQUE`` constraint on the raw text would happily accept all four. That is the
account-collision class of bug this module exists to close, which is why the
rule lives here -- infrastructure independent, importable by the domain -- and
not inside the persistence adapter that happens to own the column.

The rule, in order:

1. Unicode NFKC. Compatibility composition folds full-width and other
   presentation variants onto their canonical code points and composes
   decomposed sequences, so ``Ａ`` and ``A``, and ``e``+U+0301 and ``é``,
   converge here.
2. Trim leading and trailing Unicode whitespace.
3. Reject any remaining whitespace, and reject control, format, surrogate,
   private-use and unassigned code points. Invisible characters inside a name
   are indistinguishable to a reader and would defeat step 1.
4. ``str.casefold`` -- the full Unicode case folding, not ``str.lower``: it also
   folds ``ẞ`` to ``ss`` and the final sigma onto the medial one.
5. NFKC again, because case folding can leave a string unnormalized.
6. Bound the result length.

Display names keep the operator's own capitalization and are normalized only
enough to stay renderable; they carry no uniqueness meaning.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final, Never

USERNAME_MAX_LENGTH: Final[int] = 64
DISPLAY_NAME_MAX_LENGTH: Final[int] = 128
#: Administrator creation and user creation apply one rule to the same kind
#: of account and explicitly forbid the two places from diverging, so the rule
#: has exactly one definition: 3-32 characters drawn from letters, digits, dot,
#: underscore and hyphen. It is applied to the *normalized* name, because that is
#: the value the UNIQUE constraint compares.
ACCOUNT_NAME_MIN_LENGTH: Final[int] = 3
ACCOUNT_NAME_MAX_LENGTH: Final[int] = 32
ACCOUNT_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9._-]+$")
#: Ten times the longest accepted name: enough headroom for any legitimate
#: decomposed form, small enough that a hostile input cannot make normalization
#: expensive before the length check runs.
_MAX_RAW_LENGTH: Final[int] = USERNAME_MAX_LENGTH * 10
_MAX_RAW_DISPLAY_LENGTH: Final[int] = DISPLAY_NAME_MAX_LENGTH * 10
#: Control (Cc), format (Cf), surrogate (Cs), private use (Co) and unassigned
#: (Cn) code points, plus the line/paragraph/space separators that survive NFKC.
_REJECTED_CATEGORIES: Final[frozenset[str]] = frozenset(
    {"Cc", "Cf", "Cn", "Co", "Cs", "Zl", "Zp", "Zs"}
)


class UsernameError(ValueError):
    """A rejected account name carrying a stable machine-readable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"account name rejected: {code}")


def _fail(code: str) -> Never:
    raise UsernameError(code)


def _reject_invisible(value: str) -> None:
    for character in value:
        if character.isspace():
            _fail("whitespace")
        if unicodedata.category(character) in _REJECTED_CATEGORIES:
            _fail("invisible_character")


def normalize_username(value: str) -> str:
    """Return the canonical form that ``core.users.username_norm`` stores."""
    if not isinstance(value, str):
        _fail("not_text")
    if len(value) > _MAX_RAW_LENGTH:
        _fail("too_long")

    composed = unicodedata.normalize("NFKC", value).strip()
    if not composed:
        _fail("empty")
    _reject_invisible(composed)

    normalized = unicodedata.normalize("NFKC", composed.casefold())
    if not normalized:
        _fail("empty")
    if len(normalized) > USERNAME_MAX_LENGTH:
        _fail("too_long")
    return normalized


def normalize_display_name(value: str) -> str:
    """Return a renderable display name; it never decides account uniqueness."""
    if not isinstance(value, str):
        _fail("not_text")
    if len(value) > _MAX_RAW_DISPLAY_LENGTH:
        _fail("too_long")

    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized:
        _fail("empty")
    for character in normalized:
        if unicodedata.category(character) in _REJECTED_CATEGORIES and character != " ":
            _fail("invisible_character")
    if len(normalized) > DISPLAY_NAME_MAX_LENGTH:
        _fail("too_long")
    return normalized


def normalize_account_name(value: str) -> str:
    """Return the canonical name of an account, or raise ``UsernameError``.

    This is :func:`normalize_username` plus the product-level shape rule. The
    two are separate because normalization decides *identity* (and must apply to
    every stored name, including names created before this rule existed) while
    the shape rule decides *acceptance* at the points where a human types one.
    """
    normalized = normalize_username(value)
    if len(normalized) < ACCOUNT_NAME_MIN_LENGTH:
        _fail("too_short")
    if len(normalized) > ACCOUNT_NAME_MAX_LENGTH:
        _fail("too_long")
    if ACCOUNT_NAME_PATTERN.match(normalized) is None:
        _fail("unsupported_characters")
    return normalized

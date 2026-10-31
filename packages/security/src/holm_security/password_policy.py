"""User password policy, the sole server-side rule definition.

This module evaluates passwords chosen by users. System-generated temporary
passwords deliberately bypass it: their security property is CSPRNG entropy,
and probabilistic dictionary rejection would make issuance nondeterministic.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from importlib.resources import files
from typing import Final

MINIMUM_PASSWORD_CHARACTERS: Final[int] = 8
MAXIMUM_PASSWORD_CHARACTERS: Final[int] = 128
MINIMUM_COMMON_PASSWORD_ENTRIES: Final[int] = 10_000
_VENDOR_HEADER_PREFIX: Final[str] = "# holm-vendor:"
_TRAILING_VARIATION: Final[re.Pattern[str]] = re.compile(r"[\W_\d]+$", re.UNICODE)
_LEET_TRANSLATION: Final[dict[int, str]] = str.maketrans(
    {
        "0": "o",
        "1": "i",
        "3": "e",
        "4": "a",
        "5": "s",
        "7": "t",
        "@": "a",
        "$": "s",
    }
)


class PasswordPolicyReason(StrEnum):
    """Stable wire codes; localized display text belongs to the web client."""

    TOO_SHORT = "too_short"
    TOO_LONG = "too_long"
    NOT_PRINTABLE = "not_printable"
    COMMON_PASSWORD = "common_password"
    SIMILAR_TO_USERNAME = "similar_to_username"
    SIMILAR_TO_DISPLAY_NAME = "similar_to_display_name"


@dataclass(frozen=True, slots=True)
class PasswordPolicyViolation:
    """One failed condition without retaining or echoing password material."""

    code: PasswordPolicyReason
    current_length: int | None = None

    def as_dict(self) -> dict[str, int | str]:
        result: dict[str, int | str] = {"code": self.code.value}
        if self.current_length is not None:
            result["current_length"] = self.current_length
        return result


@dataclass(frozen=True, slots=True)
class PasswordPolicyResult:
    """Complete evaluation so callers can show every missing condition at once."""

    violations: tuple[PasswordPolicyViolation, ...]

    @property
    def accepted(self) -> bool:
        return not self.violations


class PasswordPolicyError(ValueError):
    """Rejected user-selected password with structured, non-secret reasons."""

    def __init__(self, violations: tuple[PasswordPolicyViolation, ...]) -> None:
        if not violations:
            raise ValueError("password policy error requires at least one violation")
        self.violations = violations
        super().__init__("user-selected password rejected by policy")


class PasswordPolicyContextError(ValueError):
    """Required similarity context is absent or unusable."""


class PasswordPolicyConfigurationError(RuntimeError):
    """The vendored dictionary is absent, empty, or unexpectedly truncated."""


def password_character_length(password: str) -> int:
    """Count Unicode code points, independently of the Argon2 byte ceiling."""
    return len(password)


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def load_common_passwords(text: str) -> frozenset[str]:
    """Load vendored entries and fail loudly when the effective set is too small."""
    entries = frozenset(
        _normalize(value)
        for value in text.splitlines()
        if value and not value.startswith(_VENDOR_HEADER_PREFIX)
    )
    if len(entries) < MINIMUM_COMMON_PASSWORD_ENTRIES:
        raise PasswordPolicyConfigurationError(
            "common-password dictionary contains fewer than "
            f"{MINIMUM_COMMON_PASSWORD_ENTRIES} unique entries"
        )
    return entries


def _read_common_passwords() -> frozenset[str]:
    resource = files("holm_security").joinpath("resources/common-passwords.txt")
    try:
        text = resource.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as error:
        raise PasswordPolicyConfigurationError(
            "common-password dictionary is unavailable"
        ) from error
    return load_common_passwords(text)


COMMON_PASSWORDS: Final[frozenset[str]] = _read_common_passwords()


def assess_user_password(
    password: str,
    *,
    username: str,
    display_name: str,
) -> PasswordPolicyResult:
    """Evaluate every password policy condition without short-circuiting.

    ``username`` and ``display_name`` are mandatory security context. An empty
    display name must be replaced by the caller with the account's effective
    display label (normally its username), never with a value that skips the
    check.
    """
    if not isinstance(password, str):
        raise TypeError("password must be text")
    _require_identity_context("username", username)
    _require_identity_context("display_name", display_name)

    length = password_character_length(password)
    violations: list[PasswordPolicyViolation] = []
    if length < MINIMUM_PASSWORD_CHARACTERS:
        violations.append(
            PasswordPolicyViolation(PasswordPolicyReason.TOO_SHORT, current_length=length)
        )
    if length > MAXIMUM_PASSWORD_CHARACTERS:
        violations.append(
            PasswordPolicyViolation(PasswordPolicyReason.TOO_LONG, current_length=length)
        )
    if any(not character.isprintable() for character in password):
        violations.append(PasswordPolicyViolation(PasswordPolicyReason.NOT_PRINTABLE))
    if _is_common_password(password):
        violations.append(PasswordPolicyViolation(PasswordPolicyReason.COMMON_PASSWORD))
    if _is_identity_similar(password, username):
        violations.append(PasswordPolicyViolation(PasswordPolicyReason.SIMILAR_TO_USERNAME))
    if _is_identity_similar(password, display_name):
        violations.append(PasswordPolicyViolation(PasswordPolicyReason.SIMILAR_TO_DISPLAY_NAME))
    return PasswordPolicyResult(tuple(violations))


def require_user_password(
    password: str,
    *,
    username: str,
    display_name: str,
) -> None:
    """Raise with all stable reasons when a user-selected password is invalid."""
    result = assess_user_password(password, username=username, display_name=display_name)
    if not result.accepted:
        raise PasswordPolicyError(result.violations)


def _require_identity_context(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise PasswordPolicyContextError(f"{name} is required for password similarity checks")


def _is_common_password(password: str) -> bool:
    normalized = _normalize(password)
    leet_normalized = normalized.translate(_LEET_TRANSLATION)
    candidates = {normalized, leet_normalized}
    for candidate in tuple(candidates):
        stripped = _TRAILING_VARIATION.sub("", candidate)
        if len(stripped) >= 4:
            candidates.add(stripped)
    return any(candidate in COMMON_PASSWORDS for candidate in candidates)


def _identity_key(value: str) -> str:
    normalized = _normalize(value).translate(_LEET_TRANSLATION)
    return "".join(character for character in normalized if character.isalnum())


def _is_identity_similar(password: str, identity: str) -> bool:
    normalized_identity = _normalize(identity)
    normalized_password = _normalize(password)
    if (
        normalized_identity in normalized_password
        or normalized_identity[::-1] in normalized_password
    ):
        return True
    identity_key = _identity_key(identity)
    candidate = _identity_key(password)
    if not identity_key:
        return False
    if identity_key in candidate or identity_key[::-1] in candidate:
        return True
    if len(identity_key) < 3:
        return False
    maximum_distance = 1 if len(identity_key) <= 5 else 2
    minimum_width = max(1, len(identity_key) - maximum_distance)
    maximum_width = len(identity_key) + maximum_distance
    for width in range(minimum_width, maximum_width + 1):
        for start in range(0, len(candidate) - width + 1):
            if _edit_distance_at_most(
                candidate[start : start + width], identity_key, maximum_distance
            ):
                return True
    return False


def _edit_distance_at_most(left: str, right: str, limit: int) -> bool:
    if abs(len(left) - len(right)) > limit:
        return False
    previous = list(range(len(right) + 1))
    for left_index, left_character in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_character in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_character != right_character),
                )
            )
        if min(current) > limit:
            return False
        previous = current
    return previous[-1] <= limit

"""CSPRNG temporary passwords that the account must replace on first login.

"临时密码由 CSPRNG 生成不少于 128 bit 熵，首次登录强制修改；数据库只保存
Argon2id Hash." Three separate requirements, each with a mechanism here:

* **CSPRNG.** Only :mod:`secrets` is used. :mod:`random` is a Mersenne
    Twister: 624 consecutive outputs reveal its entire internal state, which
    makes every later password predictable. The security package imports
    :mod:`random` nowhere, and a test asserts that by parsing the source.
* **>= 128 bit.** Entropy is a property of the generator, not of the string
    that comes out, so the length is *derived* from the requested bit count and
    the alphabet size instead of being a hand-picked constant that someone can
    shorten later without noticing.
* **Forced change.** :class:`TemporaryPassword` reports
    ``must_change_password`` as a read-only ``True``. There is no code path that
    produces a temporary password with that flag unset.

The alphabet omits ``0/O`` and ``1/I`` because a temporary password is read off
a screen and typed somewhere else; a character an operator cannot transcribe
costs a support round trip and buys nothing.
"""

from __future__ import annotations

import math
import secrets
from dataclasses import dataclass, field
from typing import Final, Literal, Never

from holm_security.passwords import (
    DEFAULT_ARGON2ID_PARAMETERS,
    PasswordHasher,
)

#: 32 unambiguous characters: digits 2-9 and A-Z without I and O. Exactly 32
#: symbols means exactly 5 bits per character, so the entropy arithmetic below
#: is exact rather than rounded.
TEMPORARY_PASSWORD_ALPHABET: Final[str] = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
MINIMUM_TEMPORARY_PASSWORD_ENTROPY_BITS: Final[int] = 128
#: Above the 128-bit floor: a temporary password is transported by hand and may
#: sit in an operator's message history longer than anyone intends.
DEFAULT_TEMPORARY_PASSWORD_ENTROPY_BITS: Final[int] = 160


class TemporaryPasswordError(ValueError):
    """A rejected temporary-password request with a stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"temporary password rejected: {code}")


def _fail(code: str) -> Never:
    raise TemporaryPasswordError(code)


def temporary_password_length(
    entropy_bits: int = DEFAULT_TEMPORARY_PASSWORD_ENTROPY_BITS,
    alphabet: str = TEMPORARY_PASSWORD_ALPHABET,
) -> int:
    """Return the character count that reaches at least ``entropy_bits``."""
    if isinstance(entropy_bits, bool) or not isinstance(entropy_bits, int):
        _fail("invalid_entropy_bits")
    if entropy_bits < MINIMUM_TEMPORARY_PASSWORD_ENTROPY_BITS:
        _fail("entropy_below_minimum")
    if len(set(alphabet)) != len(alphabet) or len(alphabet) < 2:
        _fail("invalid_alphabet")
    return math.ceil(entropy_bits / math.log2(len(alphabet)))


def temporary_password_entropy_bits(
    password: str,
    alphabet: str = TEMPORARY_PASSWORD_ALPHABET,
) -> float:
    """Return the entropy a uniform draw of this length over ``alphabet`` has."""
    if len(set(alphabet)) != len(alphabet) or len(alphabet) < 2:
        _fail("invalid_alphabet")
    if not password or any(character not in alphabet for character in password):
        _fail("outside_alphabet")
    return len(password) * math.log2(len(alphabet))


def generate_temporary_password(
    *,
    entropy_bits: int = DEFAULT_TEMPORARY_PASSWORD_ENTROPY_BITS,
) -> str:
    """Draw a temporary password uniformly from the CSPRNG.

    ``secrets.choice`` draws from the operating system entropy source through
    ``random.SystemRandom``; it is rejection sampled, so no character is more
    likely than another.
    """
    length = temporary_password_length(entropy_bits)
    return "".join(secrets.choice(TEMPORARY_PASSWORD_ALPHABET) for _ in range(length))


@dataclass(frozen=True, slots=True)
class TemporaryPassword:
    """The one and only moment the plaintext exists outside the operator's hands.

    ``password`` is excluded from ``repr`` so an accidental log line, traceback
    frame or debugger dump of this object cannot carry it.
    """

    password: str = field(repr=False)
    password_hash: str

    @property
    def must_change_password(self) -> Literal[True]:
        """Always true: a temporary password is never a permanent credential."""
        return True


def issue_temporary_password(
    hasher: PasswordHasher | None = None,
    *,
    entropy_bits: int = DEFAULT_TEMPORARY_PASSWORD_ENTROPY_BITS,
) -> TemporaryPassword:
    """Generate a temporary password together with the only thing stored for it."""
    password_hasher = PasswordHasher(DEFAULT_ARGON2ID_PARAMETERS) if hasher is None else hasher
    password = generate_temporary_password(entropy_bits=entropy_bits)
    return TemporaryPassword(password=password, password_hash=password_hasher.hash(password))

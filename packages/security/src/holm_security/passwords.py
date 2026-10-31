"""Argon2id password hashing with parameters that travel inside the hash.

The default configuration fixes the baseline at ``m=64 MiB, t=3, p=1`` and then requires the
installer to calibrate it to 250-500 ms on the machine it lands on. Those two
sentences together are the whole reason this module looks the way it does: the
parameters *will* change after accounts already exist, so a verifier that used
its own configured cost would reject every password hashed before the
calibration.

The PHC string Argon2 emits carries the version, memory cost, time cost,
parallelism and salt of the hash that produced it::

    $argon2id$v=19$m=65536,t=3,p=1$<salt>$<tag>

Verification reads those values back out of the stored string, so a
:class:`PasswordHasher` configured with new parameters still verifies old
hashes; :meth:`PasswordHasher.needs_rehash` is what tells the caller to upgrade
one opportunistically at the next successful login. Nothing anywhere stores the
parameters in a second place, so there is no way for them to disagree.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Final, Never

import argon2
from argon2.exceptions import (
    HashingError,
    InvalidHashError,
    VerificationError,
    VerifyMismatchError,
)
from argon2.low_level import Type

from holm_security.password_policy import require_user_password

#: Default baseline. 65536 KiB is 64 MiB.
DEFAULT_MEMORY_COST_KIB: Final[int] = 65_536
DEFAULT_TIME_COST: Final[int] = 3
DEFAULT_PARALLELISM: Final[int] = 1
DEFAULT_HASH_LENGTH_BYTES: Final[int] = 32
DEFAULT_SALT_LENGTH_BYTES: Final[int] = 16

#: OWASP's lowest accepted Argon2id configuration (m=19 MiB, t=2, p=1). Calibration
#: allows installation-time calibration but forbids going below this floor, so
#: the floor is enforced here rather than left to whoever writes the config.
OWASP_MINIMUM_MEMORY_COST_KIB: Final[int] = 19_456
OWASP_MINIMUM_TIME_COST: Final[int] = 2
MINIMUM_PARALLELISM: Final[int] = 1
MINIMUM_HASH_LENGTH_BYTES: Final[int] = 32
MINIMUM_SALT_LENGTH_BYTES: Final[int] = 16

#: A hash that does not start with this prefix is not Argon2id and must never be
#: written to ``core.users.password_hash``; the column carries the same check.
ENCODED_HASH_PREFIX: Final[str] = "$argon2id$"
ARGON2_VERSION: Final[int] = 19
_AUTHENTICATION_SENTINEL: Final[str] = "holm-login-enumeration-equalizer"

#: Argon2 itself has no input length limit, but an unbounded password is an
#: unbounded amount of work for an unauthenticated caller. This byte ceiling is
#: intentionally separate from the 8-128 *character* policy.
MAX_PASSWORD_BYTES: Final[int] = 4_096

_ENCODED_HASH: Final[re.Pattern[str]] = re.compile(
    r"^\$argon2id\$v=(?P<version>\d+)"
    r"\$m=(?P<memory>\d+),t=(?P<time>\d+),p=(?P<parallelism>\d+)"
    r"(?:,keyid=[^$,]*)?(?:,data=[^$,]*)?"
    r"\$(?P<salt>[A-Za-z0-9+/]+)"
    r"\$(?P<tag>[A-Za-z0-9+/]+)$"
)


class PasswordHashError(ValueError):
    """A rejected password or stored hash, with a stable machine-readable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"password rejected: {code}")


def _fail(code: str) -> Never:
    raise PasswordHashError(code)


@dataclass(frozen=True, slots=True)
class Argon2idParameters:
    """One Argon2id cost configuration, validated against the OWASP floor."""

    memory_cost_kib: int = DEFAULT_MEMORY_COST_KIB
    time_cost: int = DEFAULT_TIME_COST
    parallelism: int = DEFAULT_PARALLELISM
    hash_length_bytes: int = DEFAULT_HASH_LENGTH_BYTES
    salt_length_bytes: int = DEFAULT_SALT_LENGTH_BYTES

    def __post_init__(self) -> None:
        for name in (
            "memory_cost_kib",
            "time_cost",
            "parallelism",
            "hash_length_bytes",
            "salt_length_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                _fail("invalid_parameters")
        if self.memory_cost_kib < OWASP_MINIMUM_MEMORY_COST_KIB:
            _fail("memory_cost_below_owasp_minimum")
        if self.time_cost < OWASP_MINIMUM_TIME_COST:
            _fail("time_cost_below_owasp_minimum")
        if self.parallelism < MINIMUM_PARALLELISM:
            _fail("invalid_parameters")
        if self.hash_length_bytes < MINIMUM_HASH_LENGTH_BYTES:
            _fail("hash_length_below_minimum")
        if self.salt_length_bytes < MINIMUM_SALT_LENGTH_BYTES:
            _fail("salt_length_below_minimum")


DEFAULT_ARGON2ID_PARAMETERS: Final[Argon2idParameters] = Argon2idParameters()


def _decode_unpadded_base64(value: str) -> bytes:
    """Argon2 emits base64 without ``=`` padding; restore it before decoding."""
    return base64.b64decode(value + "=" * (-len(value) % 4), validate=True)


def parse_argon2id_parameters(encoded_hash: str) -> Argon2idParameters:
    """Read back the parameters the given hash was produced with.

    This is the property the whole module rests on: the cost of an existing hash
    is a fact about that hash, not about the current configuration.
    """
    if not isinstance(encoded_hash, str):
        _fail("invalid_hash")
    match = _ENCODED_HASH.match(encoded_hash)
    if match is None:
        _fail("invalid_hash")
    if int(match.group("version")) != ARGON2_VERSION:
        _fail("unsupported_argon2_version")

    try:
        salt = _decode_unpadded_base64(match.group("salt"))
        tag = _decode_unpadded_base64(match.group("tag"))
    except (ValueError, TypeError):
        _fail("invalid_hash")

    return Argon2idParameters(
        memory_cost_kib=int(match.group("memory")),
        time_cost=int(match.group("time")),
        parallelism=int(match.group("parallelism")),
        hash_length_bytes=len(tag),
        salt_length_bytes=len(salt),
    )


def _validate_password(password: str) -> bytes:
    if not isinstance(password, str):
        _fail("not_text")
    encoded = password.encode("utf-8")
    if not encoded:
        _fail("empty")
    if len(encoded) > MAX_PASSWORD_BYTES:
        _fail("too_long")
    return encoded


class PasswordHasher:
    """Hash with the configured parameters; verify with the stored ones."""

    __slots__ = ("_hasher", "_parameters")

    def __init__(self, parameters: Argon2idParameters = DEFAULT_ARGON2ID_PARAMETERS) -> None:
        if not isinstance(parameters, Argon2idParameters):
            _fail("invalid_parameters")
        self._parameters = parameters
        self._hasher = argon2.PasswordHasher(
            time_cost=parameters.time_cost,
            memory_cost=parameters.memory_cost_kib,
            parallelism=parameters.parallelism,
            hash_len=parameters.hash_length_bytes,
            salt_len=parameters.salt_length_bytes,
            type=Type.ID,
        )

    @property
    def parameters(self) -> Argon2idParameters:
        """The parameters new hashes are produced with, not the stored ones."""
        return self._parameters

    def hash(self, password: str) -> str:
        """Hash trusted/system material without applying user-choice policy.

        Product code must use :meth:`hash_user_password`. This primitive stays
        available for generated temporary passwords and low-level hash tests;
        an AST gate rejects its use from application production code.
        """
        encoded = _validate_password(password)
        try:
            result = self._hasher.hash(encoded)
        except HashingError:
            _fail("hashing_failed")
        if not result.startswith(ENCODED_HASH_PREFIX):
            _fail("hashing_failed")
        return result

    def hash_user_password(
        self,
        password: str,
        *,
        username: str,
        display_name: str,
    ) -> str:
        """Enforce the password policy before hashing a password selected by a user."""
        require_user_password(password, username=username, display_name=display_name)
        return self.hash(password)

    def hash_authentication_sentinel(self) -> str:
        """Hash the fixed login timing sentinel without accepting caller input."""
        return self.hash(_AUTHENTICATION_SENTINEL)

    def verify(self, encoded_hash: str, password: str) -> bool:
        """Verify against the parameters embedded in ``encoded_hash``.

        Returns ``False`` for a wrong password and raises
        :class:`PasswordHashError` for a stored value that is not a usable
        Argon2id hash -- the caller must be able to tell "wrong password" from
        "corrupt record", and neither may be reported as the other.
        """
        # Reject the shape before handing anything to the C binding, so a
        # malformed stored value fails the same way regardless of the library.
        parse_argon2id_parameters(encoded_hash)
        candidate = _validate_password(password)
        try:
            return bool(self._hasher.verify(encoded_hash, candidate))
        except VerifyMismatchError:
            return False
        except InvalidHashError:
            _fail("invalid_hash")
        except VerificationError:
            return False

    def needs_rehash(self, encoded_hash: str) -> bool:
        """Whether the stored hash is weaker than the current configuration."""
        stored = parse_argon2id_parameters(encoded_hash)
        return (
            stored.memory_cost_kib != self._parameters.memory_cost_kib
            or stored.time_cost != self._parameters.time_cost
            or stored.parallelism != self._parameters.parallelism
            or stored.hash_length_bytes != self._parameters.hash_length_bytes
            or stored.salt_length_bytes != self._parameters.salt_length_bytes
        )

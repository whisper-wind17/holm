"""Session and CSRF tokens: high-entropy values stored only as digests.

Session security fixes two things about a browser session: the identifier is 256 bit
CSPRNG, and "只把 Hash 写数据库". The ``core.sessions`` row repeats it from the
other side -- "不保存 Cookie 明文". So the plaintext exists in exactly two
places: the ``Set-Cookie`` header on the response that created it, and the
request cookie the browser sends back. Never a column, never a log line.

The digest is a plain SHA-256, deliberately *not* Argon2id. A password is a
low-entropy human secret, so the stored form has to be expensive to attack
offline. A session token is 256 uniformly random bits: brute forcing it is
already infeasible, and paying 64 MiB of memory on every single request to look
one up would turn the session table into a denial-of-service surface. Nor is a
pepper needed here (``core.api_keys`` uses one because its lookup is by a public
identifier); the digest input is itself unguessable.

Comparison of a presented token against a stored digest goes through
``secrets.compare_digest`` so lookup time does not leak how much of a candidate
digest matched.
"""

from __future__ import annotations

import hashlib
import math
import secrets
from dataclasses import dataclass, field
from typing import Final, Never

#: Session ID is 256 bit CSPRNG. CSRF gets the same, because it is
#: also a bearer value compared against a stored digest.
SESSION_TOKEN_ENTROPY_BITS: Final[int] = 256
CSRF_TOKEN_ENTROPY_BITS: Final[int] = 256
MINIMUM_TOKEN_ENTROPY_BITS: Final[int] = 128

TOKEN_HASH_ALGORITHM: Final[str] = "sha256"
#: What ``core.sessions.session_hash`` and ``csrf_hash`` accept. Lowercase hex
#: of a fixed length is not just a convention: the database check constraint
#: built from it structurally cannot hold a URL-safe base64 token, so storing a
#: plaintext token by mistake is a write error rather than a silent leak.
TOKEN_HASH_LENGTH: Final[int] = 64
TOKEN_HASH_PATTERN: Final[str] = "^[0-9a-f]{64}$"

_HEX_DIGITS: Final[frozenset[str]] = frozenset("0123456789abcdef")


class TokenError(ValueError):
    """A rejected token or token digest with a stable machine-readable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"token rejected: {code}")


def _fail(code: str) -> Never:
    raise TokenError(code)


def generate_token(*, entropy_bits: int = SESSION_TOKEN_ENTROPY_BITS) -> str:
    """Return a URL-safe token carrying at least ``entropy_bits`` of CSPRNG output."""
    if isinstance(entropy_bits, bool) or not isinstance(entropy_bits, int):
        _fail("invalid_entropy_bits")
    if entropy_bits < MINIMUM_TOKEN_ENTROPY_BITS:
        _fail("entropy_below_minimum")
    return secrets.token_urlsafe(math.ceil(entropy_bits / 8))


def hash_token(token: str) -> str:
    """Return the lowercase hex SHA-256 digest that the database stores."""
    if not isinstance(token, str):
        _fail("not_text")
    if not token:
        _fail("empty")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_token_hash(value: str) -> bool:
    """Whether ``value`` has the exact shape the session columns accept."""
    return (
        isinstance(value, str)
        and len(value) == TOKEN_HASH_LENGTH
        and all(character in _HEX_DIGITS for character in value)
    )


def token_matches_hash(token: str, token_hash: str) -> bool:
    """Constant-time comparison of a presented token against a stored digest."""
    if not is_token_hash(token_hash):
        _fail("invalid_token_hash")
    return secrets.compare_digest(hash_token(token), token_hash)


@dataclass(frozen=True, slots=True)
class SessionTokenPair:
    """The plaintext session and CSRF tokens, alive for exactly one response.

    Both plaintexts are excluded from ``repr``: this object is the only thing
    that ever holds them, and it must not become the reason they reach a log.
    """

    session_token: str = field(repr=False)
    csrf_token: str = field(repr=False)

    @property
    def session_hash(self) -> str:
        """What ``core.sessions.session_hash`` stores."""
        return hash_token(self.session_token)

    @property
    def csrf_hash(self) -> str:
        """What ``core.sessions.csrf_hash`` stores."""
        return hash_token(self.csrf_token)


def generate_session_token_pair() -> SessionTokenPair:
    """Draw an independent session token and CSRF token from the CSPRNG."""
    return SessionTokenPair(
        session_token=generate_token(entropy_bits=SESSION_TOKEN_ENTROPY_BITS),
        csrf_token=generate_token(entropy_bits=CSRF_TOKEN_ENTROPY_BITS),
    )

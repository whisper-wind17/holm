"""Login-enumeration and exponential-backoff primitives.

The database stores protection state for both real and nonexistent login
identifiers.  The identifier itself is therefore represented by a fixed-size
digest, while the delay calculation stays framework and persistence agnostic.
"""

from __future__ import annotations

import hashlib
import unicodedata
from typing import Final

LOGIN_FAILURE_THRESHOLD: Final[int] = 5
INITIAL_LOGIN_LOCK_SECONDS: Final[int] = 60
MAXIMUM_LOGIN_LOCK_SECONDS: Final[int] = 15 * 60


def login_principal_hash(identifier: str) -> str:
    """Return the stable digest used to protect one submitted login name.

    Valid usernames are normalized more strictly by ``holm_common`` before
    account lookup.  This deliberately total function also accepts an invalid
    submitted name: invalid and nonexistent identifiers must still pay the same
    persisted lockout cost as a real account.
    """
    if not isinstance(identifier, str):
        raise TypeError("login identifier must be text")
    canonical = unicodedata.normalize("NFKC", identifier).strip().casefold()
    # JSON permits lone UTF-16 surrogate escapes even though valid account names
    # reject them. ``surrogatepass`` gives those invalid submissions a stable
    # byte representation so they still take the dummy-Argon and lockout path
    # instead of escaping through a cheap UnicodeEncodeError/500 oracle.
    return hashlib.sha256(canonical.encode("utf-8", errors="surrogatepass")).hexdigest()


def login_lock_seconds(failed_attempts: int) -> int:
    """Return the lock applied after ``failed_attempts`` consecutive failures."""
    if isinstance(failed_attempts, bool) or not isinstance(failed_attempts, int):
        raise TypeError("failed_attempts must be an integer")
    if failed_attempts < 0:
        raise ValueError("failed_attempts must not be negative")
    if failed_attempts < LOGIN_FAILURE_THRESHOLD:
        return 0
    exponent = min(failed_attempts - LOGIN_FAILURE_THRESHOLD, 4)
    return min(
        INITIAL_LOGIN_LOCK_SECONDS * (1 << exponent),
        MAXIMUM_LOGIN_LOCK_SECONDS,
    )


__all__ = [
    "INITIAL_LOGIN_LOCK_SECONDS",
    "LOGIN_FAILURE_THRESHOLD",
    "MAXIMUM_LOGIN_LOCK_SECONDS",
    "login_lock_seconds",
    "login_principal_hash",
]

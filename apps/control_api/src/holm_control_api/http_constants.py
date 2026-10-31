"""Single definitions for browser-authentication HTTP names and cache policy."""

from typing import Final

CACHE_CONTROL_VALUE: Final[str] = "no-store"
SESSION_COOKIE_NAME: Final[str] = "__Host-pws_session"
CSRF_COOKIE_NAME: Final[str] = "__Host-pws_csrf"
CSRF_HEADER_NAME: Final[str] = "X-CSRF-Token"

__all__ = [
    "CACHE_CONTROL_VALUE",
    "CSRF_COOKIE_NAME",
    "CSRF_HEADER_NAME",
    "SESSION_COOKIE_NAME",
]

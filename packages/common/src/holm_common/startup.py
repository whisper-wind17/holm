"""Sanitized startup diagnostics for project-owned error codes."""

from __future__ import annotations

import re


def startup_failure_code(error: Exception, known_types: tuple[type[Exception], ...]) -> str:
    if isinstance(error, known_types):
        code = getattr(error, "code", None)
        if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]*(?::[a-z][a-z0-9_]*)*", code):
            return code
    return type(error).__name__

"""Numeric dotted-version comparison shared by host and runtime checks."""

import re
from itertools import zip_longest


def compare_versions(current: str, minimum: str) -> int | None:
    """Return -1/0/1, or None for versions outside the 1–4 numeric segment format."""
    if any(re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,3}", value) is None for value in (current, minimum)):
        return None
    try:
        left = tuple(int(part) for part in current.split("."))
        right = tuple(int(part) for part in minimum.split("."))
    except ValueError:
        return None
    for a, b in zip_longest(left, right, fillvalue=0):
        if a != b:
            return 1 if a > b else -1
    return 0

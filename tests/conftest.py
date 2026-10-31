"""Make reusable test-only fixture packages importable during pytest collection."""

from __future__ import annotations

import sys
from pathlib import Path

_TESTS_ROOT = str(Path(__file__).resolve().parent)
if _TESTS_ROOT not in sys.path:
    sys.path.insert(0, _TESTS_ROOT)

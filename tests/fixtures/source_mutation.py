"""Apply a named mutation to a production source file, for positive controls.

A guard that is only ever run against correct code proves nothing.  The controls
in this repository therefore break the *production* source and require the guard
to fail -- feeding a hand-built structure to the checking function instead would
only prove that the checking function still runs (see ``LESSONS.md``: M1-09B,
M1-01G and M2-01B all shipped that mistake).

``mutate_source`` insists every anchor matches exactly once, so a control cannot
silently degrade into "the replacement never applied and the guard passed for
the ordinary reason".
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType


def mutate_source(path: Path, replacements: Sequence[tuple[str, str]]) -> str:
    """Return the file's text with each anchor replaced exactly once."""

    if not replacements:
        raise ValueError("a mutation must change something")
    text = path.read_text(encoding="utf-8")
    for anchor, replacement in replacements:
        occurrences = text.count(anchor)
        if occurrences != 1:
            raise AssertionError(
                f"anchor {anchor!r} matched {occurrences} times in {path.name}; "
                "the control would not have been applied"
            )
        text = text.replace(anchor, replacement)
    return text


def load_mutated_module(
    path: Path,
    replacements: Sequence[tuple[str, str]],
    *,
    module_name: str,
    destination: Path,
) -> ModuleType:
    """Import a mutated copy of a production module under its own name."""

    destination.write_text(mutate_source(path, replacements), encoding="utf-8")
    specification = importlib.util.spec_from_file_location(module_name, destination)
    if specification is None or specification.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load {destination}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[module_name] = module
    try:
        specification.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


__all__ = ["load_mutated_module", "mutate_source"]

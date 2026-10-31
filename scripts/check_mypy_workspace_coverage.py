"""Fail when strict mypy omits a uv workspace member's source tree."""

from __future__ import annotations

import argparse
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _workspace_members(manifest_path: Path) -> tuple[str, ...]:
    try:
        document: dict[str, Any] = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        raw_members = document["tool"]["uv"]["workspace"]["members"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"cannot read uv workspace members: {error}") from error
    if not isinstance(raw_members, list) or not raw_members:
        raise ValueError("uv workspace members must be a non-empty list")
    if not all(isinstance(member, str) and member for member in raw_members):
        raise ValueError("every uv workspace member must be a non-empty string")
    return tuple(raw_members)


def workspace_source_targets(manifest_path: Path) -> tuple[str, ...]:
    """Return normalized ``src`` targets declared by the uv workspace."""
    return tuple(
        (PurePosixPath(member) / "src").as_posix() for member in _workspace_members(manifest_path)
    )


def missing_workspace_sources(
    manifest_path: Path,
    mypy_targets: Sequence[str],
) -> tuple[str, ...]:
    """Return workspace ``src`` directories absent from the mypy target list."""
    normalized_targets = {PurePosixPath(target).as_posix().rstrip("/") for target in mypy_targets}
    return tuple(
        target
        for target in workspace_source_targets(manifest_path)
        if target not in normalized_targets
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "pyproject.toml",
        help="root pyproject.toml to inspect",
    )
    parser.add_argument("mypy_targets", nargs="+", help="source paths passed to mypy")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    manifest_path: Path = arguments.manifest
    mypy_targets: list[str] = arguments.mypy_targets
    try:
        expected = workspace_source_targets(manifest_path)
        missing = missing_workspace_sources(manifest_path, mypy_targets)
    except ValueError as error:
        print(
            f"ERROR: mypy workspace coverage guard cannot inspect the manifest: {error}",
            file=sys.stderr,
        )
        return 2
    if missing:
        print("ERROR: strict mypy is missing workspace member source target(s):", file=sys.stderr)
        for target in missing:
            print(f"  - {target}", file=sys.stderr)
        return 1

    print(f"strict mypy workspace coverage: {len(expected)}/{len(expected)} member src targets")
    for target in expected:
        print(f"  checked {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

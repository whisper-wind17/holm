"""Generate or verify System API JSON Schema snapshots."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Final

from holm_control_api import SystemComponentsResponse, SystemServerResponse
from pydantic import BaseModel

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
SNAPSHOT_ROOT: Final[Path] = ROOT / "tests" / "contract" / "snapshots"
MODELS: Final[dict[str, type[BaseModel]]] = {
    "m1_09a_system_components.json": SystemComponentsResponse,
    "m1_09a_system_server.json": SystemServerResponse,
}


def _render(model: type[BaseModel]) -> str:
    return json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"


def write_snapshots() -> None:
    SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)
    for filename, model in MODELS.items():
        (SNAPSHOT_ROOT / filename).write_text(_render(model), encoding="utf-8")


def check_snapshots(snapshot_root: Path = SNAPSHOT_ROOT) -> None:
    mismatches = [
        filename
        for filename, model in MODELS.items()
        if not (snapshot_root / filename).is_file()
        or (snapshot_root / filename).read_text(encoding="utf-8") != _render(model)
    ]
    if mismatches:
        raise RuntimeError(f"System schema snapshot drift: {', '.join(sorted(mismatches))}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--stdout", choices=tuple(MODELS))
    parser.add_argument("--check-against", type=Path, metavar="DIR")
    return parser.parse_args()


def main() -> int:
    arguments = _parse_args()
    if arguments.stdout:
        print(_render(MODELS[arguments.stdout]), end="")
    elif arguments.write:
        write_snapshots()
    else:
        check_snapshots(arguments.check_against or SNAPSHOT_ROOT)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from None

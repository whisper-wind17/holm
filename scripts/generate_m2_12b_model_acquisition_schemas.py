"""Generate or verify model-acquisition API contract snapshots."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Final

from holm_contracts import CursorPage
from holm_control_api.download_command_routes import DownloadCommandResponse
from holm_control_api.model_acquisition_read_routes import (
    DownloadTaskItemResponse,
    ModelArtifactPageResponse,
)
from holm_domain.model_acquisition import DOWNLOAD_ERROR_STAGES
from pydantic import BaseModel

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
SNAPSHOT_ROOT: Final[Path] = ROOT / "tests" / "contract" / "snapshots"
MODELS: Final[dict[str, type[BaseModel]]] = {
    "m2_12b_download_tasks.json": CursorPage[DownloadTaskItemResponse],
    "m2_12b_model_artifacts.json": ModelArtifactPageResponse,
    "m2_12b_download_command.json": DownloadCommandResponse,
}
ERROR_CODES_FILENAME: Final[str] = "m2_12b_download_error_codes.json"


def _render_model(model: type[BaseModel]) -> str:
    return json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"


def _render_error_codes() -> str:
    values = sorted(member.value for member in DOWNLOAD_ERROR_STAGES)
    return json.dumps(values, indent=2, sort_keys=True) + "\n"


def _rendered_snapshots() -> dict[str, str]:
    return {
        **{filename: _render_model(model) for filename, model in MODELS.items()},
        ERROR_CODES_FILENAME: _render_error_codes(),
    }


def write_snapshots() -> None:
    SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)
    for filename, content in _rendered_snapshots().items():
        (SNAPSHOT_ROOT / filename).write_text(content, encoding="utf-8")


def check_snapshots(snapshot_root: Path = SNAPSHOT_ROOT) -> None:
    mismatches = [
        filename
        for filename, content in _rendered_snapshots().items()
        if not (snapshot_root / filename).is_file()
        or (snapshot_root / filename).read_text(encoding="utf-8") != content
    ]
    if mismatches:
        raise RuntimeError(
            "model-acquisition schema snapshot drift: " + ", ".join(sorted(mismatches))
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check-against", type=Path, metavar="DIR")
    return parser.parse_args()


def main() -> int:
    arguments = _parse_args()
    if arguments.write:
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

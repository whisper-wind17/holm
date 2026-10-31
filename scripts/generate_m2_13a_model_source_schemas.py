"""Generate or verify model-source JSON Schema snapshots."""

from __future__ import annotations

import argparse
import json
import sys
from enum import Enum
from pathlib import Path
from typing import Final

from holm_control_api.model_source_routes import (
    ModelSourceCredentialRequest,
    ModelSourceResponse,
)
from holm_domain.model_acquisition import (
    ModelSourceConfigurationState,
    ModelSourceType,
    ModelSourceValidationAction,
    ModelSourceValidationReason,
    ModelSourceValidationStatus,
)
from pydantic import BaseModel, RootModel

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
SNAPSHOT_ROOT: Final[Path] = ROOT / "tests" / "contract" / "snapshots"


class ModelSourceListResponse(RootModel[list[ModelSourceResponse]]):
    """The complete response body returned by GET /system/model-sources."""


MODELS: Final[dict[str, type[BaseModel]]] = {
    "m2_13a_model_source_credential_request.json": ModelSourceCredentialRequest,
    "m2_13a_model_source_list_response.json": ModelSourceListResponse,
}
ENUMS: Final[dict[str, type[Enum]]] = {
    "ModelSourceConfigurationState": ModelSourceConfigurationState,
    "ModelSourceType": ModelSourceType,
    "ModelSourceValidationAction": ModelSourceValidationAction,
    "ModelSourceValidationReason": ModelSourceValidationReason,
    "ModelSourceValidationStatus": ModelSourceValidationStatus,
}
ENUM_SNAPSHOT: Final[str] = "m2_13a_model_source_enums.json"


def _render_model(model: type[BaseModel]) -> str:
    return json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"


def _enum_values() -> dict[str, list[str]]:
    return {
        name: [str(member.value) for member in enum_type]
        for name, enum_type in sorted(ENUMS.items())
    }


def _render_enums() -> str:
    return json.dumps(_enum_values(), indent=2, sort_keys=True) + "\n"


def write_snapshots() -> None:
    SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)
    for filename, model in MODELS.items():
        (SNAPSHOT_ROOT / filename).write_text(_render_model(model), encoding="utf-8")
    (SNAPSHOT_ROOT / ENUM_SNAPSHOT).write_text(_render_enums(), encoding="utf-8")


def check_snapshots(snapshot_root: Path = SNAPSHOT_ROOT) -> None:
    expected = {filename: _render_model(model) for filename, model in MODELS.items()}
    expected[ENUM_SNAPSHOT] = _render_enums()
    mismatches = [
        filename
        for filename, rendered in expected.items()
        if not (snapshot_root / filename).is_file()
        or (snapshot_root / filename).read_text(encoding="utf-8") != rendered
    ]
    if mismatches:
        raise RuntimeError(f"model-source schema snapshot drift: {', '.join(sorted(mismatches))}")


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

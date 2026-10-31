"""Generate or verify model-selection JSON Schema snapshots."""

from __future__ import annotations

import argparse
import json
import sys
from enum import Enum
from pathlib import Path
from typing import Final

from holm_contracts import CursorPage
from holm_control_api.download_creation_routes import DownloadCreatedResponse
from holm_control_api.model_catalog_routes import ModelCatalogItemResponse
from holm_control_api.model_registration_routes import (
    RegisteredModelResponse,
    RegistrationUnresolvedResponse,
)
from holm_control_api.model_resolution_routes import (
    ExternalPathAcceptedResponse,
    ResolveConclusion,
    ResolvedPreviewResponse,
    ResolveRequest,
    UnresolvedResponse,
)
from holm_domain import (
    LocalAvailability,
    MetadataKnowledgeState,
    MetadataStatus,
    RevisionImmutability,
)
from pydantic import BaseModel

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
SNAPSHOT_ROOT: Final[Path] = ROOT / "tests" / "contract" / "snapshots"
MODELS: Final[dict[str, type[BaseModel]]] = {
    "m2_13b_resolve_request.json": ResolveRequest,
    "m2_13b_resolved_preview_response.json": ResolvedPreviewResponse,
    "m2_13b_external_path_accepted_response.json": ExternalPathAcceptedResponse,
    "m2_13b_unresolved_response.json": UnresolvedResponse,
    "m2_13b_registered_model_response.json": RegisteredModelResponse,
    "m2_13b_registration_unresolved_response.json": RegistrationUnresolvedResponse,
    "m2_13b_download_created_response.json": DownloadCreatedResponse,
    "m2_13b_model_catalog_page.json": CursorPage[ModelCatalogItemResponse],
}
ENUMS: Final[dict[str, type[Enum]]] = {
    "LocalAvailability": LocalAvailability,
    "MetadataKnowledgeState": MetadataKnowledgeState,
    "MetadataStatus": MetadataStatus,
    "ResolveConclusion": ResolveConclusion,
    "RevisionImmutability": RevisionImmutability,
}
DISPOSITIONS: Final[tuple[str, ...]] = ("created", "joined_existing")
ENUM_SNAPSHOT: Final[str] = "m2_13b_model_selection_enums.json"


def _render_model(model: type[BaseModel]) -> str:
    return json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"


def _render_enums() -> str:
    values = {
        name: [str(member.value) for member in enum_type]
        for name, enum_type in sorted(ENUMS.items())
    }
    values["disposition"] = list(DISPOSITIONS)
    return json.dumps(values, indent=2, sort_keys=True) + "\n"


def _rendered_snapshots() -> dict[str, str]:
    return {
        **{filename: _render_model(model) for filename, model in MODELS.items()},
        ENUM_SNAPSHOT: _render_enums(),
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
            "model-selection schema snapshot drift: " + ", ".join(sorted(mismatches))
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

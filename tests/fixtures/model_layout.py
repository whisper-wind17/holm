"""FX-MS-FIX-23-LAYOUT: recorded test-environment listings, plus an unknown-listing variant."""

import json
from pathlib import Path

from holm_domain import (
    Identified,
    ModelOriginType,
    RemoteFile,
    ResolvedModel,
    RevisionImmutability,
    Unrecognized,
)

IMAGE_REPOSITORY = "Qwen/Qwen-Image-2.1"
TEXT_REPOSITORY = "Qwen/Qwen2.5-0.5B-Instruct"


def resolved_layout(repository: str, *, unknown: bool = False) -> ResolvedModel:
    fixture = json.loads((Path(__file__).parent / "modelscope/fx_ms_fix23_layout.json").read_text())
    assert fixture["fixture_id"] == "FX-MS-FIX-23-LAYOUT"
    files = tuple(RemoteFile(**entry) for entry in fixture["repositories"][repository])
    return ResolvedModel(
        source=ModelOriginType.MODELSCOPE,
        repository_id=repository,
        requested_revision_norm="@default",
        resolved_revision="master",
        revision_immutability=RevisionImmutability.MUTABLE_REFERENCE,
        files=Unrecognized() if unknown else Identified(files),
        total_size_bytes=Unrecognized()
        if unknown
        else Identified(sum(f.size_bytes for f in files)),
        architecture=Unrecognized(),
        modalities=Unrecognized(),
        precision=Unrecognized(),
        quantization=Unrecognized(),
        context_length=Unrecognized(),
        candidate_runtimes=Unrecognized(),
        license=Unrecognized(),
    )

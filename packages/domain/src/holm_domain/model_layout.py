"""Shared path-only deployment layout check."""

from collections.abc import Iterable
from enum import StrEnum
from typing import Final

from holm_domain.artifact_manifest import canonical_artifact_path

TOKENIZER_CONFIGURATION_NAMES: Final[frozenset[str]] = frozenset(
    {"tokenizer.json", "tokenizer_config.json", "tokenizer.model"}
)
WEIGHT_SUFFIXES: Final[tuple[str, ...]] = (
    ".safetensors",
    ".bin",
    ".pt",
    ".pth",
    ".gguf",
)


class DeployableLayout(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


def deployable_layout(paths: Iterable[str] | None) -> DeployableLayout:
    """Unknown listings pass through; known listings use the Verifying rule.

    Root configuration names are exact; weights may be at any depth. Content
    validation remains the Worker's responsibility after download.
    """
    if paths is None:
        return DeployableLayout.UNKNOWN
    scanned = frozenset(canonical_artifact_path(path) for path in paths)
    config_present = "config.json" in scanned
    tokenizer_present = any(name in scanned for name in TOKENIZER_CONFIGURATION_NAMES)
    weight_present = any(path.endswith(WEIGHT_SUFFIXES) for path in scanned)
    if not config_present or not tokenizer_present or not weight_present:
        return DeployableLayout.UNSUPPORTED
    return DeployableLayout.SUPPORTED

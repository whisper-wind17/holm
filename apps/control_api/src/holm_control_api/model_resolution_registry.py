"""The composition point for Resolve-preview Adapters.

``ModelResolutionRegistry`` itself knows nothing about any source: it
dispatches to whatever it is handed and answers with an explicit non-answer when
a source has no Adapter.  This module is where the build declares which
Adapters exist, so the set of supported sources is one
readable fact instead of something spread across call sites.

``Local Path`` is registered because ``realpath``, the symbolic-link
escape check and the allowed-root Allowlist are implemented and the Adapter is
genuinely consulted, so the registration is a fact rather than a placeholder.
Its Adapter needs the configured External Roots, which is why they are a
required argument here -- a build that cannot say which roots it allows must
not silently allow some.

``ModelScope`` is registered through its Hub API, and its answers are judged
from the numeric ``Code`` plus the HTTP
status.  It is a separate Adapter rather than the Hugging Face one pointed at
another address, because the two sources disagree about Revision immutability,
about what a 200 with no file listing means and about whether "not found" is
observable anonymously (see ``modelscope_resolution``).  A source with no
Adapter at all still answers with an explicit non-answer, which is the property
the failure vocabulary defines and which an empty registry keeps exercising.
"""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from holm_domain import ModelOriginType

from holm_control_api.external_path_resolution import (
    ExternalPathPolicy,
    ExternalPathResolutionAdapter,
)
from holm_control_api.hugging_face_resolution import (
    HUGGING_FACE_OFFICIAL_ENDPOINT,
    HuggingFaceResolutionAdapter,
)
from holm_control_api.model_resolution import (
    DEFAULT_RESOLUTION_TIMEOUT_SECONDS,
    ModelResolutionRegistry,
)
from holm_control_api.modelscope_resolution import (
    MODELSCOPE_OFFICIAL_ENDPOINT,
    ModelScopeResolutionAdapter,
)


def create_default_resolution_registry(
    *,
    external_model_roots: Sequence[Path],
    hugging_face_endpoint: str = HUGGING_FACE_OFFICIAL_ENDPOINT,
    modelscope_endpoint: str = MODELSCOPE_OFFICIAL_ENDPOINT,
    scratch_root: Path | None = None,
    executor: ThreadPoolExecutor | None = None,
    modelscope_executor: ThreadPoolExecutor | None = None,
    external_path_executor: ThreadPoolExecutor | None = None,
    timeout_seconds: float = DEFAULT_RESOLUTION_TIMEOUT_SECONDS,
) -> ModelResolutionRegistry:
    """Build the registry this build actually ships.

    ``external_model_roots`` is ``storage.external_model_roots`` and has no
    default: the allowed roots decide what the External Path Adapter accepts,
    and a forgotten argument must not resolve to a guess in either direction.
    """

    return ModelResolutionRegistry(
        {
            ModelOriginType.HUGGING_FACE: HuggingFaceResolutionAdapter(
                endpoint=hugging_face_endpoint,
                scratch_root=scratch_root,
                executor=executor,
                timeout_seconds=timeout_seconds,
            ),
            ModelOriginType.MODELSCOPE: ModelScopeResolutionAdapter(
                endpoint=modelscope_endpoint,
                executor=modelscope_executor,
                timeout_seconds=timeout_seconds,
            ),
        },
        external_path_adapters={
            ModelOriginType.LOCAL_PATH: ExternalPathResolutionAdapter(
                policy=ExternalPathPolicy.for_configured_roots(external_model_roots),
                executor=external_path_executor,
            )
        },
        timeout_seconds=timeout_seconds,
    )


__all__ = ["create_default_resolution_registry"]

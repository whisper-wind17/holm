"""The External Path Resolve-preview Adapter.

Path resolution uses four judgements for a Local Path.  Three of them are decidable
by the control plane alone:

1. ``realpath`` the input;
2. refuse a relative path and a path that does not exist;
3. refuse a symbolic-link escape and a path that is not inside one of the
   configured ``allowed_external_model_roots``.

Those three are **not defined here**.  We need the identical judgement on
the Host Agent, which must redo it rather than trust a caller. Two equivalent
copies would drift on symbolic-link semantics without
either side failing.  The rule therefore lives in
``holm_contracts.external_paths`` and both sides import it; this module
imports and re-exports it so the call sites and their tests keep one
name for it.

The fourth judgement -- "does the *Runtime's* UID have read access" -- is
structurally unreachable from this process. The capability boundary limits
the Control API
to the read-only ``GetHostInfo``, ``ListGPUs`` and ``StreamLogs`` methods;
none validates a path, ``ValidatePath`` belongs to ``download-worker``, and no
download is started by preview. So an accepted path carries
``RuntimeReadability.UNVERIFIED`` rather than
silence, an omitted field or an implied pass. The download worker performs
the Agent-side check later.

What remains here is the control plane's own use of the shared rule: a bounded
thread pool, the single collapsed refusal conclusion and the one accepted
outcome this build can reach.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Final

from holm_contracts.external_paths import (
    ExternalPathPolicy,
    ExternalPathRejectionRule,
    ExternalPathVerdict,
    judge_external_path,
    path_is_within,
)
from holm_domain import (
    ModelSourceValidationAction,
    ModelSourceValidationReason,
    ModelSourceValidationStatus,
)

from holm_control_api.model_resolution import (
    ExternalPathAccepted,
    ExternalPathRequest,
    ResolutionFailed,
    ResolutionOutcome,
    RuntimeReadability,
    RuntimeReadabilityUnverifiedCause,
)
from holm_control_api.model_source_validation import ModelSourceValidationResult

BLOCKING_WORKER_LIMIT: Final[int] = 4
"""Filesystem calls block, so they run off the ASGI event loop in a bounded
pool (the implementation conventions)."""


EXTERNAL_PATH_UNUSABLE_RESULT: Final[ModelSourceValidationResult] = ModelSourceValidationResult(
    status=ModelSourceValidationStatus.NOT_FOUND,
    reason_code=ModelSourceValidationReason.SOURCE_NOT_FOUND,
    next_action=ModelSourceValidationAction.CHECK_SOURCE_REFERENCE,
)
"""The single conclusion every refusal collapses onto.

All seven rules answer with this one result on purpose.  Each distinguishable
answer is one bit about a directory the caller cannot otherwise see,
so the response only gives the operator "this path is unusable" plus a direction, not
a map of the server."""

EXTERNAL_PATH_ACCEPTED_OUTCOME: Final[ExternalPathAccepted] = ExternalPathAccepted(
    control_plane_readable=True,
    runtime_readability=RuntimeReadability.UNVERIFIED,
    runtime_readability_cause=(
        RuntimeReadabilityUnverifiedCause.RUNTIME_IDENTITY_CHECK_UNAVAILABLE
    ),
)
"""The one accepted conclusion this build can reach."""


class ExternalPathResolutionAdapter:
    """One bounded, write-free judgement of a server path."""

    __slots__ = ("_executor", "_policy")

    def __init__(
        self,
        *,
        policy: ExternalPathPolicy,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        self._policy = policy
        self._executor = executor or ThreadPoolExecutor(
            max_workers=BLOCKING_WORKER_LIMIT,
            thread_name_prefix="external-path",
        )

    @property
    def policy(self) -> ExternalPathPolicy:
        return self._policy

    async def evaluate(self, request: ExternalPathRequest) -> ResolutionOutcome:
        loop = asyncio.get_running_loop()
        verdict = await loop.run_in_executor(
            self._executor,
            partial(judge_external_path, request.untrusted_path, self._policy),
        )
        if not verdict.accepted:
            return ResolutionFailed(EXTERNAL_PATH_UNUSABLE_RESULT)
        return EXTERNAL_PATH_ACCEPTED_OUTCOME


__all__ = [
    "BLOCKING_WORKER_LIMIT",
    "EXTERNAL_PATH_ACCEPTED_OUTCOME",
    "EXTERNAL_PATH_UNUSABLE_RESULT",
    "ExternalPathPolicy",
    "ExternalPathRejectionRule",
    "ExternalPathResolutionAdapter",
    "ExternalPathVerdict",
    "judge_external_path",
    "path_is_within",
]

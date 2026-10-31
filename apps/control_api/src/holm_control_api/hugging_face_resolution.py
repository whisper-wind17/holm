"""The Hugging Face Resolve-preview Adapter.

The Resolve contract supports an initially empty Adapter registry.  This module
fills in the ``Hugging Face`` key: it resolves a Branch/Tag to the immutable
Commit the Hub reports, asks the official SDK for a dry-run file plan without
writing a byte, and maps the source answer onto ``ResolvedModel``.

Three properties are structural rather than remembered:

* **The filter rules are one empty constant.**  The
  ``allow_patterns``/``ignore_patterns`` rules come from the Runtime
  compatibility configuration.  This adapter deliberately does not connect its
  catalog loader to the download path, so the honest download rule set stays
  empty.  Inventing a second list here would create another product truth source,
  and no user input reaches these parameters at all.
* **The dry run may not touch disk.**  It is pointed at a throwaway cache
  directory and ``assert_no_disk_residue`` fails the request if anything was
  written, so "the preview quietly started downloading" cannot pass unnoticed.
* **The credential exists only as a function argument.**  It is never written to
  ``HF_TOKEN``/``HUGGING_FACE_HUB_TOKEN``, to ``~/.cache/huggingface/token``, to
  argv or to an SDK cache configuration file (the credential privacy boundary).  When no
  credential is configured the SDK is told ``token=False`` -- "no auth" -- and
  not ``None``, which would let it pick an ambient token up from the
  environment or from the user's Hub login file.

Source conclusions are limited to what the Hub response actually distinguishes.
In particular, repository parsing has no invalid-credential branch: a 401 with
no ``GatedRepo`` code means only that the repository is missing or invisible.
Credential validity is judged separately by the authenticated ``whoami-v2``
endpoint.  The Hub's own English prose never reaches a response body, log,
exception, or audit detail (the privacy policy).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Final, assert_never

from holm_domain import (
    Identified,
    MetadataKnowledge,
    ModelOriginType,
    ModelSourceValidationAction,
    ModelSourceValidationReason,
    ModelSourceValidationStatus,
    RemoteFile,
    ResolvedModel,
    RevisionImmutability,
    Unrecognized,
)
from holm_persistence import DEFAULT_REQUESTED_REVISION
from huggingface_hub import HfApi, constants, get_session, hf_hub_url, snapshot_download
from huggingface_hub.errors import HfHubHTTPError
from huggingface_hub.hf_api import ModelInfo

# ``huggingface_hub.utils`` re-exports both of these but declares no ``__all__``,
# so a strict type checker cannot see them as public.  The alternative is
# importing from ``huggingface_hub.utils._headers``/``.tqdm`` directly, which
# reaches further into the package than this does.
from huggingface_hub.utils import (  # type: ignore[attr-defined]
    build_hf_headers,
    disable_progress_bars,
)

from holm_control_api.model_resolution import (
    DEFAULT_RESOLUTION_TIMEOUT_SECONDS,
    ResolutionFailed,
    ResolutionOutcome,
    ResolutionRequest,
    ResolutionSucceeded,
)
from holm_control_api.model_source_validation import (
    HUGGING_FACE_OFFICIAL_ENDPOINT,
    ModelSourceValidationResult,
    suppress_hugging_face_http_debug_logs,
)

"""The one address this Adapter talks to.  A request cannot influence it: the
Resolve request Schema has no Endpoint, Base URL, mirror or proxy field, and
this constant is the Adapter's default rather than something a caller supplies
per call."""

HUGGING_FACE_ALLOW_PATTERNS: Final[tuple[str, ...]] = ()
HUGGING_FACE_IGNORE_PATTERNS: Final[tuple[str, ...]] = ()
"""The complete download filter rule set, and its only definition source.

Empty is a conclusion, not a placeholder: These rules are derived from the
Runtime compatibility configuration, and that configuration declares no Runtime
yet.  Runtime integration must wire its parsed rules in here; until then any non-empty
literal would be a product rule this repository cannot justify."""

CONFIG_FILENAME: Final[str] = "config.json"
"""Preview may read a small set of configuration files; this is the only one."""

MAX_CONFIG_BYTES: Final[int] = 1 << 20
SNAPSHOT_MAX_WORKERS: Final[int] = 4
BLOCKING_WORKER_LIMIT: Final[int] = 4


class HuggingFaceResolutionError(RuntimeError):
    """One opaque failure.  It never carries the Hub's response text."""

    def __init__(self) -> None:
        super().__init__("hugging face resolution failed")


class DryRunWroteToDisk(RuntimeError):
    """A dry run left something behind, which means it was not a dry run."""


def assert_no_disk_residue(directory: Path) -> None:
    """Fail loudly if anything at all exists under ``directory``.

    Used by production after every dry run and by credential-disclosure positive
    control, so the check that guards the property and the check the test
    trusts are literally the same function.
    """

    residue = sorted(str(path.relative_to(directory)) for path in directory.rglob("*"))
    if residue:
        raise DryRunWroteToDisk(f"a dry run wrote {len(residue)} entries: {residue[:8]}")


def sdk_patterns(patterns: tuple[str, ...]) -> list[str] | None:
    """Project the platform's rule set onto the SDK's parameter.

    The SDK reads an empty list as "allow nothing" rather than "no rule", so an
    empty rule set has to be spelled ``None``.  Keeping that translation in one
    named function is what lets tests assert on the call site instead of
    on a downstream result.
    """

    return list(patterns) or None


@dataclass(frozen=True, slots=True)
class _SourceReading:
    """Everything one source round trip produced, before it becomes a preview."""

    info: ModelInfo
    commit: str
    files: tuple[RemoteFile, ...]
    configuration: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class _NotFoundOrInaccessible:
    """A 401 that cannot distinguish a missing repository from an invisible one."""


@dataclass(frozen=True, slots=True)
class _PermissionDenied:
    """A 401 carrying the stable ``GatedRepo`` source code."""


@dataclass(frozen=True, slots=True)
class _NotFound:
    """A 404 observed only while a credential was supplied."""


@dataclass(frozen=True, slots=True)
class _RateLimited:
    """A source throttle response, which must never look like a credential failure."""


@dataclass(frozen=True, slots=True)
class _UnclassifiedSourceFailure:
    """A failure for which the recordings authorize no more specific conclusion."""


type _SourceFailure = (
    _NotFoundOrInaccessible
    | _PermissionDenied
    | _NotFound
    | _RateLimited
    | _UnclassifiedSourceFailure
)
type _SourceReadOutcome = _SourceReading | _SourceFailure


_NOT_FOUND_OR_INACCESSIBLE_RESULT: Final[ModelSourceValidationResult] = ModelSourceValidationResult(
    status=None,
    reason_code=ModelSourceValidationReason.SOURCE_NOT_FOUND_OR_INACCESSIBLE,
    next_action=(ModelSourceValidationAction.CHECK_SOURCE_REFERENCE_AND_CONFIGURE_CREDENTIAL),
)
_PERMISSION_DENIED_RESULT: Final[ModelSourceValidationResult] = ModelSourceValidationResult(
    status=ModelSourceValidationStatus.PERMISSION_DENIED,
    reason_code=ModelSourceValidationReason.SOURCE_PERMISSION_DENIED,
    next_action=ModelSourceValidationAction.CHECK_SOURCE_ACCESS,
)
_NOT_FOUND_RESULT: Final[ModelSourceValidationResult] = ModelSourceValidationResult(
    status=ModelSourceValidationStatus.NOT_FOUND,
    reason_code=ModelSourceValidationReason.SOURCE_NOT_FOUND,
    next_action=ModelSourceValidationAction.CHECK_SOURCE_REFERENCE,
)
_RATE_LIMITED_RESULT: Final[ModelSourceValidationResult] = ModelSourceValidationResult(
    status=ModelSourceValidationStatus.RATE_LIMITED,
    reason_code=ModelSourceValidationReason.SOURCE_RATE_LIMITED,
    next_action=ModelSourceValidationAction.RETRY_LATER,
)


class HuggingFaceResolutionAdapter:
    """One bounded, side-effect-free metadata read against the Hugging Face Hub."""

    __slots__ = ("_endpoint", "_executor", "_scratch_root", "_timeout_seconds")

    def __init__(
        self,
        *,
        endpoint: str = HUGGING_FACE_OFFICIAL_ENDPOINT,
        scratch_root: Path | None = None,
        executor: ThreadPoolExecutor | None = None,
        timeout_seconds: float = DEFAULT_RESOLUTION_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("resolution timeout must be positive")
        suppress_hugging_face_http_debug_logs()
        # The SDK draws a terminal progress bar by default.  Nothing reads a
        # terminal inside the Control API, and the bar would only land in the
        # service log, so it is turned off once here rather than per call.
        disable_progress_bars()
        self._endpoint = endpoint
        self._scratch_root = scratch_root
        self._executor = executor or ThreadPoolExecutor(
            max_workers=BLOCKING_WORKER_LIMIT,
            thread_name_prefix="hf-resolve",
        )
        self._timeout_seconds = timeout_seconds

    @property
    def endpoint(self) -> str:
        return self._endpoint

    async def resolve(
        self,
        request: ResolutionRequest,
        *,
        credential: bytes | None,
    ) -> ResolutionOutcome:
        """Run the blocking SDK off the event loop and answer with a preview."""

        # ``False`` means "send no Authorization header"; ``None`` would let the
        # SDK fall back to an ambient token from the environment or the Hub
        # login file, which is exactly the thing that must never happen here.
        token: str | bool = False if credential is None else credential.decode("utf-8")
        loop = asyncio.get_running_loop()
        outcome = await loop.run_in_executor(
            self._executor,
            partial(self._resolve_blocking, request, token),
        )
        if isinstance(outcome, ResolutionFailed):
            return outcome
        return ResolutionSucceeded(outcome)

    # ----------------------------------------------------------- blocking half
    def _resolve_blocking(
        self,
        request: ResolutionRequest,
        token: str | bool,
    ) -> ResolvedModel | ResolutionFailed:
        reading = self._read_source(request, token)
        if not isinstance(reading, _SourceReading):
            return _render_source_failure(reading)
        info = reading.info
        commit = reading.commit
        files = reading.files
        return ResolvedModel(
            source=ModelOriginType.HUGGING_FACE,
            repository_id=request.repository_id,
            requested_revision_norm=request.requested_revision_norm,
            resolved_revision=commit,
            # The Hub resolves a Branch/Tag to a repository-level Commit, so
            # this source's reference is immutable and says so.
            revision_immutability=RevisionImmutability.IMMUTABLE,
            files=Identified(files),
            total_size_bytes=Identified(sum(item.size_bytes for item in files)),
            architecture=_architecture(info),
            # No Hub field states a modality; ``pipeline_tag`` is a task, and
            # the metadata contract forbids guessing a field the source did not confirm.
            modalities=Unrecognized(),
            precision=_precision(info),
            quantization=Unrecognized(),
            context_length=_context_length(reading.configuration),
            # Candidate Runtimes come from the same Runtime compatibility
            # configuration as the filter rules, and it declares none yet.
            candidate_runtimes=Unrecognized(),
            license=_license(info),
        )

    def _read_source(
        self,
        request: ResolutionRequest,
        token: str | bool,
    ) -> _SourceReadOutcome:
        """Return only the source facts the recorded status and error code distinguish."""

        try:
            api = HfApi(endpoint=self._endpoint, token=token)
            info = api.model_info(
                request.repository_id,
                revision=_requested_reference(request.requested_revision_norm),
                timeout=self._timeout_seconds,
            )
            commit = info.sha
            if not isinstance(commit, str) or not commit:
                return _UnclassifiedSourceFailure()
            files = self._file_plan(request.repository_id, commit, token)
            configuration = self._configuration(request.repository_id, commit, token)
        except DryRunWroteToDisk:
            # A preview that wrote to disk is a defect in this Adapter, not a
            # source refusal.  It stays loud instead of being folded into "the
            # source said no".
            raise
        except Exception as error:
            return _classify_source_failure(error, credential_supplied=token is not False)
        return _SourceReading(info=info, commit=commit, files=files, configuration=configuration)

    def _file_plan(
        self,
        repository_id: str,
        commit: str,
        token: str | bool,
    ) -> tuple[RemoteFile, ...]:
        """Ask the SDK what it *would* download, and prove it downloaded nothing."""

        with TemporaryDirectory(dir=self._scratch_root, prefix="hf-dry-run-") as scratch:
            plan = snapshot_download(
                repository_id,
                revision=commit,
                endpoint=self._endpoint,
                cache_dir=scratch,
                dry_run=True,
                token=token,
                allow_patterns=sdk_patterns(HUGGING_FACE_ALLOW_PATTERNS),
                ignore_patterns=sdk_patterns(HUGGING_FACE_IGNORE_PATTERNS),
                max_workers=SNAPSHOT_MAX_WORKERS,
                etag_timeout=self._timeout_seconds,
            )
            assert_no_disk_residue(Path(scratch))
        return tuple(
            sorted(
                (RemoteFile(path=item.filename, size_bytes=item.file_size) for item in plan),
                key=lambda item: item.path,
            )
        )

    def _configuration(
        self,
        repository_id: str,
        commit: str,
        token: str | bool,
    ) -> Mapping[str, Any] | None:
        """Read the one allowed metadata configuration file, in memory only."""

        url = hf_hub_url(
            repository_id,
            CONFIG_FILENAME,
            revision=commit,
            endpoint=self._endpoint,
        )
        response = get_session().get(
            url,
            headers=build_hf_headers(token=token),
            timeout=self._timeout_seconds,
            follow_redirects=True,
        )
        if response.status_code != 200 or len(response.content) > MAX_CONFIG_BYTES:
            return None
        try:
            document = response.json()
        except ValueError:
            return None
        return document if isinstance(document, dict) else None


def _classify_source_failure(
    error: Exception,
    *,
    credential_supplied: bool,
) -> _SourceFailure:
    """Classify from status plus ``x-error-code`` without retaining source prose."""

    if not isinstance(error, HfHubHTTPError):
        return _UnclassifiedSourceFailure()
    status_code = error.response.status_code
    error_code = error.response.headers.get("x-error-code")
    if status_code == 401 and error_code == "GatedRepo":
        return _PermissionDenied()
    if status_code == 401:
        return _NotFoundOrInaccessible()
    if status_code == 404 and credential_supplied:
        return _NotFound()
    if status_code == 429:
        return _RateLimited()
    return _UnclassifiedSourceFailure()


def _render_source_failure(failure: _SourceFailure) -> ResolutionFailed:
    """Map the closed parsing-failure union to the shared sanitized vocabulary."""

    if isinstance(failure, _NotFoundOrInaccessible):
        return ResolutionFailed(_NOT_FOUND_OR_INACCESSIBLE_RESULT)
    if isinstance(failure, _PermissionDenied):
        return ResolutionFailed(_PERMISSION_DENIED_RESULT)
    if isinstance(failure, _NotFound):
        return ResolutionFailed(_NOT_FOUND_RESULT)
    if isinstance(failure, _RateLimited):
        return ResolutionFailed(_RATE_LIMITED_RESULT)
    if isinstance(failure, _UnclassifiedSourceFailure):
        # Raised outside the SDK exception handler so the sanitized exception
        # has neither a cause nor a context carrying source prose.
        raise HuggingFaceResolutionError()
    assert_never(failure)


def _requested_reference(requested_revision_norm: str) -> str:
    """Project the platform's "未指定" sentinel onto the SDK's default Revision."""

    if requested_revision_norm == DEFAULT_REQUESTED_REVISION:
        return constants.DEFAULT_REVISION
    return requested_revision_norm


def _architecture(info: ModelInfo) -> MetadataKnowledge[str]:
    configuration = info.config or {}
    architectures = configuration.get("architectures")
    if isinstance(architectures, list) and len(architectures) == 1:
        first = architectures[0]
        if isinstance(first, str) and first:
            return Identified(first)
    return Unrecognized()


def _precision(info: ModelInfo) -> MetadataKnowledge[str]:
    safetensors = info.safetensors
    if safetensors is None:
        return Unrecognized()
    dtypes = [name for name, count in safetensors.parameters.items() if count]
    # More than one stored dtype means the platform cannot name "the" precision
    # without picking one, and picking one is guessing.
    if len(dtypes) == 1 and dtypes[0]:
        return Identified(dtypes[0])
    return Unrecognized()


def _license(info: ModelInfo) -> MetadataKnowledge[str]:
    card_data = info.card_data
    if card_data is None:
        return Unrecognized()
    value = card_data.get("license")
    if isinstance(value, str) and value:
        return Identified(value)
    return Unrecognized()


def _context_length(configuration: Mapping[str, Any] | None) -> MetadataKnowledge[int]:
    if configuration is None:
        return Unrecognized()
    value = configuration.get("max_position_embeddings")
    if "max_position_embeddings" not in configuration:
        text_config = configuration.get("text_config")
        if isinstance(text_config, dict):
            value = text_config.get("max_position_embeddings")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return Identified(value)
    return Unrecognized()


__all__ = [
    "BLOCKING_WORKER_LIMIT",
    "CONFIG_FILENAME",
    "HUGGING_FACE_ALLOW_PATTERNS",
    "HUGGING_FACE_IGNORE_PATTERNS",
    "HUGGING_FACE_OFFICIAL_ENDPOINT",
    "MAX_CONFIG_BYTES",
    "SNAPSHOT_MAX_WORKERS",
    "DryRunWroteToDisk",
    "HuggingFaceResolutionAdapter",
    "HuggingFaceResolutionError",
    "assert_no_disk_residue",
    "sdk_patterns",
]

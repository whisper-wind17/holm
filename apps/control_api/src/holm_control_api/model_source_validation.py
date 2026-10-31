"""Credential-validation port and fail-loud source registry."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Protocol

from holm_domain import (
    ModelSourceType,
    ModelSourceValidationAction,
    ModelSourceValidationReason,
    ModelSourceValidationStatus,
)
from huggingface_hub import get_session

# Same import path and reason as ``hugging_face_resolution``: ``huggingface_hub.utils``
# re-exports this but declares no ``__all__``, so a strict type checker cannot see it
# as public, and reaching into ``huggingface_hub.utils._headers`` would go deeper still.
from huggingface_hub.utils import build_hf_headers  # type: ignore[attr-defined]

from holm_control_api.modelscope_source import (
    MODELSCOPE_OFFICIAL_ENDPOINT,
    MODELSCOPE_USERS_INFO_PATH,
    ModelScopeCode,
    fetch_reply,
)

DEFAULT_VALIDATION_TIMEOUT_SECONDS: Final[float] = 10.0
HUGGING_FACE_OFFICIAL_ENDPOINT: Final[str] = "https://huggingface.co"
VALIDATION_WORKER_LIMIT: Final[int] = 2
_SOURCE_HTTP_LOGGERS: Final[tuple[str, ...]] = ("httpcore", "httpx")


def suppress_hugging_face_http_debug_logs() -> None:
    """Keep source response headers and bodies out of third-party debug records."""

    for logger_name in _SOURCE_HTTP_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.WARNING)


@dataclass(frozen=True, slots=True)
class ModelSourceValidationResult:
    """One sanitized conclusion; credential material has no return path."""

    status: ModelSourceValidationStatus | None
    reason_code: ModelSourceValidationReason
    next_action: ModelSourceValidationAction | None


class ModelSourceCredentialValidator(Protocol):
    """Future Adapter-owned single-attempt credential verification."""

    async def validate(
        self,
        *,
        credential: bytes | None,
    ) -> ModelSourceValidationResult: ...


_UNAVAILABLE_RESULT: Final[ModelSourceValidationResult] = ModelSourceValidationResult(
    status=None,
    reason_code=ModelSourceValidationReason.VALIDATOR_UNAVAILABLE,
    next_action=ModelSourceValidationAction.INSTALL_VALIDATOR,
)
_TIMEOUT_RESULT: Final[ModelSourceValidationResult] = ModelSourceValidationResult(
    status=None,
    reason_code=ModelSourceValidationReason.VALIDATION_TIMEOUT,
    next_action=ModelSourceValidationAction.RETRY_LATER,
)
_FAILED_RESULT: Final[ModelSourceValidationResult] = ModelSourceValidationResult(
    status=None,
    reason_code=ModelSourceValidationReason.VALIDATION_FAILED,
    next_action=ModelSourceValidationAction.RETRY_LATER,
)
_VALID_RESULT: Final[ModelSourceValidationResult] = ModelSourceValidationResult(
    status=ModelSourceValidationStatus.ACCESSIBLE,
    reason_code=ModelSourceValidationReason.CREDENTIAL_VALID,
    next_action=None,
)
_INVALID_RESULT: Final[ModelSourceValidationResult] = ModelSourceValidationResult(
    status=ModelSourceValidationStatus.INVALID_CREDENTIAL,
    reason_code=ModelSourceValidationReason.CREDENTIAL_INVALID,
    next_action=ModelSourceValidationAction.UPDATE_CREDENTIAL,
)


class HuggingFaceCredentialValidator:
    """Validate one credential only against the Hub's authenticated identity endpoint."""

    __slots__ = ("_endpoint", "_executor", "_timeout_seconds")

    def __init__(
        self,
        *,
        endpoint: str = HUGGING_FACE_OFFICIAL_ENDPOINT,
        executor: ThreadPoolExecutor | None = None,
        timeout_seconds: float = DEFAULT_VALIDATION_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("validation timeout must be positive")
        suppress_hugging_face_http_debug_logs()
        self._endpoint = endpoint.rstrip("/")
        self._executor = executor or ThreadPoolExecutor(
            max_workers=VALIDATION_WORKER_LIMIT,
            thread_name_prefix="hf-credential",
        )
        self._timeout_seconds = timeout_seconds

    async def validate(
        self,
        *,
        credential: bytes | None,
    ) -> ModelSourceValidationResult:
        """Treat only a 200 from ``whoami-v2`` as positive credential evidence."""

        token: str | bool = False if credential is None else credential.decode("utf-8")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._validate_blocking, token)

    def _validate_blocking(self, token: str | bool) -> ModelSourceValidationResult:
        try:
            response = get_session().get(
                f"{self._endpoint}/api/whoami-v2",
                headers=build_hf_headers(token=token),
                timeout=self._timeout_seconds,
            )
            status_code = response.status_code
        except Exception:
            return _FAILED_RESULT
        if status_code == 200:
            return _VALID_RESULT
        if status_code == 401:
            return _INVALID_RESULT
        return _FAILED_RESULT


class ModelScopeCredentialValidator:
    """Validate one credential only against ModelScope's authenticated endpoint.

    Same shape as the Hugging Face validator above and for the same reason:
    reading a *public* repository succeeds with
    an invalid token -- recorded source responses include a full 200 for exactly that -- so a
    successful parse is not evidence about a credential.  The only positive
    evidence is a success from an endpoint that requires authentication, and
    ``GET /api/v1/users/info`` is that endpoint.

    "A valid token is judged valid" has no recording: the recording session had
    no ModelScope account (``recorded/PROVENANCE.md``, known gaps).  Nothing is
    synthesized to fill it -- a made-up 200 would make the control that proves
    this validator is not a public-repository parse permanently green.  The
    unrecorded case therefore reaches ``VALIDATION_FAILED`` here and stays
    suspended in the suite (``BLOCKED_ENV``).
    """

    __slots__ = ("_endpoint", "_executor", "_timeout_seconds")

    def __init__(
        self,
        *,
        endpoint: str = MODELSCOPE_OFFICIAL_ENDPOINT,
        executor: ThreadPoolExecutor | None = None,
        timeout_seconds: float = DEFAULT_VALIDATION_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("validation timeout must be positive")
        suppress_hugging_face_http_debug_logs()
        self._endpoint = endpoint.rstrip("/")
        self._executor = executor or ThreadPoolExecutor(
            max_workers=VALIDATION_WORKER_LIMIT,
            thread_name_prefix="ms-credential",
        )
        self._timeout_seconds = timeout_seconds

    async def validate(
        self,
        *,
        credential: bytes | None,
    ) -> ModelSourceValidationResult:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._validate_blocking, credential)

    def _validate_blocking(self, credential: bytes | None) -> ModelSourceValidationResult:
        try:
            reply = fetch_reply(
                endpoint=self._endpoint,
                path=MODELSCOPE_USERS_INFO_PATH,
                params=None,
                credential=credential,
                timeout_seconds=self._timeout_seconds,
            )
        except Exception:
            return _FAILED_RESULT
        if reply.http_status == 200 and reply.code == ModelScopeCode.SUCCESS:
            return _VALID_RESULT
        if reply.http_status == 401 and reply.code == ModelScopeCode.USER_NOT_LOGGED_IN:
            # The recordings for "no credential" and "invalid credential" are
            # byte-identical apart from the ``RequestId``, so this conclusion is
            # the same for both -- a collapse of the source's, asserted rather
            # than papered over (authentication and transport failure must remain distinct).
            return _INVALID_RESULT
        return _FAILED_RESULT


class ModelSourceValidatorRegistry:
    """Dispatch one bounded attempt; absence is explicit and never valid."""

    __slots__ = ("_timeout_seconds", "_validators")

    def __init__(
        self,
        validators: Mapping[ModelSourceType, ModelSourceCredentialValidator] | None = None,
        *,
        hugging_face_endpoint: str = HUGGING_FACE_OFFICIAL_ENDPOINT,
        hugging_face_executor: ThreadPoolExecutor | None = None,
        modelscope_endpoint: str = MODELSCOPE_OFFICIAL_ENDPOINT,
        modelscope_executor: ThreadPoolExecutor | None = None,
        timeout_seconds: float = DEFAULT_VALIDATION_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("validation timeout must be positive")
        supplied: dict[ModelSourceType, ModelSourceCredentialValidator] = (
            {
                ModelSourceType.HUGGING_FACE: HuggingFaceCredentialValidator(
                    endpoint=hugging_face_endpoint,
                    executor=hugging_face_executor,
                    timeout_seconds=timeout_seconds,
                ),
                ModelSourceType.MODELSCOPE: ModelScopeCredentialValidator(
                    endpoint=modelscope_endpoint,
                    executor=modelscope_executor,
                    timeout_seconds=timeout_seconds,
                ),
            }
            if validators is None
            else dict(validators)
        )
        if any(not isinstance(source_type, ModelSourceType) for source_type in supplied):
            raise TypeError("validator registry keys must be ModelSourceType values")
        self._validators = MappingProxyType(supplied)
        self._timeout_seconds = timeout_seconds

    @property
    def registered_sources(self) -> frozenset[ModelSourceType]:
        """Exactly the sources backed by a credential validator in this registry."""

        return frozenset(self._validators)

    async def validate(
        self,
        source_type: ModelSourceType,
        *,
        credential: bytes | None,
    ) -> ModelSourceValidationResult:
        validator = self._validators.get(ModelSourceType(source_type))
        if validator is None:
            return _UNAVAILABLE_RESULT
        try:
            async with asyncio.timeout(self._timeout_seconds):
                return await validator.validate(credential=credential)
        except TimeoutError:
            return _TIMEOUT_RESULT
        except Exception:
            return _FAILED_RESULT


__all__ = [
    "DEFAULT_VALIDATION_TIMEOUT_SECONDS",
    "HUGGING_FACE_OFFICIAL_ENDPOINT",
    "MODELSCOPE_OFFICIAL_ENDPOINT",
    "VALIDATION_WORKER_LIMIT",
    "HuggingFaceCredentialValidator",
    "ModelScopeCredentialValidator",
    "ModelSourceCredentialValidator",
    "ModelSourceValidationResult",
    "ModelSourceValidatorRegistry",
    "suppress_hugging_face_http_debug_logs",
]

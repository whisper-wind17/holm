"""Spawn-isolated model-source resolution with sanitized IPC failures."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import re
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Final, Protocol

from holm_domain import JobExecutionKey, ModelOriginType, ResolvedModel
from holm_persistence import create_database_engine, create_session_factory
from holm_security import load_master_keyring

from holm_download_worker.configuration import load_database_url
from holm_download_worker.process_isolation import DEFAULT_COOPERATIVE_STOP_SECONDS
from holm_download_worker.process_isolation import (
    cooperatively_stop_process as _cooperatively_stop_process,
)
from holm_download_worker.process_isolation import (
    install_child_environment as _install_child_environment,
)
from holm_download_worker.process_isolation import (
    install_parent_death_fence as _install_parent_death_fence,
)
from holm_download_worker.process_isolation import (
    silence_child_stdio as _silence_child_stdio,
)
from holm_download_worker.process_isolation import stop_process as _stop_process

_POLL_SECONDS: Final[float] = 0.01
_SAFE_FAILURE_CODE: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9_]{0,127}")


@dataclass(frozen=True, slots=True, repr=False)
class ResolutionSubprocessRequest:
    source: ModelOriginType
    repository_id: str = field(repr=False)
    requested_revision_norm: str = field(repr=False)
    execution_key: JobExecutionKey

    def __repr__(self) -> str:
        return (
            "ResolutionSubprocessRequest("
            f"source={self.source.value!r}, repository_id=<redacted>, "
            "requested_revision_norm=<redacted>, execution_key=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class ResolutionProcessFailure:
    code: str

    def __post_init__(self) -> None:
        if _SAFE_FAILURE_CODE.fullmatch(self.code) is None:
            raise ValueError("resolution failure code is not a safe token")


type ResolutionProcessOutcome = ResolvedModel | ResolutionProcessFailure


class ResolutionProcessCallable(Protocol):
    def __call__(self, request: ResolutionSubprocessRequest) -> ResolutionProcessOutcome: ...


@dataclass(frozen=True, slots=True, repr=False)
class ProductionResolutionProcess:
    """Build and call the already-shipped Adapter registry inside the child."""

    database_url_file: Path = field(repr=False)
    master_keyring_file: Path = field(repr=False)
    external_model_roots: tuple[Path, ...] = ()
    scratch_root: Path | None = None
    hugging_face_endpoint: str = "https://huggingface.co"
    modelscope_endpoint: str = "https://www.modelscope.cn"

    def __call__(self, request: ResolutionSubprocessRequest) -> ResolutionProcessOutcome:
        return asyncio.run(self._resolve(request))

    async def _resolve(self, request: ResolutionSubprocessRequest) -> ResolutionProcessOutcome:
        # Keep SDK modules out of the Worker parent. Under ``spawn`` this import
        # happens only after the child environment has been reduced to its
        # fixed allowlist.
        from holm_control_api.model_resolution import ResolutionFailed, ResolutionSucceeded
        from holm_control_api.model_resolution_registry import (
            create_default_resolution_registry,
        )
        from holm_control_api.model_resolution_service import ModelResolutionService

        engine = create_database_engine(load_database_url(self.database_url_file))
        try:
            service = ModelResolutionService(
                session_factory=create_session_factory(engine),
                keyring=load_master_keyring(self.master_keyring_file),
                registry=create_default_resolution_registry(
                    external_model_roots=self.external_model_roots,
                    hugging_face_endpoint=self.hugging_face_endpoint,
                    modelscope_endpoint=self.modelscope_endpoint,
                    scratch_root=self.scratch_root,
                ),
            )
            preview = await service.resolve(
                source=request.source,
                repository_id=request.repository_id,
                external_path=None,
                revision=request.requested_revision_norm,
            )
            if isinstance(preview.outcome, ResolutionSucceeded):
                return preview.outcome.resolved
            if isinstance(preview.outcome, ResolutionFailed):
                reason = preview.outcome.result.reason_code
                return ResolutionProcessFailure(
                    "resolution_unavailable" if reason is None else reason.value
                )
            return ResolutionProcessFailure("resolution_process_invalid_result")
        finally:
            await engine.dispose()


@dataclass(frozen=True, slots=True, repr=False)
class _ResolutionProcessMessage:
    child_pid: int
    outcome: ResolutionProcessOutcome


@dataclass(frozen=True, slots=True, repr=False)
class ResolutionSubprocessResult:
    child_pid: int
    resolved: ResolvedModel = field(repr=False)


class ResolutionSubprocessError(RuntimeError):
    """A stable failure that never carries SDK prose, URLs, or credentials."""

    def __init__(self, code: str) -> None:
        self.code = code if _SAFE_FAILURE_CODE.fullmatch(code) else "resolution_process_failed"
        super().__init__("model source resolution failed")


def _resolution_process_main(
    sender: Connection,
    parent_pid: int,
    resolver: ResolutionProcessCallable,
    request: ResolutionSubprocessRequest,
) -> None:
    try:
        if not _install_parent_death_fence(parent_pid):
            sender.send(
                _ResolutionProcessMessage(
                    os.getpid(), ResolutionProcessFailure("parent_death_fence_unavailable")
                )
            )
            return
        _install_child_environment()
        _silence_child_stdio()
        try:
            outcome: object = resolver(request)
            if not isinstance(outcome, (ResolvedModel, ResolutionProcessFailure)):
                outcome = ResolutionProcessFailure("resolution_process_invalid_result")
        except Exception:
            outcome = ResolutionProcessFailure("resolution_process_failed")
        sender.send(_ResolutionProcessMessage(os.getpid(), outcome))
    finally:
        sender.close()


class SubprocessModelResolver:
    """Run exactly one resolution in a fresh ``spawn`` child process."""

    __slots__ = (
        "_cooperative_stop_seconds",
        "_resolver",
        "_timeout_seconds",
        "_last_child_pid",
    )

    def __init__(
        self,
        resolver: ResolutionProcessCallable,
        *,
        timeout_seconds: float = 15.0,
        cooperative_stop_seconds: float = DEFAULT_COOPERATIVE_STOP_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("resolution process timeout must be positive")
        if cooperative_stop_seconds <= 0:
            raise ValueError("cooperative stop timeout must be positive")
        self._resolver = resolver
        self._timeout_seconds = timeout_seconds
        self._cooperative_stop_seconds = cooperative_stop_seconds
        self._last_child_pid: int | None = None

    @property
    def last_child_pid(self) -> int | None:
        return self._last_child_pid

    async def resolve(self, request: ResolutionSubprocessRequest) -> ResolutionSubprocessResult:
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_resolution_process_main,
            args=(sender, os.getpid(), self._resolver, request),
            name="holm-model-resolution",
        )
        try:
            start_failed = False
            try:
                process.start()
            except Exception:
                start_failed = True
            if start_failed:
                process.close()
                raise ResolutionSubprocessError("resolution_process_start_failed")
            self._last_child_pid = process.pid
            sender.close()
            deadline = asyncio.get_running_loop().time() + self._timeout_seconds
            message: object | None = None
            while asyncio.get_running_loop().time() < deadline:
                if receiver.poll():
                    try:
                        message = receiver.recv()
                    except (EOFError, OSError):
                        message = None
                    break
                process.join(timeout=0)
                if not process.is_alive():
                    break
                await asyncio.sleep(_POLL_SECONDS)
            await asyncio.to_thread(_stop_process, process)
            exit_code = process.exitcode
            process.close()
        except asyncio.CancelledError:
            await asyncio.to_thread(
                _cooperatively_stop_process,
                process,
                self._cooperative_stop_seconds,
            )
            process.close()
            raise
        finally:
            sender.close()
            receiver.close()

        if not isinstance(message, _ResolutionProcessMessage):
            raise ResolutionSubprocessError("resolution_process_failed")
        if message.child_pid != self._last_child_pid or exit_code != 0:
            raise ResolutionSubprocessError("resolution_process_failed")
        outcome = message.outcome
        if isinstance(outcome, ResolutionProcessFailure):
            raise ResolutionSubprocessError(outcome.code)
        if (
            outcome.source is not request.source
            or outcome.repository_id != request.repository_id
            or outcome.requested_revision_norm != request.requested_revision_norm
        ):
            raise ResolutionSubprocessError("resolution_process_identity_mismatch")
        return ResolutionSubprocessResult(child_pid=message.child_pid, resolved=outcome)


__all__ = [
    "DEFAULT_COOPERATIVE_STOP_SECONDS",
    "ProductionResolutionProcess",
    "ResolutionProcessCallable",
    "ResolutionProcessFailure",
    "ResolutionSubprocessError",
    "ResolutionSubprocessRequest",
    "ResolutionSubprocessResult",
    "SubprocessModelResolver",
]

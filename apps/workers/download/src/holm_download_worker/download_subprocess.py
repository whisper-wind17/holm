"""Spawn-isolated source SDK download execution with sanitized progress IPC."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import re
import stat
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Final, Protocol

from holm_domain import (
    CredentialType,
    JobExecutionKey,
    ModelOriginType,
    ModelSourceType,
)
from holm_persistence import (
    ModelSourceRepository,
    create_database_engine,
    create_session_factory,
)
from holm_security import CredentialDecryptionError, load_master_keyring

from holm_download_worker.artifact_publishing import CACHEDIR_SIGNATURE
from holm_download_worker.configuration import (
    DEFAULT_DOWNLOAD_MAX_WORKERS,
    load_database_url,
)
from holm_download_worker.process_isolation import (
    DEFAULT_COOPERATIVE_STOP_SECONDS,
    cooperatively_stop_process,
    install_child_environment,
    install_parent_death_fence,
    install_staging_write_fence,
    silence_child_stdio,
    stop_process,
)

_POLL_SECONDS: Final[float] = 0.01
_SAFE_FAILURE_CODE: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9_]{0,127}")
_HUGGING_FACE_TRANSFER_PROGRESS_NAME: Final[str] = "huggingface_hub.snapshot_download.transfer"
_BYTECODE_WRITES_ENVIRONMENT_VARIABLE: Final[str] = "PYTHONDONTWRITEBYTECODE"
_CHILD_SPAWN_ENVIRONMENT_LOCK: Final = threading.Lock()


@dataclass(frozen=True, slots=True)
class DownloadFilterRules:
    """Future Runtime-derived SDK filters; the download path currently has none."""

    hugging_face_allow_patterns: tuple[str, ...] = ()
    hugging_face_ignore_patterns: tuple[str, ...] = ()
    modelscope_allow_patterns: tuple[str, ...] = ()
    modelscope_ignore_patterns: tuple[str, ...] = ()
    modelscope_allow_file_pattern: tuple[str, ...] = ()
    modelscope_ignore_file_pattern: tuple[str, ...] = ()


DOWNLOAD_FILTER_RULES: Final[DownloadFilterRules] = DownloadFilterRules()
"""The sole download-filter definition until a later card integrates the catalog."""


def _sdk_patterns(patterns: tuple[str, ...]) -> list[str] | None:
    """Spell the platform's empty rule set as the SDKs' ``None`` value."""

    return list(patterns) or None


def _modelscope_resumed_bytes(staging_path: Path, filename: str) -> int:
    """Count SDK partial files whose initial callback updates are not new bytes."""

    target = staging_path / filename
    candidates = [target.with_suffix(target.suffix + ".incomplete")]
    try:
        siblings = tuple(target.parent.iterdir())
    except OSError:
        siblings = ()
    parallel_prefix = f"{target.name}_"
    for candidate in siblings:
        suffix = candidate.name.removeprefix(parallel_prefix)
        start, separator, end = suffix.partition("_")
        if (
            candidate.name.startswith(parallel_prefix)
            and separator
            and start.isdigit()
            and end.isdigit()
        ):
            candidates.append(candidate)
    resumed = 0
    for candidate in candidates:
        try:
            metadata = candidate.lstat()
        except OSError:
            continue
        if stat.S_ISREG(metadata.st_mode):
            resumed += metadata.st_size
    return resumed


def _prepare_modelscope_staging_state(staging_path: Path) -> Path:
    """Keep ModelScope's operational cache and credential metadata in staging."""

    state_path = staging_path / ".cache" / "modelscope"
    state_path.mkdir(parents=True, exist_ok=True)
    (state_path / "CACHEDIR.TAG").write_bytes(CACHEDIR_SIGNATURE + b"\n")
    overrides = {
        "MODELSCOPE_CACHE": str(state_path / "cache"),
        "MODELSCOPE_HOME": str(state_path / "home"),
    }
    os.environ.update(overrides)
    return Path(overrides["MODELSCOPE_CACHE"])


def _clear_modelscope_staging_state() -> None:
    """Restore the fixed child environment after either SDK outcome."""

    os.environ.pop("MODELSCOPE_CACHE", None)
    os.environ.pop("MODELSCOPE_HOME", None)


@dataclass(frozen=True, slots=True, repr=False)
class DownloadSubprocessRequest:
    source: ModelOriginType
    repository_id: str = field(repr=False)
    resolved_revision: str = field(repr=False)
    staging_path: Path = field(repr=False)
    endpoint: str = field(repr=False)
    execution_key: JobExecutionKey = field(repr=False)

    def __repr__(self) -> str:
        return (
            "DownloadSubprocessRequest("
            f"source={self.source.value!r}, repository_id=<redacted>, "
            "resolved_revision=<redacted>, staging_path=<redacted>, "
            "endpoint=<redacted>, execution_key=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class DownloadProcessFailure:
    code: str

    def __post_init__(self) -> None:
        if _SAFE_FAILURE_CODE.fullmatch(self.code) is None:
            raise ValueError("download failure code is not a safe token")


@dataclass(frozen=True, slots=True)
class DownloadProcessCompleted:
    pass


type DownloadProcessOutcome = DownloadProcessCompleted | DownloadProcessFailure
type DownloadProgressCallback = Callable[[int], None]
type AsyncDownloadProgressCallback = Callable[[int], Awaitable[None]]


class DownloadProcessCallable(Protocol):
    def __call__(
        self,
        request: DownloadSubprocessRequest,
        progress: DownloadProgressCallback,
    ) -> DownloadProcessOutcome: ...


@dataclass(frozen=True, slots=True, repr=False)
class ProductionDownloadProcess:
    """Load a source credential and call the official SDK only inside the child."""

    database_url_file: Path = field(repr=False)
    master_keyring_file: Path = field(repr=False)
    max_workers: int = DEFAULT_DOWNLOAD_MAX_WORKERS
    filter_rules: DownloadFilterRules = DOWNLOAD_FILTER_RULES

    def __post_init__(self) -> None:
        if isinstance(self.max_workers, bool) or self.max_workers < 1:
            raise ValueError("download max_workers must be a positive integer")

    def __call__(
        self,
        request: DownloadSubprocessRequest,
        progress: DownloadProgressCallback,
    ) -> DownloadProcessOutcome:
        if request.source not in {
            ModelOriginType.HUGGING_FACE,
            ModelOriginType.MODELSCOPE,
        }:
            return DownloadProcessFailure("download_source_unsupported")
        try:
            credential = asyncio.run(self._credential(request.source))
        except Exception:
            return DownloadProcessFailure("download_credential_unavailable")
        token: str | bool | None = None
        try:
            if credential is not None:
                token = credential.decode("utf-8")
            if request.source is ModelOriginType.HUGGING_FACE:
                self._download_hugging_face(request, progress, token)
            elif request.source is ModelOriginType.MODELSCOPE:
                self._download_modelscope(request, progress, token)
        except Exception:
            return DownloadProcessFailure("download_process_failed")
        finally:
            _clear_modelscope_staging_state()
            credential = None
            token = False
        return DownloadProcessCompleted()

    def _download_hugging_face(
        self,
        request: DownloadSubprocessRequest,
        progress: DownloadProgressCallback,
        token: str | bool | None,
    ) -> None:
        # These imports happen only after ``install_child_environment`` has
        # reduced the child to its fixed allowlist. Credentials remain function
        # arguments and are never written to SDK environment or cache config.
        from huggingface_hub import snapshot_download
        from huggingface_hub.utils.tqdm import tqdm as hugging_face_tqdm

        reporter = progress

        class _DownloadProgress(hugging_face_tqdm):
            def __init__(self, *args: object, **kwargs: object) -> None:
                self._reports_transfer = kwargs.get("name") == _HUGGING_FACE_TRANSFER_PROGRESS_NAME
                super().__init__(*args, **kwargs)  # type: ignore[no-untyped-call]

            def update(self, amount: int | float | None = 1) -> None:
                super().update(amount)
                increment = int(amount or 0)
                if self._reports_transfer and increment > 0:
                    reporter(increment)

        assert_no_refusal = install_staging_write_fence(request.staging_path)
        snapshot_download(
            request.repository_id,
            revision=request.resolved_revision,
            local_dir=request.staging_path,
            token=False if token is None else token,
            endpoint=request.endpoint,
            max_workers=self.max_workers,
            allow_patterns=_sdk_patterns(self.filter_rules.hugging_face_allow_patterns),
            ignore_patterns=_sdk_patterns(self.filter_rules.hugging_face_ignore_patterns),
            tqdm_class=_DownloadProgress,
        )
        assert_no_refusal()

    def _download_modelscope(
        self,
        request: DownloadSubprocessRequest,
        progress: DownloadProgressCallback,
        token: str | bool | None,
    ) -> None:
        cache_path = _prepare_modelscope_staging_state(request.staging_path)
        from modelscope import snapshot_download  # type: ignore[import-untyped]
        from modelscope.hub.callback import ProgressCallback  # type: ignore[import-untyped]

        reporter = progress

        class _DownloadProgress(ProgressCallback):  # type: ignore[misc]
            def __init__(self, filename: str, file_size: int) -> None:
                super().__init__(filename, file_size)
                self._resumed_bytes = _modelscope_resumed_bytes(
                    request.staging_path,
                    filename,
                )
                self._resume_lock = threading.Lock()

            def update(self, size: int) -> None:
                if size <= 0:
                    return
                with self._resume_lock:
                    ignored = min(size, self._resumed_bytes)
                    self._resumed_bytes -= ignored
                transferred = size - ignored
                if transferred > 0:
                    reporter(transferred)

            def end(self) -> None:
                return

        assert_no_refusal = install_staging_write_fence(request.staging_path)
        snapshot_download(
            request.repository_id,
            revision=request.resolved_revision,
            local_dir=str(request.staging_path),
            cache_dir=cache_path,
            token=token if isinstance(token, str) else None,
            endpoint=request.endpoint,
            max_workers=self.max_workers,
            allow_patterns=_sdk_patterns(self.filter_rules.modelscope_allow_patterns),
            ignore_patterns=_sdk_patterns(self.filter_rules.modelscope_ignore_patterns),
            allow_file_pattern=_sdk_patterns(self.filter_rules.modelscope_allow_file_pattern),
            ignore_file_pattern=_sdk_patterns(self.filter_rules.modelscope_ignore_file_pattern),
            progress_callbacks=[_DownloadProgress],
        )
        assert_no_refusal()

    async def _credential(self, source: ModelOriginType) -> bytes | None:
        engine = create_database_engine(load_database_url(self.database_url_file))
        try:
            async with create_session_factory(engine)() as session:
                record = await ModelSourceRepository(session).get_by_type(
                    ModelSourceType(source.value)
                )
            if record is None or record.credential is None:
                return None
            keyring = load_master_keyring(self.master_keyring_file)
            if not keyring.can_decrypt(record.credential.key_id):
                raise CredentialDecryptionError()
            return keyring.decrypt(
                record.credential,
                source_id=record.id,
                credential_type=CredentialType.ACCESS_TOKEN.value,
            )
        finally:
            await engine.dispose()


@dataclass(frozen=True, slots=True)
class _DownloadProgressMessage:
    child_pid: int
    transferred_bytes: int


@dataclass(frozen=True, slots=True, repr=False)
class _DownloadEnvironmentMessage:
    child_pid: int
    environment: tuple[tuple[str, str], ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class _DownloadOutcomeMessage:
    child_pid: int
    outcome: DownloadProcessOutcome


@dataclass(frozen=True, slots=True)
class DownloadSubprocessResult:
    child_pid: int


class DownloadSubprocessError(RuntimeError):
    """A stable failure that never carries SDK prose, paths, or credentials."""

    def __init__(self, code: str) -> None:
        self.code = code if _SAFE_FAILURE_CODE.fullmatch(code) else "download_process_failed"
        super().__init__("model download failed")


def _download_process_main(
    sender: Connection,
    parent_pid: int,
    downloader: DownloadProcessCallable,
    request: DownloadSubprocessRequest,
    sample_environment: bool = False,
) -> None:
    try:
        if not install_parent_death_fence(parent_pid):
            sender.send(
                _DownloadOutcomeMessage(
                    os.getpid(), DownloadProcessFailure("parent_death_fence_unavailable")
                )
            )
            return
        install_child_environment()
        silence_child_stdio()
        send_lock = threading.Lock()

        def report(transferred_bytes: int) -> None:
            if transferred_bytes <= 0:
                return
            try:
                with send_lock:
                    sender.send(_DownloadProgressMessage(os.getpid(), transferred_bytes))
            except (BrokenPipeError, EOFError, OSError):
                return

        try:
            outcome: object = downloader(request, report)
            if not isinstance(outcome, (DownloadProcessCompleted, DownloadProcessFailure)):
                outcome = DownloadProcessFailure("download_process_invalid_result")
        except Exception:
            outcome = DownloadProcessFailure("download_process_failed")
        if sample_environment:
            with send_lock:
                sender.send(
                    _DownloadEnvironmentMessage(os.getpid(), tuple(os.environ.copy().items()))
                )
        sender.send(_DownloadOutcomeMessage(os.getpid(), outcome))
    finally:
        sender.close()


class SubprocessModelDownloader:
    """Run exactly one SDK download in a fresh ``spawn`` child process."""

    __slots__ = ("_cooperative_stop_seconds", "_downloader", "_last_child_pid")

    def __init__(
        self,
        downloader: DownloadProcessCallable,
        *,
        cooperative_stop_seconds: float = DEFAULT_COOPERATIVE_STOP_SECONDS,
    ) -> None:
        if cooperative_stop_seconds <= 0:
            raise ValueError("cooperative stop timeout must be positive")
        self._downloader = downloader
        self._cooperative_stop_seconds = cooperative_stop_seconds
        self._last_child_pid: int | None = None

    @property
    def last_child_pid(self) -> int | None:
        return self._last_child_pid

    async def download(
        self,
        request: DownloadSubprocessRequest,
        progress: AsyncDownloadProgressCallback,
        *,
        observe_child_environment: Callable[[tuple[tuple[str, str], ...]], None] | None = None,
    ) -> DownloadSubprocessResult:
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_download_process_main,
            args=(
                sender,
                os.getpid(),
                self._downloader,
                request,
                observe_child_environment is not None,
            ),
            name="holm-model-download",
        )
        outcome_message: _DownloadOutcomeMessage | None = None
        started = False
        try:
            try:
                # ``spawn`` imports Python modules before the child target can
                # disable bytecode writes.  Set the interpreter switch only for
                # the synchronous spawn window, then restore the parent exactly.
                with _CHILD_SPAWN_ENVIRONMENT_LOCK:
                    prior_bytecode_setting = os.environ.get(_BYTECODE_WRITES_ENVIRONMENT_VARIABLE)
                    os.environ[_BYTECODE_WRITES_ENVIRONMENT_VARIABLE] = "1"
                    try:
                        process.start()
                    finally:
                        if prior_bytecode_setting is None:
                            os.environ.pop(_BYTECODE_WRITES_ENVIRONMENT_VARIABLE, None)
                        else:
                            os.environ[_BYTECODE_WRITES_ENVIRONMENT_VARIABLE] = (
                                prior_bytecode_setting
                            )
                started = True
            except Exception:
                process.close()
                raise DownloadSubprocessError("download_process_start_failed") from None
            self._last_child_pid = process.pid
            sender.close()
            while True:
                if receiver.poll():
                    try:
                        message: object = receiver.recv()
                    except (EOFError, OSError):
                        break
                    if isinstance(message, _DownloadProgressMessage):
                        if message.child_pid != self._last_child_pid:
                            raise DownloadSubprocessError("download_process_identity_mismatch")
                        await progress(message.transferred_bytes)
                        continue
                    if isinstance(message, _DownloadEnvironmentMessage):
                        if message.child_pid != self._last_child_pid:
                            raise DownloadSubprocessError("download_process_identity_mismatch")
                        if observe_child_environment is not None:
                            observe_child_environment(message.environment)
                        continue
                    if isinstance(message, _DownloadOutcomeMessage):
                        outcome_message = message
                        break
                    raise DownloadSubprocessError("download_process_invalid_result")
                process.join(timeout=0)
                if not process.is_alive():
                    break
                await asyncio.sleep(_POLL_SECONDS)
            await asyncio.to_thread(stop_process, process)
            exit_code = process.exitcode
            process.close()
        except asyncio.CancelledError:
            if started:
                await asyncio.to_thread(
                    cooperatively_stop_process,
                    process,
                    self._cooperative_stop_seconds,
                )
                process.close()
            raise
        except BaseException:
            if started:
                await asyncio.to_thread(
                    cooperatively_stop_process,
                    process,
                    self._cooperative_stop_seconds,
                )
                process.close()
            raise
        finally:
            sender.close()
            receiver.close()

        if outcome_message is None:
            raise DownloadSubprocessError("download_process_failed")
        if outcome_message.child_pid != self._last_child_pid or exit_code != 0:
            raise DownloadSubprocessError("download_process_failed")
        if isinstance(outcome_message.outcome, DownloadProcessFailure):
            raise DownloadSubprocessError(outcome_message.outcome.code)
        return DownloadSubprocessResult(child_pid=outcome_message.child_pid)


__all__ = [
    "AsyncDownloadProgressCallback",
    "DOWNLOAD_FILTER_RULES",
    "DownloadFilterRules",
    "DownloadProcessCallable",
    "DownloadProcessCompleted",
    "DownloadProcessFailure",
    "DownloadSubprocessError",
    "DownloadSubprocessRequest",
    "DownloadSubprocessResult",
    "ProductionDownloadProcess",
    "SubprocessModelDownloader",
]

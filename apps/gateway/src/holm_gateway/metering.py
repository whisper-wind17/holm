"""Three-level Token metering that never delays a first event or a response.

Metering orders the sources of truth for one request's Token counts:

1. the Runtime's final Usage;
2. the Runtime's per-request Metrics, when the Runtime is certified for them;
3. an in-memory count from the verified Tokenizer Bundle published beside the
   Artifact.

Level 3 is the only one that costs CPU inside the Gateway, so every part of it -
Bundle load, verification, and counting - runs on a bounded worker pool that is
started *after* Runtime Dispatch and is never awaited by the streaming first
event or by the non-stream response path.  A cold load may delay when
the terminal Ledger event is written; it may not delay what the client sees.

Nothing here retains request text beyond the point where that request's metering
is decided, and nothing here returns text: the only outputs are Token counts and
two closed enumerations.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Coroutine, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from holm_domain import ArtifactStorageType, MeteringSource, MetricsCapability

from holm_gateway.tokenizer_bundle import (
    BundleTokenizer,
    TokenizerBundleReason,
    TokenizerBundleUnusable,
    load_bundle_tokenizer,
)

_LOGGER: Final[logging.Logger] = logging.getLogger("holm.gateway.metering")

#: Default ceiling for the per-request observed-output buffer.  A current-release
#: Runtime is bounded by its context length, so this is unreachable in practice;
#: it exists so a hostile or misbehaving Runtime cannot grow Gateway memory.
DEFAULT_METERING_TEXT_MAX_BYTES: Final[int] = 1024 * 1024
DEFAULT_TOKENIZER_CACHE_BYTES: Final[int] = 512 * 1024 * 1024
DEFAULT_TOKENIZER_WORKER_THREADS: Final[int] = 2
DEFAULT_TOKENIZER_TIMEOUT_SECONDS: Final[float] = 5.0

type TokenizerLoader = Callable[[Path, str], BundleTokenizer]


@dataclass(frozen=True, slots=True)
class TokenizerBundleReference:
    """Where one Artifact's verified Tokenizer Bundle lives, if it has one."""

    path: Path
    manifest_hash: str


def bundle_reference(
    *,
    tokenizer_bundle_path: str | None,
    tokenizer_bundle_hash: str | None,
    artifact_storage_type: str | None,
) -> tuple[TokenizerBundleReference | None, TokenizerBundleReason | None]:
    """Decide whether a route may be metered from a Bundle, and why not if it may not.

    An External Artifact is rejected before any path is touched: the read-only boundary keeps
    ``tokenizer_fallback=unsupported`` for External sources and the platform must
    never read, copy, or even stat an External source directory.
    """

    if artifact_storage_type == ArtifactStorageType.EXTERNAL.value:
        return None, TokenizerBundleReason.EXTERNAL_ARTIFACT
    if not tokenizer_bundle_path or not tokenizer_bundle_hash:
        return None, TokenizerBundleReason.NOT_PUBLISHED
    return (
        TokenizerBundleReference(Path(tokenizer_bundle_path), tokenizer_bundle_hash),
        None,
    )


@dataclass(frozen=True, slots=True)
class MeteringOutcome:
    """The decided provenance of one request's Token counts."""

    input_tokens: int | None
    output_tokens: int | None
    source: MeteringSource
    capability: MetricsCapability
    reason: str | None = None


class _TokenizerCache:
    """Process-wide LRU of loaded Bundles under a byte ceiling (512 MiB).

    Entries are accounted by the loader's own estimate of a Bundle's in-memory
    footprint rather than its on-disk size, because the parsed vocabulary and
    merge table dominate and are several times larger than the JSON.
    """

    __slots__ = ("_entries", "_lock", "_load_locks", "_max_bytes", "_total_bytes")

    def __init__(self, max_bytes: int) -> None:
        if max_bytes < 1:
            raise ValueError("Tokenizer cache ceiling must be positive")
        self._max_bytes = max_bytes
        self._entries: OrderedDict[tuple[str, str], BundleTokenizer] = OrderedDict()
        self._total_bytes = 0
        self._lock = threading.Lock()
        self._load_locks: dict[tuple[str, str], threading.Lock] = {}

    @property
    def resident_bytes(self) -> int:
        with self._lock:
            return self._total_bytes

    @property
    def resident_count(self) -> int:
        with self._lock:
            return len(self._entries)

    def get_or_load(
        self,
        reference: TokenizerBundleReference,
        loader: TokenizerLoader,
    ) -> BundleTokenizer:
        key = (str(reference.path), reference.manifest_hash)
        cached = self._lookup(key)
        if cached is not None:
            return cached
        with self._load_lock(key):
            cached = self._lookup(key)
            if cached is not None:
                return cached
            tokenizer = loader(reference.path, reference.manifest_hash)
            self._insert(key, tokenizer)
            return tokenizer

    def _load_lock(self, key: tuple[str, str]) -> threading.Lock:
        with self._lock:
            return self._load_locks.setdefault(key, threading.Lock())

    def _lookup(self, key: tuple[str, str]) -> BundleTokenizer | None:
        with self._lock:
            tokenizer = self._entries.get(key)
            if tokenizer is not None:
                self._entries.move_to_end(key)
            return tokenizer

    def _insert(self, key: tuple[str, str], tokenizer: BundleTokenizer) -> None:
        size = max(1, tokenizer.estimated_bytes)
        with self._lock:
            if key in self._entries:
                return
            if size > self._max_bytes:
                # A single Bundle larger than the whole ceiling is used for this
                # request but never retained, so the ceiling still holds.
                return
            self._entries[key] = tokenizer
            self._total_bytes += size
            while self._total_bytes > self._max_bytes and len(self._entries) > 1:
                evicted_key, evicted = self._entries.popitem(last=False)
                self._total_bytes -= max(1, evicted.estimated_bytes)
                self._load_locks.pop(evicted_key, None)


class RequestMetering:
    """One request's metering state; owns the only copy of its metering text."""

    __slots__ = (
        "_bundle",
        "_input_task",
        "_input_texts",
        "_output_bytes",
        "_output_chunks",
        "_output_task",
        "_overflow",
        "_released",
        "_service",
        "_unusable_reason",
    )

    def __init__(
        self,
        service: MeteringService,
        bundle: TokenizerBundleReference | None,
        unusable_reason: TokenizerBundleReason | None,
    ) -> None:
        self._service = service
        self._bundle = bundle
        self._unusable_reason = unusable_reason
        self._input_texts: tuple[str, ...] = ()
        self._output_chunks: list[str] = []
        self._output_bytes = 0
        self._overflow = False
        self._released = False
        self._input_task: asyncio.Task[int | TokenizerBundleReason] | None = None
        self._output_task: asyncio.Task[int | TokenizerBundleReason] | None = None

    @property
    def fallback_available(self) -> bool:
        return self._bundle is not None

    def start_input(self, texts: Sequence[str]) -> None:
        """Begin preparing the fallback count in the background after Dispatch.

        This returns immediately.  The caller is on the request's hot path and
        must never await the result here.
        """

        if self._bundle is None or self._input_task is not None or self._released:
            return
        self._input_texts = tuple(texts)
        self._input_task = asyncio.create_task(
            self._service._count(self._bundle, self._input_texts),
            name="metering-input-tokens",
        )

    def observe_output(self, text: str) -> None:
        if self._bundle is None or self._released or self._overflow or not text:
            return
        size = len(text.encode("utf-8"))
        if self._output_bytes + size > self._service.text_max_bytes:
            self._overflow = True
            self._output_chunks.clear()
            self._output_bytes = 0
            return
        self._output_chunks.append(text)
        self._output_bytes += size

    def release(self) -> None:
        """Drop every byte of metering text; counts may still be in flight."""

        self._released = True
        self._input_texts = ()
        self._output_chunks = []
        self._output_bytes = 0

    async def resolve(
        self,
        *,
        usage_input: int | None,
        usage_output: int | None,
        metrics_input: int | None = None,
        metrics_output: int | None = None,
    ) -> MeteringOutcome:
        """Apply the metering priority and return the decided provenance."""

        try:
            if usage_input is not None and usage_output is not None:
                return MeteringOutcome(
                    usage_input,
                    usage_output,
                    MeteringSource.RUNTIME_USAGE,
                    MetricsCapability.FULL,
                )
            if metrics_input is not None and metrics_output is not None:
                return MeteringOutcome(
                    metrics_input,
                    metrics_output,
                    MeteringSource.RUNTIME_METRICS,
                    MetricsCapability.FULL,
                )
            return await self._fallback()
        finally:
            self.release()
            self._abandon_counts()

    def _abandon_counts(self) -> None:
        """Stop awaiting counts this request no longer needs.

        The worker thread may still be finishing a cold load; that is bounded by
        the pool and its result simply lands in the shared cache for the next
        request instead of being discarded.
        """

        for task in (self._input_task, self._output_task):
            if task is not None and not task.done():
                task.cancel()
        self._input_task = None
        self._output_task = None

    async def _fallback(self) -> MeteringOutcome:
        if self._bundle is None:
            reason = self._unusable_reason or TokenizerBundleReason.NOT_PUBLISHED
            return MeteringOutcome(
                None,
                None,
                MeteringSource.UNAVAILABLE,
                MetricsCapability.UNSUPPORTED,
                reason.value,
            )
        deadline = time.monotonic() + self._service.timeout_seconds
        input_tokens, input_reason = await self._await_input(deadline)
        output_tokens, output_reason = await self._count_output(deadline)
        if input_tokens is None and output_tokens is None:
            fallback_reason = (
                input_reason or output_reason or TokenizerBundleReason.UNREADABLE.value
            )
            # A capability that existed but did not finish in time is
            # `partial`; only a Bundle the Gateway can never use is
            # `unsupported`.
            capability = (
                MetricsCapability.PARTIAL
                if fallback_reason
                in {"timeout", "overflow", TokenizerBundleReason.WORKER_SATURATED.value}
                else MetricsCapability.UNSUPPORTED
            )
            return MeteringOutcome(
                None,
                None,
                MeteringSource.UNAVAILABLE,
                capability,
                fallback_reason,
            )
        # A Gateway-side count never observes the Runtime's chat template or the
        # special Tokens it adds, so a fallback measurement is by construction
        # incomplete against the Runtime's own accounting - `partial`, never
        # `full`.  It is a real count of observed text, not an estimate.
        return MeteringOutcome(
            input_tokens,
            output_tokens,
            MeteringSource.TOKENIZER_FALLBACK,
            MetricsCapability.PARTIAL,
            input_reason or output_reason,
        )

    async def _await_input(self, deadline: float) -> tuple[int | None, str | None]:
        task = self._input_task
        if task is None:
            return None, "not_started"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, "timeout"
        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
        except TimeoutError:
            return None, "timeout"
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a metering failure never fails a request
            return None, TokenizerBundleReason.UNREADABLE.value
        if isinstance(result, TokenizerBundleReason):
            return None, result.value
        return result, None

    async def _count_output(self, deadline: float) -> tuple[int | None, str | None]:
        if self._overflow:
            return None, "overflow"
        chunks = tuple(self._output_chunks)
        if not chunks:
            return 0, None
        if self._bundle is None:
            return None, TokenizerBundleReason.NOT_PUBLISHED.value
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, "timeout"
        task = asyncio.create_task(
            self._service._count(self._bundle, chunks),
            name="metering-output-tokens",
        )
        self._output_task = task
        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
        except TimeoutError:
            return None, "timeout"
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a metering failure never fails a request
            return None, TokenizerBundleReason.UNREADABLE.value
        if isinstance(result, TokenizerBundleReason):
            return None, result.value
        return result, None


class MeteringService:
    """Own the bounded Tokenizer worker pool, the LRU cache, and deferred writes."""

    __slots__ = (
        "_cache",
        "_executor",
        "_loader",
        "_metrics_runtimes",
        "_permits",
        "_tasks",
        "_text_max_bytes",
        "_timeout_seconds",
    )

    def __init__(
        self,
        *,
        cache_max_bytes: int = DEFAULT_TOKENIZER_CACHE_BYTES,
        worker_threads: int = DEFAULT_TOKENIZER_WORKER_THREADS,
        timeout_seconds: float = DEFAULT_TOKENIZER_TIMEOUT_SECONDS,
        text_max_bytes: int = DEFAULT_METERING_TEXT_MAX_BYTES,
        per_request_metrics_runtimes: Iterable[str] = (),
        loader: TokenizerLoader = load_bundle_tokenizer,
    ) -> None:
        if worker_threads < 1 or timeout_seconds <= 0 or text_max_bytes < 1:
            raise ValueError("metering limits must be positive")
        self._cache = _TokenizerCache(cache_max_bytes)
        self._executor = ThreadPoolExecutor(
            max_workers=worker_threads,
            thread_name_prefix="holm-tokenizer",
        )
        # The executor's own queue is unbounded, so admission is bounded here
        # instead: metering work that cannot get a permit within the request's
        # metering deadline is reported unavailable rather than queued forever.
        self._permits = asyncio.Semaphore(worker_threads * 4)
        self._timeout_seconds = timeout_seconds
        self._text_max_bytes = text_max_bytes
        self._metrics_runtimes = frozenset(per_request_metrics_runtimes)
        self._loader = loader
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    @property
    def text_max_bytes(self) -> int:
        return self._text_max_bytes

    @property
    def resident_cache_bytes(self) -> int:
        return self._cache.resident_bytes

    def supports_per_request_metrics(self, runtime_name: str) -> bool:
        """Level 2 is enabled only for a Runtime certified for per-request Metrics."""

        return runtime_name in self._metrics_runtimes

    def begin(
        self,
        *,
        tokenizer_bundle_path: str | None,
        tokenizer_bundle_hash: str | None,
        artifact_storage_type: str | None,
    ) -> RequestMetering:
        reference, reason = bundle_reference(
            tokenizer_bundle_path=tokenizer_bundle_path,
            tokenizer_bundle_hash=tokenizer_bundle_hash,
            artifact_storage_type=artifact_storage_type,
        )
        return RequestMetering(self, reference, reason)

    def track(self, coroutine: Coroutine[Any, Any, None]) -> None:
        """Run deferred terminal work outside the request, and remember it."""

        task = asyncio.create_task(coroutine, name="metering-terminal")
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            _LOGGER.warning(
                "deferred request metering failed",
                extra={"component": "gateway", "error_class": type(error).__name__},
            )

    async def drain(self, timeout_seconds: float = 10.0) -> None:
        """Let deferred terminal writes finish before the process stops accepting."""

        while self._tasks:
            pending = tuple(self._tasks)
            done, _ = await asyncio.wait(pending, timeout=timeout_seconds)
            if len(done) < len(pending):
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                return

    async def aclose(self) -> None:
        await self.drain()
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def _count(
        self,
        reference: TokenizerBundleReference,
        texts: Sequence[str],
    ) -> int | TokenizerBundleReason:
        """Load (if needed) and count on a bounded worker; never on the Event Loop."""

        try:
            await asyncio.wait_for(self._permits.acquire(), timeout=self._timeout_seconds)
        except TimeoutError:
            return TokenizerBundleReason.WORKER_SATURATED
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                self._executor,
                self._load_and_count,
                reference,
                tuple(texts),
            )
        except RuntimeError:
            # The pool is shutting down; metering is unavailable, not wrong.
            return TokenizerBundleReason.WORKER_SATURATED
        finally:
            self._permits.release()

    def _load_and_count(
        self,
        reference: TokenizerBundleReference,
        texts: tuple[str, ...],
    ) -> int | TokenizerBundleReason:
        try:
            tokenizer = self._cache.get_or_load(reference, self._loader)
        except TokenizerBundleUnusable as error:
            _LOGGER.warning(
                "Tokenizer Bundle unusable for metering",
                extra={"component": "gateway", "metering_reason": error.reason.value},
            )
            return error.reason
        except OSError:
            return TokenizerBundleReason.UNREADABLE
        return tokenizer.count_all(texts)


__all__ = [
    "DEFAULT_METERING_TEXT_MAX_BYTES",
    "DEFAULT_TOKENIZER_CACHE_BYTES",
    "DEFAULT_TOKENIZER_TIMEOUT_SECONDS",
    "DEFAULT_TOKENIZER_WORKER_THREADS",
    "MeteringOutcome",
    "MeteringService",
    "RequestMetering",
    "TokenizerBundleReference",
    "bundle_reference",
]

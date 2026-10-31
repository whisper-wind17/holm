"""Process-local, session-bound sanitized previews; never a disk cache."""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

from holm_control_api.diagnostics_service import DiagnosticAssembly

PREVIEW_TTL_SECONDS = 300
MAX_PREVIEWS = 8
MAX_PREVIEW_BYTES = 16 * 1024 * 1024
MAX_GENERATING = 2


class PreviewUnavailable(Exception):
    """No receipt, identity or cached data is included in the error."""


class DiagnosticsBusy(Exception):
    """Capacity is exhausted; callers retry explicitly without an internal queue."""


@dataclass(frozen=True)
class _Preview:
    owner: tuple[UUID, UUID]
    expires: float
    assembly: DiagnosticAssembly
    size: int


class DiagnosticPreviewStore:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl: float = PREVIEW_TTL_SECONDS,
        max_entries: int = MAX_PREVIEWS,
        max_bytes: int = MAX_PREVIEW_BYTES,
        max_generating: int = MAX_GENERATING,
    ) -> None:
        if min(ttl, max_entries, max_bytes, max_generating) <= 0:
            raise ValueError("diagnostic preview limits must be positive")
        self.clock = clock
        self.ttl = ttl
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.max_generating = max_generating
        self._entries: dict[str, _Preview] = {}
        self._generating = 0

    def _prune(self) -> None:
        for receipt, entry in tuple(self._entries.items()):
            if entry.expires <= self.clock():
                self._entries.pop(receipt, None)

    def acquire(self) -> Callable[[], None]:
        # All access is on the owning ASGI event loop; no await between check/increment.
        self._prune()
        if self._generating >= self.max_generating:
            raise DiagnosticsBusy()
        self._generating += 1
        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                self._generating -= 1

        return release

    @contextmanager
    def generating(self) -> Iterator[None]:
        release = self.acquire()
        try:
            yield
        finally:
            release()

    def put(self, owner: tuple[UUID, UUID], assembly: DiagnosticAssembly) -> str:
        self._prune()
        size = sum(len(value) for _, value in assembly.files) + len(
            json.dumps(assembly.manifest.json(), ensure_ascii=False).encode("utf-8")
        )
        if (
            len(self._entries) >= self.max_entries
            or size + sum(entry.size for entry in self._entries.values()) > self.max_bytes
        ):
            raise DiagnosticsBusy()
        receipt = secrets.token_urlsafe(32)
        self._entries[receipt] = _Preview(owner, self.clock() + self.ttl, assembly, size)
        # Expired material is released even if there are no subsequent requests.
        asyncio.get_running_loop().call_later(self.ttl, self._entries.pop, receipt, None)
        return receipt

    def get(self, owner: tuple[UUID, UUID], receipt: str) -> DiagnosticAssembly:
        self._prune()
        entry = self._entries.get(receipt)
        if entry is None or entry.owner != owner:
            raise PreviewUnavailable()
        return entry.assembly

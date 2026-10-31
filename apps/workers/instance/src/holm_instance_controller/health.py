"""Bounded loopback health checks for a Runtime managed by the Host Agent."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Final, Protocol

MAX_MODELS_RESPONSE_BYTES: Final[int] = 1024 * 1024
MAX_WARMUP_RESPONSE_BYTES: Final[int] = 1024 * 1024
_WARMUP_PROMPT: Final[str] = "."


class RuntimeHealthProbe(Protocol):
    async def ready(self, endpoint: str, served_model_name: str) -> bool:
        """Return true only when health and model discovery both agree."""

    async def warmup(self, endpoint: str, served_model_name: str) -> bool:
        """Return true only after a minimal inference-shaped request succeeds."""


def _get(endpoint: str, path: str, timeout_seconds: float) -> bytes:
    request = urllib.request.Request(
        f"http://{endpoint}{path}",
        method="GET",
        headers={"Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
        if response.status != 200:
            raise OSError("Runtime health response was not successful")
        body = bytes(response.read(MAX_MODELS_RESPONSE_BYTES + 1))
    if len(body) > MAX_MODELS_RESPONSE_BYTES:
        raise OSError("Runtime health response exceeded its bound")
    return body


def _warmup(endpoint: str, served_model_name: str, timeout_seconds: float) -> bytes:
    body = json.dumps(
        {
            "model": served_model_name,
            "messages": [{"role": "user", "content": _WARMUP_PROMPT}],
            "max_tokens": 1,
            "stream": False,
        },
        separators=(",", ":"),
    ).encode()
    request = urllib.request.Request(
        f"http://{endpoint}/v1/chat/completions",
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
        if response.status != 200:
            raise OSError("Runtime warmup response was not successful")
        response_body = bytes(response.read(MAX_WARMUP_RESPONSE_BYTES + 1))
    if len(response_body) > MAX_WARMUP_RESPONSE_BYTES:
        raise OSError("Runtime warmup response exceeded its bound")
    return response_body


class HttpRuntimeHealthProbe:
    __slots__ = ("_timeout_seconds",)

    def __init__(self, *, timeout_seconds: float = 2.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("health timeout must be positive")
        self._timeout_seconds = timeout_seconds

    async def ready(self, endpoint: str, served_model_name: str) -> bool:
        try:
            await asyncio.to_thread(_get, endpoint, "/health", self._timeout_seconds)
            raw = await asyncio.to_thread(
                _get,
                endpoint,
                "/v1/models",
                self._timeout_seconds,
            )
            payload: object = json.loads(raw)
        except (
            OSError,
            TimeoutError,
            UnicodeError,
            ValueError,
            urllib.error.URLError,
        ):
            return False
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            return False
        return any(
            isinstance(item, dict) and item.get("id") == served_model_name
            for item in payload["data"]
        )

    async def warmup(self, endpoint: str, served_model_name: str) -> bool:
        try:
            raw = await asyncio.to_thread(
                _warmup,
                endpoint,
                served_model_name,
                self._timeout_seconds,
            )
            payload: object = json.loads(raw)
        except (
            OSError,
            TimeoutError,
            UnicodeError,
            ValueError,
            urllib.error.URLError,
        ):
            return False
        return (
            isinstance(payload, dict)
            and isinstance(payload.get("choices"), list)
            and bool(payload["choices"])
            and all(isinstance(choice, dict) for choice in payload["choices"])
        )


__all__ = ["HttpRuntimeHealthProbe", "RuntimeHealthProbe"]

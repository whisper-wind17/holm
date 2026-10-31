"""Bounded, credential-free HTTP observations for Gateway and Edge."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Final, Literal

import httpx
from holm_contracts import VersionPayload

PROBE_TIMEOUT_SECONDS: Final[float] = 2.0
MAX_PROBE_RESPONSE_BYTES: Final[int] = 16 * 1024
_LOGGER = logging.getLogger(__name__)


async def _without_credentials(request: httpx.Request) -> None:
    # Responses from one probe must not seed credentials for another probe.
    request.headers.pop("cookie", None)
    request.headers.pop("authorization", None)


@dataclass(frozen=True, slots=True)
class ProbeObservation:
    reason: Literal[
        "probe_ready",
        "probe_unreachable",
        "gateway_route_snapshot_unavailable",
        "gateway_ledger_unavailable",
    ]
    build_version: str | None = None


class ComponentProbeClient:
    def __init__(
        self,
        *,
        gateway_url: str | None = None,
        edge_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._gateway_url = gateway_url
        self._edge_url = edge_url
        self._transport = transport
        # HTTP client debug/info logs contain internal URLs; match source clients.
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.WARNING)

    async def _get(
        self, client: httpx.AsyncClient, base: str, path: str
    ) -> tuple[int, object] | None:
        try:
            url = httpx.URL(base)
            if (
                url.scheme not in {"http", "https"}
                or not url.host
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ValueError
            async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
                async with client.stream("GET", str(url).rstrip("/") + path) as response:
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=MAX_PROBE_RESPONSE_BYTES):
                        body.extend(chunk)
                        if len(body) > MAX_PROBE_RESPONSE_BYTES:
                            raise ValueError
                    return response.status_code, json.loads(body)
        except (httpx.HTTPError, httpx.InvalidURL, TimeoutError, ValueError):
            _LOGGER.warning("probe_unreachable")
            return None

    async def read(self) -> tuple[ProbeObservation | None, ProbeObservation | None]:
        if self._gateway_url is None and self._edge_url is None:
            return None, None
        async with httpx.AsyncClient(
            timeout=PROBE_TIMEOUT_SECONDS,
            follow_redirects=False,
            trust_env=False,
            transport=self._transport,
            event_hooks={"request": [_without_credentials]},
        ) as client:
            gateway, edge = await asyncio.gather(self._gateway(client), self._edge(client))
            return gateway, edge

    async def _gateway(self, client: httpx.AsyncClient) -> ProbeObservation | None:
        if self._gateway_url is None:
            return None
        health, version = await asyncio.gather(
            self._get(client, self._gateway_url, "/internal/health/ready"),
            self._get(client, self._gateway_url, "/internal/version"),
        )
        build_version = None
        if version is not None and version[0] == 200:
            try:
                build_version = VersionPayload.model_validate(version[1]).build_version
            except ValueError:
                _LOGGER.warning("probe_version_unavailable")
        if health == (200, {"status": "ready"}):
            return ProbeObservation("probe_ready", build_version)
        if health == (503, {"reason": "route_snapshot_unavailable"}):
            return ProbeObservation("gateway_route_snapshot_unavailable", build_version)
        if health == (503, {"reason": "ledger_unavailable"}):
            return ProbeObservation("gateway_ledger_unavailable", build_version)
        return ProbeObservation("probe_unreachable", build_version)

    async def _edge(self, client: httpx.AsyncClient) -> ProbeObservation | None:
        if self._edge_url is None:
            return None
        health = await self._get(client, self._edge_url, "/internal/health/live")
        return ProbeObservation(
            "probe_ready" if health == (200, {"status": "live"}) else "probe_unreachable"
        )

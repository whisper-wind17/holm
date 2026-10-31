"""M4-14 第四棒：FX-RT-03 的预热连接必须可被后续请求复用。"""

from __future__ import annotations

import asyncio

import httpx
from fixtures.stub_runtime.server import RuntimeMode, StubRuntime, StubRuntimeConfig


def test_M4_14_TC_PF_01_FX_RT_03_reuses_one_warmed_connection() -> None:
    async def exercise(runtime: StubRuntime) -> None:
        limits = httpx.Limits(max_connections=1, max_keepalive_connections=1)
        async with httpx.AsyncClient(limits=limits, trust_env=False) as client:
            for _ in range(2):
                response = await client.post(
                    f"{runtime.base_url}/v1/chat/completions",
                    json={"model": "fixture", "messages": [], "stream": True},
                )
                assert response.status_code == 200
                assert runtime.active_request_count == 1
                assert runtime.live_handler_thread_count == 1

    config = StubRuntimeConfig(
        mode=RuntimeMode.FX_RT_03,
        completion_tokens=1,
        event_interval_seconds=0,
    )
    with StubRuntime(config) as runtime:
        asyncio.run(exercise(runtime))
